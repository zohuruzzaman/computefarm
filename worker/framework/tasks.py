"""
ComputeFarm task runner.

solve_geostudio: copy from raw/ -> solve locally -> copy to solved/
run_command:     generic command execution

Architecture - rsync pattern (NOT shared-file work):
  Every solve_geostudio attempt only touches the share for THREE things:
    1. shutil.copy2 of the raw .gsz into local PID-scoped scratch
    2. shutil.copy2 of the solved .gsz back to solved/
    3. shutil.copy2 of stdout/stderr logs back to logs/<job_id>/
    plus tiny appends to logs/<job_id>/meta.txt (cross-worker audit trail)
  The actual gsi solver, all subprocess stdout/stderr writes, and any
  intermediate gsi state happen on LOCAL disk only. SMB load is bounded
  to a few short copies per solve.

  Local scratch is PID-namespaced (CFG.local_scratch / hostname / pid /
  job_id) so duplicate workers on the same hostname can never collide.

  Orphan PID dirs from crashed workers are auto-cleaned at worker boot
  via the worker_ready signal in celery_app.py.

Retry policy:
  - Up to SOLVE_MAX_ATTEMPTS total attempts (1 original + N-1 retries).
  - SOLVE_RETRY_DELAY_SEC between attempts. Flat, not exponential.
  - Retries get requeued through Celery so any worker subscribed to the
    queue can pick them up - not pinned to the worker that hit the failure.
  - Worker crashes mid-solve are handled separately via acks_late +
    task_reject_on_worker_lost in celery_app.py; those redeliveries do NOT
    burn a retry slot.
  - A *_PARTIAL.gsz is saved into solved/ only on the FINAL failure, not on
    every interim retry.

Env-requeue (worker-environment failures):
  - If a worker can't even read the source .gsz, that's a worker setup
    problem and must NOT consume the per-job retry budget. We re-publish
    the job to the same queue with a small delay and Ignore the current
    attempt.
"""

import os
import shutil
import socket
import subprocess
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from celery.exceptions import Ignore
from celery_app import app
from config import CFG


SOLVE_MAX_ATTEMPTS = 6           # 1 original + 5 retries (real solve failures)
SOLVE_RETRY_DELAY_SEC = 300      # 5 minutes between attempts

ENV_REQUEUE_DELAY_SEC = 60       # how long to wait before another worker can pick it up
ENV_REQUEUE_CAP = 50             # safety: stop the loop after this many env-requeues


def ts():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def safe_id(raw: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in raw)


def meta(path: Path, lines: list[str]):
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _worker_scratch_root() -> Path:
    """Per-worker-process scratch directory. Includes PID so duplicate
    workers on the same host can never collide on the same path."""
    return CFG.local_scratch / socket.gethostname() / str(os.getpid())


@app.task(bind=True, max_retries=SOLVE_MAX_ATTEMPTS - 1)
def solve_geostudio(self, job: dict):
    job_id = safe_id(job["id"])
    hostname = socket.gethostname()
    pid = os.getpid()
    attempt = self.request.retries + 1
    max_attempts = SOLVE_MAX_ATTEMPTS

    gsz_path = Path(job["gsz_path"])
    mesh_edge = job.get("mesh_edge", CFG.default_mesh_edge)
    timeout_minutes = int(job.get("timeout_minutes", CFG.default_timeout))
    scripts_dir = job.get("scripts_dir", CFG.scripts_dir)
    # solve_script precedence: per-job override > SOLVE_SCRIPT_OVERRIDE env var
    # (set on a single worker for A/B pilots without flipping the shared config)
    # > CFG.solve_script from config.yaml.
    solve_script = (
        job.get("solve_script")
        or os.environ.get("SOLVE_SCRIPT_OVERRIDE")
        or CFG.solve_script
    )

    # --------------------------------------------------------------
    # Pre-flight: can THIS worker reach the source .gsz?
    # If not, do NOT touch the per-job retry budget. Re-publish the
    # job (with a small delay so other workers get a shot) and Ignore
    # the current attempt. A broken worker cannot poison a job's life.
    # --------------------------------------------------------------
    if not gsz_path.is_file():
        env_retries = int(job.get("_env_retries", 0)) + 1
        if env_retries > ENV_REQUEUE_CAP:
            raise RuntimeError(
                f"gsz path {gsz_path} not readable on {hostname} after "
                f"{ENV_REQUEUE_CAP} env-requeues - giving up. "
                f"Check that every worker has the path mounted/shared."
            )
        new_job = dict(job)
        new_job["_env_retries"] = env_retries
        # Readable task_id so Flower shows the filename even after env-requeue
        new_task_id = f"{job_id}-{uuid.uuid4().hex[:8]}"
        solve_geostudio.apply_async(
            args=[new_job],
            queue=self.request.delivery_info.get("routing_key", "cpu"),
            countdown=ENV_REQUEUE_DELAY_SEC,
            task_id=new_task_id,
        )
        print(
            f"[env-requeue {env_retries}/{ENV_REQUEUE_CAP}] "
            f"{job_id}: {gsz_path} not readable on {hostname}; "
            f"re-queued with {ENV_REQUEUE_DELAY_SEC}s delay",
            flush=True,
        )
        raise Ignore()

    # ---- shared paths (over SMB if root is a UNC share) ----
    job_log_dir = CFG.logs_dir / job_id
    job_log_dir.mkdir(parents=True, exist_ok=True)
    CFG.solved_dir.mkdir(parents=True, exist_ok=True)

    meta_file = job_log_dir / "meta.txt"
    remote_stdout = job_log_dir / f"stdout.attempt{attempt}.log"
    remote_stderr = job_log_dir / f"stderr.attempt{attempt}.log"
    solved_gsz = CFG.solved_dir / gsz_path.name

    # ---- local paths (always on this worker's own disk) ----
    local_dir = _worker_scratch_root() / job_id
    local_dir.mkdir(parents=True, exist_ok=True)
    local_gsz = local_dir / gsz_path.name
    local_stdout = local_dir / "stdout.log"
    local_stderr = local_dir / "stderr.log"

    meta(meta_file, [
        "-" * 60,
        f"Attempt {attempt}/{max_attempts}",
        f"Job ID: {job_id}",
        f"Host: {hostname}  (pid={pid})",
        f"Started: {ts()}",
        f"Source (raw): {gsz_path}",
        f"Local copy: {local_gsz}",
        f"Destination (solved): {solved_gsz}",
        f"Mesh edge: {mesh_edge}",
    ])

    try:
        # Step 1: rsync raw -> local
        meta(meta_file, [f"Copy to local: {ts()}"])
        shutil.copy2(str(gsz_path), str(local_gsz))
        size_mb = local_gsz.stat().st_size / (1024 * 1024)
        meta(meta_file, [f"Local copy ready: {size_mb:.1f} MB"])

        # Step 2: Solve locally - subprocess output buffered to LOCAL files,
        # not streamed over SMB. We copy them to the share after the solve
        # finishes so SMB only takes the hit once per attempt.
        cmd = [
            "powershell", "-ExecutionPolicy", "Bypass", "-File",
            solve_script,
            "-GszPath", str(local_gsz),
        ]
        if mesh_edge > 0:
            cmd += ["-MeshEdge", str(mesh_edge)]
        if scripts_dir:
            cmd += ["-ScriptsDir", scripts_dir]

        meta(meta_file, [f"Solve started: {ts()}", f"Command: {' '.join(cmd)}"])

        try:
            with open(local_stdout, "w", encoding="utf-8") as out, \
                 open(local_stderr, "w", encoding="utf-8") as err:
                result = subprocess.run(
                    cmd,
                    cwd=str(local_dir),
                    stdout=out,
                    stderr=err,
                    text=True,
                    timeout=timeout_minutes * 60,
                )
        finally:
            # Copy logs back even if the solve failed/timed out, so the
            # shared logs/<job_id>/ folder always reflects what happened.
            for src, dst in ((local_stdout, remote_stdout),
                             (local_stderr, remote_stderr)):
                try:
                    if src.exists():
                        shutil.copy2(str(src), str(dst))
                except Exception:
                    pass  # best-effort - failure to copy logs must not mask the solve outcome

        if result.returncode != 0:
            raise RuntimeError(f"solve_gsz.ps1 exit code {result.returncode}")

        # Step 3: rsync solved -> share
        meta(meta_file, [f"Copy to solved: {ts()}"])
        shutil.copy2(str(local_gsz), str(solved_gsz))
        solved_size_mb = local_gsz.stat().st_size / (1024 * 1024)
        meta(meta_file, [
            f"Copy done: {ts()}",
            f"Solved file: {solved_gsz} ({solved_size_mb:.1f} MB)",
        ])

        # Clean up stale _PARTIAL from a previous failed attempt
        partial = CFG.solved_dir / f"{gsz_path.stem}_PARTIAL.gsz"
        if partial.exists():
            try:
                partial.unlink()
                meta(meta_file, [f"Removed stale partial: {partial}"])
            except Exception:
                pass

        # Step 4: Clean up local scratch for this job
        shutil.rmtree(str(local_dir), ignore_errors=True)
        meta(meta_file, [
            f"Finished: {ts()}",
            f"Status: DONE (attempt {attempt}/{max_attempts})",
        ])

        return {
            "job_id": job_id,
            "status": "done",
            "host": hostname,
            "attempt": attempt,
            "source": str(gsz_path),
            "solved": str(solved_gsz),
            "size_mb": round(solved_size_mb, 1),
        }

    except Exception as exc:
        meta(meta_file, [
            f"Attempt {attempt}/{max_attempts} failed: {ts()}",
            traceback.format_exc(),
        ])

        is_final = attempt >= max_attempts

        if is_final and local_gsz.exists():
            try:
                partial = CFG.solved_dir / f"{gsz_path.stem}_PARTIAL.gsz"
                shutil.copy2(str(local_gsz), str(partial))
                meta(meta_file, [f"Partial saved (final failure): {partial}"])
            except Exception:
                pass

        shutil.rmtree(str(local_dir), ignore_errors=True)

        if is_final:
            meta(meta_file, [
                f"Status: FAILED after {max_attempts} attempts",
                "Manual intervention required - inspect stdout.attempt*.log",
            ])
            raise
        else:
            meta(meta_file, [
                f"Retrying in {SOLVE_RETRY_DELAY_SEC}s "
                f"(next attempt {attempt + 1}/{max_attempts})",
            ])
            raise self.retry(exc=exc, countdown=SOLVE_RETRY_DELAY_SEC)


@app.task(bind=True, max_retries=2)
def run_command(self, job: dict):
    job_id = safe_id(job["id"])
    hostname = socket.gethostname()

    command = job["command"]
    args = job.get("args", [])
    workdir = job.get("workdir", str(CFG.root))
    env_extra = job.get("env", {})
    timeout_minutes = int(job.get("timeout_minutes", 60))

    job_log_dir = CFG.logs_dir / job_id
    job_log_dir.mkdir(parents=True, exist_ok=True)

    stdout_file = job_log_dir / "stdout.log"
    stderr_file = job_log_dir / "stderr.log"
    meta_file = job_log_dir / "meta.txt"

    full_cmd = [command] + [str(x) for x in args]

    env = os.environ.copy()
    env.update(env_extra)
    env["COMPUTE_FARM_JOB_ID"] = job_id

    meta(meta_file, [
        f"Job ID: {job_id}",
        f"Host: {hostname}",
        f"Type: command",
        f"Started: {ts()}",
        f"Command: {' '.join(full_cmd)}",
        f"Workdir: {workdir}",
    ])

    try:
        with open(stdout_file, "w", encoding="utf-8") as out, \
             open(stderr_file, "w", encoding="utf-8") as err:
            completed = subprocess.run(
                full_cmd,
                cwd=workdir,
                env=env,
                stdout=out,
                stderr=err,
                text=True,
                timeout=timeout_minutes * 60,
            )

        if completed.returncode != 0:
            raise RuntimeError(f"Exit code {completed.returncode}")

        meta(meta_file, [f"Finished: {ts()}", "Status: DONE"])
        return {"job_id": job_id, "status": "done", "host": hostname}

    except Exception as exc:
        meta(meta_file, [f"Error: {ts()}", traceback.format_exc()])
        try:
            raise self.retry(exc=exc, countdown=60)
        except self.MaxRetriesExceededError:
            meta(meta_file, ["Status: FAILED"])
            raise
