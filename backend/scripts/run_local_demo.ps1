$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $workspaceRoot

# This explicit local demo launcher uses the repository SQLite demo database.
# It does not change .env, the configured PostgreSQL database, or production data.
$env:APP_ENV = 'development'
$env:DEMO_MODE = 'true'
$env:AUTO_SEED_DEMO_DATA = 'true'
$env:DATABASE_URL = 'sqlite:///./aerointel_demo.db'

$python = Join-Path $workspaceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'The project virtual environment was not found. Install the requirements first.'
}
& $python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
