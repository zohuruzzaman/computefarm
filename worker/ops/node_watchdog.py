#!/usr/bin/env python
"""
node_watchdog.py - per-box keeper for ComputeFarm. PLAIN SCRIPT: no AI, no
external calls beyond this farm's own share; every line auditable.

Windows Task Scheduler runs this every 2 minutes on each worker PC
(ops\\install_watchdog.bat registers the task). Each run, in order:

  1. HEARTBEAT  - write control/heartbeats/<HOST>.json on the share:
                  cpu%, ram%, disk%, gpu util/mem/temp (nvidia-smi, if any),
                  uptime, whether a worker is running. `farm stats` reads these.
  2. ZOMBIE SWEEP - if NO worker is running on this box, any GeoCmd.exe /
                  SolveServer.exe is an orphan from a killed solve -> kill.
                  (If a worker IS running, solver processes may be legit
                  solves - leave them alone; the worker's own boot cleanup
                  and the driver watchdog govern those.)
  3. ENSURE WORKER - if no cpu worker is running AND no stop sentinel exists
                  (WORKERS.stop global, control/<HOST>.stop per-box), launch
                  one via framework\\start_worker_cpu_only.bat (minimized).
                  This is what makes `farm start all` work remotely: deleting
                  the sentinels lets every box bring itself back up within
                  one tick.

Exit code is always 0 (Task Scheduler treats nonzero as task failure noise).
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent          # <share>\ops
ROOT = HERE.parent                              # <share>
FRAMEWORK = ROOT / "framework"
CONTROL = ROOT / "control"
HEARTBEATS = CONTROL / "heartbeats"
HOST = socket.gethostname().upper()

GLOBAL_STOP = ROOT / "WORKERS.stop"
HOST_STOP = CONTROL / f"{HOST}.stop"
GPU_ENABLED = (CONTROL / f"{HOST}.gpu").exists()   # opt-in marker per box


def _run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except Exception:
        return None


def _local_scratch() -> Path:
    """Same resolution as framework config.py (without importing celery)."""
    sys.path.insert(0, str(FRAMEWORK))
    try:
        from config import CFG
        return Path(CFG.local_scratch)
    except Exception:
        return Path(r"C:\ComputeFarm_scratch")


def _pid_alive(pid: int) -> bool:
    r = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=15)
    return bool(r) and str(pid) in (r.stdout or "")


def worker_running(role: str) -> int:
    """1 if a worker for this role is running on this box, else 0.
    Source of truth = celery's own --pidfile (written by the wrapper bats).
    No process-list sniffing: command-line matching is fragile and a query
    can match ITSELF (the 2026-06-12 phantom-worker bug)."""
    pidfile = _local_scratch() / f"celery_{role}_{HOST}.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except Exception:
        return 0
    if _pid_alive(pid):
        return 1
    try:
        pidfile.unlink()  # stale - celery would also clear it on next start
    except Exception:
        pass
    return 0


def gpu_stats():
    r = _run(["nvidia-smi",
              "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
              "--format=csv,noheader,nounits"], timeout=10)
    if not r or r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        util, mem, temp = [x.strip() for x in
                           r.stdout.strip().splitlines()[0].split(",")]
        return {"util_pct": int(float(util)), "mem_used_mb": int(float(mem)),
                "temp_c": int(float(temp))}
    except Exception:
        return None


def sys_stats():
    """CPU/RAM/disk/uptime without third-party deps (works on any box)."""
    # RAM via GlobalMemoryStatusEx
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    mem = MEMORYSTATUSEX(); mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    # uptime via GetTickCount64
    uptime_s = ctypes.windll.kernel32.GetTickCount64() // 1000
    # CPU: 2-sample of \Processor(_Total) via typeperf (fast, built-in)
    cpu_pct = None
    r = _run(["typeperf", r"\Processor(_Total)\% Processor Time",
              "-sc", "2", "-si", "1"], timeout=15)
    if r and r.returncode == 0:
        try:
            last = [ln for ln in r.stdout.splitlines() if '","' in ln][-1]
            cpu_pct = round(float(last.split('","')[-1].strip('"')))
        except Exception:
            pass
    # disk: free % of the scratch drive (C:)
    free = ctypes.c_ulonglong(0); total = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p("C:\\"), None, ctypes.byref(total), ctypes.byref(free))
    disk_pct = round(100 * (1 - free.value / total.value)) if total.value else None
    return {"cpu_pct": cpu_pct, "ram_pct": mem.dwMemoryLoad,
            "disk_pct": disk_pct, "uptime_s": int(uptime_s)}


def zombie_sweep(cpu_workers: int):
    if cpu_workers > 0:
        return 0  # solver processes may be live solves - hands off
    killed = 0
    for name in ("GeoCmd.exe", "SolveServer.exe"):
        r = _run(["taskkill", "/F", "/IM", name], timeout=15)
        if r and r.returncode == 0:
            killed += 1
    return killed


def ensure_worker(role: str, bat: str):
    if GLOBAL_STOP.exists() or HOST_STOP.exists():
        return "sentinel"
    if worker_running(role) > 0:
        return "running"
    # launch minimized in the interactive session; the wrapper's own
    # single-instance lock makes a race between two ticks harmless.
    subprocess.Popen(
        ["cmd", "/c", "start", "/min", f"ComputeFarm {role} worker",
         str(FRAMEWORK / bat)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return "launched"


def main():
    HEARTBEATS.mkdir(parents=True, exist_ok=True)
    cpu_n = worker_running("cpu")
    swept = zombie_sweep(cpu_n)
    action = ensure_worker("cpu", "start_worker_cpu_only.bat")
    gpu_action = None
    if GPU_ENABLED:
        gpu_action = ensure_worker("gpu", "start_worker_gpu_only.bat")

    hb = {"host": HOST, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          "worker_running": cpu_n > 0 or action == "launched",
          "cpu_workers": cpu_n, "worker_action": action,
          "gpu_worker_action": gpu_action, "zombies_swept": swept,
          "gpu": gpu_stats()}
    hb.update(sys_stats())
    tmp = HEARTBEATS / f"{HOST}.json.tmp"
    tmp.write_text(json.dumps(hb, indent=1), encoding="utf-8")
    os.replace(tmp, HEARTBEATS / f"{HOST}.json")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never propagate - scheduler noise helps no one
    sys.exit(0)
