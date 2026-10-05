# Build the floating overlay (overlay/, Rust + Tauri) and copy jarvis-overlay.exe to -Dest.
# Needs Rust (stable, MSVC) and the uv environment (for the icon, drawn in code).
param([string]$Dest = "")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

uv run python (Join-Path $root "packaging\make_icon.py") | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }
New-Item -ItemType Directory -Force (Join-Path $root "overlay\icons") | Out-Null
Copy-Item (Join-Path $root "packaging\jarvis.ico") (Join-Path $root "overlay\icons\icon.ico") -Force

$cargoArgs = @("build", "--release", "--manifest-path", (Join-Path $root "overlay\Cargo.toml"))
if (Test-Path (Join-Path $root "overlay\Cargo.lock")) { $cargoArgs += "--locked" }
& cargo @cargoArgs
if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }

$exe = Join-Path $root "overlay\target\release\jarvis-overlay.exe"
if (-not (Test-Path $exe)) { throw "jarvis-overlay.exe was not produced" }
if ($Dest) {
    New-Item -ItemType Directory -Force $Dest | Out-Null
    Copy-Item $exe $Dest -Force
}
Write-Output $exe
