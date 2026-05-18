# ComputeFarm

ComputeFarm is a distributed batch-solving setup for GeoStudio `.gsz` files
and similar single-file workloads. This repository is already extracted into
two deployable parts:

- `orchestrator/` - Celery worker framework, solver scripts, queue helpers,
  and Windows batch files for submitting and managing jobs.
- `worker/` - Raspberry Pi / head-node bundle for Redis, Flower, Ray,
  monitoring, Samba file-drop workflows, and Windows Ray worker setup.

There is no zip step in this checkout. Use the folders directly.

## Repository Layout

```text
computerfarm/
|-- README.md
|-- SETUP.md
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
|   |   |-- config.yaml
|   |   |-- config.py
|   |   |-- celery_app.py
|   |   |-- tasks.py
|   |   |-- generate_manifest.py
|   |   |-- submit_manifest.py
|   |   |-- setup.bat
|   |   |-- setup_check.bat
|   |   |-- start_workers.bat
|   |   |-- start_worker_cpu_only.bat
|   |   |-- start_worker_gpu_only.bat
|   |   |-- requirements.txt
|   |   |-- _ensure_python312.bat
|   |   |-- _resolve_path.py
|   |   `-- _resubmit_helper.py
|   `-- tools/
|       |-- solve_gsz_geocmd.ps1
|       |-- solve_gsz.ps1
|       `-- geostudio_automation/scripts/solve_and_extract.py
`-- worker/
    |-- README.md
    |-- computefarm/
    |   |-- docker-compose.yml
    |   |-- reset_panel.py
    |   |-- computefarm-control.service
    |   `-- worker_aliases.json
    `-- cluster/
        |-- rpi_setup.sh
        |-- setup_worker.ps1
        |-- submit.ps1
        |-- status.ps1
        |-- worker.py
        `-- jobs/
            |-- example_handler.py
            `-- solver.py
```

Runtime folders such as `raw/`, `solved/`, `logs/`, `manifests/`, `pending/`,
`active/`, `results/`, and `failed/` are created by the setup scripts or by
the running services.

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

You can also run it non-interactively:

```powershell
.\configure.ps1 `
  -RpiIP 192.168.1.50 `
  -StoragePC STORAGE-PC `
  -StoragePCIP 192.168.1.20 `
  -ShareName ComputeFarm `
  -CpuConcurrency 2 `
  -GpuConcurrency 1 `
  -NonInteractive
```

## Architecture

ComputeFarm has two related layers that can be used independently:

| Layer | Folder | Purpose |
| --- | --- | --- |
| Celery queue farm | `orchestrator/` | Submit `.gsz` jobs, run Windows Celery workers, copy solved files back to shared storage, and manage retries/logs. |
| Pi/Ray head-node bundle | `worker/` | Set up the Raspberry Pi head node, Redis/Flower/control panel, Ray cluster services, Samba file-drop directories, and Windows Ray workers. |

Typical deployment:

```text
Raspberry Pi / Linux head node
  Redis, Flower, control panel, Ray, monitoring, Samba

Windows storage hub
  Shared orchestrator folder with raw inputs, solved outputs, logs, and manifests

Windows worker PCs
  Run Celery workers from orchestrator/framework or Ray workers from worker/cluster
```

## Celery Workflow

The Celery workflow is under `orchestrator/`.

1. Run `configure.ps1` from the repository root.
2. On the storage hub, run `orchestrator/framework/setup.bat`.
3. Put input `.gsz` files in the configured `raw/` directory.
4. Run `orchestrator/submit.bat` to enqueue jobs.
5. Start workers with `orchestrator/framework/start_workers.bat`.
6. Finished `.gsz` files are copied to `solved/`; logs are written under
   `logs/<job_id>/`.

`orchestrator/framework/setup.bat` shares the `orchestrator/` folder as
`\\<STORAGE_PC>\ComputeFarm` when you choose the share option. On mapped
worker PCs, that means the framework path is normally `Z:\framework`, not
`Z:\orchestrator\framework`.

Useful commands:

| Action | Command |
| --- | --- |
| Submit all raw jobs | `orchestrator/submit.bat` |
| Resubmit missing solved files only | `orchestrator/resubmit.bat` |
| Generate a manifest without submitting | `orchestrator/make_manifest.bat` |
| Purge queued jobs | `orchestrator/purge_queue.bat` |
| Restart running workers | `orchestrator/restart_all_workers.bat` |
| Stop workers | `orchestrator/stop_all_workers.bat` |
| Clean local scratch data | `orchestrator/clean_scratch.bat` |

The default fast GeoStudio path is:

```yaml
solve_script: "solve_gsz_geocmd.ps1"
```

That script lives at `orchestrator/tools/solve_gsz_geocmd.ps1` and runs
Seequent GeoStudio through `GeoCmd.exe`. The legacy `gsi` path is
`orchestrator/tools/solve_gsz.ps1`.

## Pi / Ray Workflow

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

The Ray file-drop path watches the Samba share and moves jobs through
`pending/`, `active/`, `results/`, and `failed/`.

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

Replace these placeholders before deployment:

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

## Adapting the Solver

For a different single-file workload, add a PowerShell script under
`orchestrator/tools/` and point `orchestrator/framework/config.yaml` at it:

```yaml
solve_script: "my_solver.ps1"
```

The Celery framework expects the script to accept a local input path and exit
with code `0` on success:

```powershell
param([Parameter(Mandatory=$true)][string]$GszPath)

& "C:\path\to\solver.exe" $GszPath
exit $LASTEXITCODE
```

The queueing, retries, logging, and worker management stay the same.

## Security Notes

This repository assumes the head node, storage hub, and workers are on a
trusted LAN, VPN, or mesh network. The bundled Flower/control-panel services
are intended for trusted networks unless you add authentication and firewall
rules.

Redis and SMB access should be restricted to your worker machines. Do not
expose Redis, Flower, the control panel, Grafana, Ray, MLflow, Prometheus, or
the Samba share directly to the public internet.
