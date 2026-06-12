#!/usr/bin/env python
"""
farm_console.py - interactive numbered dashboard for ComputeFarm.
Launched by double-clicking farm.bat (no arguments). Every page is a
numbered menu: type the number, Enter. 0 always goes back / home.
Persistent until you choose Exit (or close the window).

All actions reuse ops/farm.py - this is only a menu skin over the same CLI.
"""
import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "framework"))

import farm  # noqa: E402  (the CLI module; we call its cmd_* functions)
from config import CFG  # noqa: E402

IS_TTY = sys.stdout.isatty()


def cls():
    if IS_TTY:
        os.system("cls")


def ask(prompt="select> "):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "0"


def pause():
    ask("\n[Enter] to go back... ")


def run(fn, **kw):
    """Call a farm.py cmd_* with a Namespace; never let it kill the console."""
    try:
        fn(argparse.Namespace(**kw))
    except SystemExit:
        pass
    except Exception as e:
        print(f"\nerror: {type(e).__name__}: {e}")


def pick(items, title, render=lambda x: str(x)):
    """Numbered chooser. Returns the chosen item or None (0/blank = back)."""
    if not items:
        print("  (nothing available)")
        pause()
        return None
    print(f"\n {title}")
    for i, it in enumerate(items, 1):
        print(f"  {i:3d}. {render(it)}")
    print("    0. back")
    c = ask()
    if c.isdigit() and 1 <= int(c) <= len(items):
        return items[int(c) - 1]
    return None


def header():
    r = farm._broker()
    waiting = sum(r.llen(q) for q in farm._all_queue_keys(r))
    hb = list(farm.HEARTBEATS.glob("*.json")) if farm.HEARTBEATS.exists() else []
    fresh = sum(1 for p in hb if time.time() - p.stat().st_mtime < 360)
    batch = farm._active_batch() or "(none)"
    stops = [p.stem for p in farm.CONTROL.glob("*.stop")]
    if farm.GLOBAL_STOP.exists():
        stops.insert(0, "ALL")
    print("=" * 62)
    print(" ComputeFarm Dashboard")
    print("=" * 62)
    print(f" queue waiting: {waiting:<5}  boxes heartbeating: {fresh}/{len(hb)}"
          f"   batch: {batch}")
    if stops:
        print(f" STOPPED: {', '.join(stops)}")
    print("-" * 62)


def manifests():
    return sorted(CFG.manifests_dir.glob("*.yaml"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def hosts():
    return sorted(farm._known_hosts())


# ----------------------------------------------------------------- pages ---
def page_queue():
    while True:
        cls()
        print(" QUEUE - waiting jobs (in run order)\n")
        r = farm._broker()
        rows = []  # (queue, raw, job_id)
        for qk in farm._all_queue_keys(r):
            for raw in reversed(r.lrange(qk, 0, -1)):
                job, _t, _i = farm._decode_msg(raw)
                if job:
                    rows.append((qk, raw, str(job.get("id", "?"))))
        if not rows:
            print("  queue is empty")
            pause()
            return
        for i, (qk, _raw, jid) in enumerate(rows, 1):
            print(f"  {i:3d}. {jid:44s} [{qk}]")
        print("\n  d<N> drop   n<N> run-next   a<N> assign to a PC")
        print("  r refresh   0 back")
        c = ask()
        if c in ("0", "", "q"):
            return
        if c == "r":
            continue
        act, num = c[:1].lower(), c[1:].strip()
        if act in "dna" and num.isdigit() and 1 <= int(num) <= len(rows):
            qk, raw, jid = rows[int(num) - 1]
            if act == "d":
                run(farm.cmd_queue, action="drop", job_id=jid)
            elif act == "n":
                run(farm.cmd_queue, action="next", job_id=jid)
            else:
                h = pick(hosts(), "assign to which PC?")
                if h:
                    run(farm.cmd_assign, job_id=jid, host=h)
            time.sleep(1)


def page_submit(mode):
    """mode: submit | resume | retry"""
    cls()
    m = pick(manifests(), f"{mode.upper()} - choose a manifest",
             lambda p: f"{p.name}  ({time.strftime('%m-%d %H:%M', time.localtime(p.stat().st_mtime))})")
    if not m:
        return
    if mode == "submit":
        opts = ["--fresh  (purge everything + new batch, then submit) [recommended]",
                "plain submit (join current batch)",
                "--fresh --force (also re-solve jobs already in solved\\)",
                "dry-run (show what would be submitted)"]
        o = pick(opts, f"how to submit {m.name}?")
        if o is None:
            return
        i = opts.index(o)
        run(farm.cmd_submit, manifest=str(m), fresh=i in (0, 2),
            force=i == 2, to=None, dry_run=i == 3)
    elif mode == "resume":
        run(farm.cmd_resume, manifest=str(m), dry_run=False)
    else:
        run(farm.cmd_retry_failed, manifest=str(m), force=False, dry_run=False)
    pause()


def page_boxes():
    while True:
        cls()
        print(" WORKER PCs - stop / start / restart\n")
        hs = hosts()
        for i, h in enumerate(hs, 1):
            stopped = (farm.CONTROL / f"{h}.stop").exists()
            print(f"  {i:3d}. {h:20s} {'[STOPPED - sentinel]' if stopped else '[enabled]'}")
        print(f"\n  s<N> stop box   g<N> start box   t<N> restart box")
        print("  S stop ALL      G start ALL      T restart ALL      0 back")
        c = ask()
        if c in ("0", "", "q"):
            return
        if c == "S":
            run(farm.cmd_stop, host="all")
        elif c == "G":
            run(farm.cmd_start, host="all")
        elif c == "T":
            run(farm.cmd_restart, host="all")
        else:
            act, num = c[:1], c[1:].strip()
            if act in ("s", "g", "t") and num.isdigit() and 1 <= int(num) <= len(hs):
                h = hs[int(num) - 1]
                run({"s": farm.cmd_stop, "g": farm.cmd_start,
                     "t": farm.cmd_restart}[act], host=h)
        time.sleep(1.5)


MENU = [
    ("Status (workers, queues, live job progress + stalls)",
     lambda: (run(farm.cmd_status), pause())),
    ("PC stats (CPU / RAM / GPU / temp / uptime)",
     lambda: (run(farm.cmd_stats), pause())),
    ("Queue (view / drop / run-next / assign)", page_queue),
    ("Submit a manifest", lambda: page_submit("submit")),
    ("Resume a manifest (only unsolved jobs)", lambda: page_submit("resume")),
    ("Retry failed jobs of a manifest", lambda: page_submit("retry")),
    ("Purge queue", lambda: (run(farm.cmd_purge, force=ask(
        "also kill in-flight jobs? y/N> ").lower() == "y"), pause())),
    ("Worker PCs (stop / start / restart boxes)", page_boxes),
    ("Ingest incoming\\ (move to raw + manifest + submit)",
     lambda: (run(farm.cmd_ingest, fresh=ask(
         "submit --fresh? Y/n> ").lower() != "n"), pause())),
    ("NUKE (stop fleet + purge + flush Redis)",
     lambda: (run(farm.cmd_nuke, yes=False), pause())),
]


def main():
    while True:
        cls()
        try:
            header()
        except Exception as e:
            print(f"(header unavailable: {e})")
        for i, (label, _fn) in enumerate(MENU, 1):
            print(f"  {i:2d}. {label}")
        print("   0. Exit")
        c = ask("\nselect> ")
        if c in ("0", "exit", "quit", "q"):
            return
        if c.isdigit() and 1 <= int(c) <= len(MENU):
            cls()
            try:
                MENU[int(c) - 1][1]()
            except (EOFError, KeyboardInterrupt):
                pass  # back to home, never crash the dashboard


if __name__ == "__main__":
    main()
