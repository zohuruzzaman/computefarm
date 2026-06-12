# solve_gsz_geocmd.ps1 - PILOT variant of solve_gsz.ps1.
#
# Drop-in compatible (same -GszPath / -MeshEdge / -HeartbeatSec / -ScriptsDir
# params), but solves via GeoStudio's official command-line solver GeoCmd.exe
# instead of the gsi gRPC server. ~160x faster on healthy H15 files
# (19.8s vs 53min median) — same underlying solver as the GUI's Solve Manager.
#
# Mesh handling: raw .gsz from the corpus generator may have mesh_1.ply stripped.
# GeoCmd.exe /solve auto-regenerates it from geometry on the first analysis
# (same as the GUI's Solve button on an unbaked file). No gsi pre-step needed.
#
# Analysis ordering: invoked with /solve and no analysis list, GeoCmd runs the
# full chain respecting <ParentID> dependencies. Healthy files complete cleanly
# in 20-60s. Files with model bugs (e.g. Rainfall step grid mismatched to
# L/D Day count) fail loudly with "File not found: Analysis\N\node.csv" —
# that's a CORPUS bug, not a solver bug; gsi fails on the same files.
#
# If -MeshEdge > 0 is supplied, a short gsi pre-step sets the global element
# size (DisplayMeshDefaultEdgeLength) and saves, so GeoCmd picks up the
# override. With -MeshEdge 0 (default), the .gsz's existing edge length is used.
#
# Usage:
#   .\solve_gsz_geocmd.ps1 -GszPath C:\path\to\G_00000.gsz
#   .\solve_gsz_geocmd.ps1 -GszPath ... -MeshEdge 4.0

param(
    [Parameter(Mandatory=$true)]
    [string]$GszPath,
    [double]$MeshEdge = 0,
    [int]$HeartbeatSec = 15,
    [string]$ScriptsDir = $env:GEO_SCRIPTS_DIR,
    # Progress watchdog: kill ONLY if the .gsz output stops growing for this many
    # minutes (no hard total time limit). The .gsz batch-flushes steps in chunks
    # (flat ~2 min between flushes is normal), so this must be generous. A genuine
    # stall sits flat for hours. Override via env GEO_STALL_IDLE_MIN.
    [int]$StallIdleMin = 30
)
if ($env:GEO_STALL_IDLE_MIN) { $StallIdleMin = [int]$env:GEO_STALL_IDLE_MIN }

# ---------------------------------------------------------------------------
# Locate GeoCmd.exe. Allow override via env var for non-standard install paths.
# ---------------------------------------------------------------------------
$GeoCmd = $env:GEOCMD_EXE
if (-not $GeoCmd) {
    $GeoCmd = 'C:\Program Files\Seequent\GeoStudio 2025.2\Bin\GeoCmd.exe'
}
if (-not (Test-Path $GeoCmd)) {
    Write-Host "ERROR: GeoCmd.exe not found at '$GeoCmd'." -ForegroundColor Red
    Write-Host "       Set GEOCMD_EXE env var to the correct path, or install GeoStudio 2025.2." -ForegroundColor Red
    exit 3
}

if (-not (Test-Path $GszPath)) {
    Write-Host "ERROR: $GszPath not found" -ForegroundColor Red
    exit 2
}
$GszAbs = (Resolve-Path $GszPath).Path

# ---------------------------------------------------------------------------
# Optional pre-step: set mesh edge length via gsi, then save and close.
# We only pay this cost if -MeshEdge > 0; otherwise GeoCmd uses the value
# already in the .gsz.
# ---------------------------------------------------------------------------
function Invoke-MeshEdgePreStep {
    param([string]$Path, [double]$Edge, [string]$ScriptsDir)

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyLauncher) {
        Write-Host "ERROR: -MeshEdge requested but 'py' launcher not available for gsi pre-step." -ForegroundColor Red
        exit 3
    }
    & py -3.12 --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Python 3.12 not registered with the py launcher." -ForegroundColor Red
        exit 3
    }

    if (-not $ScriptsDir) {
        Write-Host "ERROR: -MeshEdge requires -ScriptsDir (for _force_remesh helper)." -ForegroundColor Red
        exit 2
    }
    if (-not (Test-Path (Join-Path $ScriptsDir "solve_and_extract.py"))) {
        Write-Host "ERROR: solve_and_extract.py not found in $ScriptsDir" -ForegroundColor Red
        exit 2
    }
    if ($ScriptsDir -match '^\\\\') {
        $localImportDir = Join-Path $env:TEMP "computefarm_scripts"
        New-Item -ItemType Directory -Force -Path $localImportDir | Out-Null
        Copy-Item (Join-Path $ScriptsDir "solve_and_extract.py") (Join-Path $localImportDir "solve_and_extract.py") -Force
        $ScriptsDir = $localImportDir
    }

    $preTemplate = @'
import sys
from pathlib import Path

SCRIPTS = r"__SCRIPTS__"
GSZ     = Path(r"__GSZ__")
EDGE    = __EDGE__

sys.path.insert(0, SCRIPTS)
import gsi
from solve_and_extract import discover_chain, _force_remesh

chain = discover_chain(GSZ)
proj = gsi.OpenProject(str(GSZ.resolve()))
try:
    edge = _force_remesh(proj, chain[0], edge_override=EDGE)
    print(f"  mesh edge set to {edge:.3f} (will rebake on first solve)", flush=True)
    proj.Save()
finally:
    try: proj.Close()
    except Exception: pass
'@
    $py = $preTemplate `
        -replace '__SCRIPTS__', $ScriptsDir.Replace('\', '\\') `
        -replace '__GSZ__',     $Path.Replace('\', '\\') `
        -replace '__EDGE__',    $Edge

    $py | & py -3.12 -
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: mesh-edge pre-step failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ("=" * 64)
Write-Host "  Variant : GeoCmd.exe (pilot)"
Write-Host "  GSZ     : $(Split-Path $GszAbs -Leaf)"
Write-Host "  Path    : $GszAbs"
Write-Host "  GeoCmd  : $GeoCmd"
if ($MeshEdge -gt 0) {
    Write-Host "  MeshEdge: $MeshEdge ft (will pre-set via gsi)"
} else {
    Write-Host "  MeshEdge: <keep as-is in .gsz>"
}
Write-Host ("=" * 64)

$start = Get-Date

# ---------------------------------------------------------------------------
# Pre-step (mesh edge only)
# ---------------------------------------------------------------------------
if ($MeshEdge -gt 0) {
    Write-Host ""
    Write-Host "Pre-step: setting mesh edge via gsi..."
    Invoke-MeshEdgePreStep -Path $GszAbs -Edge $MeshEdge -ScriptsDir $ScriptsDir
}

# ---------------------------------------------------------------------------
# Run GeoCmd.exe /solve on the full chain. GeoCmd respects analysis
# dependencies (parents finish before children start). Heartbeat shows the
# log file growing — without it the worker looks frozen.
# ---------------------------------------------------------------------------
$solverLog = [System.IO.Path]::GetTempFileName() + ".geocmd.log"

Write-Host ""
Write-Host "Solving via GeoCmd.exe /solve (log: $solverLog)..."

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $GeoCmd
# PS 5.1 lacks ArgumentList; .Arguments takes a single string. Quote paths.
$psi.Arguments = ('"{0}" /solve "/log:{1}"' -f $GszAbs, $solverLog)
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError  = $true
$psi.CreateNoWindow = $true

$proc = [System.Diagnostics.Process]::Start($psi)
$stdoutSb = New-Object System.Text.StringBuilder
$stderrSb = New-Object System.Text.StringBuilder
$null = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived `
    -SourceIdentifier "GeoCmdOut.$($proc.Id)" -Action {
        if ($EventArgs.Data) { [void]$Event.MessageData.Append($EventArgs.Data + "`n") }
    } -MessageData $stdoutSb
$null = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived `
    -SourceIdentifier "GeoCmdErr.$($proc.Id)" -Action {
        if ($EventArgs.Data) { [void]$Event.MessageData.Append($EventArgs.Data + "`n") }
    } -MessageData $stderrSb
$proc.BeginOutputReadLine()
$proc.BeginErrorReadLine()

$lastLogSize = -1
$lastGszSize = -1
$lastChange  = Get-Date
$StallIdleSec = $StallIdleMin * 60
# WaitForExit($ms) returns true on exit, false on timeout (wakes immediately on
# exit). PROGRESS WATCHDOG (replaces the old hard timeout): any change in the
# .gsz OR solver-log size counts as progress and resets the idle clock; we kill
# ONLY if both stay flat for $StallIdleMin minutes. No hard total time limit, so
# a genuinely-long-but-progressing solve runs to completion.
while (-not $proc.WaitForExit($HeartbeatSec * 1000)) {
    $elapsed = ((Get-Date) - $start).TotalSeconds
    $logsz = 0; if (Test-Path $solverLog) { $logsz = (Get-Item $solverLog).Length }
    $gszsz = 0; if (Test-Path $GszAbs)    { $gszsz = (Get-Item $GszAbs).Length }
    if ($gszsz -ne $lastGszSize -or $logsz -ne $lastLogSize) { $lastChange = Get-Date }
    $lastGszSize = $gszsz; $lastLogSize = $logsz
    $idle = ((Get-Date) - $lastChange).TotalSeconds
    Write-Host ("    .. running, {0:N0}s, gsz {1:N0}B, idle {2:N0}/{3:N0}s" -f $elapsed, $gszsz, $idle, $StallIdleSec)
    if ($idle -ge $StallIdleSec) {
        Write-Host ("    STALL WATCHDOG: no .gsz/log growth for {0:N0}s (> {1} min) -> killing." -f $idle, $StallIdleMin) -ForegroundColor Yellow
        & taskkill /F /T /PID $proc.Id 2>$null | Out-Null
        Start-Sleep -Seconds 2
        Unregister-Event -SourceIdentifier "GeoCmdOut.$($proc.Id)" -ErrorAction SilentlyContinue
        Unregister-Event -SourceIdentifier "GeoCmdErr.$($proc.Id)" -ErrorAction SilentlyContinue
        Write-Host ("Stalled (no progress) after {0:N1}s" -f $elapsed) -ForegroundColor Red
        exit 7
    }
}
Unregister-Event -SourceIdentifier "GeoCmdOut.$($proc.Id)" -ErrorAction SilentlyContinue
Unregister-Event -SourceIdentifier "GeoCmdErr.$($proc.Id)" -ErrorAction SilentlyContinue
Get-Job | Where-Object { $_.State -eq 'Completed' -or $_.State -eq 'Failed' } | Remove-Job -Force -ErrorAction SilentlyContinue

$code = $proc.ExitCode
$elapsed = (Get-Date) - $start

$soStr = $stdoutSb.ToString().Trim()
$seStr = $stderrSb.ToString().Trim()
if ($soStr) { Write-Host ""; Write-Host "---- GeoCmd stdout ----"; Write-Host $soStr }
if ($seStr) { Write-Host ""; Write-Host "---- GeoCmd stderr ----"; Write-Host $seStr }

if (Test-Path $solverLog) {
    Write-Host ""
    Write-Host "---- GeoCmd solver log (last 30 lines) ----"
    Get-Content $solverLog -Tail 30 | ForEach-Object { Write-Host "    $_" }
    if ($code -eq 0) { Remove-Item $solverLog -Force -ErrorAction SilentlyContinue }
}

Write-Host ""
Write-Host ("=" * 64)
Write-Host ("  Total wall time: {0:N2} min ({1:N1}s)" -f $elapsed.TotalMinutes, $elapsed.TotalSeconds)
Write-Host ("  Exit code      : {0}" -f $code)
Write-Host ("=" * 64)

# ---------------------------------------------------------------------------
# Post-solve summary (mesh + FoS), same as solve_gsz.ps1's tail.
# Pure-zip read; no gsi dependency.
# ---------------------------------------------------------------------------
if ($code -eq 0) {
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($GszAbs)
        try {
            # GeoCmd names the top-level mesh after the geometry id (mesh_1.ply,
            # mesh_3.ply, ...), not always mesh_1.ply.
            $meshEntry = $zip.Entries | Where-Object { $_.FullName -match '^mesh_\d+\.ply$' } | Select-Object -First 1
            if ($meshEntry) {
                $sr = New-Object System.IO.StreamReader($meshEntry.Open())
                $head = $sr.ReadToEnd().Substring(0, [Math]::Min(2048, $meshEntry.Length))
                $sr.Close()
                $nodes = [regex]::Match($head, 'element vertex (\d+)').Groups[1].Value
                $elems = [regex]::Match($head, 'element face (\d+)').Groups[1].Value
                Write-Host ("  Mesh         : $($meshEntry.FullName)")
                if ($nodes) { Write-Host "  Mesh nodes   : $nodes" }
                if ($elems) { Write-Host "  Mesh elements: $elems" }
            } else {
                Write-Host "  WARN: no mesh_*.ply at top level after solve" -ForegroundColor Yellow
            }
            $fosCount = ($zip.Entries | Where-Object { $_.FullName -like '*lambdafos*' -and $_.FullName.EndsWith('.csv') }).Count
            if ($fosCount -gt 0) { Write-Host "  FoS files    : $fosCount" }
        } finally {
            $zip.Dispose()
        }
    } catch {
        Write-Host "  WARN: post-solve summary read failed: $_" -ForegroundColor Yellow
    }
}

if ($code -eq 0) {
    Write-Host ("Wall time: {0:N1}s" -f $elapsed.TotalSeconds) -ForegroundColor Green
} else {
    Write-Host ("Failed after {0:N1}s (exit {1})" -f $elapsed.TotalSeconds, $code) -ForegroundColor Red
}
exit $code
