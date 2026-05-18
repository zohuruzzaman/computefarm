# ComputeFarm Queue Setup

This file covers the generic queue workflow under `orchestrator/`. Software-
specific notes, including the bundled GeoStudio example, live in separate
guides such as [GEOSTUDIO.md](GEOSTUDIO.md).

## Orchestrator Layout

```text
orchestrator/
|-- connect_drive.ps1
|-- submit.bat
|-- resubmit.bat
|-- purge_queue.bat
|-- restart_all_workers.bat
|-- stop_all_workers.bat
|-- clean_scratch.bat
|-- make_manifest.bat
|-- framework/
|   |-- config.yaml
|   |-- setup.bat
|   |-- setup_check.bat
|   |-- start_workers.bat
|   |-- generate_manifest.py
|   |-- submit_manifest.py
|   |-- tasks.py
|   `-- requirements.txt
`-- tools/
    |-- <software-runner>.ps1
    `-- ...
```

Runtime folders such as `raw/`, `solved/`, `logs/`, and `manifests/` are
created by setup or by the running workflow.

## First-Time Setup

Run the repository-level configuration script first:

```powershell
cd E:\Github\computerfarm
.\configure.ps1
```

On the Windows storage hub:

```powershell
cd E:\Github\computerfarm\orchestrator\framework
.\setup.bat
.\setup_check.bat
```

`configure.ps1` sets the Redis host, storage share details, worker
concurrency, and the selected script from `orchestrator/tools/`.

## Worker PCs

On each Windows worker:

```powershell
\\STORAGE-PC\ComputeFarm\connect_drive.ps1
Z:
cd Z:\framework
.\setup.bat
.\setup_check.bat
.\start_workers.bat
```

If you used `orchestrator/framework/setup.bat` to create the share, the
`orchestrator/` folder is shared as `\\STORAGE-PC\ComputeFarm`. In that common
case, the mapped path is `Z:\framework`.

If you instead shared the repository root, use `Z:\orchestrator\framework`.

## Job Flow

```text
raw/<input files>
  -> copied to worker local scratch
  -> processed by orchestrator/tools/<configured-script>
  -> copied back to solved/
  -> logs written under logs/<job_id>/
```

The configured script is selected in:

```text
orchestrator/framework/config.yaml
```

or with:

```powershell
.\configure.ps1 -SolveScript <software-runner>.ps1
```

## Submitting Jobs

From the storage hub:

```powershell
cd E:\Github\computerfarm\orchestrator
.\make_manifest.bat
.\submit.bat
```

Useful commands:

| Action | Command |
| --- | --- |
| Generate manifest only | `make_manifest.bat` |
| Submit jobs | `submit.bat` |
| Resubmit missing outputs only | `resubmit.bat` |
| Purge queued jobs | `purge_queue.bat` |
| Restart workers | `restart_all_workers.bat` |
| Stop workers | `stop_all_workers.bat` |
| Clean scratch | `clean_scratch.bat` |

Manual manifest submission is also available:

```powershell
cd E:\Github\computerfarm\orchestrator\framework
python generate_manifest.py ..\raw -o ..\manifests\batch.yaml
python submit_manifest.py ..\manifests\batch.yaml
```

The bundled manifest helpers currently scan the top level of `raw/` for the
extension configured in their source. For a different file extension, update
`generate_manifest.py` and `_resubmit_helper.py`, or prepare manifest YAML
files manually.

## Retry Policy

A processing failure is automatically requeued for another attempt, up to the
retry limit in:

```text
orchestrator/framework/tasks.py
```

Look for:

```python
SOLVE_MAX_ATTEMPTS
SOLVE_RETRY_DELAY_SEC
```

Worker crashes mid-run are handled separately by Celery message redelivery and
do not count against the processing retry budget.

After the final failed attempt, the worker may write a partial forensic copy
to `solved/`, depending on the task implementation and whether a local output
file exists.

## Path Detection

`orchestrator/framework/config.py` resolves paths relative to the
`orchestrator/` folder:

```text
config.py:      Z:\orchestrator\framework\config.py
root:           Z:\orchestrator
raw_dir:        Z:\orchestrator\raw
solved_dir:     Z:\orchestrator\solved
logs_dir:       Z:\orchestrator\logs
tools_dir:      Z:\orchestrator\tools
```

If the storage hub shares `orchestrator/` directly, the same layout appears on
workers as:

```text
config.py:      Z:\framework\config.py
root:           Z:\
raw_dir:        Z:\raw
solved_dir:     Z:\solved
logs_dir:       Z:\logs
tools_dir:      Z:\tools
```

Local scratch storage is selected separately by the worker code, typically
under `C:\ComputeFarm_scratch\` for network-based runs.

## Monitoring

Flower dashboard:

```text
http://<RPI_IP>:5555
```

Per-job logs:

```text
orchestrator/logs/<job_id>/
```

Typical files include `meta.txt`, `stdout.attemptN.log`, and
`stderr.attemptN.log`.
