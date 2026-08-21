# Start the platform: backend agent (port 8123) + frontend (port 3000) in one console.
# Ctrl-C tears both down: the console interrupt reaches both processes, and the finally
# block tree-kills the agent (uv spawns python as a child, so a plain Stop-Process on uv
# alone would orphan the actual server).
#
#   .\dev.ps1

$agent = Start-Process -FilePath "uv" -ArgumentList "run", "python", "main.py" `
    -WorkingDirectory "$PSScriptRoot\agent" -PassThru -NoNewWindow

try {
    Set-Location $PSScriptRoot
    npm.cmd run dev
}
finally {
    if ($agent -and -not $agent.HasExited) {
        taskkill /PID $agent.Id /T /F | Out-Null
    }
}
