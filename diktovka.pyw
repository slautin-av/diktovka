"""Диктовка: горячая клавиша → говоришь → текст появляется там, где стоит курсор.

Распознавание — Groq Whisper (бесплатный ключ: console.groq.com → API Keys).
Ограничения по длине нет: длинная запись режется на куски и распознаётся по частям.
Только Windows. Лицензия MIT.

Запуск без консоли:  pythonw diktovka.pyw
Проверка на файле:   python diktovka.pyw --test файл.wav
"""
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import winsound
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
RECORDS_DIR = HERE / "zapisi"          # аудио последних диктовок — страховка, если сеть подвела
HISTORY_PATH = HERE / "istoriya.md"    # все распознанные тексты — ничего не теряется
# Где искать ключ Groq: переменная окружения GROQ_API_KEY → groq.env рядом с программой → общий сейф ~/.claude
KEY_FILES = [HERE / "groq.env", Path.home() / ".claude" / "secrets" / "groq.env"]
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

DEFAULT_CONFIG = {
    "hotkey": "ctrl+space",
    "model": "whisper-large-v3",
    "language": "ru",
    # Подсказка распознавателю: как пишутся имена и термины. Пиши предложениями с пунктуацией —
    # Whisper подражает стилю подсказки.
    # Допиши сюда свои имена, названия и термины — распознаваться будут правильно.
    "prompt": "Диктовка. Claude Code, VS Code, GSD, GitHub, Telegram, Groq.",
    "paste": True,            # True — вставить в поле с курсором; False — только положить в буфер обмена
    "chunk_minutes": 10,      # длинную запись режем на такие куски (лимит Groq — 25 МБ на файл)
    "keep_records": 20,       # сколько последних аудиозаписей хранить в zapisi/
}

# Фразы, которые Whisper «слышит» в тишине: титры с русских субтитров из его обучающих данных
HALLUCINATIONS = [
    "Субтитры сделал DimaTorzok", "Субтитры создавал DimaTorzok", "Субтитры делал DimaTorzok",
    "Редактор субтитров А.Синецкая Корректор А.Егорова", "Продолжение следует...",
    "Спасибо за просмотр!", "Спасибо за внимание!",
]

SAMPLE_RATE = 16000

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p]
user32.OpenClipboard.argtypes = [wt.HWND]
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE

WM_HOTKEY = 0x0312
WM_APP_ESC_ON = 0x8001
WM_APP_ESC_OFF = 0x8002
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
VK_ESCAPE, VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN, VK_V = 0x1B, 0x11, 0x12, 0x10, 0x5B, 0x5C, 0x56
HOTKEY_MAIN, HOTKEY_ESC = 1, 2

SPECIAL_KEYS = {
    "space": 0x20, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "scrolllock": 0x91, "capslock": 0x14,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
MODIFIERS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}


def load_config():
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    else:
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def load_api_key():
    if os.environ.get("GROQ_API_KEY"):
        return os.environ["GROQ_API_KEY"].strip()
    for path in KEY_FILES:
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("GROQ_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
    return None


def parse_hotkey(text):
    """'ctrl+alt+d' → (модификаторы, код клавиши). Буквы по коду клавиши — раскладка не важна."""
    mods, vk = 0, None
    for part in text.lower().replace(" ", "").split("+"):
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif part in SPECIAL_KEYS:
            vk = SPECIAL_KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"не понимаю клавишу «{part}» в «{text}»")
    if vk is None:
        raise ValueError(f"в «{text}» нет основной клавиши")
    return mods, vk


KEY_NAMES_RU = {"space": "Пробел", "pause": "Pause", "insert": "Insert"}


def hotkey_label(text):
    return "+".join(KEY_NAMES_RU.get(p) or (p.capitalize() if len(p) > 1 else p.upper()) for p in text.split("+"))


# ---------- распознавание ----------

def clean_text(text, prompt):
    text = text.strip()
    for phrase in HALLUCINATIONS:
        text = text.replace(phrase, "")
    # На неразборчивом звуке Whisper повторяет подсказку — вырезаем цепочки из 4+ её терминов подряд
    terms = sorted({t.strip() for t in prompt.replace(".", ",").split(",") if t.strip()}, key=len, reverse=True)
    if terms:
        echo = r"(?:(?:" + "|".join(re.escape(t) for t in terms) + r")[,.]?\s*){4,}"
        text = re.sub(echo, "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def transcribe(audio, sample_rate, config, api_key):
    """Режет запись на куски и распознаёт по порядку; хвост предыдущего куска — подсказка следующему."""
    chunk = int(config["chunk_minutes"] * 60 * sample_rate)
    parts = []
    for start in range(0, len(audio), chunk):
        piece = audio[start:start + chunk]
        if len(piece) < sample_rate // 2:      # огрызок короче полсекунды не отправляем
            continue
        buf = io.BytesIO()
        sf.write(buf, piece, sample_rate, format="FLAC")
        prompt = config["prompt"]
        if parts:
            prompt = f"{prompt} {parts[-1][-200:]}"
        parts.append(clean_text(groq_request(buf.getvalue(), prompt, config, api_key), config["prompt"]))
    return " ".join(p for p in parts if p)


def groq_request(flac_bytes, prompt, config, api_key):
    last_error = None
    for pause in (0, 2, 5, 10):
        time.sleep(pause)
        try:
            response = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("dictation.flac", flac_bytes, "audio/flac")},
                data={"model": config["model"], "language": config["language"],
                      "prompt": prompt, "response_format": "json", "temperature": "0"},
                timeout=180,
            )
        except requests.RequestException:
            last_error = "нет связи с Groq — проверь интернет и VPN"
            continue
        if response.status_code == 200:
            return response.json().get("text", "")
        last_error = {
            401: "Groq не принял ключ — проверь groq.env",
            403: "Groq не пускает из твоей страны — включи VPN",
            413: "кусок записи слишком большой — уменьши chunk_minutes в config.json",
            429: "лимит бесплатного Groq — подожди минуту",
        }.get(response.status_code, f"Groq ответил {response.status_code}: {response.text[:100]}")
        if response.status_code not in (429, 500, 502, 503, 504):
            break
    raise RuntimeError(last_error)


# ---------- Windows: буфер обмена, вставка, горячие клавиши ----------

def set_clipboard(text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("буфер обмена занят другой программой")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))   # GMEM_MOVEABLE
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(13, handle)                 # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()


def paste_at_cursor():
    # Ждём, пока отпущены Ctrl/Alt/Shift/Win от горячей клавиши, иначе вместо Ctrl+V уйдёт Ctrl+Alt+V
    deadline = time.time() + 3
    while time.time() < deadline and any(
            user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)):
        time.sleep(0.03)
    time.sleep(0.05)
    user32.keybd_event(VK_CONTROL, 0, 0, None)
    user32.keybd_event(VK_V, 0, 0, None)
    user32.keybd_event(VK_V, 0, 2, None)          # KEYEVENTF_KEYUP
    user32.keybd_event(VK_CONTROL, 0, 2, None)


class HotkeyThread(threading.Thread):
    """Глобальные клавиши через RegisterHotKey: работают в любом окне, нажатие не уходит в программу."""

    def __init__(self, mods, vk, events):
        super().__init__(daemon=True)
        self.mods, self.vk, self.events = mods, vk, events
        self.ready = threading.Event()
        self.registered = False
        self.thread_id = None

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        self.registered = bool(user32.RegisterHotKey(None, HOTKEY_MAIN, self.mods | MOD_NOREPEAT, self.vk))
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # создать очередь сообщений потока
        self.ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.events.put("main" if msg.wParam == HOTKEY_MAIN else "esc")
            elif msg.message == WM_APP_ESC_ON:
                user32.RegisterHotKey(None, HOTKEY_ESC, MOD_NOREPEAT, VK_ESCAPE)
            elif msg.message == WM_APP_ESC_OFF:
                user32.UnregisterHotKey(None, HOTKEY_ESC)

    def escape_enabled(self, on):
        """Esc перехватываем только на время записи — в остальное время он работает как обычно."""
        user32.PostThreadMessageW(self.thread_id, WM_APP_ESC_ON if on else WM_APP_ESC_OFF, 0, 0)


def beep(frequency, duration=70):
    threading.Thread(target=winsound.Beep, args=(frequency, duration), daemon=True).start()


# ---------- приложение ----------

class Dictation:
    BG, FG, DIM = "#1d1f26", "#f2f2f2", "#9aa0ad"
    RED, GREEN, AMBER, BLUE = "#ff4d6d", "#4cd38a", "#ffb84d", "#6aa8ff"

    def __init__(self, config, api_key):
        self.config, self.api_key = config, api_key
        self.events = queue.Queue()
        self.state = "idle"
        self.frames, self.level, self.stream = [], 0.0, None
        self.started_at = 0.0
        self.hide_job = None
        self.key_label = hotkey_label(config["hotkey"])

        self.root = tk.Tk()
        self.root.title("Диктовка")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=self.BG)
        frame = tk.Frame(self.root, bg=self.BG, padx=16, pady=10)
        frame.pack()
        self.dot = tk.Label(frame, text="●", font=("Segoe UI", 14), bg=self.BG, fg=self.RED)
        self.dot.pack(side="left")
        self.label = tk.Label(frame, text="", font=("Segoe UI", 12), bg=self.BG, fg=self.FG)
        self.label.pack(side="left", padx=(8, 12))
        self.meter = tk.Canvas(frame, width=90, height=8, bg="#2b2e38", highlightthickness=0)
        self.meter.pack(side="left")
        self.meter_bar = self.meter.create_rectangle(0, 0, 0, 8, fill=self.GREEN, width=0)
        self.hint = tk.Label(frame, text="", font=("Segoe UI", 10), bg=self.BG, fg=self.DIM)
        self.hint.pack(side="left", padx=(12, 0))
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Выключить диктовку", command=self.quit)
        for widget in (self.root, frame, self.dot, self.label, self.hint, self.meter):
            widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        self.root.update()
        self.hwnd = user32.GetParent(self.root.winfo_id())
        # Плашка никогда не забирает фокус — иначе Ctrl+V ушёл бы в неё, а не в твоё поле
        style = user32.GetWindowLongPtrW(self.hwnd, -20)                     # GWL_EXSTYLE
        user32.SetWindowLongPtrW(self.hwnd, -20, style | 0x08000000 | 0x80 | 0x8)  # NOACTIVATE|TOOLWINDOW|TOPMOST
        user32.ShowWindow(self.hwnd, 0)
        self.root.attributes("-alpha", 0.94)

        mods, vk = parse_hotkey(config["hotkey"])
        self.hotkeys = HotkeyThread(mods, vk, self.events)
        self.hotkeys.start()
        self.hotkeys.ready.wait(3)
        if not self.hotkeys.registered:
            self.show("✗", self.AMBER, f"{self.key_label} занята другой программой",
                      "поменяй «hotkey» в config.json", hide_after=15000)
        self.root.after(50, self.poll)

    # --- плашка ---
    def show(self, mark, color, text, hint="", meter=False, hide_after=None):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None
        self.dot.configure(text=mark, fg=color)
        self.label.configure(text=text)
        self.hint.configure(text=hint)
        if meter:
            self.meter.pack(side="left", before=self.hint)
        else:
            self.meter.pack_forget()
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = self.root.winfo_screenheight() - height - 90
        self.root.geometry(f"+{x}+{y}")
        user32.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x40)
        if hide_after:
            self.hide_job = self.root.after(hide_after, self.hide)

    def hide(self):
        self.hide_job = None
        user32.ShowWindow(self.hwnd, 0)

    # --- цикл событий ---
    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if isinstance(event, tuple):
                    kind, payload = event
                    if kind in ("mic_ready", "mic_error"):
                        self.on_mic(kind, payload)
                    else:
                        self.on_result(kind, payload)
                elif event == "main":
                    self.toggle()
                elif event == "esc" and self.state in ("starting", "recording"):
                    self.cancel()
        except queue.Empty:
            pass
        if self.state == "recording":
            self.tick()
        self.root.after(50 if self.state == "recording" else 120, self.poll)

    def toggle(self):
        if self.state == "idle":
            self.start()
        elif self.state == "starting":
            self.cancel()
        elif self.state == "recording":
            self.stop()
        else:
            self.show("⏳", self.BLUE, "Ещё распознаю предыдущую запись…")

    def start(self):
        # Микрофон открываем в фоне: антивирус может держать его, пока спрашивает разрешение
        self.frames, self.level = [], 0.0
        self.state = "starting"
        self.hotkeys.escape_enabled(True)
        self.show("●", self.DIM, "Включаю микрофон…", "если антивирус спросит — «Разрешить»")
        threading.Thread(target=self.open_stream, daemon=True).start()

    def open_stream(self):
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=self.on_audio)
            stream.start()
        except Exception as error:
            self.events.put(("mic_error", str(error)))
            return
        self.events.put(("mic_ready", stream))

    def on_mic(self, kind, payload):
        if kind == "mic_error":
            if self.state == "starting":
                self.state = "idle"
                self.hotkeys.escape_enabled(False)
                self.show("✗", self.AMBER, "Микрофон не открылся", payload[:80], hide_after=8000)
            return
        if self.state != "starting":      # пока ждали микрофон, запись отменили
            payload.stop()
            payload.close()
            return
        self.stream = payload
        self.frames = []
        self.state = "recording"
        self.started_at = time.time()
        beep(880)
        self.tick()

    def on_audio(self, indata, frames, time_info, status):
        self.frames.append(indata.copy())
        self.level = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2))) / 32768

    def tick(self):
        elapsed = int(time.time() - self.started_at)
        blink = self.RED if (time.time() % 1) < 0.6 else "#7a2535"
        self.show("●", blink, f"{elapsed // 60}:{elapsed % 60:02d}",
                  f"{self.key_label} — готово   ·   Esc — отмена", meter=True)
        width = min(90, int(90 * (self.level * 12) ** 0.5))
        self.meter.coords(self.meter_bar, 0, 0, width, 8)

    def finish_stream(self):
        self.hotkeys.escape_enabled(False)
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        return np.concatenate(self.frames)[:, 0] if self.frames else np.zeros(0, dtype=np.int16)

    def cancel(self):
        self.finish_stream()
        self.state = "idle"
        beep(330)
        self.show("✕", self.DIM, "Запись отменена", hide_after=1200)

    def stop(self):
        audio = self.finish_stream()
        beep(660)
        seconds = len(audio) / SAMPLE_RATE
        if seconds < 0.6:
            self.state = "idle"
            self.show("✕", self.DIM, "Слишком коротко — ничего не отправил", hide_after=1800)
            return
        if np.abs(audio).max() < 200:
            self.state = "idle"
            self.show("✗", self.AMBER, "Микрофон молчит",
                      "Параметры Windows → Конфиденциальность → Микрофон → доступ классическим приложениям",
                      hide_after=9000)
            return
        self.state = "transcribing"
        self.show("⏳", self.BLUE, f"Распознаю… ({int(seconds) // 60}:{int(seconds) % 60:02d} записи)")
        threading.Thread(target=self.worker, args=(audio, seconds), daemon=True).start()

    def worker(self, audio, seconds):
        stamp = datetime.now()
        record = RECORDS_DIR / f"{stamp:%Y-%m-%d_%H-%M-%S}.flac"
        try:
            RECORDS_DIR.mkdir(exist_ok=True)
            sf.write(record, audio, SAMPLE_RATE, format="FLAC")   # сначала сохраняем — потом в сеть
            prune_records(self.config["keep_records"])
            text = transcribe(audio, SAMPLE_RATE, self.config, self.api_key)
            if text:
                save_history(stamp, seconds, text)
                set_clipboard(text)
                if self.config["paste"]:
                    paste_at_cursor()
            self.events.put(("ok", text))
        except Exception as error:
            self.events.put(("error", f"{error}"))

    def on_result(self, kind, payload):
        self.state = "idle"
        if kind == "error":
            winsound.MessageBeep(0x30)
            self.show("✗", self.AMBER, "Не распознал: " + payload[:70],
                      "запись сохранена в zapisi/", hide_after=9000)
        elif not payload:
            self.show("✕", self.DIM, "Тишина — слов не услышал", hide_after=2500)
        else:
            words = len(payload.split())
            action = "Вставлено" if self.config["paste"] else "В буфере обмена"
            self.show("✓", self.GREEN, f"{action} · {words} {plural(words, 'слово', 'слова', 'слов')}",
                      "текст остался и в буфере — Ctrl+V", hide_after=2200)

    def quit(self):
        if self.stream:
            self.finish_stream()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def save_history(stamp, seconds, text):
    new = not HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", encoding="utf-8") as history:
        if new:
            history.write("# История диктовок\n\n")
        history.write(f"## {stamp:%Y-%m-%d %H:%M} · {int(seconds) // 60}:{int(seconds) % 60:02d}\n\n{text}\n\n")


def prune_records(keep):
    records = sorted(RECORDS_DIR.glob("*.flac"))
    for old in records[:-keep] if keep > 0 else records:
        old.unlink(missing_ok=True)


def main():
    if sys.stderr is None:   # под pythonw консоли нет — ошибки пишем в файл, чтобы было что разбирать
        sys.stderr = sys.stdout = open(HERE / "oshibki.log", "a", encoding="utf-8", buffering=1)
    config = load_config()
    api_key = load_api_key()
    if not api_key:
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Диктовка", "Не нашёл ключ Groq.\n\nЗапусти установщик ещё раз или положи файл "
                             f"groq.env со строкой GROQ_API_KEY=... в папку\n{HERE}")
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        audio, sample_rate = sf.read(sys.argv[2], dtype="int16")
        if audio.ndim > 1:
            audio = audio[:, 0]
        started = time.time()
        text = transcribe(audio, sample_rate, config, api_key)
        sys.stdout.reconfigure(encoding="utf-8")
        print(f"[{len(audio) / sample_rate:.1f} с аудио → {time.time() - started:.2f} с] {text}")
        return
    kernel32.CreateMutexW(None, False, "Local\\diktovka-single-instance")
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS — уже запущена
        return
    Dictation(config, api_key).run()


if __name__ == "__main__":
    main()
