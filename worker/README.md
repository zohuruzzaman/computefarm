# ComputeFarm Monitoring Bundle

This folder contains the Flower dashboard and the small control panel used by
the Celery queue workflow.

Use the files in place. There is no archive to unzip in this repository.

## Folder Layout

```text
worker/
|-- README.md
`-- computefarm/
    |-- docker-compose.yml
    |-- reset_panel.py
    |-- computefarm-control.service
    `-- worker_aliases.json
```

## What Each Part Does

| Path | Purpose |
| --- | --- |
| `computefarm/docker-compose.yml` | Starts Flower on the head node. |
| `computefarm/reset_panel.py` | Small HTTP control panel for worker/session controls. |
| `computefarm/computefarm-control.service` | systemd service file for the control panel. |
| `computefarm/worker_aliases.json` | Friendly-name mapping for Celery workers. |

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

Install Redis on the head node first, then install the Flower/control-panel
files:

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

## Web Services

After setup, these services are expected on the head node:

| Service | URL |
| --- | --- |
| Flower | `http://<RPI_IP>:5555` |
| Control panel | `http://<RPI_IP>:5556` |

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
