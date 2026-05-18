# ComputeFarm

ComputeFarm is a generic distributed batch runner for Windows-heavy workloads:
simulation solves, command-line software runs, data processing jobs, model
training, or any task that can be expressed as "copy one input locally, run a
script, copy results back."

The repository is already extracted into deployable folders. There is no zip
or unzip step in this checkout.

## What It Provides

- A Celery + Redis queue path for shared-folder batch jobs.
- A Raspberry Pi / Linux head-node bundle for Redis, Flower, Ray, monitoring,
  Samba file-drop workflows, and control-panel services.
- Windows worker launchers with local scratch directories, retry handling,
  logs, and restart/stop controls.
- A configurable `orchestrator/tools/` folder where each software package gets
  its own run script.

The bundled example is GeoStudio, documented separately in [GEOSTUDIO.md](GEOSTUDIO.md).

## Repository Layout

```text
computerfarm/
|-- README.md
|-- SETUP.md
|-- GEOSTUDIO.md
|-- configure.ps1
|-- orchestrator/
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
`-- worker/
    |-- README.md
    |-- computefarm/
    `-- cluster/
```

Runtime folders such as `raw/`, `solved/`, `logs/`, `manifests/`, `pending/`,
`active/`, `results/`, and `failed/` are created by setup scripts or services.

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
| `orchestrator/framework/config.yaml` | Redis host/auth, concurrency, solver script |
| `orchestrator/connect_drive.ps1` | Storage hostname/IP and share name |
| `worker/cluster/setup_worker.ps1` | Default Ray head-node IP |
| `worker/cluster/worker.py` | Default Ray head-node IP and storage UNC |

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

ComputeFarm has two related layers that can be used independently:

| Layer | Folder | Purpose |
| --- | --- | --- |
| Queue farm | `orchestrator/` | Submit files, run Windows workers, copy outputs back to shared storage, and manage retries/logs. |
| Head-node/Ray bundle | `worker/` | Set up the Pi/Linux head node, Redis/Flower/control panel, Ray services, Samba directories, and Windows Ray workers. |

Typical deployment:

```text
Raspberry Pi / Linux head node
  Redis, Flower, control panel, Ray, monitoring, Samba

Windows storage hub
  Shared orchestrator folder with raw inputs, outputs, logs, and manifests

Windows worker PCs
  Run queue workers from orchestrator/framework or Ray workers from worker/cluster
```

## Queue Workflow

The generic queue workflow is:

1. Put input files in the configured `raw/` folder.
2. Generate a manifest with `orchestrator/make_manifest.bat`.
3. Submit the manifest with `orchestrator/submit.bat`.
4. Workers copy each input to local scratch.
5. Workers run the configured script from `orchestrator/tools/`.
6. Results are copied back to `solved/`; logs are written under `logs/<job_id>/`.

`orchestrator/framework/setup.bat` shares the `orchestrator/` folder as
`\\<STORAGE_PC>\ComputeFarm` when you choose the share option. On mapped
worker PCs, the framework path is normally `Z:\framework`, not
`Z:\orchestrator\framework`.

Useful commands:

| Action | Command |
| --- | --- |
| Generate a manifest | `orchestrator/make_manifest.bat` |
| Submit jobs | `orchestrator/submit.bat` |
| Resubmit missing outputs only | `orchestrator/resubmit.bat` |
| Purge queued jobs | `orchestrator/purge_queue.bat` |
| Restart running workers | `orchestrator/restart_all_workers.bat` |
| Stop workers | `orchestrator/stop_all_workers.bat` |
| Clean local scratch data | `orchestrator/clean_scratch.bat` |

The current manifest helpers are intentionally simple and scan top-level
`raw/*.gsz` files. For another file type, edit the extension in
`orchestrator/framework/generate_manifest.py` and
`orchestrator/framework/_resubmit_helper.py`, or prepare manifests manually.

## Plugging In Software

Software-specific logic belongs in:

```text
orchestrator/tools/
```

Add a run script for the software, for example:

```text
orchestrator/tools/run_plaxis.ps1
orchestrator/tools/run_training.ps1
orchestrator/tools/run_my_processor.ps1
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

## Head-Node / Ray Workflow

The Pi and Ray bundle is under `worker/`.

On the Raspberry Pi or Linux head node:

```bash
chmod +x worker/cluster/rpi_setup.sh
./worker/cluster/rpi_setup.sh

mkdir -p ~/computefarm
cp worker/computefarm/* ~/computefarm/
cd ~/computefarm
docker compose up -d
sudo cp computefarm-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now computefarm-control
```

On each Windows Ray worker, run PowerShell as Administrator:

```powershell
.\worker\cluster\setup_worker.ps1 -HeadIP <RPI_IP> -NumCPUs 12 -NumGPUs 1
```

Submit and inspect Ray jobs with:

```powershell
.\worker\cluster\submit.ps1 -Type solver -Config .\my_job.json -HeadIP <RPI_IP>
.\worker\cluster\status.ps1 -Results -Failed -HeadIP <RPI_IP>
```

## Web UIs

After the head-node services are running:

| Service | URL |
| --- | --- |
| Flower | `http://<RPI_IP>:5555` |
| Control panel | `http://<RPI_IP>:5556` |
| Grafana | `http://<RPI_IP>:3000` |
| Ray dashboard | `http://<RPI_IP>:8265` |
| MLflow | `http://<RPI_IP>:5000` |
| Prometheus | `http://<RPI_IP>:9090` |
| Registry API | `http://<RPI_IP>:8090/workers` |

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
Redis, Flower, the control panel, Grafana, Ray, MLflow, Prometheus, or the
Samba share directly to the public internet.
