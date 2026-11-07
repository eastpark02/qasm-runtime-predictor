$ErrorActionPreference = 'SilentlyContinue'
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 1 -UseBasicParsing
        if ($response.StatusCode -eq 200) { Start-Process 'http://127.0.0.1:8000'; exit 0 }
    } catch { }
    Start-Sleep -Milliseconds 500
}
