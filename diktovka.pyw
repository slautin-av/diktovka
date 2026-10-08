"""Диктовка: горячая клавиша → говоришь → текст появляется там, где стоит курсор.

Распознавание — GigaAM-v3 (открытая модель Сбера для русской речи) в полной точности, прямо на компьютере:
без интернета, ключей и VPN. Модель (~0,9 ГБ) скачивается один раз — при установке или при первом запуске.
Ограничения по длине нет: длинная запись режется на паузах и распознаётся по частям.
Только Windows. Лицензия MIT.

Запуск без консоли:  pythonw diktovka.pyw
Скачать модель:      python diktovka.pyw --download
Проверка на файле:   python diktovka.pyw --test файл.flac
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import http.client
import json
import os
import queue
import re
import struct
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.error
import urllib.parse
import urllib.request
import winsound
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
RECORDS_DIR = HERE / "zapisi"          # аудио последних диктовок — страховка, если распознавание подвело
HISTORY_PATH = HERE / "istoriya.md"    # все распознанные тексты — ничего не теряется
MODELS_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "diktovka" / "models"

# Модель: GigaAM-v3 e2e rnnt (Сбер, MIT) в полной точности — ONNX-файлы istupakov/gigaam-v3-onnx (MIT).
# Ревизия закреплена: на ней проверено, что текст символ в символ совпадает с официальным пакетом gigaam.
# Паузы ищет Silero VAD (MIT). Файл: (репозиторий, ревизия, размер в байтах, sha256).
# Качаем из выпуска этого репозитория на GitHub (там те же файлы без изменений), запасной источник —
# Hugging Face на закреплённой ревизии. Каждый файл сверяется по sha256, откуда бы ни пришёл.
GITHUB_URL = "https://github.com/slautin-av/diktovka/releases/download/model-gigaam-v3-e2e-rnnt-1/{name}"
HF_URL = "https://huggingface.co/{repo}/resolve/{rev}/{name}"
GIGAAM_REPO = ("istupakov/gigaam-v3-onnx", "322c3b29492673eb7d0b434bfa9dfb8653e34d02")
SILERO_REPO = ("istupakov/silero-vad-onnx", "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6")
MODELS = {
    "gigaam-v3-e2e-rnnt": {
        "config.json": (*GIGAAM_REPO, 135, "0641fcf73f4af791c73f05083e38a658ff5dcbee3534a0c61396c10f4b97f0fe"),
        "v3_e2e_rnnt_encoder.onnx": (*GIGAAM_REPO, 885084534,
                                     "cd60b3764a832e8560ae6d3ad0b10adc1a42ffae412b9476f25620aae4f4a508"),
        "v3_e2e_rnnt_decoder.onnx": (*GIGAAM_REPO, 4599910,
                                     "7b0a16d67fd2cb37061decc93c69e364a9ab27afee3c57495d55b1c974cf7231"),
        "v3_e2e_rnnt_joint.onnx": (*GIGAAM_REPO, 2712896,
                                   "602ff7017a93311aad34df1437c8d7f49911353c13d6eae7a6ee7b041339465c"),
        "v3_e2e_rnnt_vocab.txt": (*GIGAAM_REPO, 13354,
                                  "39abae20e692998290c574e606f11a9edef2902a1995463fcff63d1490cf22b7"),
    },
}
VAD_FILE = ("silero-vad", "silero_vad.onnx",
            (*SILERO_REPO, 2327524, "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"))

DEFAULT_CONFIG = {
    "hotkey": "ctrl+space",
    "paste": True,            # True — вставить в поле с курсором; False — только положить в буфер обмена
    "keep_records": 20,       # сколько последних аудиозаписей хранить в zapisi/
    "model": "gigaam-v3-e2e-rnnt",   # полная GigaAM-v3 (не сжатая) — другой нет
    "threads": 0,             # сколько ядер процессора отдавать распознаванию; 0 — подобрать самой
    # Свой словарь: «как модель пишет на слух» → «как надо». Слева — слово или регулярное выражение,
    # регистр не важен, совпадение только целым словом. Пример: "мультикрид": "МультиКрит".
    "replacements": {
        "кл[оа]у?д[ -]?код": "Claude Code",
        "гит[ -]?хаб": "GitHub",
        "в[иэ][ -]?эс[ -]?код|вс[ -]?код": "VS Code",
        "джи[ -]?эс[ -]?ди": "GSD",
    },
}
# Настройки версии на Whisper (Groq) — больше не нужны
OLD_KEYS = ("prompt", "chunk_minutes", "language")

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
kernel32.GetCurrentProcess.restype = wt.HANDLE
kernel32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
kernel32.GetLogicalProcessorInformationEx.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]

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
    """config.json рядом с программой. Нет файла — создаём; файл от версии на Whisper — обновляем,
    а прежний сохраняем в config-whisper.json (там и старая подсказка — термины из неё переносятся в replacements)."""
    user = {}
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    changed = not CONFIG_PATH.exists()
    if any(key in user for key in OLD_KEYS) or str(user.get("model", "")).startswith("whisper"):
        backup = HERE / "config-whisper.json"
        if not backup.exists():
            backup.write_text(CONFIG_PATH.read_text(encoding="utf-8-sig"), encoding="utf-8")
        for key in OLD_KEYS:
            user.pop(key, None)
        if str(user.get("model", "")).startswith("whisper"):
            user.pop("model")
        changed = True
    if set(DEFAULT_CONFIG) - set(user):
        changed = True
    config = dict(DEFAULT_CONFIG)
    config.update(user)
    if changed:
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if config["model"] not in MODELS:
        raise ValueError(f"в config.json неизвестная модель «{config['model']}» — поставь gigaam-v3-e2e-rnnt")
    return config


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


# ---------- модель: скачать один раз ----------

def model_files(model):
    """[(путь на диске, [адреса: GitHub, запасной Hugging Face], размер, sha256)] — все файлы модели и поиска пауз."""
    files = [(MODELS_DIR / model / name, repo, rev, size, sha) for name, (repo, rev, size, sha) in MODELS[model].items()]
    folder, name, (repo, rev, size, sha) = VAD_FILE
    files.append((MODELS_DIR / folder / name, repo, rev, size, sha))
    return [(path, [GITHUB_URL.format(name=path.name), HF_URL.format(repo=repo, rev=rev, name=path.name)], size, sha)
            for path, repo, rev, size, sha in files]


def missing_files(model):
    return [f for f in model_files(model) if not (f[0].exists() and f[0].stat().st_size == f[2])]


def download_size_mb(model):
    return sum(f[2] for f in missing_files(model)) / 2**20


def download_models(model, report=None, say=None):
    """Докачивает недостающие файлы с проверкой sha256: сначала с GitHub, не вышло — с Hugging Face.
    Оборванная загрузка продолжается с места обрыва (файлы в обоих местах одинаковые — докачка тоже).
    report(скачано_байт, всего_байт) — для полоски прогресса; say(текст) — сообщить о смене источника."""
    todo = missing_files(model)
    total, done = sum(f[2] for f in todo), 0
    for path, urls, size, sha in todo:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        download_file(urls, part, size, sha, report and (lambda got, base=done: report(base + got, total)), say)
        part.replace(path)
        done += size


def download_file(urls, part, size, sha, report=None, say=None):
    """Качает файл в part: адреса по очереди, на каждом до трёх попыток. Не вышло ни с одного — RuntimeError."""
    problems, offline = [], True
    for number, url in enumerate(urls):
        host = urllib.parse.urlsplit(url).hostname
        if number and say:
            say(f"{problems[-1]} — качаю с {host}")
        for attempt in range(3):
            try:
                fetch(url, part, size, report)
            except (OSError, http.client.HTTPException) as error:   # сюда же URLError, обрыв связи, таймаут
                if isinstance(error, urllib.error.HTTPError) and error.code < 500:
                    problems.append(f"{host} ответил {error.code}")
                    offline = False
                    break                                # этот адрес не поможет — к следующему
                if attempt == 2:
                    if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
                        problems.append(f"нет связи с {host}")
                    else:
                        problems.append(f"{host}: {error}")
                        offline = False
                    break
                time.sleep(3 * (attempt + 1))
                continue
            if file_sha256(part) == sha:
                return
            part.unlink(missing_ok=True)                 # испорчен — следующий адрес качает заново
            problems.append(f"с {host} файл {part.stem} пришёл с ошибкой")
            offline = False
            break
    hosts = " и ".join(urllib.parse.urlsplit(url).hostname for url in urls)
    if offline:
        raise RuntimeError(f"нет связи с {hosts} — проверь интернет и запусти ещё раз")
    raise RuntimeError("модель не скачалась: " + "; ".join(problems))


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for block in iter(lambda: data.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url, part, size, report):
    """Качает url в part (докачка по Range, если part уже начат). Возвращает размер файла."""
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have == size:
        return size
    headers = {"User-Agent": "diktovka"}
    if have:
        headers["Range"] = f"bytes={have}-"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        if have and response.status != 206:      # сервер не умеет докачку — с начала
            have = 0
        with part.open("ab" if have else "wb") as out:
            got = have
            while block := response.read(1 << 20):
                out.write(block)
                got += len(block)
                if report:
                    report(got)
    if got != size:
        raise OSError(f"скачалось {got} байт из {size}")
    return size


def console_download(model):
    """Скачивание в консоли (установщик, --download, --test) — с процентами."""
    if not missing_files(model):
        print("Модель распознавания уже на месте:", MODELS_DIR, flush=True)
        return
    print(f"Скачиваю модель распознавания GigaAM-v3 с GitHub ({download_size_mb(model):.0f} МБ, один раз)...", flush=True)
    shown = [-1]

    def report(done, total):
        percent = int(100 * done / total) if total else 100
        if percent != shown[0]:
            shown[0] = percent
            print(f"\r  {percent}%  ({done / 2**20:.0f} из {total / 2**20:.0f} МБ)", end="", flush=True)
    download_models(model, report, lambda text: print("\n  " + text, flush=True))
    print("\nМодель скачана:", MODELS_DIR, flush=True)


# ---------- распознавание ----------

def cpu_cores():
    """(физических ядер, процессор гибридный?) — через GetLogicalProcessorInformationEx.
    Гибридный — когда у ядер разный EfficiencyClass (быстрые P-ядра и медленные E-ядра Intel 12-го поколения и новее)."""
    size = wt.DWORD(0)
    kernel32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))      # 0 = RelationProcessorCore
    buffer = ctypes.create_string_buffer(size.value)
    if not size.value or not kernel32.GetLogicalProcessorInformationEx(0, buffer, ctypes.byref(size)):
        return os.cpu_count() or 1, False
    classes, offset = [], 0
    while offset < size.value:
        entry_size = struct.unpack_from("<I", buffer.raw, offset + 4)[0]
        classes.append(buffer.raw[offset + 9])                                  # PROCESSOR_RELATIONSHIP.EfficiencyClass
        offset += entry_size
    return len(classes), len(set(classes)) > 1


def default_threads():
    """Сколько потоков отдать модели, если в config.json "threads": 0.
    Обычный процессор — все физические ядра. Гибридный — половина логических: замер на i5-1235U
    (2 P + 8 E ядер, 12 потоков) — 4–6 потоков ≈ 7,5 с на минуту записи, все 10 ядер ≈ 9 с, 12 потоков ≈ 10 с:
    медленные E-ядра тормозят общую работу."""
    cores, hybrid = cpu_cores()
    if hybrid:
        return max(1, (os.cpu_count() or 2) // 2)
    return max(1, cores)


def compile_replacements(rules):
    """{"как слышит модель": "как надо"} → список (регулярка, замена). Ошибочные правила — в список ошибок."""
    if not isinstance(rules, dict):
        return [], ['нужно в виде {"как слышит модель": "как надо"}']
    compiled, errors = [], []
    for pattern, replacement in rules.items():
        try:
            if not isinstance(replacement, str):
                raise re.error("справа должен быть текст в кавычках")
            rx = re.compile(rf"(?<!\w)(?:{pattern})(?!\w)", re.IGNORECASE)
            rx.sub(replacement, "")          # заодно проверит ссылки на группы вроде \1
            compiled.append((rx, replacement))
        except re.error as error:
            errors.append(f"«{pattern}»: {error}")
    return compiled, errors


def apply_replacements(text, compiled):
    for rx, replacement in compiled:
        text = rx.sub(replacement, text)
    return text


class Recognizer:
    """GigaAM-v3 e2e rnnt (полная) + Silero VAD. Загружается один раз — при старте программы.

    Модель рассчитана на куски до ~25 с, поэтому запись режется сплошь (ничего не выбрасывается)
    на куски не длиннее 22 с — посередине пауз, которые нашёл Silero VAD. Рез «в лоб» по времени
    попадал бы посреди слова и портил его."""

    MAX_PIECE = 22 * SAMPLE_RATE

    def __init__(self, model, threads):
        import onnxruntime as rt
        import onnx_asr
        options = rt.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        self.asr = onnx_asr.load_model(model, MODELS_DIR / model, quantization=None, sess_options=options,
                                       providers=["CPUExecutionProvider"])
        vad_options = rt.SessionOptions()
        vad_options.intra_op_num_threads = 1
        vad_options.inter_op_num_threads = 1
        vad_options.log_severity_level = 3
        folder, name, _ = VAD_FILE
        self.vad = rt.InferenceSession(str(MODELS_DIR / folder / name), sess_options=vad_options,
                                       providers=["CPUExecutionProvider"])
        self.threads = threads

    def transcribe(self, audio, sample_rate, progress=None):
        """audio — int16 или float32, моно. progress(готово_кусков, всего_кусков)."""
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if sample_rate != SAMPLE_RATE:
            audio = resample(audio, sample_rate)
        if len(audio) < SAMPLE_RATE // 10:
            return ""
        pieces = self.split(audio)
        texts = []
        for number, (start, end) in enumerate(pieces, 1):
            texts.append(self.asr.recognize(audio[start:end], sample_rate=SAMPLE_RATE).strip())
            if progress:
                progress(number, len(pieces))
        return " ".join(" ".join(texts).split())

    def split(self, audio):
        """Сплошная нарезка на куски ≤ 22 с с резами посередине пауз. Возвращает [(начало, конец)] в отсчётах."""
        total = len(audio)
        if total <= self.MAX_PIECE:
            return [(0, total)]
        speech = self.speech(audio)
        pauses = [(end + next_start) // 2 for (_, end), (next_start, _) in zip(speech, speech[1:])]
        pieces, start = [], 0
        while total - start > self.MAX_PIECE:
            fits = [cut for cut in pauses if start + SAMPLE_RATE < cut <= start + self.MAX_PIECE]
            cut = max(fits) if fits else start + self.MAX_PIECE   # паузы нет — режем по времени
            pieces.append((start, cut))
            start = cut
        pieces.append((start, total))
        if len(pieces) > 1 and total - start < SAMPLE_RATE:   # огрызок меньше секунды — к предыдущему куску
            pieces[-2:] = [(pieces[-2][0], total)]
        return pieces

    def speech(self, audio):
        """Где речь: вероятность речи по Silero VAD на окнах по 32 мс → отрезки речи (в отсчётах)."""
        window, context_size = 512, 64
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, context_size), dtype=np.float32)
        rate = np.array(SAMPLE_RATE, dtype=np.int64)
        probs = []
        for start in range(0, len(audio), window):
            chunk = audio[start:start + window]
            if len(chunk) < window:
                chunk = np.pad(chunk, (0, window - len(chunk)))
            x = np.concatenate([context, chunk[None, :]], axis=1)
            out, state = self.vad.run(None, {"input": x, "state": state, "sr": rate})
            context = x[:, -context_size:]
            probs.append(float(out[0][0]))
        return speech_segments(probs, len(audio))


def speech_segments(probs, total, threshold=0.5, min_speech_ms=250, max_speech_s=20,
                    min_silence_ms=150, pad_ms=100, min_silence_at_max_ms=98):
    """Вероятности речи по окнам 512 отсчётов → [(начало, конец)] в отсчётах.
    Перенос get_speech_timestamps_from_probs из silero-vad 6.2.3 (MIT, (c) Silero Team) без torch,
    с теми же настройками, на которых проверялась нарезка."""
    window = 512
    neg_threshold = max(threshold - 0.15, 0.01)
    min_speech = SAMPLE_RATE * min_speech_ms / 1000
    pad = SAMPLE_RATE * pad_ms / 1000
    max_speech = SAMPLE_RATE * max_speech_s - window - 2 * pad
    min_silence = SAMPLE_RATE * min_silence_ms / 1000
    min_silence_at_max = SAMPLE_RATE * min_silence_at_max_ms / 1000

    triggered, speeches, current = False, [], {}
    temp_end = prev_end = next_start = 0
    possible_ends = []
    for i, prob in enumerate(probs):
        sample = window * i
        if prob >= threshold and temp_end:
            silence = sample - temp_end
            if silence > min_silence_at_max:
                possible_ends.append((temp_end, silence))
            temp_end = 0
            if next_start < prev_end:
                next_start = sample
        if prob >= threshold and not triggered:
            triggered = True
            current["start"] = sample
            continue
        if triggered and sample - current["start"] > max_speech:
            if possible_ends:
                prev_end, duration = max(possible_ends, key=lambda end: end[1])
                current["end"] = prev_end
                speeches.append(current)
                current = {}
                next_start = prev_end + duration
                if next_start < prev_end + sample:
                    current["start"] = next_start
                else:
                    triggered = False
                prev_end = next_start = temp_end = 0
                possible_ends = []
            else:
                current["end"] = sample
                speeches.append(current)
                current = {}
                prev_end = next_start = temp_end = 0
                triggered = False
                possible_ends = []
                continue
        if prob < neg_threshold and triggered:
            if not temp_end:
                temp_end = sample
            if sample - temp_end < min_silence:
                continue
            current["end"] = temp_end
            if current["end"] - current["start"] > min_speech:
                speeches.append(current)
            current = {}
            prev_end = next_start = temp_end = 0
            triggered = False
            possible_ends = []
            continue
    if current and total - current["start"] > min_speech:
        current["end"] = total
        speeches.append(current)
    for i, speech in enumerate(speeches):
        if i == 0:
            speech["start"] = int(max(0, speech["start"] - pad))
        if i != len(speeches) - 1:
            silence = speeches[i + 1]["start"] - speech["end"]
            if silence < 2 * pad:
                speech["end"] += int(silence // 2)
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - silence // 2))
            else:
                speech["end"] = int(min(total, speech["end"] + pad))
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - pad))
        else:
            speech["end"] = int(min(total, speech["end"] + pad))
    return [(s["start"], s["end"]) for s in speeches]


def resample(audio, sample_rate):
    """Файл не в 16 кГц (только для --test: запись программы всегда 16 кГц) → 16 кГц."""
    from onnx_asr.preprocessors.resampler import Resampler
    from onnx_asr.utils import is_supported_sample_rate
    if not is_supported_sample_rate(sample_rate):
        raise ValueError(f"частота {sample_rate} Гц не поддерживается — нужна 8, 11, 16, 22, 24, 32, 44,1 или 48 кГц")
    resampled, lengths = Resampler(SAMPLE_RATE, {"providers": ["CPUExecutionProvider"]})(
        audio[None, :], np.array([len(audio)], dtype=np.int64), sample_rate)
    return np.ascontiguousarray(resampled[0, :lengths[0]], dtype=np.float32)


class Engine:
    """Распознаватель на всю жизнь программы: скачивается (если нужно) и грузится один раз, в фоне при старте.
    Если не вышло (например, нет интернета при первом запуске) — следующая диктовка попробует ещё раз."""

    def __init__(self, config):
        self.model = config["model"]
        self.threads = config["threads"] or default_threads()
        self.lock = threading.Lock()
        self.recognizer = None
        self.load_seconds = 0.0

    def get(self, report_download=None):
        with self.lock:
            if self.recognizer is None:
                if missing_files(self.model):
                    download_models(self.model, report_download)
                started = time.perf_counter()
                self.recognizer = Recognizer(self.model, self.threads)
                self.load_seconds = time.perf_counter() - started
            return self.recognizer


class MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


def memory_mb():
    """(сейчас, пик) — рабочий набор процесса в МБ."""
    counters = MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return counters.WorkingSetSize / 2**20, counters.PeakWorkingSetSize / 2**20


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

    def __init__(self, config):
        self.config = config
        self.engine = Engine(config)
        self.replacements, replacement_errors = compile_replacements(config["replacements"])
        self.events = queue.Queue()
        self.state = "idle"
        self.frames, self.level, self.stream = [], 0.0, None
        self.started_at = 0.0
        self.hide_job = None
        self.audio_label = ""
        self.downloaded, self.download_percent = False, None
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
        if replacement_errors:
            print("Ошибки в replacements (config.json):", *replacement_errors, sep="\n  ", file=sys.stderr)
        if not self.hotkeys.registered:
            self.show("✗", self.AMBER, f"{self.key_label} занята другой программой",
                      "поменяй «hotkey» в config.json", hide_after=15000)
        elif replacement_errors:
            self.show("✗", self.AMBER, "В replacements (config.json) ошибка — правило пропущено",
                      replacement_errors[0][:80], hide_after=12000)
        threading.Thread(target=self.preload, daemon=True).start()
        self.root.after(50, self.poll)

    # --- модель ---
    def preload(self):
        """Модель грузится сразу при старте — к первой диктовке она уже в памяти."""
        try:
            self.engine.get(self.report_download)
            self.events.put(("engine_ready", None))
        except Exception as error:
            traceback.print_exc()
            self.events.put(("engine_error", str(error)))

    def report_download(self, done, total):
        percent = int(100 * done / total) if total else 100
        if percent != self.download_percent:
            self.download_percent = percent
            self.downloaded = True
            self.events.put(("download", (percent, total)))

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
                    elif kind in ("download", "engine_ready", "engine_error"):
                        self.on_engine(kind, payload)
                    elif kind == "progress":
                        self.on_progress(payload)
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

    def on_engine(self, kind, payload):
        if self.state in ("starting", "recording"):     # плашку записи не перебиваем
            return
        if kind == "download":
            percent, total = payload
            after = "потом распознаю запись" if self.state == "transcribing" else "один раз — дальше без интернета"
            self.show("⬇", self.BLUE, f"Скачиваю модель распознавания… {percent}%",
                      f"{total / 2**20:.0f} МБ · {after}")
        elif kind == "engine_ready" and self.downloaded and self.state == "idle":
            self.show("✓", self.GREEN, "Модель скачана — можно диктовать",
                      f"{self.key_label} — начать", hide_after=5000)
        elif kind == "engine_error" and self.state == "idle":
            winsound.MessageBeep(0x30)
            self.show("✗", self.AMBER, "Модель не загрузилась: " + payload[:70],
                      "следующая диктовка попробует ещё раз", hide_after=15000)

    def on_progress(self, payload):
        done, total = payload
        if self.state == "transcribing" and total > 1:
            self.show("⏳", self.BLUE, f"Распознаю… {100 * done // total}%", self.audio_label)

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
            self.show("✕", self.DIM, "Слишком коротко — ничего не распознавал", hide_after=1800)
            return
        if np.abs(audio).max() < 200:
            self.state = "idle"
            self.show("✗", self.AMBER, "Микрофон молчит",
                      "Параметры Windows → Конфиденциальность → Микрофон → доступ классическим приложениям",
                      hide_after=9000)
            return
        self.state = "transcribing"
        self.audio_label = f"{int(seconds) // 60}:{int(seconds) % 60:02d} записи"
        self.show("⏳", self.BLUE, "Распознаю…", self.audio_label)
        threading.Thread(target=self.worker, args=(audio, seconds), daemon=True).start()

    def worker(self, audio, seconds):
        stamp = datetime.now()
        record = RECORDS_DIR / f"{stamp:%Y-%m-%d_%H-%M-%S}.flac"
        try:
            RECORDS_DIR.mkdir(exist_ok=True)
            sf.write(record, audio, SAMPLE_RATE, format="FLAC")   # сначала сохраняем — потом распознаём
            prune_records(self.config["keep_records"])
            recognizer = self.engine.get(self.report_download)   # обычно уже в памяти
            text = recognizer.transcribe(audio, SAMPLE_RATE,
                                         progress=lambda done, total: self.events.put(("progress", (done, total))))
            text = apply_replacements(text, self.replacements)
            if text:
                save_history(stamp, seconds, text)
                set_clipboard(text)
                if self.config["paste"]:
                    paste_at_cursor()
            self.events.put(("ok", text))
        except Exception as error:
            traceback.print_exc()
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


def test_file(config, path):
    """--test: распознать файл и показать скорость и память. Первая строка — цифры, дальше — текст."""
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if missing_files(config["model"]):
        console_download(config["model"])
    engine = Engine(config)
    recognizer = engine.get()
    started = time.perf_counter()
    text = recognizer.transcribe(audio, sample_rate)
    elapsed = time.perf_counter() - started
    replacements, errors = compile_replacements(config["replacements"])
    for error in errors:
        print("Ошибка в replacements, правило пропущено:", error)
    text = apply_replacements(text, replacements)
    seconds = len(audio) / sample_rate
    now, peak = memory_mb()
    print(f"[{seconds:.1f} с аудио → {elapsed:.1f} с ({elapsed / seconds * 60:.1f} с на минуту записи) · "
          f"модель загружена за {engine.load_seconds:.1f} с · потоков {engine.threads} · "
          f"память {now:.0f} МБ, пик {peak:.0f} МБ]")
    print(text)


def main():
    if sys.stderr is None:   # под pythonw консоли нет — ошибки пишем в файл, чтобы было что разбирать
        sys.stderr = sys.stdout = open(HERE / "oshibki.log", "a", encoding="utf-8", buffering=1)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        config = load_config()
    except Exception as error:
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Диктовка", f"Не читается config.json:\n{error}\n\nПапка: {HERE}")
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--download":
        console_download(config["model"])
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        test_file(config, sys.argv[2])
        return
    kernel32.CreateMutexW(None, False, "Local\\diktovka-single-instance")
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS — уже запущена
        return
    Dictation(config).run()


if __name__ == "__main__":
    main()
