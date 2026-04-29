param(
    [string]$PythonCommand = "python"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$workerDir = Join-Path $repoRoot "worker"

function Start-WorkerWindow {
    param(
        [string]$WorkerId,
        [int]$Port,
        [int]$MaxConcurrent
    )

    $command = @(
        "Set-Location '$workerDir'"
        "& $PythonCommand -m pip install -r requirements.txt"
        "`$env:WORKER_ID='$WorkerId'"
        "`$env:MAX_CONCURRENT='$MaxConcurrent'"
        "python -m uvicorn app.main:app --host 0.0.0.0 --port $Port"
    ) -join "; "

    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        $command
    )
}

Start-WorkerWindow -WorkerId "worker-a" -Port 9001 -MaxConcurrent 8
Start-WorkerWindow -WorkerId "worker-b" -Port 9002 -MaxConcurrent 6
Start-WorkerWindow -WorkerId "worker-c" -Port 9003 -MaxConcurrent 10

Write-Host "Started worker-a on 9001, worker-b on 9002, and worker-c on 9003."
