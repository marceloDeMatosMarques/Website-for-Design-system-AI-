if (!(Get-Command "uv" -ErrorAction SilentlyContinue)) {
    Write-Host "Aviso: 'uv' no esta instalado. Iniciando instalacao..." -ForegroundColor Yellow
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
}

if (-Not (Test-Path ".ambiente")) {
    Write-Host "Criando ambiente virtual (.ambiente)..." -ForegroundColor Cyan
    uv venv .ambiente
}

Write-Host "Sincronizando bibliotecas do requirements.txt..." -ForegroundColor Cyan
uv pip install -p .ambiente -r requirements.txt

Write-Host "Garantindo que o Chromium do Playwright esta instalado..." -ForegroundColor Cyan
.\.ambiente\Scripts\playwright.exe install chromium

Write-Host "Iniciando Servidor Flask..." -ForegroundColor Green
Write-Host ">>> Pressione CTRL+C para parar o servidor." -ForegroundColor Yellow
Write-Host ">>> O projeto estara disponivel em: http://localhost:5001" -ForegroundColor Yellow
Write-Host "--------------------------------------------------------" -ForegroundColor Cyan

.\.ambiente\Scripts\python.exe app.py

pause
