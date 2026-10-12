param([string]$Go = "go")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root "bin"
New-Item -ItemType Directory -Path $output -Force | Out-Null
Push-Location (Join-Path $root "extractor")
try {
    $env:CGO_ENABLED = "0"
    & $Go build -trimpath -ldflags="-s -w" -o (Join-Path $output "115work-extract.exe") .
    if ($LASTEXITCODE -ne 0) { throw "Extractor build failed" }
} finally {
    Pop-Location
}
