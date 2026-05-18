# solve_gsz.ps1 - mesh + solve one .gsz file with verbose progress output.
#
# Wraps the gsi solver to give live feedback:
#   - prints the full analysis chain at the start
#   - "[i/N] solving <Analysis Name>..." as each one begins
#   - heartbeat every 15s while an analysis is running (so you know it's alive)
#   - per-analysis duration + ETA based on rolling average
#   - FoS summary (min/max By Force, By Moment) after FS analysis completes
#   - mesh stats (node count, element count) at the end
#
# Auto-meshes via the gsi DisplayMeshDefaultEdgeLength dirty-bit trick - no
# GUI, no pyautogui. After this finishes, the .gsz has mesh_1.ply + per-
# analysis/step result CSVs baked in.
#
# Usage:
#   .\solve_gsz.ps1 -GszPath C:\path\to\G_00000.gsz
#   .\solve_gsz.ps1 -GszPath ... -MeshEdge 4.0       # override edge length (ft)
#
# Requires (one-time per worker):
#   - GeoStudio 2024/2025+ installed
#   - Python 3.12 with gsi wheel installed
#       py -3.12 -m pip install "C:\Program Files\Seequent\GeoStudio 2025.2\API\gsi-2025.2.1-py3-none-any.whl"
#   - py -3.12 -m pip install pandas plyfile
#   - $env:GEO_SCRIPTS_DIR pointing at geostudio_automation\scripts\
#     (or pass -ScriptsDir explicitly)
#
# Python interpreter is pinned to 3.12 via the `py` launcher so it works
# even if PATH has multiple Pythons or a Microsoft Store python.exe alias.

param(
    [Parameter(Mandatory=$true)]
    [string]$GszPath,
    [double]$MeshEdge = 0,
    [int]$HeartbeatSec = 15,
    [string]$ScriptsDir = $env:GEO_SCRIPTS_DIR
)

# ---- Pin to Python 3.12 ---------------------------------------------------
# Resolve `py -3.12` to a usable interpreter. If the py launcher exists and
# has a 3.12 install registered, use it. Otherwise fall back to a system
# python.exe at well-known paths. Bare `python` is intentionally NOT used
# (the Microsoft Store alias / mixed environments break gsi import).
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
$pythonOK   = $false
if ($pyLauncher) {
    & py -3.12 --version *> $null
    if ($LASTEXITCODE -eq 0) { $pythonOK = $true }
}
if (-not $pythonOK) {
    Write-Host "ERROR: Python 3.12 not available via 'py -3.12'." -ForegroundColor Red
    Write-Host "       Run framework\setup.bat (or framework\_ensure_python312.bat) first." -ForegroundColor Red
    exit 3
}

if (-not $ScriptsDir) {
    Write-Host "ERROR: GEO_SCRIPTS_DIR is not set and -ScriptsDir was not passed." -ForegroundColor Red
    Write-Host "Set it to the path of geostudio_automation\scripts\ (contains solve_and_extract.py)."
    exit 2
}
if (-not (Test-Path (Join-Path $ScriptsDir "solve_and_extract.py"))) {
    Write-Host "ERROR: solve_and_extract.py not found in $ScriptsDir" -ForegroundColor Red
    exit 2
}
if (-not (Test-Path $GszPath)) {
    Write-Host "ERROR: $GszPath not found" -ForegroundColor Red
    exit 2
}

# Python cannot import from UNC paths via sys.path on Windows.
# Copy solve_and_extract.py to a local temp dir when ScriptsDir is a UNC path.
if ($ScriptsDir -match '^\\\\') {
    $localImportDir = Join-Path $env:TEMP "computefarm_scripts"
    New-Item -ItemType Directory -Force -Path $localImportDir | Out-Null
    Copy-Item (Join-Path $ScriptsDir "solve_and_extract.py") (Join-Path $localImportDir "solve_and_extract.py") -Force
    $ScriptsDir = $localImportDir
}

$GszAbs  = (Resolve-Path $GszPath).Path
$meshArg = if ($MeshEdge -gt 0) { "$MeshEdge" } else { "None" }

# Build the Python program. Single-quoted here-string so PowerShell does NOT
# expand $ inside. We splice in three values via simple replacement below.
$pyTemplate = @'
import io, re, sys, threading, time, zipfile
from pathlib import Path

SCRIPTS = r"__SCRIPTS__"
GSZ     = Path(r"__GSZ__")
MESH    = __MESH__
HB      = __HB__

sys.path.insert(0, SCRIPTS)
import gsi
from solve_and_extract import discover_chain, _force_remesh

print()
print("=" * 64)
print(f"  GSZ      : {GSZ.name}")
print(f"  Path     : {GSZ}")
print(f"  Heartbeat: every {HB}s during each analysis")
print("=" * 64)

chain = discover_chain(GSZ)
print(f"\nAnalysis chain ({len(chain)} analyses):")
for i, name in enumerate(chain, 1):
    print(f"  {i:2}. {name}")
print()

proj = gsi.OpenProject(str(GSZ.resolve()))
overall_start = time.perf_counter()
per_analysis = []

try:
    # Force re-mesh by toggling the global element size.
    edge = _force_remesh(proj, chain[0], edge_override=MESH)
    print(f"Mesh edge length: {edge:.3f} ft  (will regenerate mesh_1.ply on first solve)\n")

    for idx, an in enumerate(chain, 1):
        rem = len(chain) - idx + 1
        eta_str = ""
        if per_analysis:
            avg = sum(per_analysis) / len(per_analysis)
            eta = avg * rem
            eta_str = f"   ETA ~{eta/60:.1f} min remaining"
        print(f"[{idx:2}/{len(chain)}] solving '{an}'...{eta_str}", flush=True)

        t0 = time.perf_counter()
        done = threading.Event()

        def heartbeat(name=an, start=t0):
            while not done.wait(HB):
                el = time.perf_counter() - start
                print(f"    .. '{name}' still running, {el:.0f}s elapsed", flush=True)

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        try:
            resp = proj.SolveAnalyses(gsi.SolveAnalysesRequest(
                analyses=[an], solve_dependencies=False))
        finally:
            done.set()
            hb.join(0.1)

        dt = time.perf_counter() - t0
        per_analysis.append(dt)

        if not resp.all_succeeded:
            errs = []
            for k in resp.completion_status:
                v = resp.completion_status[k]
                if not v.succeeded:
                    errs.append(f"{k}: {v.error_message[:200].strip()}")
            print(f"    FAIL after {dt:.1f}s  ->  {' | '.join(errs)}")
            raise SystemExit(1)
        print(f"    ok ({dt:.1f}s)")

    proj.Save()

finally:
    try: proj.Close()
    except Exception: pass

total = time.perf_counter() - overall_start

# ------------------------------------------------------------------
# Post-solve summary: mesh stats + FoS
# ------------------------------------------------------------------
print()
print("=" * 64)
print(f"  Total solve time: {total/60:.2f} min ({total:.1f}s)")
print("=" * 64)

with zipfile.ZipFile(GSZ) as z:
    names = z.namelist()

    if "mesh_1.ply" in names:
        ply_text = z.read("mesh_1.ply").decode("utf-8", errors="replace")
        m = re.search(r"element vertex (\d+)", ply_text)
        if m: print(f"  Mesh nodes   : {int(m.group(1))}")
        m = re.search(r"element face (\d+)", ply_text)
        if m: print(f"  Mesh elements: {int(m.group(1))}")

    fos_files = sorted(n for n in names
                       if "lambdafos" in n.lower() and n.endswith(".csv"))
    if fos_files:
        try:
            import pandas as pd
        except ImportError:
            pd = None
        print(f"  FoS files    : {len(fos_files)}")
        for fn in fos_files:
            if pd is None:
                continue
            try:
                df = pd.read_csv(io.BytesIO(z.read(fn)))
            except Exception:
                continue
            parts = []
            for col in ("FOSByMoment", "FOSByForce"):
                if col in df.columns:
                    v = pd.to_numeric(df[col], errors="coerce").dropna()
                    if len(v):
                        parts.append(f"{col}: min={v.min():.3f} max={v.max():.3f}")
            if parts:
                print(f"    {fn}  ->  " + "  |  ".join(parts))

print()
print("OK")
raise SystemExit(0)
'@

$py = $pyTemplate `
    -replace '__SCRIPTS__', $ScriptsDir.Replace('\', '\\') `
    -replace '__GSZ__',     $GszAbs.Replace('\', '\\') `
    -replace '__MESH__',    $meshArg `
    -replace '__HB__',      $HeartbeatSec

$start = Get-Date
$py | & py -3.12 -
$code = $LASTEXITCODE
$elapsed = (Get-Date) - $start

if ($code -eq 0) {
    Write-Host ("Wall time: {0:N1}s" -f $elapsed.TotalSeconds) -ForegroundColor Green
} else {
    Write-Host ("Failed after {0:N1}s (exit {1})" -f $elapsed.TotalSeconds, $code) -ForegroundColor Red
}
exit $code
