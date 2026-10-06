# Loads .env and starts the neuroflash gateway on http://127.0.0.1:8090
Set-Location $PSScriptRoot
if (-not (Test-Path .env)) {
    Write-Host "No .env found: copy .env.example to .env and fill in NF_CLIENT_ID / NF_CLIENT_SECRET"
    exit 1
}
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*)\s*$') { Set-Item -Path "env:$($matches[1])" -Value $matches[2] }
}
python nf_gate.py @args