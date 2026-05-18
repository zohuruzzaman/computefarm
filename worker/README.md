# ComputeFarm — Distributable Bundle

A hybrid compute farm with two coexisting layers:

- **Celery + Flower** (the `computefarm/` directory) — task queue with a Redis
  broker, Flower dashboard, and a small custom control panel for session
  management and worker aliasing.
- **Ray cluster** (the `cluster/` directory) — Raspberry Pi head node,
  Windows worker nodes, file-drop job submission via a Samba share, and a
  Prometheus + Grafana monitoring stack.

The two layers run side by side on the same Pi and can be used independently
or together — drop `.gsz` files in `pending/` for the Ray path, submit Celery
tasks via your own producer for the queue path.

## Placeholders you must replace before deploying

| Token              | Replace with                                                |
| ------------------ | ----------------------------------------------------------- |
| `<RPI_IP>`         | The Raspberry Pi head node's LAN IP (e.g. `192.168.x.y`)    |
| `<RPI_USER>`       | The Linux user account on the Pi that owns the install      |
| `<WORKER_IP>`      | (Examples only) any worker IP referenced in help comments   |
| `<WORKER_HOSTNAME>`| (Examples only) any worker hostname referenced in comments  |

Grep across the tree to find them:

```bash
grep -rn '<RPI_IP>\|<RPI_USER>\|<WORKER_IP>\|<WORKER_HOSTNAME>' .
```

## Layout

```
computefarm/                       # Celery / Flower / control panel (runs on RPi)
  docker-compose.yml               # Flower container (port 5555)
  reset_panel.py                   # Tiny HTTP control panel (port 5556)
  computefarm-control.service      # systemd unit for reset_panel.py
  worker_aliases.json              # Friendly names for Celery workers (empty template)

cluster/                           # Ray head + Windows workers
  rpi_setup.sh                     # One-shot Pi head installer (Ray, Prometheus,
                                   #   Grafana, MLflow, Samba, registry API)
  setup_worker.ps1                 # One-shot Windows worker installer (Python,
                                   #   Ray, exporters, scheduled task)
  worker.py                        # Pull-based worker loop deployed to Windows
  submit.ps1                       # Submit a JSON job to the Ray coordinator
  status.ps1                       # Show cluster queue + worker status
  jobs/
    example_handler.py             # Template for new job types
    solver.py                      # GeoStudio FEM solver handler (uses gsi)
```

## Quick start

### Raspberry Pi (head node)

```bash
# 1. Ray + monitoring stack
chmod +x cluster/rpi_setup.sh
./cluster/rpi_setup.sh

# 2. Celery / Flower / control panel
mkdir -p ~/computefarm
cp computefarm/* ~/computefarm/
cd ~/computefarm
docker compose up -d
sudo cp computefarm-control.service /etc/systemd/system/
# Edit the unit to set User= / Group= / WorkingDirectory= for your account
sudo systemctl daemon-reload
sudo systemctl enable --now computefarm-control
```

Web UIs that come up:

- Flower         — `http://<RPI_IP>:5555`
- Control panel  — `http://<RPI_IP>:5556`
- Grafana        — `http://<RPI_IP>:3000` (default `admin`/`admin`)
- Ray dashboard  — `http://<RPI_IP>:8265`
- MLflow         — `http://<RPI_IP>:5000`
- Prometheus     — `http://<RPI_IP>:9090`
- Registry API   — `http://<RPI_IP>:8090/workers`

### Windows workers

Run once as Administrator on each machine, pointing at the Pi:

```powershell
.\cluster\setup_worker.ps1 -HeadIP <RPI_IP> -NumCPUs 12 -NumGPUs 1
```

The script installs Python + Ray, registers a scheduled task that survives
reboots, installs `windows_exporter` and `nvidia_gpu_exporter`, and copies
your job handlers from `cluster\jobs\` to `C:\ray_worker\jobs\`.

### Celery workers (if you also want the queue path)

The Celery worker side is **not** included in this bundle — that lives in
your domain-specific producer/consumer repo. The Flower container in
`docker-compose.yml` will auto-discover any Celery worker that points at the
same Redis broker:

```
redis://computefarm@<RPI_IP>:6379/0   (broker, db 0)
redis://computefarm@<RPI_IP>:6379/1   (result backend, db 1)
```

If you want Flower's "Grow pool" / "Shrink pool" buttons to work, start each
worker with the **prefork** pool. On Windows that requires:

```powershell
$env:FORKED_BY_MULTIPROCESSING = "1"
celery -A <your_app> worker --pool=prefork --concurrency=2 -n <name>@%h -Q cpu
```

`solo`, `threads`, `gevent`, and `eventlet` pools don't implement `grow`.

## Submitting jobs

### Ray path (file drop)

Drop any `.gsz` or `.json` into the Samba share:

```
\\<RPI_IP>\storage\pending\
```

The RPi watcher infers job type from extension, moves the file to
`active\`, and queues it on the coordinator. Workers pull, solve, write
results to `results\`. Failed jobs land in `failed\`.

### Ray path (programmatic)

```powershell
.\cluster\submit.ps1 -Type solver -Config .\my_job.json -HeadIP <RPI_IP>
.\cluster\status.ps1 -Results -Failed -HeadIP <RPI_IP>
```

### Celery path

Use your own producer with the broker URL above. Flower at `:5555` shows
live task events; the control panel at `:5556` provides session naming,
worker aliasing, and broker/result-backend reset buttons.

## Adding a new job type

1. Copy `cluster/jobs/example_handler.py` to `cluster/jobs/<your_type>.py`
2. Implement `def run(job: dict) -> dict:`
3. Drop it into `C:\ray_worker\jobs\` on each worker — the worker reloads
   handlers from disk every 60 seconds, no restart needed.
4. Submit a job with `"type": "<your_type>"` in the JSON config.

## Notes

- The control panel has **no auth** and assumes you trust everyone on the
  LAN. Don't expose `:5555` or `:5556` to the public internet without
  adding `--basic-auth=user:pass` to Flower and a reverse proxy in front
  of the panel.
- `FLOWER_UNAUTHENTICATED_API=true` is set in `docker-compose.yml` so the
  Grow Pool / Shrink Pool buttons work. Same LAN-only caveat applies.
- The `solver.py` handler imports `solve_and_extract` and `enrich_gauss`
  from `GEO_SCRIPTS_DIR` (defaults to `~/.claude/skills/geostudio_automation/scripts/`).
  Deploy those scripts separately on each worker, or set `GEO_SCRIPTS_DIR`
  to wherever you keep them.
