#!/usr/bin/env python
"""
farm.py - ComputeFarm unified operator CLI. One entry point for everything.

    farm status                  workers + queue depths + live per-job progress + stall flags
    farm stats                   per-PC CPU/RAM/disk/GPU/uptime (from watchdog heartbeats)
    farm queue                   list jobs WAITING in Redis (position order)
    farm queue drop <job_id>     delete one waiting job
    farm queue next <job_id>     move one waiting job to the front of the line
    farm assign <job_id> <host>  move a waiting job to a specific PC's personal queue
    farm purge [--force]         empty all queues; --force also revokes/kills in-flight
    farm manifest <folder>       create a YAML manifest from a folder of .gsz
    farm submit <manifest> [--fresh] [--force] [--to HOST]
    farm resume <manifest>       submit only jobs whose solved output is missing
    farm retry-failed <manifest> resubmit only jobs that FAILED/stalled
    farm restart [host|all]      rolling worker restart (wrappers relaunch)
    farm stop <host|all>         sentinel-stop worker(s); they stay down
    farm start <host|all>        remove sentinel(s); watchdog relaunches within ~2 min
    farm nuke [--yes]            stop everything + purge + flush Redis (clean slate)
    farm ingest [--fresh]        move incoming/ -> raw/, manifest, submit

Run from anywhere:  H:\\ComputeFarm\\worker_pc\\farm.bat <command>
Requires Python 3.12 with celery/redis/yaml (same env as the framework).
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import socket
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parent / "framework"
sys.path.insert(0, str(FRAMEWORK))

from config import CFG  # noqa: E402
import redis as redis_lib  # noqa: E402
import yaml  # noqa: E402

CONTROL = CFG.root / "control"
HEARTBEATS = CONTROL / "heartbeats"
BATCH_FILE = CONTROL / "active_batch.txt"
GLOBAL_STOP = CFG.root / "WORKERS.stop"

QUEUES = ["cpu", "gpu", "gpu_ml", "default"]
STALL_WARN_S = 600          # flag a job in `status` after 10 min of no growth
STALL_KILL_S = 1800         # the driver watchdog kills at 30 min (info only)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _app():
    from celery_app import app
    return app


def _broker():
    return redis_lib.Redis.from_url(CFG.redis_broker)


def _backend():
    return redis_lib.Redis.from_url(CFG.redis_backend)


def _all_queue_keys(r) -> list[str]:
    """The 4 shared queues + any per-host personal queues that exist.
    The farm's Redis ACL user may not have KEYS/SCAN - fall back to
    candidate names derived from known hosts, EXISTS-checked."""
    keys = list(QUEUES)
    try:
        for pat in ("cpu.*", "gpu.*"):
            keys += [k.decode() for k in r.scan_iter(match=pat, count=200)]
    except redis_lib.exceptions.NoPermissionError:
        for h in _known_hosts():
            for cand in (f"cpu.{h}", f"gpu.{h}"):
                try:
                    if r.exists(cand):
                        keys.append(cand)
                except Exception:
                    pass
    # de-dup, keep order
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _decode_msg(raw: bytes):
    """Decode one kombu message from a Redis queue list.
    Returns (job_dict, task_name, task_id) or (None, None, None)."""
    try:
        msg = json.loads(raw)
        body = json.loads(base64.b64decode(msg["body"]))
        job = body[0][0] if body and body[0] else {}
        headers = msg.get("headers", {})
        return job, headers.get("task", "?"), headers.get("id", "?")
    except Exception:
        return None, None, None


def _find_in_queues(r, job_id: str):
    """Locate a waiting job by its manifest id. Returns (queue_key, raw_msg,
    job, task_name) or Nones. Kombu publishes with LPUSH and workers consume
    with BRPOP, so index -1 (right end) is NEXT to be consumed."""
    for qk in _all_queue_keys(r):
        for raw in r.lrange(qk, 0, -1):
            job, task_name, _tid = _decode_msg(raw)
            if job and str(job.get("id")) == job_id:
                return qk, raw, job, task_name
    return None, None, None, None


def _active_batch() -> str | None:
    try:
        return BATCH_FILE.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def _set_batch(batch_id: str):
    CONTROL.mkdir(parents=True, exist_ok=True)
    BATCH_FILE.write_text(batch_id + "\n", encoding="utf-8")


def _beacon_last(job_id: str) -> dict | None:
    """Newest line of logs/<job>/progress.ndjson, or None."""
    p = CFG.logs_dir / job_id / "progress.ndjson"
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - 4096))
            lines = [ln for ln in f.read().decode("utf-8", "replace").splitlines()
                     if ln.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def _hms(seconds) -> str:
    try:
        s = int(seconds)
    except Exception:
        return "?"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else (
        f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s")


# ---------------------------------------------------------------------------
# status / stats
# ---------------------------------------------------------------------------
def cmd_status(args):
    r = _broker()
    print(f"active batch : {_active_batch() or '(none set)'}")
    if GLOBAL_STOP.exists():
        print("NOTE: global WORKERS.stop sentinel EXISTS - fleet is stopped.")
    stops = sorted(p.stem for p in CONTROL.glob("*.stop"))
    if stops:
        print(f"host stop sentinels: {', '.join(stops)}")

    print("\n--- queues (waiting in Redis) ---")
    total = 0
    for qk in _all_queue_keys(r):
        n = r.llen(qk)
        total += n
        if n or qk in ("cpu", "gpu"):
            print(f"  {qk:24s} {n}")
    print(f"  {'TOTAL waiting':24s} {total}")

    print("\n--- workers ---")
    try:
        i = _app().control.inspect(timeout=6)
        stats = i.stats() or {}
        active = i.active() or {}
        reserved = i.reserved() or {}
    except Exception as e:
        print(f"  (inspect failed: {e})")
        return
    if not stats:
        print("  none online")
    for w in sorted(stats):
        conc = stats[w].get("pool", {}).get("max-concurrency", "?")
        upt = _hms(stats[w].get("uptime", 0))
        n_act = len(active.get(w, []))
        n_res = len(reserved.get(w, []))
        print(f"  {w:42s} conc={conc} active={n_act} reserved={n_res} up={upt}")

    print("\n--- in-flight jobs (live beacon) ---")
    any_job = False
    for w, tasks_ in sorted((active or {}).items()):
        for t in tasks_:
            any_job = True
            job = (t.get("args") or [{}])[0]
            jid = str(job.get("id", "?"))
            b = _beacon_last(jid)
            host = w.split("@")[-1]
            if b is None:
                print(f"  {jid:44s} {host:16s} (no beacon yet)")
                continue
            if "final" in b:
                print(f"  {jid:44s} {host:16s} finishing ({b['final']})")
                continue
            idle = b.get("idle_s", 0)
            flag = ""
            if idle >= STALL_KILL_S:
                flag = "  [STALLED - watchdog firing]"
            elif idle >= STALL_WARN_S:
                flag = f"  [WARN idle {_hms(idle)}]"
            sizes = b.get("sizes", {})
            gsz = next((v for k, v in sizes.items() if k.endswith(".gsz")), None)
            gsz_s = f" gsz={gsz/1e6:.1f}MB" if gsz else ""
            print(f"  {jid:44s} {host:16s} run {_hms(b.get('elapsed_s'))}"
                  f"{gsz_s} idle {_hms(idle)}{flag}")
            last = b.get("last_line", "")
            if last:
                print(f"    {'':44s} > {last[:110]}")
    if not any_job:
        print("  none")


def cmd_stats(args):
    print(f"{'host':18s} {'age':>6s} {'cpu%':>5s} {'ram%':>5s} {'disk%':>6s} "
          f"{'gpu%':>5s} {'gpuMB':>7s} {'gpuC':>5s} {'up':>8s}  worker")
    found = False
    for hb in sorted(HEARTBEATS.glob("*.json")) if HEARTBEATS.exists() else []:
        found = True
        try:
            d = json.loads(hb.read_text(encoding="utf-8"))
        except Exception:
            print(f"{hb.stem:18s} (unreadable)")
            continue
        age = time.time() - hb.stat().st_mtime
        stale = " (STALE)" if age > 360 else ""
        g = d.get("gpu") or {}
        print(f"{d.get('host', hb.stem):18s} {_hms(age):>6s} "
              f"{d.get('cpu_pct', '?'):>5} {d.get('ram_pct', '?'):>5} "
              f"{d.get('disk_pct', '?'):>6} {g.get('util_pct', '-'):>5} "
              f"{g.get('mem_used_mb', '-'):>7} {g.get('temp_c', '-'):>5} "
              f"{_hms(d.get('uptime_s', 0)):>8s}  "
              f"{'YES' if d.get('worker_running') else 'no'}{stale}")
    if not found:
        print("\n  no heartbeats yet - install the node watchdog on each box")
        print("  (ops\\install_watchdog.bat, run once per PC)")


# ---------------------------------------------------------------------------
# queue management
# ---------------------------------------------------------------------------
def cmd_queue(args):
    r = _broker()
    if args.action == "list" or args.action is None:
        shown = 0
        for qk in _all_queue_keys(r):
            items = r.lrange(qk, 0, -1)
            if not items:
                continue
            print(f"\n--- {qk} ({len(items)} waiting; #1 runs next) ---")
            # BRPOP consumes from the RIGHT end -> reverse for run order
            for pos, raw in enumerate(reversed(items), 1):
                job, task_name, _tid = _decode_msg(raw)
                if job is None:
                    print(f"  {pos:3d}. (undecodable message)")
                    continue
                batch = job.get("batch_id", "-")
                stale = ""
                ab = _active_batch()
                if job.get("batch_id") and ab and job["batch_id"] != ab:
                    stale = "  [STALE - will be dropped]"
                print(f"  {pos:3d}. {job.get('id', '?'):44s} "
                      f"{task_name.split('.')[-1]:16s} batch={batch}{stale}")
                shown += 1
        if not shown:
            print("queue is empty")
        return

    if not args.job_id:
        print("need a job id"); sys.exit(2)

    qk, raw, job, task_name = _find_in_queues(r, args.job_id)
    if raw is None:
        print(f"job '{args.job_id}' not found in any waiting queue "
              f"(already running or done?)"); sys.exit(1)

    if args.action == "drop":
        r.lrem(qk, 1, raw)
        print(f"dropped {args.job_id} from '{qk}'")
    elif args.action == "next":
        # LREM then RPUSH: BRPOP consumes from the right -> this job runs next
        r.lrem(qk, 1, raw)
        r.rpush(qk, raw)
        print(f"{args.job_id} moved to the FRONT of '{qk}' (next to be taken)")


def cmd_assign(args):
    r = _broker()
    qk, raw, job, task_name = _find_in_queues(r, args.job_id)
    if raw is None:
        print(f"job '{args.job_id}' not found in any waiting queue"); sys.exit(1)
    role = "gpu" if qk.startswith("gpu") else "cpu"
    target_q = f"{role}.{args.host.upper()}"
    r.lrem(qk, 1, raw)
    r.lpush(target_q, raw)
    print(f"{args.job_id}: '{qk}' -> '{target_q}' "
          f"({args.host.upper()} takes it as its next task)")
    print("note: the host must be online and subscribed to its personal queue "
          "(workers started with the updated wrapper are).")


def cmd_purge(args):
    r = _broker()
    total = 0
    for qk in _all_queue_keys(r):
        n = r.llen(qk)
        if n:
            r.delete(qk)
            print(f"  purged {qk}: {n} waiting message(s)")
            total += n
    if total == 0:
        print("  queues already empty")
    if args.force:
        try:
            app = _app()
            i = app.control.inspect(timeout=6)
            ids = [t["id"] for v in list((i.active() or {}).values())
                   + list((i.reserved() or {}).values()) for t in v]
            for tid in ids:
                app.control.revoke(tid, terminate=True)
            print(f"  revoked+terminated {len(ids)} in-flight task(s)")
            time.sleep(3)
            # acks_late requeues terminated tasks - sweep again
            for qk in _all_queue_keys(r):
                if r.llen(qk):
                    r.delete(qk)
            print("  re-purged post-revoke requeues")
        except Exception as e:
            print(f"  revoke step failed: {e}")


# ---------------------------------------------------------------------------
# manifest / submit / resume / retry
# ---------------------------------------------------------------------------
def cmd_manifest(args):
    sys.path.insert(0, str(FRAMEWORK))
    from generate_manifest import generate
    out = args.out or str(CFG.manifests_dir /
                          f"{Path(args.folder).resolve().name}.yaml")
    generate(args.folder, out, args.queue, args.timeout, args.mesh_edge)


def _load_manifest(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _solved_path_for(job: dict) -> Path:
    rel = job.get("rel_path")
    if rel:
        return CFG.solved_dir / rel
    return CFG.solved_dir / Path(job["gsz_path"]).name


def _submit_jobs(manifest: dict, jobs: list[dict], batch_id: str,
                 queue_override: str | None, force: bool, dry: bool):
    from tasks import run_command, solve_geostudio
    queue = queue_override or manifest.get("queue", "cpu")
    job_type = manifest.get("type", "command")
    defaults = manifest.get("defaults", {})
    task = solve_geostudio if job_type == "geostudio" else run_command
    n = 0
    for job in jobs:
        merged = {**defaults, **job, "batch_id": batch_id}
        if force:
            merged["force"] = True
        if dry:
            print(f"  [DRY] {merged['id']} -> {queue}")
            continue
        safe = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in str(merged["id"]))
        task.apply_async(args=[merged], queue=queue,
                         task_id=f"{safe}-{uuid.uuid4().hex[:8]}")
        n += 1
    print(f"\n{n} job(s) submitted to '{queue}' with batch '{batch_id}'"
          if not dry else f"\n[DRY RUN] {len(jobs)} job(s) would be submitted")


def cmd_submit(args):
    m = _load_manifest(args.manifest)
    jobs = m["jobs"]
    if args.fresh:
        batch_id = f"{Path(args.manifest).stem}-{datetime.now():%Y%m%d-%H%M%S}"
        print(f"--fresh: purging queues + activating batch '{batch_id}'")
        cmd_purge(argparse.Namespace(force=True))
        _set_batch(batch_id)
    else:
        batch_id = _active_batch()
        if not batch_id:
            batch_id = f"{Path(args.manifest).stem}-{datetime.now():%Y%m%d-%H%M%S}"
            _set_batch(batch_id)
            print(f"(no active batch - created '{batch_id}')")
        else:
            print(f"joining active batch '{batch_id}'")
    queue_override = f"cpu.{args.to.upper()}" if args.to else None
    _submit_jobs(m, jobs, batch_id, queue_override, args.force, args.dry_run)


def cmd_resume(args):
    m = _load_manifest(args.manifest)
    todo = [j for j in m["jobs"] if not _solved_path_for(j).is_file()]
    done = len(m["jobs"]) - len(todo)
    print(f"{done} already solved, {len(todo)} to submit")
    if not todo:
        return
    batch_id = _active_batch() or \
        f"{Path(args.manifest).stem}-resume-{datetime.now():%Y%m%d-%H%M%S}"
    _set_batch(batch_id)
    _submit_jobs(m, todo, batch_id, None, False, args.dry_run)


def cmd_retry_failed(args):
    m = _load_manifest(args.manifest)
    todo = []
    for j in m["jobs"]:
        if _solved_path_for(j).is_file():
            continue
        meta_p = CFG.logs_dir / str(j["id"]) / "meta.txt"
        if not meta_p.is_file():
            continue  # never attempted -> that's `farm resume`'s territory
        tail = meta_p.read_text(encoding="utf-8", errors="replace")[-2000:]
        if "FAILED" in tail or "Stalled" in tail or "failed:" in tail:
            todo.append(j)
    print(f"{len(todo)} failed/stalled job(s) to retry")
    if not todo:
        return
    batch_id = _active_batch() or \
        f"retry-{datetime.now():%Y%m%d-%H%M%S}"
    _set_batch(batch_id)
    _submit_jobs(m, todo, batch_id, None, args.force, args.dry_run)


# ---------------------------------------------------------------------------
# worker lifecycle
# ---------------------------------------------------------------------------
def _known_hosts() -> set[str]:
    hosts = set()
    if HEARTBEATS.exists():
        hosts |= {p.stem.upper() for p in HEARTBEATS.glob("*.json")}
    try:
        i = _app().control.inspect(timeout=5)
        for w in (i.stats() or {}):
            hosts.add(w.split("@")[-1].upper())
    except Exception:
        pass
    return hosts


def cmd_restart(args):
    app = _app()
    if args.host and args.host.lower() != "all":
        dest = [f"{args.host.upper()}_cpu@{args.host.upper()}",
                f"{args.host.upper()}_gpu@{args.host.upper()}"]
        app.control.broadcast("shutdown", destination=dest)
        print(f"shutdown sent to {args.host.upper()} - its wrapper relaunches in ~3s")
    else:
        if GLOBAL_STOP.exists():
            print(f"WARNING: {GLOBAL_STOP} exists - workers will exit and STAY DOWN.")
        app.control.broadcast("shutdown")
        print("shutdown broadcast to ALL workers - wrappers relaunch in ~3s "
              "(picking up any config.yaml changes)")


def cmd_stop(args):
    CONTROL.mkdir(parents=True, exist_ok=True)
    if args.host.lower() == "all":
        GLOBAL_STOP.write_text(f"stopped {datetime.now()}\n", encoding="utf-8")
        print(f"created {GLOBAL_STOP}")
    else:
        p = CONTROL / f"{args.host.upper()}.stop"
        p.write_text(f"stopped {datetime.now()}\n", encoding="utf-8")
        print(f"created {p}")
    _app().control.broadcast("shutdown") if args.host.lower() == "all" else \
        _app().control.broadcast("shutdown", destination=[
            f"{args.host.upper()}_cpu@{args.host.upper()}",
            f"{args.host.upper()}_gpu@{args.host.upper()}"])
    print("shutdown sent; wrapper sees the sentinel and stays down.")


def cmd_start(args):
    removed = []
    if args.host.lower() == "all":
        if GLOBAL_STOP.exists():
            GLOBAL_STOP.unlink(); removed.append(str(GLOBAL_STOP))
        for p in CONTROL.glob("*.stop"):
            p.unlink(); removed.append(str(p))
    else:
        p = CONTROL / f"{args.host.upper()}.stop"
        if p.exists():
            p.unlink(); removed.append(str(p))
    if removed:
        print("removed: " + ", ".join(removed))
    else:
        print("no sentinels to remove")
    print("the node watchdog on each box relaunches its worker within ~2 min.")


def cmd_nuke(args):
    if not args.yes:
        print("This will: stop ALL workers (sentinel), revoke+kill all in-flight")
        print("jobs, purge all queues, and FLUSH both Redis DBs (incl. Flower")
        print("history). Solved files and logs on the share are untouched.")
        ans = input("Type NUKE to confirm: ").strip()
        if ans != "NUKE":
            print("aborted"); return
    GLOBAL_STOP.write_text(f"nuked {datetime.now()}\n", encoding="utf-8")
    print(f"[1/4] sentinel created: {GLOBAL_STOP}")
    cmd_purge(argparse.Namespace(force=True))
    print("[2/4] queues purged, in-flight revoked")
    _app().control.broadcast("shutdown")
    time.sleep(6)
    print("[3/4] shutdown broadcast")
    for db, label in ((_broker(), "broker DB0"), (_backend(), "backend DB1")):
        try:
            db.flushdb()
            print(f"[4/4] {label} flushed")
        except Exception:
            deleted = 0
            cur = 0
            while True:
                cur, keys = db.scan(cur, count=500)
                if keys:
                    db.delete(*keys); deleted += len(keys)
                if cur == 0:
                    break
            print(f"[4/4] {label}: {deleted} keys deleted")
    print("\nNUKED. `farm start all` to bring the fleet back.")


def cmd_ingest(args):
    incoming = CFG.root / "incoming"
    moved = []
    for g in sorted(incoming.rglob("*.gsz")):
        rel = g.relative_to(incoming)
        dst = CFG.raw_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(g), str(dst))
        moved.append(rel.as_posix())
    if not moved:
        print("incoming/ has no .gsz"); return
    print(f"moved {len(moved)} file(s) incoming/ -> raw/")
    out = str(CFG.manifests_dir / f"ingest-{datetime.now():%Y%m%d-%H%M%S}.yaml")
    from generate_manifest import generate
    generate(str(CFG.raw_dir), out, "cpu", CFG.default_timeout,
             CFG.default_mesh_edge)
    ns = argparse.Namespace(manifest=out, fresh=args.fresh, force=False,
                            to=None, dry_run=False)
    cmd_submit(ns)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(prog="farm", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("stats")

    q = sub.add_parser("queue")
    q.add_argument("action", nargs="?", default="list",
                   choices=["list", "drop", "next"])
    q.add_argument("job_id", nargs="?")

    a = sub.add_parser("assign")
    a.add_argument("job_id")
    a.add_argument("host")

    p = sub.add_parser("purge")
    p.add_argument("--force", action="store_true",
                   help="also revoke+terminate in-flight jobs")

    mf = sub.add_parser("manifest")
    mf.add_argument("folder")
    mf.add_argument("-o", "--out")
    mf.add_argument("--queue", default="cpu")
    mf.add_argument("--timeout", type=int, default=CFG.default_timeout)
    mf.add_argument("--mesh-edge", type=float, default=CFG.default_mesh_edge)

    s = sub.add_parser("submit")
    s.add_argument("manifest")
    s.add_argument("--fresh", action="store_true",
                   help="purge queues + supersede all prior batches first")
    s.add_argument("--force", action="store_true",
                   help="re-solve even if output exists in solved/")
    s.add_argument("--to", help="route ALL jobs to one host's personal queue")
    s.add_argument("--dry-run", action="store_true")

    rs = sub.add_parser("resume")
    rs.add_argument("manifest")
    rs.add_argument("--dry-run", action="store_true")

    rf = sub.add_parser("retry-failed")
    rf.add_argument("manifest")
    rf.add_argument("--force", action="store_true")
    rf.add_argument("--dry-run", action="store_true")

    rst = sub.add_parser("restart")
    rst.add_argument("host", nargs="?", default="all")

    st = sub.add_parser("stop")
    st.add_argument("host")

    sa = sub.add_parser("start")
    sa.add_argument("host")

    nk = sub.add_parser("nuke")
    nk.add_argument("--yes", action="store_true")

    ig = sub.add_parser("ingest")
    ig.add_argument("--fresh", action="store_true")

    args = ap.parse_args()
    {
        "status": cmd_status, "stats": cmd_stats, "queue": cmd_queue,
        "assign": cmd_assign, "purge": cmd_purge, "manifest": cmd_manifest,
        "submit": cmd_submit, "resume": cmd_resume,
        "retry-failed": cmd_retry_failed, "restart": cmd_restart,
        "stop": cmd_stop, "start": cmd_start, "nuke": cmd_nuke,
        "ingest": cmd_ingest,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
