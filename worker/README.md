# ComputeFarm Worker / Head-Node Bundle

This folder contains the already-extracted Raspberry Pi / Linux head-node and
Ray worker bundle. It also includes the Flower/control-panel files used by the
Celery side of ComputeFarm.

Use the files in place. There is no archive to unzip in this repository.

## Folder Layout

```text
worker/
|-- README.md
|-- computefarm/
|   |-- docker-compose.yml
|   |-- reset_panel.py
|   |-- computefarm-control.service
|   `-- worker_aliases.json
`-- cluster/
    |-- rpi_setup.sh
    |-- setup_worker.ps1
    |-- worker.py
    |-- submit.ps1
    |-- status.ps1
    `-- jobs/
        |-- example_handler.py
        `-- solver.py
```

## What Each Part Does

| Path | Purpose |
| --- | --- |
| `computefarm/docker-compose.yml` | Starts Flower on the head node. |
| `computefarm/reset_panel.py` | Small HTTP control panel for worker/session controls. |
| `computefarm/computefarm-control.service` | systemd service file for the control panel. |
| `computefarm/worker_aliases.json` | Friendly-name mapping for Celery workers. |
| `cluster/rpi_setup.sh` | One-shot Raspberry Pi / Linux head-node installer. |
| `cluster/setup_worker.ps1` | One-shot Windows Ray worker installer. |
| `cluster/worker.py` | Pull-based Ray worker loop deployed to Windows workers. |
| `cluster/submit.ps1` | Submit a JSON job to the Ray coordinator. |
| `cluster/status.ps1` | Inspect queue, worker, result, and failed-job state. |
| `cluster/jobs/example_handler.py` | Template for adding new job types. |
| `cluster/jobs/solver.py` | GeoStudio solver handler. |

## Required Placeholder Values

Replace these placeholders before deploying:

| Token | Replace with |
| --- | --- |
| `<RPI_IP>` | Raspberry Pi / head-node IP address |
| `<RPI_USER>` | Linux user account that owns the install |
| `<WORKER_IP>` | Example worker IP used in comments or samples |
| `<WORKER_HOSTNAME>` | Example worker hostname used in comments or samples |

From the repository root, find remaining placeholders with:

```powershell
rg "<RPI_IP>|<RPI_USER>|<WORKER_IP>|<WORKER_HOSTNAME>" worker
```

## Head-Node Setup

Run on the Raspberry Pi or Linux head node:

```bash
chmod +x worker/cluster/rpi_setup.sh
./worker/cluster/rpi_setup.sh
```

Then install the Flower/control-panel files:

```bash
mkdir -p ~/computefarm
cp worker/computefarm/* ~/computefarm/
cd ~/computefarm
docker compose up -d

sudo cp computefarm-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now computefarm-control
```

If the repository is not located at your current shell directory, adjust the
`worker/...` paths above to the actual checkout path.

## Windows Ray Worker Setup

Run PowerShell as Administrator on each Windows worker:

```powershell
.\worker\cluster\setup_worker.ps1 -HeadIP <RPI_IP> -NumCPUs 12 -NumGPUs 1
```

The setup script installs Python/Ray dependencies, registers a scheduled task,
installs exporters, and copies job handlers from `cluster/jobs/` to the worker
runtime location.

## Web Services

After setup, these services are expected on the head node:

| Service | URL |
| --- | --- |
| Flower | `http://<RPI_IP>:5555` |
| Control panel | `http://<RPI_IP>:5556` |
| Grafana | `http://<RPI_IP>:3000` |
| Ray dashboard | `http://<RPI_IP>:8265` |
| MLflow | `http://<RPI_IP>:5000` |
| Prometheus | `http://<RPI_IP>:9090` |
| Registry API | `http://<RPI_IP>:8090/workers` |

## Submitting Ray Jobs

Submit a JSON job:

```powershell
.\worker\cluster\submit.ps1 -Type solver -Config .\my_job.json -HeadIP <RPI_IP>
```

Check status and outputs:

```powershell
.\worker\cluster\status.ps1 -Results -Failed -HeadIP <RPI_IP>
```

The file-drop workflow uses the Samba storage directories created by
`cluster/rpi_setup.sh`. Jobs move through `pending/`, `active/`, `results/`,
and `failed/`.

## Adding a Ray Job Type

1. Copy `worker/cluster/jobs/example_handler.py` to
   `worker/cluster/jobs/<job_type>.py`.
2. Implement:

   ```python
   def run(job: dict) -> dict:
       ...
   ```

3. Deploy the handler to each worker runtime directory.
4. Submit a JSON job with `"type": "<job_type>"`.

## Celery / Flower Notes

The Celery worker code is in the repository-level `orchestrator/` folder, not
inside this `worker/` folder. The Flower container in
`worker/computefarm/docker-compose.yml` monitors Celery workers that use the
same Redis broker:

```text
redis://computefarm@<RPI_IP>:6379/0
```

For Flower pool controls, start Celery workers with the prefork pool. On
Windows, that usually means:

```powershell
$env:FORKED_BY_MULTIPROCESSING = "1"
celery -A <your_app> worker --pool=prefork --concurrency=2 -n <name>@%h -Q cpu
```

## Security Notes

The control panel and Flower settings are intended for a trusted LAN, VPN, or
mesh network. Do not expose these services publicly without adding
authentication and firewall rules.
