# install.ps1 — установка «Диктовки» одной командой:
#   iwr -useb https://raw.githubusercontent.com/slautin-av/diktovka/main/install.ps1 | iex
# Ставит Python (если его нет), программу в %LOCALAPPDATA%\diktovka, спрашивает ключ Groq,
# создаёт ярлыки на рабочем столе и в автозагрузке и запускает. Повторный запуск = обновление.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$raw = 'https://raw.githubusercontent.com/slautin-av/diktovka/main'
$dir = if ($env:DIKTOVKA_DIR) { $env:DIKTOVKA_DIR } else { Join-Path $env:LOCALAPPDATA 'diktovka' }

Write-Host ''
Write-Host '=== Диктовка — установка ===' -ForegroundColor Cyan

# --- 1. Python 3.10 или новее ---
function Find-Python {
    $tries = @(@('py', '-3'), @('python'), @('python3'))
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $tries += , @($_.FullName) }
    foreach ($t in $tries) {
        $exe = $t[0]; $rest = @($t | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $exe @rest -c 'import sys; print(sys.version_info >= (3, 10), sys.executable)' 2>$null
            if ($out -and $out.StartsWith('True ')) { return $out.Substring(5).Trim() }
        } catch {}
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host 'Python не найден — ставлю Python 3.12 (официальный, через winget)...' -ForegroundColor Yellow
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

# --- 4. Своё окружение Python с библиотеками для микрофона ---
$venvPy = Join-Path $dir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host 'Создаю окружение...'
    & $python -m venv (Join-Path $dir '.venv')
}
Write-Host 'Ставлю библиотеки (минута-две)...'
& $venvPy -m pip install -q --disable-pip-version-check --upgrade sounddevice soundfile numpy requests
if ($LASTEXITCODE -ne 0) { throw 'Библиотеки не поставились — проверь интернет и запусти команду ещё раз.' }

# --- 5. Ключ Groq ---
$keyFile = Join-Path $dir 'groq.env'
function Test-GroqKey($key) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://api.groq.com/openai/v1/models' `
            -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 20 | Out-Null
        return 'ok'
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 401) { return 'bad' }
        return 'network'
    }
}

$hasKey = (Test-Path $keyFile) -and ((Get-Content $keyFile -Raw) -match 'GROQ_API_KEY=gsk_')
if ($hasKey) {
    Write-Host 'Ключ Groq уже есть — оставляю.'
} else {
    Write-Host ''
    Write-Host 'Нужен бесплатный ключ Groq (через VPN — из России Groq не открывается):' -ForegroundColor Yellow
    Write-Host '  1. Открой https://console.groq.com и войди (Google или GitHub).'
    Write-Host '  2. Слева «API Keys» -> «Create API Key» -> скопируй ключ (начинается на gsk_).'
    while ($true) {
        $key = if ($env:DIKTOVKA_KEY) { $env:DIKTOVKA_KEY } else { (Read-Host 'Вставь ключ сюда и нажми Enter').Trim() }
        if (-not $key.StartsWith('gsk_')) { Write-Host 'Ключ должен начинаться на gsk_. Ещё раз.' -ForegroundColor Red; continue }
        $check = Test-GroqKey $key
        if ($check -eq 'bad') { Write-Host 'Groq не принял ключ. Скопируй заново.' -ForegroundColor Red; $env:DIKTOVKA_KEY = $null; continue }
        if ($check -eq 'network') {
            Write-Host 'Groq сейчас не отвечает — скорее всего, выключен VPN. Ключ сохраню, проверь VPN перед диктовкой.' -ForegroundColor Yellow
        } else { Write-Host 'Ключ работает.' -ForegroundColor Green }
        Set-Content -Path $keyFile -Value "GROQ_API_KEY=$key" -Encoding Ascii
        break
    }
}

# --- 6. Ярлыки: рабочий стол и автозагрузка ---
$pythonw = Join-Path $dir '.venv\Scripts\pythonw.exe'
$script = Join-Path $dir 'diktovka.pyw'
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
Write-Host 'Если антивирус спросит про микрофон — «Разрешить». Настройки и свой словарь — README.md в папке программы.'
