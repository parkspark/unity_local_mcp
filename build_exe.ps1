$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot
uv run pyinstaller --noconfirm --clean UnityLocalAgent.spec

$exe = Join-Path $PSScriptRoot "dist\UnityLocalAgent.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Build completed without producing $exe"
}

Write-Host "Built: $exe"
