# GeoStudio Workflow

This file contains the GeoStudio-specific notes for ComputeFarm. The root
README describes the generic farm; this guide covers the bundled `.gsz`
solver path.

## Bundled Solver Scripts

GeoStudio scripts live in:

```text
orchestrator/tools/
```

| Script | Purpose |
| --- | --- |
| `solve_gsz_geocmd.ps1` | Recommended GeoStudio path using `GeoCmd.exe`. |
| `solve_gsz.ps1` | Legacy `gsi` / Python API path. |
| `geostudio_automation/scripts/solve_and_extract.py` | Supporting script used by the legacy path. |

The default configuration points to:

```yaml
solve_script: "solve_gsz_geocmd.ps1"
```

You can set that with:

```powershell
.\configure.ps1 -SolveScript solve_gsz_geocmd.ps1
```

or by editing:

```text
orchestrator/framework/config.yaml
```

## Input Files

The current manifest and resubmit helpers scan only the top level of `raw/`:

```text
orchestrator/raw/*.gsz
```

Keep `.gsz` files directly in `raw/`:

```text
orchestrator/raw/model_001.gsz
orchestrator/raw/model_002.gsz
```

Nested folders are not discovered by the current helpers:

```text
orchestrator/raw/project_a/model_001.gsz
```

The worker also writes solved files flat into `solved/`, using only the input
filename. Avoid duplicate filenames across a batch.

## Storage Hub Setup

On the Windows storage hub:

```powershell
cd E:\Github\computerfarm\orchestrator\framework
.\setup.bat
.\setup_check.bat
```

When `setup.bat` asks whether to share the folder, choose `y` if workers will
mount the orchestrator folder over SMB.

The share created by the script is:

```text
\\<STORAGE_PC>\ComputeFarm
```

Mapped worker PCs normally see:

```text
Z:\framework
Z:\raw
Z:\solved
Z:\logs
Z:\tools
```

## Worker PC Setup

On each Windows worker:

```powershell
\\<STORAGE_PC>\ComputeFarm\connect_drive.ps1
Z:
cd Z:\framework
.\setup.bat
.\setup_check.bat
.\start_workers.bat
```

GeoStudio must be installed on each worker. `setup.bat` attempts to install the
GeoStudio `gsi` wheel if it finds it under the GeoStudio installation folder.

The `GeoCmd.exe` path used by the recommended script defaults to:

```text
C:\Program Files\Seequent\GeoStudio 2025.2\Bin\GeoCmd.exe
```

Override it per machine with:

```powershell
setx GEOCMD_EXE "C:\Path\To\GeoCmd.exe"
```

## Submitting GeoStudio Jobs

Put input files in:

```text
orchestrator/raw/
```

Then run from the storage hub:

```powershell
cd E:\Github\computerfarm\orchestrator
.\make_manifest.bat
.\submit.bat
```

To submit only files that do not already have a solved twin:

```powershell
.\resubmit.bat
```

## Outputs

Solved files are written to:

```text
orchestrator/solved/
```

Per-job logs are written to:

```text
orchestrator/logs/<job_id>/
```

Typical files include:

```text
meta.txt
stdout.attempt1.log
stderr.attempt1.log
```

If a job fails all retry attempts, the worker writes a forensic copy:

```text
orchestrator/solved/<stem>_PARTIAL.gsz
```

## Retry Policy

GeoStudio solve failures are retried up to 6 total attempts with 5 minutes
between attempts.

Worker-environment failures, such as a worker being unable to read the shared
input path, are requeued separately and do not consume the solve retry budget
until the environment requeue cap is reached.
