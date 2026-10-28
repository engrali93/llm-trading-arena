# Stops the LLM Trading Arena: every python/pythonw process whose command line contains arena.py.
# It asks the agent to stop gracefully first (POST /api/shutdown on localhost), so a ledger write in progress
# finishes. It force-kills a process only if it is still running after 10 seconds. Run it through stop.bat.

$procs = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @("python.exe", "pythonw.exe") -and $_.CommandLine -like "*arena.py*"
})

if ($procs.Count -eq 0) {
    Write-Host "Agent is not running"
    exit 0
}

foreach ($p in $procs) {
    # The dashboard port is on the command line (the last --port wins, like argparse); 8001 is the default.
    $port = 8001
    $found = [regex]::Matches($p.CommandLine, "--port[ =](\d+)")
    if ($found.Count -gt 0) { $port = [int]$found[$found.Count - 1].Groups[1].Value }

    $started = Get-Date
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/shutdown" -Method Post -ContentType "application/json" `
            -Body "{}" -TimeoutSec 5 | Out-Null
    } catch {
        Write-Host "  (no answer from the dashboard on port $port; waiting for the process to exit on its own)"
    }

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 250
    }

    if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $how = "was still running after 10 s, so it was force-killed"
    } else {
        $how = "stopped gracefully"
    }
    $secs = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    Write-Host ("Stopped arena.py (PID {0}, dashboard port {1}): {2} ({3} s)" -f $p.ProcessId, $port, $how, $secs)
}

Write-Host "Ollama was left running; quit it from the tray icon if you want to free the GPU."
