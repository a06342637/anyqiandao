$ProjectRoot = Split-Path -Parent $PSScriptRoot
$env:TEMP = Join-Path $ProjectRoot '.local\tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot '.local\pip-cache'
$env:npm_config_cache = Join-Path $ProjectRoot '.local\npm-cache'
$env:PNPM_HOME = Join-Path $ProjectRoot '.local\pnpm'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ProjectRoot '.local\browsers'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
foreach ($Directory in @($env:TEMP, $env:PIP_CACHE_DIR, $env:npm_config_cache, $env:PNPM_HOME, $env:PLAYWRIGHT_BROWSERS_PATH)) {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
}
