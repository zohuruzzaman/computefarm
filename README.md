# ComputeFarm

ComputeFarm is a generic distributed batch runner for Windows-heavy workloads:
simulation solves, command-line software runs, data processing jobs, model
training, or any task that can be expressed as "copy one input locally, run a
script, copy results back."

The repository is already extracted into deployable folders. There is no zip
or unzip step in this checkout.

## What It Provides

- A Celery + Redis queue for shared-folder batch jobs.
- Flower and a small control panel for monitoring and session management.
- Windows worker launchers with local scratch directories, retry handling,
  logs, and restart/stop controls.
- A configurable `worker/tools/` folder where each software package gets its
  own run script.

The bundled example is GeoStudio, documented separately in [GEOSTUDIO.md](GEOSTUDIO.md).

## Repository Layout

```text
computerfarm/
|-- README.md
|-- SETUP.md
|-- GEOSTUDIO.md
|-- configure.ps1
|-- worker/
|   |-- connect_drive.ps1
|   |-- submit.bat
|   |-- resubmit.bat
|   |-- purge_queue.bat
|   |-- restart_all_workers.bat
|   |-- stop_all_workers.bat
|   |-- clean_scratch.bat
|   |-- make_manifest.bat
|   |-- framework/
|   `-- tools/
`-- orchestrator/
    |-- README.md
    `-- computefarm/
```

Runtime folders such as `raw/`, `solved/`, `logs/`, and `manifests/` are
created by setup scripts or services.

## Configure First

Run the top-level configuration script once after cloning or copying the repo:

```powershell
.\configure.ps1
```

It asks for the Raspberry Pi/head-node IP, Windows storage PC hostname/IP,
share name, Redis credentials, worker concurrency, and solver script. It then
updates:

| File | What gets patched |
| --- | --- |
| `worker/framework/config.yaml` | Redis host/auth, concurrency, solver script |
| `worker/connect_drive.ps1` | Storage hostname/IP and share name |

Non-interactive example:

```powershell
.\configure.ps1 `
  -RpiIP 192.168.1.50 `
  -StoragePC STORAGE-PC `
  -StoragePCIP 192.168.1.20 `
  -ShareName ComputeFarm `
  -SolveScript run_my_software.ps1 `
  -CpuConcurrency 2 `
  -GpuConcurrency 1 `
  -NonInteractive
```

## Architecture

```text
Redis broker + Flower/control panel
  Runs on the Raspberry Pi, Linux host, or any always-on machine

Windows storage hub
  Shared worker folder with raw inputs, outputs, logs, and manifests

Windows worker PCs
  Run Celery workers from worker/framework
```

The active execution path is:

```text
raw/
  -> make_manifest.bat
  -> submit.bat
  -> Redis/Celery
  -> Windows Celery workers
  -> worker/tools/<configured-script>.ps1
  -> solved/
```

## Queue Workflow

1. Put input files in the configured `raw/` folder.
2. Generate a manifest with `worker/make_manifest.bat`.
3. Submit the manifest with `worker/submit.bat`.
4. Workers copy each input to local scratch.
5. Workers run the configured script from `worker/tools/`.
6. Results are copied back to `solved/`; logs are written under `logs/<job_id>/`.

`worker/framework/setup.bat` shares the `worker/` folder as
`\\<STORAGE_PC>\ComputeFarm` when you choose the share option. On mapped
worker PCs, the framework path is normally `Z:\framework`, not
`Z:\worker\framework`.

Useful commands:

| Action | Command |
| --- | --- |
| Generate a manifest | `worker/make_manifest.bat` |
| Submit jobs | `worker/submit.bat` |
| Resubmit missing outputs only | `worker/resubmit.bat` |
| Purge queued jobs | `worker/purge_queue.bat` |
| Restart running workers | `worker/restart_all_workers.bat` |
| Stop workers | `worker/stop_all_workers.bat` |
| Clean local scratch data | `worker/clean_scratch.bat` |

The current manifest helpers are intentionally simple and scan top-level
`raw/*.gsz` files. For another file type, edit the extension in
`worker/framework/generate_manifest.py` and
`worker/framework/_resubmit_helper.py`, or prepare manifests manually.

## Plugging In Software

Software-specific logic belongs in:

```text
worker/tools/
```

Add a run script for the software, for example:

```text
worker/tools/run_plaxis.ps1
worker/tools/run_training.ps1
worker/tools/run_my_processor.ps1
```

Then point ComputeFarm at it:

```powershell
.\configure.ps1 -SolveScript run_plaxis.ps1
```

or edit:

```yaml
solve_script: "run_plaxis.ps1"
```

The framework currently calls the script with `-GszPath <local-input-path>`.
For non-GeoStudio workloads, treat that parameter as the generic input path:

```powershell
param([Parameter(Mandatory=$true)][string]$GszPath)

& "C:\Path\To\YourSoftware.exe" $GszPath
exit $LASTEXITCODE
```

The queueing, retries, worker startup, logging, and copy-back behavior remain
the same.

## Orchestrator Monitoring

The monitoring files live in:

```text
orchestrator/computefarm/
```

They provide:

| File | Purpose |
| --- | --- |
| `docker-compose.yml` | Starts Flower on port `5555`. |
| `reset_panel.py` | Small control panel on port `5556`. |
| `computefarm-control.service` | systemd service for the control panel. |
| `worker_aliases.json` | Friendly names for Celery workers. |

Web UIs:

| Service | URL |
| --- | --- |
| Flower | `http://<RPI_IP>:5555` |
| Control panel | `http://<RPI_IP>:5556` |

## Placeholders

Replace these placeholders before deployment, preferably with `configure.ps1`:

| Token | Meaning |
| --- | --- |
| `<RPI_IP>` | Raspberry Pi / head-node IP address |
| `<RPI_USER>` | Linux user account used on the Pi |
| `<STORAGE_PC>` | Windows storage PC hostname |
| `<STORAGE_PC_IP>` | Windows storage PC IP address |
| `<WORKER_IP>` | Example worker IP in comments or sample commands |
| `<WORKER_HOSTNAME>` | Example worker hostname in comments or sample commands |

On PowerShell:

```powershell
rg "<RPI_IP>|<RPI_USER>|<STORAGE_PC>|<STORAGE_PC_IP>|<WORKER_IP>|<WORKER_HOSTNAME>"
```

## Security Notes

This repository assumes the head node, storage hub, and workers are on a
trusted LAN, VPN, or mesh network. The bundled Flower/control-panel services
are intended for trusted networks unless you add authentication and firewall
rules.

Redis and SMB access should be restricted to worker machines. Do not expose
Redis, Flower, the control panel, or the SMB share directly to the public
internet.
