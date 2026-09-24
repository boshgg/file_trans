param([switch]$Public, [Parameter(ValueFromRemainingArguments=$true)][string[]]$ServerArgs)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = '1'
$pythonPath = $null
$pythonPrefix = @()
$localConfig = Join-Path $PSScriptRoot '.filehub/python-path.txt'
$candidates = @()
if ($env:FILE_HUB_PYTHON) { $candidates += $env:FILE_HUB_PYTHON }
if (Test-Path -LiteralPath $localConfig) { $candidates += (Get-Content -LiteralPath $localConfig -Raw).Trim() }
$candidates += @('py', 'python', 'python3')
foreach ($candidate in $candidates) {
    $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $resolved) { continue }
    $prefix = @()
    if ($resolved.Name -match '^py(\.exe)?$') { $prefix = @('-3') }
    try {
        & $resolved.Source @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $pythonPath = $resolved.Source
            $pythonPrefix = $prefix
            break
        }
    } catch { continue }
}
if (-not $pythonPath) {
    Write-Host 'Python 3.10+ is required. Install it from https://www.python.org/downloads/ and enable Add Python to PATH.' -ForegroundColor Yellow
    exit 1
}
$entry = 'lan_file_hub.py'
if ($Public) { $entry = 'start_public.py' }
& $pythonPath @pythonPrefix -B $entry @ServerArgs
exit $LASTEXITCODE
