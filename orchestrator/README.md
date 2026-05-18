# ComputeFarm Orchestrator

This folder contains the monitoring and control-panel files for the Celery
queue workflow. The Windows worker framework lives in the repository-level
`worker/` folder.

## Folder Layout

```text
orchestrator/
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

## Head-Node Setup

Install Redis on the head node first, then install the Flower/control-panel
files:

```bash
mkdir -p ~/computefarm
cp orchestrator/computefarm/* ~/computefarm/
cd ~/computefarm
docker compose up -d

sudo cp computefarm-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now computefarm-control
```

If the repository is not located at your current shell directory, adjust the
`orchestrator/...` paths above to the actual checkout path.

## Web Services

After setup, these services are expected on the head node:

| Service | URL |
| --- | --- |
| Flower | `http://<RPI_IP>:5555` |
| Control panel | `http://<RPI_IP>:5556` |

## Celery / Flower Notes

The Celery worker code is in the repository-level `worker/` folder. The Flower
container in `orchestrator/computefarm/docker-compose.yml` monitors Celery
workers that use the same Redis broker:

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
