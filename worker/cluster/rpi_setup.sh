#!/bin/bash
# rpi_setup.sh — Run once on the Raspberry Pi (head node)
# Sets up Ray head, coordinator actor, MLflow tracking server,
# Samba file server, and file watcher for auto job submission.
set -e

HEAD_IP=$(hostname -I | awk '{print $1}')
RAY_PORT=6379
RAY_DASHBOARD_PORT=8265
RAY_CLIENT_PORT=10001
MLFLOW_PORT=5000
INSTALL_DIR=/opt/ray-cluster
STORAGE_DIR=$INSTALL_DIR/storage
SAMBA_SHARE=storage           # share name workers will use: \\<rpi-ip>\storage

echo "=== Ray Head Node Setup ==="
echo "Detected IP: $HEAD_IP"
echo "Install dir: $INSTALL_DIR"
echo "Storage dir: $STORAGE_DIR"
echo ""

# --- System update ---
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git samba

# --- Python venv ---
sudo python3 -m venv $INSTALL_DIR
sudo chown -R $USER:$USER $INSTALL_DIR
source $INSTALL_DIR/bin/activate

pip install --upgrade pip
pip install "ray[default]" mlflow watchdog fastapi "uvicorn[standard]"

# --- Monitoring: detect CPU architecture ---
ARCH=$(uname -m)
case $ARCH in
    aarch64|arm64) BIN_ARCH="arm64" ;;
    armv7l)        BIN_ARCH="armv7"  ;;
    x86_64)        BIN_ARCH="amd64"  ;;
    *)             BIN_ARCH="amd64"  ;;
esac
echo "CPU architecture: $ARCH → $BIN_ARCH"

# --- node_exporter (RPi hardware metrics → Prometheus) ---
NODE_VER="1.8.2"
wget -q --show-progress \
    "https://github.com/prometheus/node_exporter/releases/download/v${NODE_VER}/node_exporter-${NODE_VER}.linux-${BIN_ARCH}.tar.gz" \
    -O /tmp/node_exporter.tar.gz
tar -xzf /tmp/node_exporter.tar.gz -C /tmp/
sudo cp /tmp/node_exporter-${NODE_VER}.linux-${BIN_ARCH}/node_exporter /usr/local/bin/
sudo chmod +x /usr/local/bin/node_exporter
rm -rf /tmp/node_exporter*
echo "node_exporter $NODE_VER installed."

# --- Prometheus ---
PROM_VER="2.52.0"
wget -q --show-progress \
    "https://github.com/prometheus/prometheus/releases/download/v${PROM_VER}/prometheus-${PROM_VER}.linux-${BIN_ARCH}.tar.gz" \
    -O /tmp/prometheus.tar.gz
tar -xzf /tmp/prometheus.tar.gz -C /tmp/
sudo cp /tmp/prometheus-${PROM_VER}.linux-${BIN_ARCH}/{prometheus,promtool} /usr/local/bin/
sudo mkdir -p /etc/prometheus /var/lib/prometheus
sudo cp -r /tmp/prometheus-${PROM_VER}.linux-${BIN_ARCH}/{consoles,console_libraries} /etc/prometheus/
rm -rf /tmp/prometheus*
echo "Prometheus $PROM_VER installed."

# --- Grafana OSS (via apt — supports ARM natively) ---
sudo apt install -y apt-transport-https software-properties-common
wget -q -O - https://apt.grafana.com/gpg.key | \
    sudo gpg --dearmor -o /usr/share/keyrings/grafana.gpg
echo "deb [signed-by=/usr/share/keyrings/grafana.gpg] https://apt.grafana.com stable main" | \
    sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt update -qq
sudo apt install -y grafana
echo "Grafana installed."

# --- Storage and log directories ---
mkdir -p $STORAGE_DIR/{pending,active,results,failed}
mkdir -p $INSTALL_DIR/logs
chmod -R 777 $STORAGE_DIR
chmod 755 $INSTALL_DIR/logs
echo "Storage and log directories created."

# --- Samba share ---
sudo tee /etc/samba/smb.conf > /dev/null << EOF
[global]
   workgroup = WORKGROUP
   server string = Ray Cluster Storage
   map to guest = Bad User
   log level = 1

[$SAMBA_SHARE]
   path = $STORAGE_DIR
   browseable = yes
   read only = no
   guest ok = yes
   create mask = 0777
   directory mask = 0777
   force user = $USER
EOF

sudo systemctl restart smbd nmbd
sudo systemctl enable smbd nmbd
echo "Samba share '\\\\$HEAD_IP\\$SAMBA_SHARE' configured."

# --- Write log_utils.py (shared by coordinator and watcher) ---
mkdir -p $INSTALL_DIR/scripts

cat > $INSTALL_DIR/scripts/log_utils.py << 'PYEOF'
"""
Shared logging setup for all RPi cluster services.

Usage:
    from log_utils import get_logger
    log = get_logger("coordinator")
    log.info("job queued", extra={"job_id": "abc123", "worker": "-"})

Every message is written to:
    logs/<service>.log   — per-service rotating file (10 MB × 5)
    logs/cluster.log     — combined rotating file   (50 MB × 5)
    stdout               — captured by systemd journal
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path("/opt/ray-cluster/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

_FMT    = "%(asctime)s | %(service)-12s | %(levelname)-8s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _ServiceFilter(logging.Filter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def filter(self, record):
        record.service = self.service
        return True


def get_logger(service: str) -> logging.Logger:
    logger = logging.getLogger(service)
    if logger.handlers:
        return logger   # already configured (guard against double-init)

    logger.setLevel(logging.DEBUG)
    logger.addFilter(_ServiceFilter(service))

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # Per-service rotating file
    fh = RotatingFileHandler(
        LOG_DIR / f"{service}.log",
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Combined cluster log
    ch = RotatingFileHandler(
        LOG_DIR / "cluster.log",
        maxBytes=50 * 1024 * 1024,   # 50 MB
        backupCount=5,
        encoding="utf-8",
    )
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # Stdout (captured by systemd)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    return logger
PYEOF

# --- Write registry.py (worker self-registration API) ---

cat > $INSTALL_DIR/scripts/registry.py << 'PYEOF'
"""
Worker registry — tiny FastAPI service on the RPi (port 8090).
Workers call POST /register on startup with their current hostname + IP.
Registry updates Prometheus targets automatically — no manual add_worker.sh needed.
"""
import json
import sys
from pathlib import Path
from fastapi import FastAPI
from pydantic import BaseModel

sys.path.insert(0, "/opt/ray-cluster/scripts")
from log_utils import get_logger

log         = get_logger("registry")
app         = FastAPI(title="Worker Registry")
TARGETS_DIR = Path("/opt/ray-cluster/prometheus/targets")

PORTS = {
    "windows_nodes.json": 9182,   # windows_exporter
    "nvidia_nodes.json":  9835,   # nvidia_gpu_exporter
}


class WorkerInfo(BaseModel):
    hostname: str
    ip:       str


def _load(fname: str) -> list:
    f = TARGETS_DIR / fname
    if f.exists():
        return json.loads(f.read_text())
    return []


def _save(fname: str, data: list):
    (TARGETS_DIR / fname).write_text(json.dumps(data, indent=2))


@app.post("/register")
def register(w: WorkerInfo):
    for fname, port in PORTS.items():
        data   = _load(fname)
        target = f"{w.ip}:{port}"

        # Remove any old entry for this hostname (IP may have changed)
        data = [
            e for e in data
            if e.get("labels", {}).get("hostname") != w.hostname
        ]

        # Add fresh entry with hostname label for Grafana filtering
        data.append({
            "targets": [target],
            "labels":  {"hostname": w.hostname, "ip": w.ip},
        })

        _save(fname, data)

    log.info(f"REGISTERED  hostname={w.hostname} ip={w.ip}")
    return {"status": "ok", "hostname": w.hostname, "ip": w.ip}


@app.delete("/register/{hostname}")
def deregister(hostname: str):
    for fname in PORTS:
        data = [
            e for e in _load(fname)
            if e.get("labels", {}).get("hostname") != hostname
        ]
        _save(fname, data)
    log.info(f"REMOVED     hostname={hostname}")
    return {"status": "ok", "hostname": hostname}


@app.get("/workers")
def list_workers():
    data = _load("windows_nodes.json")
    return [
        {"hostname": e["labels"]["hostname"], "ip": e["labels"]["ip"]}
        for e in data
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
PYEOF

# --- Write watcher.py ---

cat > $INSTALL_DIR/scripts/watcher.py << 'PYEOF'
"""
File watcher — runs on RPi, watches storage/pending/ for new files.
Moves each file to active/ and submits a job to the coordinator.

File → job type mapping:
  *.gsz         → type = "solver"
  *.json        → reads "type" field from inside the file, defaults to "train"
  anything else → type = "unknown" (worker will reject cleanly)
"""
import os
import sys
import ray
import json
import uuid
import time
import shutil
import traceback
from pathlib import Path

sys.path.insert(0, "/opt/ray-cluster/scripts")
from log_utils import get_logger

log = get_logger("watcher")

HEAD_IP     = os.environ.get("HEAD_IP", "127.0.0.1")
STORAGE_DIR = Path("/opt/ray-cluster/storage")
PENDING_DIR = STORAGE_DIR / "pending"
ACTIVE_DIR  = STORAGE_DIR / "active"


def infer_type(path: Path) -> tuple[str, dict]:
    if path.suffix.lower() == ".gsz":
        return "solver", {}
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text())
            job_type = data.pop("type", "train")
            return job_type, data
        except Exception as e:
            log.warning(f"Could not parse {path.name} as JSON: {e} — defaulting to train")
            return "train", {}
    return "unknown", {}


def main():
    ray.init(address="auto")
    coordinator = ray.get_actor("coordinator")
    seen = set()

    log.info(f"started — watching {PENDING_DIR}")

    while True:
        try:
            for f in sorted(PENDING_DIR.iterdir()):
                if f.name.startswith(".") or not f.is_file() or f.name in seen:
                    continue

                # Wait for file to finish being written
                size_a = f.stat().st_size
                time.sleep(0.5)
                if not f.exists() or f.stat().st_size != size_a:
                    continue

                seen.add(f.name)
                job_id           = str(uuid.uuid4())[:8]
                job_type, extra  = infer_type(f)

                active_path = ACTIVE_DIR / f"{job_id}__{f.name}"
                shutil.move(str(f), str(active_path))

                unc_input  = f"\\\\{HEAD_IP}\\storage\\active\\{active_path.name}"
                unc_output = f"\\\\{HEAD_IP}\\storage\\results\\"

                job = {
                    "id":         job_id,
                    "type":       job_type,
                    "input_path": unc_input,
                    "output_dir": unc_output,
                    "filename":   f.name,
                    **extra,
                }

                ray.get(coordinator.submit.remote(job))
                log.info(f"submitted job_id={job_id} type={job_type} file={f.name}")

        except Exception:
            log.error(f"watcher loop error:\n{traceback.format_exc()}")

        time.sleep(2)


if __name__ == "__main__":
    main()
PYEOF

cat > $INSTALL_DIR/scripts/coordinator.py << 'PYEOF'
"""
JobCoordinator — detached Ray actor that lives for the lifetime of the cluster.
Workers call request_work() to pull a job and report_done()/report_failed() when finished.
All events are logged to logs/coordinator.log and logs/cluster.log.
"""
import ray
import sys
import time
import uuid

sys.path.insert(0, "/opt/ray-cluster/scripts")
from log_utils import get_logger

# Module-level logger used by the bootstrap __main__ block
_log = get_logger("coordinator")


@ray.remote
class JobCoordinator:
    def __init__(self):
        # Each actor instance gets its own logger
        sys.path.insert(0, "/opt/ray-cluster/scripts")
        from log_utils import get_logger as _get
        self.log     = _get("coordinator")
        self.pending = []
        self.active  = {}   # worker_id -> job
        self.done    = []
        self.failed  = []
        self.log.info("coordinator actor initialised")

    def submit(self, job: dict) -> str:
        if "id" not in job:
            job["id"] = str(uuid.uuid4())[:8]
        self.pending.append(job)
        self.log.info(f"QUEUED    job_id={job['id']} type={job.get('type','?')} pending={len(self.pending)}")
        return job["id"]

    def submit_batch(self, jobs: list) -> list:
        return [self.submit(j) for j in jobs]

    def request_work(self, worker_id: str):
        if self.pending:
            job = self.pending.pop(0)
            self.active[worker_id] = job
            self.log.info(f"ASSIGNED  job_id={job['id']} worker={worker_id} remaining={len(self.pending)}")
            return job
        return None

    def report_done(self, worker_id: str, result: dict):
        job = self.active.pop(worker_id, None)
        if job:
            elapsed = result.get("_elapsed_s", "?")
            self.done.append({"job": job, "result": result, "worker": worker_id})
            self.log.info(f"DONE      job_id={job['id']} worker={worker_id} elapsed={elapsed}s total_done={len(self.done)}")

    def report_failed(self, worker_id: str, error: str):
        job = self.active.pop(worker_id, None)
        if job:
            job["_retries"] = job.get("_retries", 0) + 1
            if job["_retries"] < 3:
                self.pending.append(job)
                self.log.warning(f"RETRY     job_id={job['id']} worker={worker_id} attempt={job['_retries']}")
            else:
                self.failed.append({"job": job, "error": error, "worker": worker_id})
                self.log.error(f"FAILED    job_id={job['id']} worker={worker_id} gave_up_after=3\n{error}")

    def status(self) -> dict:
        return {
            "pending":   len(self.pending),
            "active":    len(self.active),
            "completed": len(self.done),
            "failed":    len(self.failed),
            "workers":   list(self.active.keys()),
        }

    def get_results(self) -> list:
        return self.done

    def get_failed(self) -> list:
        return self.failed


if __name__ == "__main__":
    ray.init(address="auto")

    coordinator = JobCoordinator.options(
        name="coordinator",
        lifetime="detached",
        num_cpus=0.1,
    ).remote()

    _log.info("coordinator actor started and detached")

    while True:
        time.sleep(60)
PYEOF

chmod +x $INSTALL_DIR/scripts/coordinator.py

# --- systemd: Ray head ---
sudo tee /etc/systemd/system/ray-head.service > /dev/null << EOF
[Unit]
Description=Ray Head Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/bin/ray start \
    --head \
    --node-ip-address=$HEAD_IP \
    --port=$RAY_PORT \
    --ray-client-server-port=$RAY_CLIENT_PORT \
    --dashboard-host=0.0.0.0 \
    --dashboard-port=$RAY_DASHBOARD_PORT \
    --metrics-export-port=8080 \
    --block
StandardOutput=append:$INSTALL_DIR/logs/ray-head.log
StandardError=append:$INSTALL_DIR/logs/ray-head.log
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# --- systemd: Coordinator actor ---
sudo tee /etc/systemd/system/ray-coordinator.service > /dev/null << EOF
[Unit]
Description=Ray Job Coordinator Actor
After=ray-head.service
Requires=ray-head.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStartPre=/bin/sleep 15
ExecStart=$INSTALL_DIR/bin/python scripts/coordinator.py
StandardOutput=append:$INSTALL_DIR/logs/coordinator.log
StandardError=append:$INSTALL_DIR/logs/coordinator.log
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

# --- systemd: File watcher ---
sudo tee /etc/systemd/system/ray-watcher.service > /dev/null << EOF
[Unit]
Description=Ray Job File Watcher
After=ray-coordinator.service
Requires=ray-coordinator.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
Environment=HEAD_IP=$HEAD_IP
ExecStartPre=/bin/sleep 5
ExecStart=$INSTALL_DIR/bin/python scripts/watcher.py
StandardOutput=append:$INSTALL_DIR/logs/watcher.log
StandardError=append:$INSTALL_DIR/logs/watcher.log
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# --- systemd: Worker registry API ---
sudo tee /etc/systemd/system/ray-registry.service > /dev/null << EOF
[Unit]
Description=Ray Worker Registry API
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/bin/uvicorn registry:app --host 0.0.0.0 --port 8090 --app-dir $INSTALL_DIR/scripts
StandardOutput=append:$INSTALL_DIR/logs/registry.log
StandardError=append:$INSTALL_DIR/logs/registry.log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- systemd: MLflow ---
sudo tee /etc/systemd/system/mlflow.service > /dev/null << EOF
[Unit]
Description=MLflow Tracking Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/bin/mlflow server \
    --host 0.0.0.0 \
    --port $MLFLOW_PORT \
    --backend-store-uri sqlite:///$INSTALL_DIR/mlflow.db \
    --default-artifact-root $INSTALL_DIR/mlflow-artifacts
StandardOutput=append:$INSTALL_DIR/logs/mlflow.log
StandardError=append:$INSTALL_DIR/logs/mlflow.log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- systemd: node_exporter ---
sudo tee /etc/systemd/system/node_exporter.service > /dev/null << EOF
[Unit]
Description=Prometheus Node Exporter (RPi hardware metrics)
After=network.target

[Service]
Type=simple
User=$USER
ExecStart=/usr/local/bin/node_exporter \
    --collector.systemd \
    --collector.processes \
    --collector.thermal_zone
StandardOutput=append:$INSTALL_DIR/logs/node_exporter.log
StandardError=append:$INSTALL_DIR/logs/node_exporter.log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- systemd: Prometheus ---
sudo tee /etc/systemd/system/prometheus.service > /dev/null << EOF
[Unit]
Description=Prometheus Metrics Server
After=network.target

[Service]
Type=simple
User=$USER
ExecStart=/usr/local/bin/prometheus \
    --config.file=/etc/prometheus/prometheus.yml \
    --storage.tsdb.path=/var/lib/prometheus \
    --storage.tsdb.retention.time=30d \
    --web.listen-address=0.0.0.0:9090 \
    --web.enable-lifecycle
StandardOutput=append:$INSTALL_DIR/logs/prometheus.log
StandardError=append:$INSTALL_DIR/logs/prometheus.log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- logrotate ---
sudo tee /etc/logrotate.d/ray-cluster > /dev/null << EOF
$INSTALL_DIR/logs/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    size 20M
}
EOF

# --- tail_logs.sh convenience script ---
cat > $INSTALL_DIR/tail_logs.sh << 'EOF'
#!/bin/bash
# Usage:
#   ./tail_logs.sh           — tail combined cluster log
#   ./tail_logs.sh watcher   — tail a specific service log
#   ./tail_logs.sh errors    — show only WARNING/ERROR lines from all logs

LOG_DIR=/opt/ray-cluster/logs

case "${1:-cluster}" in
    errors)
        tail -f $LOG_DIR/cluster.log | grep --line-buffered -E "WARNING|ERROR"
        ;;
    list)
        ls -lh $LOG_DIR/
        ;;
    *)
        SERVICE="${1:-cluster}"
        LOGFILE="$LOG_DIR/${SERVICE}.log"
        if [ ! -f "$LOGFILE" ]; then
            echo "No log file: $LOGFILE"
            echo "Available: $(ls $LOG_DIR/*.log 2>/dev/null | xargs -n1 basename)"
            exit 1
        fi
        tail -f "$LOGFILE"
        ;;
esac
EOF
chmod +x $INSTALL_DIR/tail_logs.sh

# --- Prometheus config ---
mkdir -p $INSTALL_DIR/prometheus/targets
sudo chown -R $USER:$USER /etc/prometheus /var/lib/prometheus

sudo tee /etc/prometheus/prometheus.yml > /dev/null << EOF
global:
  scrape_interval:     15s
  evaluation_interval: 15s

scrape_configs:

  - job_name: 'rpi'
    static_configs:
      - targets: ['localhost:9100']
        labels: {instance: 'rpi-head'}

  - job_name: 'ray'
    static_configs:
      - targets: ['localhost:8080']
        labels: {instance: 'ray-head'}

  - job_name: 'windows_cpu_mem_disk'
    file_sd_configs:
      - files: ['$INSTALL_DIR/prometheus/targets/windows_nodes.json']
        refresh_interval: 30s

  - job_name: 'nvidia_gpu'
    file_sd_configs:
      - files: ['$INSTALL_DIR/prometheus/targets/nvidia_nodes.json']
        refresh_interval: 30s
EOF

# Empty target files — populated by add_worker.sh
echo '[{"targets": [], "labels": {"group": "workers"}}]' \
    > $INSTALL_DIR/prometheus/targets/windows_nodes.json
echo '[{"targets": [], "labels": {"group": "gpu"}}]' \
    > $INSTALL_DIR/prometheus/targets/nvidia_nodes.json

# --- Grafana provisioning: Prometheus datasource ---
sudo mkdir -p /etc/grafana/provisioning/datasources
sudo tee /etc/grafana/provisioning/datasources/prometheus.yml > /dev/null << 'EOF'
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: true
    editable: true
EOF

# --- add_worker.sh — manual fallback registration via registry API ---
cat > $INSTALL_DIR/add_worker.sh << 'BASHEOF'
#!/bin/bash
# Manual worker registration — only needed if a machine can't reach the registry.
# Workers normally self-register on startup.
#
# Usage: ./add_worker.sh <ip> <hostname>
#   e.g. ./add_worker.sh <WORKER_IP> <WORKER_HOSTNAME>

IP=$1
HOSTNAME=${2:-$1}

if [ -z "$IP" ]; then
    echo "Usage: $0 <ip> <hostname>"
    exit 1
fi

curl -s -X POST "http://localhost:8090/register" \
    -H "Content-Type: application/json" \
    -d "{\"hostname\": \"$HOSTNAME\", \"ip\": \"$IP\"}" \
    | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'Registered: {r}')"
BASHEOF
chmod +x $INSTALL_DIR/add_worker.sh

# --- remove_worker.sh ---
cat > $INSTALL_DIR/remove_worker.sh << 'BASHEOF'
#!/bin/bash
# Usage: ./remove_worker.sh <hostname>
#   e.g. ./remove_worker.sh <WORKER_HOSTNAME>

HOSTNAME=$1

if [ -z "$HOSTNAME" ]; then
    echo "Usage: $0 <hostname>"
    exit 1
fi

curl -s -X DELETE "http://localhost:8090/register/$HOSTNAME" \
    | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'Removed: {r}')"
BASHEOF
chmod +x $INSTALL_DIR/remove_worker.sh

# --- Enable and start ---
sudo systemctl daemon-reload
sudo systemctl enable ray-head ray-coordinator ray-watcher ray-registry mlflow \
                       node_exporter prometheus grafana-server

echo "Starting monitoring stack..."
sudo systemctl start node_exporter prometheus grafana-server ray-registry

echo "Starting Ray head..."
sudo systemctl start ray-head

echo "Waiting for Ray to initialize (20s)..."
sleep 20

sudo systemctl start ray-coordinator
sleep 10
sudo systemctl start ray-watcher mlflow

# --- Import Grafana dashboards (requires internet access on RPi) ---
echo "Waiting for Grafana to be ready..."
sleep 15

GRAFANA="http://localhost:3000"
AUTH="admin:admin"

_import_dash() {
    local id=$1 name=$2
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "$GRAFANA/api/dashboards/import" \
        -H "Content-Type: application/json" \
        -u "$AUTH" \
        -d "{\"dashboardId\":${id},\"overwrite\":true,\
\"inputs\":[{\"name\":\"DS_PROMETHEUS\",\"type\":\"datasource\",\
\"pluginId\":\"prometheus\",\"value\":\"Prometheus\"}]}")
    if [ "$code" = "200" ]; then
        echo "  ✓ $name (ID $id)"
    else
        echo "  ✗ $name (ID $id) — HTTP $code (import manually from grafana.com)"
    fi
}

echo "Importing Grafana dashboards..."
_import_dash 1860  "Node Exporter Full (RPi)"
_import_dash 14694 "Windows Exporter Node (CPU/Mem/Disk/Temp)"
_import_dash 14574 "NVIDIA GPU Overview (GPU%/VRAM/Temp)"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "  Web UIs"
echo "  ──────────────────────────────────────────────"
echo "  Grafana        : http://$HEAD_IP:3000   (admin / admin)"
echo "  Ray Dashboard  : http://$HEAD_IP:$RAY_DASHBOARD_PORT"
echo "  MLflow         : http://$HEAD_IP:$MLFLOW_PORT"
echo "  Prometheus     : http://$HEAD_IP:9090   (raw metrics)"
echo ""
echo "  Worker Registry: http://$HEAD_IP:8090/workers  (live worker list)"
echo ""
echo "  Grafana first login → change password → dashboards are pre-loaded"
echo ""
echo "  Storage share  : \\\\$HEAD_IP\\$SAMBA_SHARE"
echo ""
echo "  Storage layout:"
echo "    \\\\$HEAD_IP\\storage\\pending\\   ← drop input files here"
echo "    \\\\$HEAD_IP\\storage\\active\\    ← in-progress (auto-managed)"
echo "    \\\\$HEAD_IP\\storage\\results\\   ← workers write output here"
echo "    \\\\$HEAD_IP\\storage\\failed\\    ← jobs that failed 3x"
echo ""
echo "  Update HEAD_IP in setup_worker.ps1:"
echo "  HEAD_IP = $HEAD_IP"
echo ""
echo "  Log files:"
echo "    $INSTALL_DIR/logs/cluster.log      ← all services combined"
echo "    $INSTALL_DIR/logs/coordinator.log"
echo "    $INSTALL_DIR/logs/watcher.log"
echo "    $INSTALL_DIR/logs/ray-head.log"
echo "    $INSTALL_DIR/logs/mlflow.log"
echo ""
echo "  Tail logs:"
echo "    $INSTALL_DIR/tail_logs.sh              — combined"
echo "    $INSTALL_DIR/tail_logs.sh watcher       — watcher only"
echo "    $INSTALL_DIR/tail_logs.sh coordinator   — coordinator only"
echo "    $INSTALL_DIR/tail_logs.sh errors        — warnings and errors only"
echo ""
echo "  Workers self-register on startup — no manual step needed."
echo "  Check registered workers: http://$HEAD_IP:8090/workers"
echo ""
echo "  Check services:"
echo "  sudo systemctl status ray-head ray-coordinator ray-watcher mlflow"
echo "  sudo systemctl status node_exporter prometheus grafana-server smbd"
