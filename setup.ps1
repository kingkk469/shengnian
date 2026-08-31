param(
    [switch]$SkipDependencies
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python was not found. Install Python 3.12 and enable Add Python to PATH.'
}

& $python.Source -c "import sys; assert sys.version_info[:2] == (3, 12), 'Shengnian requires Python 3.12'; print(sys.version)"

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $python.Source -m venv .venv
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not $SkipDependencies) {
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
}

if (-not (Test-Path -LiteralPath 'src\config.toml')) {
    Copy-Item -LiteralPath 'src\config.example.toml' -Destination 'src\config.toml'
}
if (-not (Test-Path -LiteralPath 'hotwords.txt')) {
    Copy-Item -LiteralPath 'hotwords.example.txt' -Destination 'hotwords.txt'
}
if (-not (Test-Path -LiteralPath 'start-launcher.bat')) {
    Copy-Item -LiteralPath 'start-launcher.example.bat' -Destination 'start-launcher.bat'
}

Write-Host ''
Write-Host 'Shengnian local environment is ready.' -ForegroundColor Green
Write-Host 'Next: configure DEEPSEEK_API_KEY, then run start-launcher.bat.'
Write-Host 'Example: setx DEEPSEEK_API_KEY "your-key"'
