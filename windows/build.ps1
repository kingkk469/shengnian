param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [Parameter(Mandatory=$true)][string]$ModelRoot,
    [Parameter(Mandatory=$true)][string]$FfmpegRoot,
    [Parameter(Mandatory=$true)][string]$LgplSourceRoot
)
$ErrorActionPreference = 'Stop'
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $OutputRoot) { throw '请使用新的输出目录，避免覆盖已有版本。' }
$env:VOICE_JOURNAL_MODEL_ROOT = $ModelRoot
$env:VOICE_JOURNAL_FFMPEG_ROOT = $FfmpegRoot
$env:VOICE_JOURNAL_LGPL_SOURCE_ROOT = $LgplSourceRoot
& $Python -X utf8 -m PyInstaller --noconfirm --distpath (Join-Path $OutputRoot 'dist') --workpath (Join-Path $OutputRoot 'build') (Join-Path $PSScriptRoot 'shengnian.spec')
if ($LASTEXITCODE -ne 0) { throw '构建失败' }
$Payload = Join-Path $OutputRoot 'dist\声年'
& $Python -X utf8 (Join-Path $PSScriptRoot 'legal_engine.py') --analysis-toc (Join-Path $OutputRoot 'build\shengnian\Analysis-00.toc') --model-root $ModelRoot --output (Join-Path $Payload 'legal') --frozen-payload $Payload --strict
if ($LASTEXITCODE -ne 0) { throw '第三方材料检查失败' }
& $Python -X utf8 (Join-Path $PSScriptRoot 'verify_bundle.py') --root $OutputRoot
if ($LASTEXITCODE -ne 0) { throw '冻结包验证失败' }
& $Python -X utf8 (Join-Path $PSScriptRoot 'package_release.py') --root $OutputRoot
if ($LASTEXITCODE -ne 0) { throw '归档失败' }
