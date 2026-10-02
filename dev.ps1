# Start the platform: backend agent (port 8123) + frontend (port 3000) in one console.
# Ctrl-C tears both down: the console interrupt reaches both processes, and the finally block
# then waits for the agent to finish its own shutdown before tree-killing whatever is left (uv
# spawns python as a child, so a plain Stop-Process on uv alone would orphan the actual server).
#
#   .\dev.ps1              # rebuilds the local sandbox image first if agent/sandbox-image/ changed
#   .\dev.ps1 -NoRebuild   # skip that check
#   -ShutdownTimeout 30    # seconds an agent gets to shut down cleanly before it is force-killed
param([switch]$NoRebuild, [int]$ShutdownTimeout = 30)

Set-Location $PSScriptRoot

# Stops the process listening on $Port (or $ProcessId). An agent started by `python main.py` wrote
# a shutdown token to agent\data\shutdown-<port>.token: POST it to /admin/shutdown and the agent
# runs its own shutdown -- cancels graph runs and stops their CLI turns inside the sandboxes --
# before exiting. taskkill /F alone skips all of that, and a turn left running keeps editing the
# session's workspace with nobody waiting on it. Only after $ShutdownTimeout seconds (or straight
# away for a process that was never asked and isn't already stopping) is it force-killed.
# Not CTRL_BREAK via GenerateConsoleCtrlEvent: that reaches only processes in a process group on
# THIS console -- Start-Process can't create one, and a leftover agent from another console is
# out of reach entirely.
function Stop-AgentGracefully([int]$ProcessId, [int]$Port, [switch]$AlreadyStopping) {
    $asked = $false
    $tokenFile = Join-Path $PSScriptRoot "agent\data\shutdown-$Port.token"
    if (Test-Path $tokenFile) {
        try {
            $token = (Get-Content -Raw $tokenFile).Trim()
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/admin/shutdown" `
                -Headers @{ "x-aidw-shutdown-token" = $token } -TimeoutSec 5 | Out-Null
            $asked = $true
            Write-Host "agent on port $Port is shutting down -- waiting up to $ShutdownTimeout s"
        }
        catch {
            # Normal after Ctrl-C: the agent already stopped listening while it shuts down.
            Write-Host "shutdown request to port $Port not accepted: $($_.Exception.Message)"
        }
    }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) { return }
    if (($asked -or $AlreadyStopping) -and $proc.WaitForExit($ShutdownTimeout * 1000)) { return }
    Write-Host "PID $ProcessId ($($proc.ProcessName)) still running -- force-killing it"
    taskkill /PID $ProcessId /T /F | Out-Null
}

if (-not $NoRebuild) {
    # Stale = any tracked file under agent/sandbox-image/ newer than the image. Safe while sessions
    # run: local_docker.py compares image IDs on reattach and recreates containers off a new build.
    $built = docker image inspect ai-dev-workflow-sandbox:latest --format '{{.Created}}' 2>$null
    $newest = (git ls-files agent/sandbox-image | Get-Item | Measure-Object LastWriteTime -Maximum).Maximum
    if (-not $built -or [datetime]($built -replace '\.\d+', '') -lt $newest) {
        Write-Host "sandbox image older than agent/sandbox-image/ -- rebuilding (-NoRebuild to skip)"
        docker build -t ai-dev-workflow-sandbox:latest agent/sandbox-image
        if ($LASTEXITCODE) { throw "sandbox image build failed" }
    }
}

# Free the ports first: a leftover agent/next dev (another console, a crashed run, an AI
# session's background shell) otherwise makes the new one die with WinError 10048.
# /T also takes down anything the listener spawned (next dev's workers).
foreach ($port in 8123, 3000) {
    $owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $owners) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        Write-Host "port $port in use by PID $procId ($($proc.ProcessName)) -- stopping it"
        Stop-AgentGracefully -ProcessId $procId -Port $port
    }
}

$agent = Start-Process -FilePath "uv" -ArgumentList "run", "python", "main.py" `
    -WorkingDirectory "$PSScriptRoot\agent" -PassThru -NoNewWindow

try {
    npm.cmd run dev
}
finally {
    if ($agent -and -not $agent.HasExited) {
        # Ctrl-C already reached the agent too, so it may be mid-shutdown: wait for it either way.
        Stop-AgentGracefully -ProcessId $agent.Id -Port 8123 -AlreadyStopping
    }
}
