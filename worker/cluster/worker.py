"""
Ray pull-based worker — deployed to every Windows compute node.

Drop job handler scripts into the jobs/ directory alongside this file.
Each handler must expose:  def run(job: dict) -> dict

The filename becomes the job type:
    jobs/solver.py   →  {"type": "solver",   ...}
    jobs/train.py    →  {"type": "train",    ...}
    jobs/ablation.py →  {"type": "ablation", ...}

On every startup the worker:
  1. Registers its current IP with the RPi registry (Prometheus / Grafana)
  2. Mounts the RPi storage share
  3. Connects to the Ray cluster
  4. Loops: check idle → pull job → run handler → report result → repeat
"""

import ray
import time
import socket
import psutil
import subprocess
import importlib.util
import traceback
import urllib.request
import urllib.error
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────

HEAD_IP            = "<RPI_IP>"           # patched by setup_worker.ps1
STORAGE_UNC        = r"\\<RPI_IP>\storage" # patched by setup_worker.ps1

WORKER_ID          = socket.gethostname()
JOBS_DIR           = Path(__file__).parent / "jobs"
LOG_DIR            = Path(__file__).parent / "logs"

CPU_BUSY_THRESHOLD = 40    # % — back off if machine CPU is above this
RAM_FREE_MIN_GB    = 4     # GB — back off if free RAM below this
POLL_INTERVAL_S    = 3     # seconds to wait when queue is empty
IDLE_CHECK_S       = 10    # seconds to wait when machine is busy

# ── Logging ─────────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        RotatingFileHandler(
            LOG_DIR / "worker.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("worker")

# ── Self-registration ──────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """Return the IP this machine uses to reach the head node (no data sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((HEAD_IP, 80))
        return s.getsockname()[0]


def register_with_rpi():
    """POST current hostname + IP to RPi registry so Prometheus can find us."""
    ip      = get_local_ip()
    payload = json.dumps({"hostname": WORKER_ID, "ip": ip}).encode()
    url     = f"http://{HEAD_IP}:8090/register"

    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url,
                data    = payload,
                headers = {"Content-Type": "application/json"},
                method  = "POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            log.info(f"registered with RPi registry — ip={ip}")
            return
        except urllib.error.URLError:
            time.sleep(3 * (attempt + 1))   # back off: 3, 6, 9, 12, 15s

    log.warning(f"could not reach registry at {url} — Grafana metrics may be missing")


# ── Storage mount ──────────────────────────────────────────────────────────────

def ensure_storage_mounted():
    """Connect to the RPi Samba share if not already connected."""
    check = subprocess.run(
        ["net", "use", STORAGE_UNC],
        capture_output=True, text=True
    )
    if check.returncode == 0:
        return  # already connected

    mount = subprocess.run(
        ["net", "use", STORAGE_UNC, "/persistent:no"],
        capture_output=True, text=True
    )
    if mount.returncode == 0:
        log.info(f"mounted storage share {STORAGE_UNC}")
    else:
        log.warning(f"could not mount {STORAGE_UNC}: {mount.stderr.strip()}")


# ── Handler loader ─────────────────────────────────────────────────────────────

def load_handlers() -> dict:
    handlers = {}
    for path in sorted(JOBS_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        try:
            spec   = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "run"):
                handlers[path.stem] = module.run
                log.info(f"loaded handler: {path.stem}")
            else:
                log.warning(f"skipped {path.name} — no run() function")
        except Exception as e:
            log.error(f"error loading {path.name}: {e}")
    return handlers


# ── Idle check ─────────────────────────────────────────────────────────────────

def is_machine_idle() -> bool:
    cpu_pct  = psutil.cpu_percent(interval=1)
    ram_free = psutil.virtual_memory().available / (1024 ** 3)
    return cpu_pct < CPU_BUSY_THRESHOLD and ram_free > RAM_FREE_MIN_GB


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    register_with_rpi()
    ensure_storage_mounted()

    log.info(f"connecting to ray://{HEAD_IP}:10001 ...")
    ray.init(address=f"ray://{HEAD_IP}:10001")
    log.info("connected to Ray cluster")

    handlers    = load_handlers()
    last_reload = time.time()
    reload_every = 60   # reload handlers from disk every 60s

    coordinator = ray.get_actor("coordinator")
    log.info("ready — waiting for work")

    while True:

        if time.time() - last_reload > reload_every:
            handlers    = load_handlers()
            last_reload = time.time()

        if not is_machine_idle():
            log.debug("machine busy — backing off")
            time.sleep(IDLE_CHECK_S)
            continue

        job = ray.get(coordinator.request_work.remote(WORKER_ID))

        if job is None:
            time.sleep(POLL_INTERVAL_S)
            continue

        job_type = job.get("type")
        job_id   = job.get("id", "?")
        handler  = handlers.get(job_type)

        if handler is None:
            msg = f"no handler for type '{job_type}' — available: {list(handlers.keys())}"
            log.error(msg)
            coordinator.report_failed.remote(WORKER_ID, msg)
            continue

        log.info(f"starting  job_id={job_id} type={job_type} file={job.get('filename','')}")
        t0 = time.time()

        try:
            result               = handler(job)
            elapsed              = round(time.time() - t0, 2)
            result["_elapsed_s"] = elapsed
            result["_worker"]    = WORKER_ID
            coordinator.report_done.remote(WORKER_ID, result)
            log.info(f"finished  job_id={job_id} elapsed={elapsed}s")
        except Exception:
            tb = traceback.format_exc()
            log.error(f"failed    job_id={job_id}:\n{tb}")
            coordinator.report_failed.remote(WORKER_ID, tb)


if __name__ == "__main__":
    main()
