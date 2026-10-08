# install.ps1 — установка «Диктовки» одной командой:
#   irm https://raw.githubusercontent.com/slautin-av/diktovka/main/install.ps1 | iex
# Файл без BOM намеренно: запускается через irm | iex, а BOM в начале строки PowerShell принял бы за команду.
# Ставит Python (если подходящего нет — официальный с python.org, только для текущего пользователя, права
# администратора не нужны), программу в %LOCALAPPDATA%\diktovka, скачивает модель распознавания GigaAM-v3
# (~0,9 ГБ, один раз, из выпуска этого репозитория на GitHub; запасной источник — Hugging Face),
# создаёт ярлыки на рабочем столе и в автозагрузке и запускает.
# Повторный запуск = обновление (настройки и модель сохраняются).
#
# Необязательные переменные окружения — для проверок и своих сборок, обычной установке не нужны:
#   DIKTOVKA_DIR=<папка>     поставить программу в другую папку (модель — всё равно в %LOCALAPPDATA%\diktovka\models)
#   DIKTOVKA_SOURCE=<папка>  взять diktovka.pyw и README.md из своей папки, а не с GitHub
#   DIKTOVKA_NO_SHORTCUTS=1  не создавать ярлыки на рабочем столе и в автозагрузке
#   DIKTOVKA_NO_LAUNCH=1     не запускать программу в конце
# Пробная установка, которая не задевает основную: в отдельном окне PowerShell задать
# $env:LOCALAPPDATA = '<пустая папка>' и $env:DIKTOVKA_NO_SHORTCUTS = '1' — туда лягут программа, модель
# и Python (если его нет). Установщик останавливает только копию, запущенную из своей папки.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$raw = 'https://raw.githubusercontent.com/slautin-av/diktovka/main'
$dir = if ($env:DIKTOVKA_DIR) { $env:DIKTOVKA_DIR } else { Join-Path $env:LOCALAPPDATA 'diktovka' }
$script = Join-Path $dir 'diktovka.pyw'
# Python 3.10–3.13: под них есть готовая сборка onnxruntime той версии, на которой проверялась модель
$pyCheck = 'import sys; print((3, 10) <= sys.version_info[:2] <= (3, 13), sys.executable)'
# Если Python нет — этот, официальный, с python.org; sha256 — опубликованный на python.org
$pyVersion = '3.13.16'
$pySetupSha256 = 'fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b'

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

# Официальный установщик с python.org: только для текущего пользователя (права администратора не нужны),
# в %LOCALAPPDATA%\Programs\Python\Python313 — там его найдёт Find-Python; PATH и ассоциации файлов не трогает.
function Install-PythonOrg {
    $setup = Join-Path $env:TEMP "python-$pyVersion-amd64.exe"
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-amd64.exe" -OutFile $setup
    try {
        if ((Get-FileHash $setup -Algorithm SHA256).Hash -ne $pySetupSha256) { throw 'установщик Python скачался с ошибкой' }
        $target = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313'
        $setupArgs = '/quiet', 'InstallAllUsers=0', "TargetDir=`"$target`"", 'PrependPath=0', 'Include_launcher=0',
                     'InstallLauncherAllUsers=0', 'Include_test=0', 'Include_doc=0', 'Shortcuts=0', 'AssociateFiles=0'
        $run = Start-Process -FilePath $setup -ArgumentList $setupArgs -Wait -PassThru
        if ($run.ExitCode -ne 0) { throw "установщик Python завершился с кодом $($run.ExitCode)" }
    } finally { Remove-Item $setup -Force -ErrorAction SilentlyContinue }
}

$python = Find-Python
if (-not $python) {
    Write-Host "Подходящего Python нет — ставлю Python $pyVersion (официальный, с python.org, минута-две)..." -ForegroundColor Yellow
    if ($env:PROCESSOR_ARCHITECTURE -eq 'AMD64') {
        try { Install-PythonOrg } catch { Write-Host "С python.org не поставился: $($_.Exception.Message)" -ForegroundColor Yellow }
        $python = Find-Python
    }
    if (-not $python -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host 'Пробую через winget...' -ForegroundColor Yellow
        winget install -e --id Python.Python.3.13 --scope user --source winget --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $python = Find-Python
    }
    if (-not $python) { throw 'Python не поставился. Поставь Python 3.13 с python.org (галочка «Add to PATH») и запусти установку ещё раз.' }
}
Write-Host "Python: $python"

# --- 2. Остановить работающую копию из этой папки (если это обновление) ---
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($script, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
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

# --- 5. Модель распознавания: один раз, ~0,9 ГБ с GitHub (не вышло — с Hugging Face); дальше интернет не нужен ---
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
# Без return/exit: установщик запускают через iex, и они оборвали бы вызвавший его сценарий
if ($env:DIKTOVKA_NO_LAUNCH) {
    Write-Host ''
    Write-Host 'Программа установлена (запуск — отдельно).'
} else {
    Start-Process -FilePath $pythonw -ArgumentList "`"$script`"" -WorkingDirectory $dir
    Write-Host ''
    Write-Host 'Готово! Диктовка работает в фоне и будет запускаться сама при входе в Windows.' -ForegroundColor Green
    Write-Host 'Поставь курсор в любое поле -> Ctrl+Пробел -> говори -> Ctrl+Пробел. Esc — отмена.'
    Write-Host 'Распознаёт прямо на компьютере: интернет, ключ и VPN больше не нужны.'
    Write-Host 'Если антивирус спросит про микрофон — «Разрешить». Настройки и свой словарь — README.md в папке программы.'
}
