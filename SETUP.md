# ComputeFarm Queue Setup

This file covers the generic queue workflow under `worker/`. Software-specific
notes, including the bundled GeoStudio example, live in separate guides such
as [GEOSTUDIO.md](GEOSTUDIO.md).

## Worker Layout

```text
worker/
|-- ops/connect_drive.ps1
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
cd E:\Github\computerfarm\worker\framework
.\setup.bat
.\setup_check.bat
```

`configure.ps1` sets the Redis host, storage share details, worker
concurrency, and the selected script from `worker/tools/`.

## Worker PCs

On each Windows worker:

```powershell
\\STORAGE-PC\ComputeFarm\ops\connect_drive.ps1
Z:
cd Z:\framework
.\setup.bat
.\setup_check.bat
.\start_workers.bat
```

If you used `worker/framework/setup.bat` to create the share, the `worker/`
folder is shared as `\\STORAGE-PC\ComputeFarm`. In that common case, the
mapped path is `Z:\framework`.

If you instead shared the repository root, use `Z:\worker\framework`.

## Job Flow

```text
raw/<input files>
  -> copied to worker local scratch
  -> processed by worker/tools/<configured-script>
  -> copied back to solved/
  -> logs written under logs/<job_id>/
```

The configured script is selected in:

```text
worker/framework/config.yaml
```

or with:

```powershell
.\configure.ps1 -SolveScript <software-runner>.ps1
```

## Submitting Jobs

From the storage hub:

```powershell
cd E:\Github\computerfarm\worker
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
cd E:\Github\computerfarm\worker\framework
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
worker/framework/tasks.py
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

`worker/framework/config.py` resolves paths relative to the `worker/` folder:

```text
config.py:      Z:\worker\framework\config.py
root:           Z:\worker
raw_dir:        Z:\worker\raw
solved_dir:     Z:\worker\solved
logs_dir:       Z:\worker\logs
tools_dir:      Z:\worker\tools
```

If the storage hub shares `worker/` directly, the same layout appears on
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
worker/logs/<job_id>/
```

Typical files include `meta.txt`, `stdout.attemptN.log`, and
`stderr.attemptN.log`.
