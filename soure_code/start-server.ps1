param([ValidateRange(1024, 65535)][int]$Port = 8000)

$ErrorActionPreference = 'Stop'
$projectPath = $PSScriptRoot
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
$logDirectory = Join-Path $projectPath '.tmp'
$serverUrl = "http://127.0.0.1:$Port"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'The project virtual environment is missing. Follow README.md setup first.'
}

try {
    $health = Invoke-RestMethod -Uri "$serverUrl/health" -TimeoutSec 3
    if ($health.status -eq 'ok' -and $health.database -eq 'supabase') {
        Write-Output "PayScope is already running at $serverUrl/"
        exit 0
    }
} catch {
    # Start only after the local health endpoint could not be verified.
}

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$previousPreviewFlag = $env:ENABLE_SYNTHETIC_PREVIEW
try {
    $env:ENABLE_SYNTHETIC_PREVIEW = 'false'
    $serverProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @('-m', 'payments_analyst', 'serve', '--port', "$Port") `
        -WorkingDirectory $projectPath -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDirectory 'server.stdout.log') `
        -RedirectStandardError (Join-Path $logDirectory 'server.stderr.log') `
        -PassThru
} finally {
    $env:ENABLE_SYNTHETIC_PREVIEW = $previousPreviewFlag
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    $serverProcess.Refresh()
    if ($serverProcess.HasExited) {
        throw 'PayScope stopped during startup. Check soure_code/.tmp/server.stderr.log.'
    }
    try {
        $health = Invoke-RestMethod -Uri "$serverUrl/health" -TimeoutSec 2
        if ($health.status -eq 'ok' -and $health.database -eq 'supabase') {
            Set-Content -LiteralPath (Join-Path $logDirectory 'server.pid') -Value $serverProcess.Id
            Write-Output "PayScope is running at $serverUrl/"
            Write-Output 'Started as a hidden background process; logs are in soure_code/.tmp/.'
            exit 0
        }
    } catch {
        # Give the same process a bounded startup interval; never spawn duplicates.
    }
}

throw 'PayScope startup was not confirmed. Inspect .tmp/server.stderr.log before retrying.'
