# ComputeFarm Setup

This checkout is already extracted. The Celery framework lives under
`orchestrator/`, and the Pi/Ray head-node bundle lives under `worker/`.

For the standard Celery GeoStudio workflow, start in `orchestrator/`.

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
    |-- solve_gsz_geocmd.ps1
    |-- solve_gsz.ps1
    `-- geostudio_automation/scripts/solve_and_extract.py
```

Runtime folders such as `raw/`, `solved/`, `logs/`, and `manifests/` are
created by setup or by the running workflow.

## First-Time Setup

On the Windows storage hub:

```powershell
cd E:\Github\computerfarm\orchestrator\framework
notepad config.yaml
.\setup.bat
.\setup_check.bat
```

In `config.yaml`, set the Redis host, shared paths, concurrency, and solver
script for your deployment. The default fast GeoStudio solver is:

```yaml
solve_script: "solve_gsz_geocmd.ps1"
```

That script is located at `orchestrator/tools/solve_gsz_geocmd.ps1`.

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
raw/*.gsz
  -> copied to worker local scratch
  -> solved by orchestrator/tools/<solve_script>
  -> copied back to solved/
  -> logs written under logs/<job_id>/
```

No parquet or CSV outputs are required for the queue workflow. The solved
`.gsz` file is the primary output.

## Submitting Jobs

From the storage hub:

```powershell
cd E:\Github\computerfarm\orchestrator
.\submit.bat
```

Useful commands:

| Action | Command |
| --- | --- |
| Submit all raw jobs | `submit.bat` |
| Resubmit missing solved files only | `resubmit.bat` |
| Generate manifest only | `make_manifest.bat` |
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

## Retry Policy

A `.gsz` that fails to solve is automatically requeued for another attempt, up
to 6 attempts total, with 5 minutes between attempts.

Worker crashes mid-solve are handled separately by Celery message redelivery
and do not count against the retry budget.

After the final failed attempt, a `<stem>_PARTIAL.gsz` file is written to
`solved/` as a forensic record, and logs remain under `logs/<job_id>/`.

The retry constants are in:

```text
orchestrator/framework/tasks.py
```

Look for:

```python
SOLVE_MAX_ATTEMPTS
SOLVE_RETRY_DELAY_SEC
```

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
solve_script:   Z:\orchestrator\tools\solve_gsz_geocmd.ps1
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
solve_script:   Z:\tools\solve_gsz_geocmd.ps1
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
