# uninstall.ps1 — удаление «Диктовки»:
#   iwr -useb https://raw.githubusercontent.com/slautin-av/diktovka/main/uninstall.ps1 | iex
# История надиктованных текстов (istoriya.md) не удаляется — переезжает на рабочий стол.

$ErrorActionPreference = 'SilentlyContinue'
$dir = Join-Path $env:LOCALAPPDATA 'diktovka'

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*diktovka.pyw*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Milliseconds 500

foreach ($folder in [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Startup')) {
    Remove-Item (Join-Path $folder 'Диктовка.lnk') -Force
}

$history = Join-Path $dir 'istoriya.md'
if (Test-Path $history) {
    $saved = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Диктовка — история.md'
    Move-Item $history $saved -Force
    Write-Host "История диктовок сохранена: $saved"
}
Remove-Item $dir -Recurse -Force

Write-Host 'Диктовка удалена: программа, ключ, настройки, ярлыки и автозагрузка.' -ForegroundColor Green
