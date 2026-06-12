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
    """Best-effort: terminate solver processes left over from a crashed or
    killed worker. They hold scratch file handles AND eat CPU/memory
    bandwidth, starving every later solve (the 2026-06-11 zombie pileup).
    Safe at worker boot: the single-instance lock in the wrapper guarantees
    no OTHER worker on this box, so any solver process here is an orphan.
    Only runs on Windows (the solvers are Windows-only)."""
    if sys.platform != "win32":
        return
    for name in ("gsi.exe", "GSStudio.exe", "GeoStudio.exe",
                 "GeoCmd.exe", "SolveServer.exe"):
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


def _sweep_stale_tmp_solved():
    """Remove any solved/<stem>_TMP.gsz left behind by a worker that crashed
    after copy-to-tmp but before the os.replace rename in tasks.py Step 3.
    No locking needed - Celery dedups by task_id, so only one worker holds
    a given stem at a time."""
    solved = CFG.solved_dir
    if not solved.exists():
        return
    removed = 0
    for p in solved.glob("*_TMP.gsz"):
        try:
            p.unlink()
            removed += 1
        except Exception:
            pass
    if removed:
        print(f"[boot] cleaned {removed} stale solved *_TMP.gsz file(s)", flush=True)


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    """Runs once per worker process right after it connects to the broker
    and is ready to consume tasks. Cleans up after any prior crashed run
    on this machine. GPU workers skip the solver-process kill: a cpu worker
    on the SAME box may be mid-solve when the gpu worker boots."""
    if os.environ.get("COMPUTEFARM_ROLE", "cpu") != "gpu":
        _kill_orphan_solver_processes()
    _sweep_orphan_scratch_dirs()
    _sweep_stale_tmp_solved()
