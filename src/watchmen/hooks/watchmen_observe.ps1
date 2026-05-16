# Pipes hook stdin JSON to the local observer and exits 0.
# Never blocks the Claude Code session — short timeout, errors swallowed
# but logged to %USERPROFILE%\.watchmen\logs\hooks.log so debugging isn't a
# %TEMP% scavenger hunt.

$ErrorActionPreference = "Continue"

$logDir = Join-Path $env:USERPROFILE ".watchmen\logs"
$logFile = Join-Path $logDir "hooks.log"
try { New-Item -ItemType Directory -Force -Path $logDir | Out-Null } catch {}

# Read all of stdin (the hook payload). [Console]::In is the only path that
# reliably preserves binary-safe UTF-8 in older PS versions.
$payload = [Console]::In.ReadToEnd()

try {
    # Use HttpWebRequest instead of Invoke-WebRequest — Invoke-WebRequest
    # has a 30s minimum on PS 5.1 and pulls in IE COM objects on first use,
    # both unacceptable for an on-every-tool-call hook.
    $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:8765/hook")
    $req.Method = "POST"
    $req.ContentType = "application/json"
    $req.Timeout = 2000
    $req.ReadWriteTimeout = 2000
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    $req.ContentLength = $bytes.Length
    $stream = $req.GetRequestStream()
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Close()
    $resp = $req.GetResponse()
    $resp.Close()
} catch {
    try {
        $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Add-Content -Path $logFile -Value "[$stamp] watchmen_observe: POST failed ($($_.Exception.Message))"
    } catch {}
}

exit 0
