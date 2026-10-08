# install.ps1 — установка «Диктовки» одной командой:
#   irm https://raw.githubusercontent.com/slautin-av/diktovka/main/install.ps1 | iex
# Файл без BOM намеренно: запускается через irm | iex, а BOM в начале строки PowerShell принял бы за команду.
# Ставит Python (если подходящего нет), программу в %LOCALAPPDATA%\diktovka, скачивает модель распознавания
# GigaAM-v3 (~0,9 ГБ, один раз), создаёт ярлыки на рабочем столе и в автозагрузке и запускает.
# Повторный запуск = обновление (настройки и модель сохраняются).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$raw = 'https://raw.githubusercontent.com/slautin-av/diktovka/main'
$dir = if ($env:DIKTOVKA_DIR) { $env:DIKTOVKA_DIR } else { Join-Path $env:LOCALAPPDATA 'diktovka' }
# Python 3.10–3.13: под них есть готовая сборка onnxruntime той версии, на которой проверялась модель
$pyCheck = 'import sys; print((3, 10) <= sys.version_info[:2] <= (3, 13), sys.executable)'

Write-Host ''
Write-Host '=== Диктовка — установка ===' -ForegroundColor Cyan

# --- 1. Python 3.10–3.13 ---
function Find-Python {
    $tries = @(@('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'), @('py', '-3.10'), @('py', '-3'), @('python'), @('python3'))
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $tries += , @($_.FullName) }
    foreach ($t in $tries) {
        $exe = $t[0]; $rest = @($t | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $exe @rest -c $pyCheck 2>$null
            if ($out -and $out.StartsWith('True ')) { return $out.Substring(5).Trim() }
        } catch {}
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host 'Подходящего Python нет — ставлю Python 3.12 (официальный, через winget)...' -ForegroundColor Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Нет winget. Поставь Python 3.12 с python.org (галочка «Add to PATH») и запусти установку ещё раз.'
    }
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $python = Find-Python
    if (-not $python) { throw 'Python поставился, но не находится. Закрой PowerShell, открой заново и повтори команду.' }
}
Write-Host "Python: $python"

# --- 2. Остановить работающую копию (если это обновление) ---
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*diktovka.pyw*' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }

# --- 3. Файлы программы ---
New-Item -ItemType Directory -Force -Path $dir | Out-Null
foreach ($file in 'diktovka.pyw', 'README.md') {
    if ($env:DIKTOVKA_SOURCE) { Copy-Item (Join-Path $env:DIKTOVKA_SOURCE $file) (Join-Path $dir $file) -Force }
    else { Invoke-WebRequest -UseBasicParsing -Uri "$raw/$file" -OutFile (Join-Path $dir $file) }
}
Write-Host "Программа: $dir"

# --- 4. Своё окружение Python с библиотеками ---
$venv = Join-Path $dir '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
if (Test-Path $venvPy) {
    # окружение от прошлой версии могло быть собрано на Python, под который нет onnxruntime, — тогда пересобираем
    $ok = $false
    try { $ok = (& $venvPy -c $pyCheck 2>$null) -like 'True *' } catch {}
    if (-not $ok) { Write-Host 'Пересобираю окружение под подходящий Python...'; Remove-Item $venv -Recurse -Force }
}
if (-not (Test-Path $venvPy)) {
    Write-Host 'Создаю окружение...'
    & $python -m venv $venv
}
Write-Host 'Ставлю библиотеки (минута-две)...'
& $venvPy -m pip install -q --disable-pip-version-check --upgrade sounddevice soundfile numpy 'onnxruntime==1.23.2' 'onnx-asr==0.12.0'
if ($LASTEXITCODE -ne 0) { throw 'Библиотеки не поставились — проверь интернет и запусти команду ещё раз.' }

# --- 5. Модель распознавания: один раз, ~0,9 ГБ; дальше интернет не нужен ---
$script = Join-Path $dir 'diktovka.pyw'
& $venvPy $script --download
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Модель сейчас не скачалась. Диктовка докачает её сама при первом запуске — нужен интернет.' -ForegroundColor Yellow
}

# Ключ Groq от прошлой версии больше не нужен
$oldKey = Join-Path $dir 'groq.env'
if (Test-Path $oldKey) { Remove-Item $oldKey -Force; Write-Host 'Ключ Groq больше не нужен — groq.env удалён.' }

# --- 6. Ярлыки: рабочий стол и автозагрузка ---
$pythonw = Join-Path $dir '.venv\Scripts\pythonw.exe'
if (-not $env:DIKTOVKA_NO_SHORTCUTS) {
    $ws = New-Object -ComObject WScript.Shell
    foreach ($folder in [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Startup')) {
        $lnk = $ws.CreateShortcut((Join-Path $folder 'Диктовка.lnk'))
        $lnk.TargetPath = $pythonw
        $lnk.Arguments = "`"$script`""
        $lnk.WorkingDirectory = $dir
        $lnk.IconLocation = 'C:\Windows\System32\SndVol.exe,0'
        $lnk.Description = 'Диктовка: Ctrl+Пробел — говоришь, текст вставляется в поле'
        $lnk.Save()
    }
    Write-Host 'Ярлык «Диктовка» — на рабочем столе и в автозагрузке.'
}

# --- 7. Запуск ---
if (-not $env:DIKTOVKA_NO_LAUNCH) { Start-Process -FilePath $pythonw -ArgumentList "`"$script`"" -WorkingDirectory $dir }

Write-Host ''
Write-Host 'Готово! Диктовка работает в фоне и будет запускаться сама при входе в Windows.' -ForegroundColor Green
Write-Host 'Поставь курсор в любое поле -> Ctrl+Пробел -> говори -> Ctrl+Пробел. Esc — отмена.'
Write-Host 'Распознаёт прямо на компьютере: интернет, ключ и VPN больше не нужны.'
Write-Host 'Если антивирус спросит про микрофон — «Разрешить». Настройки и свой словарь — README.md в папке программы.'
