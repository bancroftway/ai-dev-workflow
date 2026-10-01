# Start the platform: backend agent (port 8123) + frontend (port 3000) in one console.
# Ctrl-C tears both down: the console interrupt reaches both processes, and the finally
# block tree-kills the agent (uv spawns python as a child, so a plain Stop-Process on uv
# alone would orphan the actual server).
#
#   .\dev.ps1              # rebuilds the local sandbox image first if agent/sandbox-image/ changed
#   .\dev.ps1 -NoRebuild   # skip that check
param([switch]$NoRebuild)

Set-Location $PSScriptRoot

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
        taskkill /PID $procId /T /F | Out-Null
    }
}

$agent = Start-Process -FilePath "uv" -ArgumentList "run", "python", "main.py" `
    -WorkingDirectory "$PSScriptRoot\agent" -PassThru -NoNewWindow

try {
    npm.cmd run dev
}
finally {
    if ($agent -and -not $agent.HasExited) {
        taskkill /PID $agent.Id /T /F | Out-Null
    }
}
