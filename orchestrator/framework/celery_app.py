"""
Celery app + worker lifecycle hooks for ComputeFarm.

worker_ready signal: when a fresh worker boots, we scan local scratch for
orphan PID directories (= scratch dirs from previous worker processes that
crashed or got killed before they could clean up) and remove them. Also
kills any orphan gsi.exe / GeoStudio processes that might still be
holding file handles. Keeps local disk tidy without operator action.
"""
import os
import shutil
import socket
import subprocess
import sys

from celery import Celery
from celery.signals import worker_ready
from config import CFG


app = Celery(
    "compute_farm",
    broker=CFG.redis_broker,
    backend=CFG.redis_backend,
    include=["tasks"],
)

app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    result_expires=CFG.result_expires,
)


def _pid_alive(pid: int) -> bool:
    """Cross-platform check whether a PID is still running on this host."""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in out.stdout
        except Exception:
            return True  # be conservative: assume alive on probe failure
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _kill_orphan_solver_processes():
    """Best-effort: terminate gsi server / GeoStudio processes left over
    from a crashed worker. They typically hold handles on local scratch
    files and block cleanup. Only runs on Windows (gsi is Windows-only)."""
    if sys.platform != "win32":
        return
    for name in ("gsi.exe", "GSStudio.exe", "GeoStudio.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass


def _sweep_orphan_scratch_dirs():
    """Find scratch dirs under <local_scratch>/<hostname>/<pid>/ where
    <pid> is no longer running on this host, and remove them.

    Backwards compatible: any pre-PID-namespaced dirs (legacy layout
    <local_scratch>/<hostname>/<job_id>/) are also removed at boot
    since they could not belong to a currently-running worker."""
    host_root = CFG.local_scratch / socket.gethostname()
    if not host_root.exists():
        return

    removed = 0
    for entry in host_root.iterdir():
        if not entry.is_dir():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            # Legacy <job_id> dir (no PID layer) - definitely orphan now
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            continue
        if pid == os.getpid():
            continue
        if not _pid_alive(pid):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1

    if removed:
        print(
            f"[boot] cleaned {removed} orphan scratch dir(s) under {host_root}",
            flush=True,
        )


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    """Runs once per worker process right after it connects to the broker
    and is ready to consume tasks. Cleans up after any prior crashed run
    on this machine."""
    _kill_orphan_solver_processes()
    _sweep_orphan_scratch_dirs()
