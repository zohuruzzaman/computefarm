"""
ComputeFarm tiny control panel — runs on the Pi alongside Flower.

Serves a single page at http://<pi-ip>:5556/ with:
  - Live queue depth, worker count, success/failure counts
  - One button: 'Clear Flower display' that runs
    `docker restart computefarm-flower` (clears Flower's in-memory
    task-events list and resets its own counters).

Workers are NOT touched - restarting them would interrupt active solves
and is rarely what you want. Just refresh the dashboard look-and-feel.

Deployment (on Pi, as your RPi user):
    sudo cp reset_panel.py /home/<RPI_USER>/computefarm/reset_panel.py
    sudo cp computefarm-control.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now computefarm-control

The systemd unit runs this script as your RPi user. That account needs
to be in the `docker` group so it can restart the Flower container.

No auth - assumes you trust everyone on the LAN that can reach :5556.
"""
import ast
import html
import http.server
import json
import os
import pathlib
import re
import socket
import subprocess
import time
import urllib.parse
from datetime import datetime

import redis

# ----- config (override via env vars if needed) -----
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_USER = os.environ.get("REDIS_USER", "computefarm")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", 5556))

FLOWER_CONTAINER = os.environ.get("FLOWER_CONTAINER", "computefarm-flower")

# Session log + current-session state live next to this script so the
# backup tool (which captures ~/computefarm) sweeps them up automatically.
STATE_DIR = pathlib.Path(
    os.environ.get("PANEL_STATE_DIR", os.path.expanduser("~/computefarm"))
)
CURRENT_SESSION_FILE = STATE_DIR / "session_current.json"
SESSION_HISTORY_FILE = STATE_DIR / "session_history.jsonl"
SESSION_HISTORY_DISPLAY = 20
SESSION_NAME_MAX = 120

WORKER_ALIASES_FILE = STATE_DIR / "worker_aliases.json"
WORKER_LABEL_MAX = 80


def _redis_url(db: int) -> str:
    auth = ""
    if REDIS_USER and REDIS_PASSWORD:
        auth = f"{REDIS_USER}:{REDIS_PASSWORD}@"
    elif REDIS_USER:
        auth = f"{REDIS_USER}@"
    elif REDIS_PASSWORD:
        auth = f":{REDIS_PASSWORD}@"
    return f"redis://{auth}{REDIS_HOST}:{REDIS_PORT}/{db}"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_current_session() -> dict | None:
    try:
        return json.loads(CURRENT_SESSION_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None


def save_current_session(name: str) -> dict:
    record = {"name": name, "started_at": _now_iso()}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_SESSION_FILE.write_text(json.dumps(record))
    return record


def clear_current_session() -> None:
    try:
        CURRENT_SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def append_session_history(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SESSION_HISTORY_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def load_session_history(limit: int = SESSION_HISTORY_DISPLAY) -> list:
    try:
        lines = SESSION_HISTORY_FILE.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-limit:][::-1]:  # newest first
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def load_worker_aliases() -> dict:
    try:
        d = json.loads(WORKER_ALIASES_FILE.read_text())
        return {
            "aliases": dict(d.get("aliases") or {}),
            "seen_workers": list(d.get("seen_workers") or []),
        }
    except (FileNotFoundError, ValueError):
        return {"aliases": {}, "seen_workers": []}


def _save_worker_aliases(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WORKER_ALIASES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))


def record_seen_workers(names) -> None:
    """Add any real worker name to the persisted 'seen' list so the
    alias form can offer it in a dropdown. Filters placeholders."""
    data = load_worker_aliases()
    seen = set(data["seen_workers"])
    added = False
    for n in names:
        if not n or n.startswith("(") or n.startswith("__"):
            continue
        if n not in seen:
            seen.add(n)
            added = True
    if added:
        data["seen_workers"] = sorted(seen)
        _save_worker_aliases(data)


def set_worker_alias(raw_name: str, friendly_label: str) -> str:
    raw_name = (raw_name or "").strip()
    friendly_label = (friendly_label or "").strip()[:WORKER_LABEL_MAX]
    if not raw_name:
        return "FAIL: raw worker name is required"
    data = load_worker_aliases()
    if friendly_label:
        data["aliases"][raw_name] = friendly_label
        # Record as seen too, so it shows up in the dropdown even before
        # we observe it via inspect().
        seen = set(data["seen_workers"]); seen.add(raw_name)
        data["seen_workers"] = sorted(seen)
        _save_worker_aliases(data)
        return f"Alias set: '{raw_name}' → '{friendly_label}'"
    else:
        removed = data["aliases"].pop(raw_name, None)
        _save_worker_aliases(data)
        return (f"Removed alias for '{raw_name}'" if removed is not None
                else f"No alias was set for '{raw_name}'")


def _extract_job_id(args_repr: str) -> str:
    """Pull a job_id like 'G_00018' out of a stringified args list.
    Handles both Python-repr form (single quotes) and JSON-like form."""
    if not args_repr:
        return ""
    m = re.search(r"['\"]id['\"]\s*:\s*['\"]([^'\"]+)['\"]", args_repr)
    if m:
        return m.group(1)
    m = re.search(r"G_\d+", args_repr)
    return m.group(0) if m else ""


def inspect_active_workers() -> dict:
    """Ask all workers what they're currently solving (via Celery's
    inspect API over Redis). Returns {worker_name: [task_dict, ...]}.

    Imports celery lazily so the script still loads + serves the
    queue-state page even if celery isn't installed. The 'Currently
    solving' section will just show 'celery not installed' instead."""
    try:
        from celery import Celery
    except ImportError:
        return {"__error__": "celery library not installed on Pi - "
                "run: pip3 install --break-system-packages celery"}
    try:
        app = Celery("panel", broker=_redis_url(0), backend=_redis_url(1))
        i = app.control.inspect(timeout=3.0)
        return i.active() or {}
    except Exception as e:
        return {"__error__": f"inspect failed: {e}"}


def gather_status() -> dict:
    """Pull live state from Redis."""
    try:
        broker = redis.Redis.from_url(_redis_url(0), decode_responses=True)
        backend = redis.Redis.from_url(_redis_url(1), decode_responses=True)
        cpu_depth = broker.llen("cpu")
        gpu_depth = broker.llen("gpu")
        unacked = broker.hlen("unacked")

        # Per-state counts in the result backend
        states = {}
        recent_success = []
        recent_failure = []
        for k in backend.scan_iter("celery-task-meta-*"):
            try:
                d = json.loads(backend.get(k))
                s = d.get("status", "?")
                states[s] = states.get(s, 0) + 1
                if s == "SUCCESS" and len(recent_success) < 10:
                    result = d.get("result") or {}
                    if isinstance(result, dict):
                        recent_success.append(result.get("job_id", k[-12:]))
                if s == "FAILURE" and len(recent_failure) < 10:
                    msg = ""
                    if isinstance(d.get("result"), dict):
                        msg = str(d["result"].get("exc_message", ""))[:80]
                    recent_failure.append((k[-12:], msg))
            except Exception:
                pass

        # Per-worker active tasks (filename + elapsed)
        now = time.time()
        active_per_worker = []
        inspect_result = inspect_active_workers() or {}
        if "__error__" in inspect_result:
            active_per_worker = [{
                "worker": "(inspect)",
                "solving": inspect_result["__error__"],
                "task_id": "",
                "elapsed_s": 0,
            }]
            inspect_result = {}
        for worker_name, tasks in inspect_result.items():
            if not tasks:
                active_per_worker.append({
                    "worker": worker_name, "solving": "(idle)",
                    "task_id": "", "elapsed_s": 0,
                })
                continue
            for t in tasks:
                # Args come back as either a list or a stringified list
                args_repr = t.get("args", "")
                if isinstance(args_repr, list):
                    args_repr = repr(args_repr)
                job_id = _extract_job_id(args_repr) or "?"
                ts = t.get("time_start") or 0
                elapsed = int(now - ts) if ts else 0
                active_per_worker.append({
                    "worker": worker_name,
                    "solving": f"{job_id}.gsz" if job_id and not job_id.endswith('.gsz') else job_id,
                    "task_id": t.get("id", ""),
                    "elapsed_s": elapsed,
                })

        # If nobody at all responded, leave it empty so the UI shows
        # 'no workers reachable' rather than 'idle'.

        record_seen_workers(inspect_result.keys())

        return {
            "ok": True,
            "cpu_depth": cpu_depth,
            "gpu_depth": gpu_depth,
            "unacked": unacked,
            "states": states,
            "recent_success": recent_success,
            "recent_failure": recent_failure,
            "active_per_worker": active_per_worker,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def render_status_html(status: dict, last_action: str = "") -> str:
    if not status.get("ok"):
        body = f'<p class="err">Redis error: {status.get("error", "?")}</p>'
    else:
        states_rows = "".join(
            f"<tr><td>{s}</td><td>{c}</td></tr>"
            for s, c in sorted(status["states"].items())
        ) or "<tr><td colspan=2><i>no records</i></td></tr>"

        success_rows = "".join(
            f"<li><code>{j}</code></li>" for j in status["recent_success"]
        ) or "<li><i>none</i></li>"

        failure_rows = "".join(
            f"<li><code>{j}</code> <span style='color:#c00'>{m}</span></li>"
            for j, m in status["recent_failure"]
        ) or "<li><i>none</i></li>"

        # Progress / ETA card (uses live worker count from inspect())
        n_workers = 0
        for r in status.get("active_per_worker", []):
            w = r.get("worker", "")
            if w and not w.startswith("(") and w != "__error__":
                n_workers += 1
        eta = compute_progress_eta(workers_active=n_workers)
        if eta.get("ok"):
            pct = eta["pct"]
            bar_pct = max(0.0, min(100.0, pct))
            if eta["total_done"] == 0 and eta["remaining"] == 0:
                bar_text = "idle — no work in flight or queued"
            else:
                bar_text = (
                    f"{eta['total_done']} done / {eta['remaining']} remaining "
                    f"({pct:.1f}%)"
                )

            rate_str = (f"{eta['rate_per_min']:.2f} / min "
                        f"({eta['completed_in_window']} in last "
                        f"{eta['window_min']} min)")
            eta_all_str = _fmt_eta(eta["eta_all_seconds"])
            eta_one_str = _fmt_eta(eta["eta_one_seconds"])
            if eta["remaining"] == 0:
                eta_all_str = eta_one_str = "—  (nothing remaining)"
            elif eta["rate_per_min"] <= 0:
                eta_all_str = eta_one_str = (
                    f"—  (no SUCCESS in last {eta['window_min']} min)")

            if eta["median_task_seconds"] is not None:
                dur_block = (
                    f"<span class='k'>median solve time</span>"
                    f"<span class='v'>{eta['median_task_seconds']:.1f} s "
                    f"<span style='color:#888'>(from <code>"
                    f"{html.escape(eta['duration_source'] or '?')}</code>, "
                    f"n={eta['duration_sample_count']})</span></span>"
                    f"<span class='k'>p90 solve time</span>"
                    f"<span class='v'>{eta['p90_task_seconds']:.1f} s</span>"
                )
            else:
                dur_block = (
                    f"<span class='k'>solve time distribution</span>"
                    f"<span class='v' style='color:#888'>—  no per-task "
                    f"duration in result dict (looked for: "
                    f"{', '.join(_DURATION_FIELDS)})</span>"
                )

            worker_label = (f"all {n_workers} workers" if n_workers
                            else "all workers (none reachable)")
            progress_card = f"""<div class="progress-card">
<h3>Session progress</h3>
<div class="progress-bar">
  <div class="progress-fill" style="width:{bar_pct:.1f}%"></div>
  <div class="progress-text">{bar_text}</div>
</div>
<div class="eta-grid">
  <span class="k">throughput (last {eta['window_min']} min)</span>
  <span class="v">{rate_str}</span>
  <span class="k">ETA — {worker_label}</span>
  <span class="v">{eta_all_str}</span>
  <span class="k">ETA — single worker</span>
  <span class="v">{eta_one_str}</span>
  {dur_block}
</div>
<div class="eta-note">Rate counts SUCCESS only; queues = cpu({eta['queue_cpu']}) + gpu({eta['queue_gpu']}) + unacked({eta['unacked']}).</div>
</div>"""
        else:
            progress_card = (
                f"<div class='progress-card'><h3>Session progress</h3>"
                f"<p class='err'>ETA error: "
                f"{html.escape(eta.get('error', '?'))}</p></div>"
            )

        # Currently-solving table (per worker), with alias-aware display
        worker_data = load_worker_aliases()
        aliases = worker_data["aliases"]
        active_rows = ""
        for row in status.get("active_per_worker", []):
            elapsed = row["elapsed_s"]
            elapsed_str = f"{elapsed//60}m {elapsed%60:02d}s" if elapsed else "—"
            solving = row["solving"]
            task_id = row["task_id"]
            task_short = (task_id[:24] + "…") if len(task_id) > 24 else task_id
            raw_worker = row["worker"]
            friendly = aliases.get(raw_worker)
            if friendly:
                worker_cell = (
                    f"<b title='raw: {html.escape(raw_worker)}'>"
                    f"{html.escape(friendly)}</b><br>"
                    f"<span style='font-size:10px;color:#888'>"
                    f"<code>{html.escape(raw_worker)}</code></span>"
                )
            else:
                worker_cell = f"<code>{html.escape(raw_worker)}</code>"
            active_rows += (
                f"<tr><td>{worker_cell}</td>"
                f"<td><b>{html.escape(str(solving))}</b></td>"
                f"<td>{elapsed_str}</td>"
                f"<td style='font-size:11px;color:#666'>{html.escape(task_short)}</td></tr>"
            )
        if not active_rows:
            active_rows = "<tr><td colspan=4><i>no workers reachable</i></td></tr>"

        # ---- worker alias management UI ---------------------------------
        seen_options = "".join(
            f'<option value="{html.escape(w)}">'
            for w in worker_data["seen_workers"]
        )
        if aliases:
            alias_rows = "".join(
                f"<tr><td><code>{html.escape(raw)}</code></td>"
                f"<td><b>{html.escape(label)}</b></td>"
                f"<td><form method='post' action='/set-worker-alias' "
                f"style='display:inline'>"
                f"<input type='hidden' name='raw_name' value='{html.escape(raw)}'>"
                f"<input type='hidden' name='friendly_label' value=''>"
                f"<button type='submit' style='background:#888;font-size:12px;"
                f"padding:4px 10px'>remove</button></form></td></tr>"
                for raw, label in sorted(aliases.items())
            )
            alias_table = (
                "<table style='margin-top:10px'>"
                "<tr><th>raw name</th><th>friendly label</th><th></th></tr>"
                f"{alias_rows}</table>"
            )
        else:
            alias_table = ("<p style='color:#888;margin-top:8px'>"
                           "<i>No aliases set. Workers display by their raw "
                           "Celery name above.</i></p>")
        worker_alias_section = f"""<h2>Worker aliases <span style='font-weight:normal;font-size:12px;color:#666'>— display-only, no worker restart</span></h2>
<form method="post" action="/set-worker-alias">
  <input type="text" name="raw_name" list="known-workers" required size="40"
         placeholder="raw worker name (e.g. celery@hostname)">
  <input type="text" name="friendly_label" size="30" maxlength="{WORKER_LABEL_MAX}"
         placeholder="friendly label (empty = remove)">
  <button type="submit" class="safe">Save alias</button>
</form>
<datalist id="known-workers">{seen_options}</datalist>
{alias_table}
<p style='color:#888;font-size:11px;margin-top:4px'>
Aliases persist in <code>{html.escape(str(WORKER_ALIASES_FILE))}</code> and ride along in the next backup.
The raw name is still shown as a tooltip + small caption so you can match worker logs.
</p>"""

        body = f"""
{progress_card}

<h2>Currently solving</h2>
<table>
<tr><th>worker</th><th>file</th><th>elapsed</th><th>task id</th></tr>
{active_rows}
</table>

<h2>Live state</h2>
<table>
<tr><th>cpu queue depth</th><td>{status['cpu_depth']}</td></tr>
<tr><th>gpu queue depth</th><td>{status['gpu_depth']}</td></tr>
<tr><th>unacked (in-flight)</th><td>{status['unacked']}</td></tr>
</table>

<h2>Result-backend states</h2>
<table>
<tr><th>state</th><th>count</th></tr>
{states_rows}
</table>

<h2>Recent successes</h2>
<ul>{success_rows}</ul>

<h2>Recent failures</h2>
<ul>{failure_rows}</ul>

{worker_alias_section}

<p style="color:#666;font-size:12px">Fetched at {status['fetched_at']}</p>
"""

    action_banner = ""
    if last_action:
        action_banner = f'<div class="banner">{html.escape(last_action)}</div>'

    # ---- session-name bar ------------------------------------------------
    current = load_current_session()
    if current:
        cur_name = html.escape(current.get("name", "(unnamed)"))
        cur_started = html.escape(current.get("started_at", "?"))
        session_bar = f"""<div class="session-bar">
<span class="label">Current session</span><br>
<span class="name">{cur_name}</span>
<span class="when">started {cur_started}</span>
<form method="post" action="/set-session-name">
  <input type="text" name="session_name" maxlength="{SESSION_NAME_MAX}"
         placeholder="rename session…">
  <button type="submit">Rename</button>
</form>
<form method="post" action="/set-session-name" style="display:inline">
  <input type="hidden" name="session_name" value="">
  <button type="submit" class="clear" title="Clear the current session label (does NOT reset counters)">Clear name</button>
</form>
</div>"""
    else:
        session_bar = f"""<div class="session-bar">
<span class="label">Current session</span><br>
<span class="when"><i>not set — name it before you start the batch so it appears in the history when you reset</i></span>
<form method="post" action="/set-session-name">
  <input type="text" name="session_name" maxlength="{SESSION_NAME_MAX}"
         placeholder="e.g. boundary-tests batch 47">
  <button type="submit">Set name</button>
</form>
</div>"""

    # ---- past sessions table --------------------------------------------
    history = load_session_history()
    if history:
        hist_rows = ""
        for h in history:
            name = html.escape(h.get("name", "(unnamed)"))
            started = html.escape(h.get("started_at") or "—")
            ended = html.escape(h.get("ended_at") or "—")
            ok = h.get("successes", 0)
            fail = h.get("failures", 0)
            other = h.get("other_states") or {}
            other_str = ", ".join(f"{html.escape(s)}:{c}" for s, c in other.items()) or "—"
            recents = h.get("recent_failures") or []
            recent_str = "<br>".join(
                f"<code>{html.escape(r.get('task_id_suffix',''))}</code> "
                f"{html.escape(r.get('message','') or '')}"
                for r in recents[:5]
            ) or "—"
            hist_rows += (
                f"<tr><td><b>{name}</b></td>"
                f"<td>{started}<br>→ {ended}</td>"
                f"<td class='ok'>{ok}</td>"
                f"<td class='fail'>{fail}</td>"
                f"<td>{other_str}</td>"
                f"<td class='failmsg'>{recent_str}</td></tr>"
            )
        history_section = f"""<h2>Past sessions <span style='font-weight:normal;font-size:12px;color:#666'>(newest first, last {SESSION_HISTORY_DISPLAY})</span></h2>
<table class="history">
<tr><th>name</th><th>started → ended</th><th>ok</th><th>fail</th><th>other states</th><th>recent failures</th></tr>
{hist_rows}
</table>
<p style="color:#888;font-size:11px">Full log: <code>{html.escape(str(SESSION_HISTORY_FILE))}</code></p>"""
    else:
        history_section = (
            "<h2>Past sessions</h2>"
            "<p style='color:#888'><i>No sessions logged yet. Set a name above, "
            "then hit <b>Reset session counters</b> when the batch is done.</i></p>"
        )

    return f"""<!doctype html>
<html><head><title>ComputeFarm Control</title>
<meta http-equiv="refresh" content="15">
<style>
body{{font-family:sans-serif;max-width:920px;margin:30px auto;padding:0 20px;color:#222}}
h1{{margin-bottom:0}}
h2{{margin-top:30px;border-bottom:1px solid #ddd;padding-bottom:4px}}
table{{border-collapse:collapse;margin:8px 0}}
th,td{{border:1px solid #ddd;padding:6px 12px;text-align:left}}
th{{background:#f3f3f3}}
button{{font-size:16px;padding:10px 18px;background:#c33;color:white;border:none;border-radius:4px;cursor:pointer;margin-right:8px}}
button:hover{{background:#911}}
button.safe{{background:#258}}
button.safe:hover{{background:#147}}
button.danger{{background:#700}}
button.danger:hover{{background:#500}}
.banner{{background:#efe;border:1px solid #8a8;padding:8px 12px;margin:12px 0;border-radius:4px}}
.err{{color:#c00}}
.links a{{margin-right:14px}}
ul{{list-style:square inside;padding:0}}
.session-bar{{background:#f7f4e8;border:1px solid #d4c98a;padding:10px 14px;margin:14px 0;border-radius:4px}}
.session-bar .label{{color:#666;font-size:12px;text-transform:uppercase;letter-spacing:1px}}
.session-bar .name{{font-size:18px;font-weight:bold;color:#333;margin-right:10px}}
.session-bar .when{{color:#666;font-size:13px}}
.session-bar form{{display:inline;margin-left:12px}}
.session-bar input[type=text]{{padding:6px 10px;font-size:14px;border:1px solid #aaa;border-radius:3px;width:280px}}
.session-bar button{{font-size:14px;padding:6px 14px;background:#258;margin-left:4px}}
.session-bar button.clear{{background:#888}}
.history th,.history td{{font-size:13px;vertical-align:top}}
.history td.ok{{color:#070;text-align:right}}
.history td.fail{{color:#c00;text-align:right}}
.history td.failmsg{{color:#666;font-size:11px;max-width:380px}}
.progress-card{{background:#fafafa;border:1px solid #ccc;border-radius:6px;padding:14px 16px;margin:14px 0}}
.progress-card h3{{margin:0 0 8px 0;font-size:14px;text-transform:uppercase;letter-spacing:1px;color:#555}}
.progress-bar{{width:100%;background:#eee;border:1px solid #999;border-radius:4px;height:26px;position:relative;overflow:hidden}}
.progress-fill{{background:linear-gradient(180deg,#4b9,#2a7);height:100%;transition:width .6s ease}}
.progress-text{{position:absolute;inset:0;text-align:center;line-height:26px;font-weight:bold;color:#000;text-shadow:0 1px 0 #fff}}
.eta-grid{{display:grid;grid-template-columns:auto auto;gap:4px 24px;margin-top:10px;font-size:13px}}
.eta-grid .k{{color:#666}}
.eta-grid .v{{font-family:ui-monospace,Menlo,monospace}}
.eta-note{{color:#888;font-size:11px;margin-top:8px}}
</style>
</head><body>
<h1>ComputeFarm Control Panel</h1>
<p class="links">
<a href="http://{socket.gethostname()}:5555/">Flower dashboard</a>
<a href="/">refresh status</a>
</p>
{action_banner}

{session_bar}

<form method="post" action="/reset-flower" style="display:inline">
<button type="submit" title="Restart the Flower container - clears the task-events table">
Clear Flower display
</button>
</form>
<form method="post" action="/reset-session" style="display:inline"
      onsubmit="return confirm('HARD RESET for new session:\n\n  - Delete every celery-task-meta-* record from the result backend (db=1)\n  - Drain cpu, gpu, unacked, unacked_index, unacked_mutex from the broker (db=0)\n  - Restart Flower\n\nWorkers still in flight will keep solving, and their results will count toward the new session. Any code still polling for an OLD task id will see PENDING.\n\nContinue?');">
<button type="submit" class="danger"
        title="Delete celery-task-meta-* from db=1, drain Celery broker queues + in-flight tracking, restart Flower. Does not touch rq:* keys.">
Reset session counters
</button>
</form>
<div style="color:#666;font-size:13px;margin-top:6px">
<b>Clear Flower display</b> only restarts the Flower view; the totals come right back from Redis.
<b>Reset session counters</b> deletes the result-backend records Flower reads from, so totals actually start from zero.
</div>

{body}

{history_section}

<p style="color:#888;font-size:11px;margin-top:40px">
Auto-refresh every 15s. Source: ~/computefarm/reset_panel.py
</p>
</body></html>
"""


def do_reset_flower() -> str:
    try:
        r = subprocess.run(
            ["docker", "restart", FLOWER_CONTAINER],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return f"OK: restarted {FLOWER_CONTAINER}. Allow ~5 sec for Flower to come back up."
        return f"FAIL: docker restart {FLOWER_CONTAINER} returned {r.returncode}: {r.stderr.strip()}"
    except Exception as e:
        return f"FAIL: {e}"


# Broker keys drained on a session reset. rq:* keys are intentionally
# excluded — rq-dashboard is a separate system on a separate port.
_SESSION_BROKER_KEYS = ("cpu", "gpu", "unacked", "unacked_index", "unacked_mutex")

# Rolling window for rate/ETA, in minutes (overridable via env).
ETA_WINDOW_MIN = int(os.environ.get("ETA_WINDOW_MIN", "15"))
# First match wins. If your workers return a result dict with one of these
# numeric keys, it's treated as per-task seconds and feeds median/p90.
_DURATION_FIELDS = ("duration_s", "wall_time_s", "elapsed_s",
                    "solve_time_s", "runtime_s", "seconds")


def _fmt_eta(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def compute_progress_eta(window_minutes: int = ETA_WINDOW_MIN,
                         workers_active: int = 0) -> dict:
    """Scan db=1 SUCCESS task-meta + db=0 broker depth and derive:
       - rolling SUCCESS rate over the last `window_minutes`
       - remaining work (queues + in-flight)
       - ETA under current worker count and under 1 worker
       - median + p90 per-task seconds, if results carry a duration field"""
    try:
        backend = redis.Redis.from_url(_redis_url(1), decode_responses=True)
        broker = redis.Redis.from_url(_redis_url(0), decode_responses=True)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    now = time.time()
    cutoff = now - window_minutes * 60
    completed_in_window = 0
    total_done = 0
    durations = []
    duration_field = None

    try:
        for k in backend.scan_iter("celery-task-meta-*", count=500):
            v = backend.get(k)
            if not v:
                continue
            try:
                d = json.loads(v)
            except ValueError:
                continue
            if d.get("status") != "SUCCESS":
                continue
            total_done += 1
            ts = None
            dd = d.get("date_done")
            if dd:
                try:
                    ts = datetime.fromisoformat(dd.replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    ts = None
            if ts is None or ts < cutoff:
                continue
            completed_in_window += 1
            result = d.get("result")
            if isinstance(result, dict):
                for dk in _DURATION_FIELDS:
                    val = result.get(dk)
                    if isinstance(val, (int, float)) and val > 0:
                        durations.append(float(val))
                        duration_field = dk
                        break
    except Exception as e:
        return {"ok": False, "error": f"db=1 scan: {e}"}

    try:
        cpu = broker.llen("cpu")
        gpu = broker.llen("gpu")
        unacked = broker.hlen("unacked")
    except Exception:
        cpu = gpu = unacked = 0
    remaining = cpu + gpu + unacked

    rate_per_min = completed_in_window / window_minutes if window_minutes else 0.0
    eta_all_s = eta_one_s = None
    if remaining > 0 and rate_per_min > 0:
        eta_all_s = (remaining / rate_per_min) * 60.0
        if workers_active > 0:
            # Per-worker rate = rate / W → time for 1 worker = eta_all * W.
            eta_one_s = eta_all_s * workers_active

    median = p90 = None
    if durations:
        durations.sort()
        n = len(durations)
        median = durations[n // 2]
        p90 = durations[min(n - 1, max(0, int(n * 0.9) - 1))]

    pct = 0.0
    denom = total_done + remaining
    if denom > 0:
        pct = 100.0 * total_done / denom

    return {
        "ok": True,
        "window_min": window_minutes,
        "completed_in_window": completed_in_window,
        "rate_per_min": rate_per_min,
        "total_done": total_done,
        "remaining": remaining,
        "queue_cpu": cpu, "queue_gpu": gpu, "unacked": unacked,
        "workers_active": workers_active,
        "eta_all_seconds": eta_all_s,
        "eta_one_seconds": eta_one_s,
        "median_task_seconds": median,
        "p90_task_seconds": p90,
        "duration_source": duration_field,
        "duration_sample_count": len(durations),
        "pct": pct,
    }


def do_reset_session() -> str:
    """Snapshot current counters into session_history.jsonl, then clear
    everything Flower's counters are derived from, drain Celery broker
    state, and restart Flower."""
    # Snapshot BEFORE wipe so the archive has real numbers.
    snap = gather_status()
    states = snap.get("states", {}) if snap.get("ok") else {}
    recent_failures = snap.get("recent_failure", []) if snap.get("ok") else []

    current = load_current_session() or {}
    record = {
        "name": (current.get("name") or "(unnamed)"),
        "started_at": current.get("started_at"),
        "ended_at": _now_iso(),
        "successes": states.get("SUCCESS", 0),
        "failures": states.get("FAILURE", 0),
        "other_states": {s: c for s, c in states.items()
                         if s not in ("SUCCESS", "FAILURE")},
        "recent_failures": [
            {"task_id_suffix": j, "message": m} for j, m in recent_failures
        ],
    }

    try:
        backend = redis.Redis.from_url(_redis_url(1), decode_responses=True)
        broker = redis.Redis.from_url(_redis_url(0), decode_responses=True)

        deleted_meta = 0
        batch = []
        for k in backend.scan_iter("celery-task-meta-*", count=500):
            batch.append(k)
            if len(batch) >= 500:
                deleted_meta += backend.delete(*batch)
                batch = []
        if batch:
            deleted_meta += backend.delete(*batch)

        deleted_broker = broker.delete(*_SESSION_BROKER_KEYS)
    except Exception as e:
        return f"FAIL: redis cleanup error: {e}"

    record["meta_records_deleted"] = deleted_meta
    record["broker_keys_deleted"] = deleted_broker

    history_warn = ""
    try:
        append_session_history(record)
        clear_current_session()
    except Exception as e:
        history_warn = f" (history write failed: {e})"

    flower_msg = do_reset_flower()
    return (
        f"OK: archived '{record['name']}' ({record['successes']} ok, "
        f"{record['failures']} fail); cleared {deleted_meta} meta + "
        f"{deleted_broker} broker keys; {flower_msg}{history_warn}"
    )


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quieter access log
        print(f"[{datetime.now():%H:%M:%S}] {self.address_string()} {fmt % args}")

    def _send(self, status: int, body: str, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-type", ctype)
        self.send_header("Content-length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    # 303 See Other so the browser GETs / next. Without this, the form's URL
    # stays in the address bar and the page's meta-refresh re-issues the POST
    # path as a GET — which 404s.
    def _redirect_home(self, msg: str = ""):
        location = "/"
        if msg:
            location += "?msg=" + urllib.parse.quote(msg[:300], safe="")
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            qs = urllib.parse.parse_qs(parsed.query)
            msg = (qs.get("msg", [""])[0] or "")[:300]
            self._send(200, render_status_html(gather_status(), last_action=msg))
        else:
            self._send(404, "<h1>404</h1>")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/reset-flower":
            self._redirect_home(do_reset_flower())
        elif path == "/reset-session":
            self._redirect_home(do_reset_session())
        elif path == "/set-session-name":
            length = int(self.headers.get("Content-length", "0") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            fields = urllib.parse.parse_qs(raw, keep_blank_values=True)
            name = (fields.get("session_name", [""])[0] or "").strip()[:SESSION_NAME_MAX]
            if name:
                rec = save_current_session(name)
                msg = f"Session set to '{name}' at {rec['started_at']}"
            else:
                clear_current_session()
                msg = "Session name cleared (counters untouched)"
            self._redirect_home(msg)
        elif path == "/set-worker-alias":
            length = int(self.headers.get("Content-length", "0") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            fields = urllib.parse.parse_qs(raw, keep_blank_values=True)
            raw_name = fields.get("raw_name", [""])[0]
            friendly = fields.get("friendly_label", [""])[0]
            self._redirect_home(set_worker_alias(raw_name, friendly))
        else:
            self._send(404, "<h1>404</h1>")


def main():
    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"ComputeFarm control panel listening on http://{LISTEN_HOST}:{LISTEN_PORT}/")
    print(f"  Redis target: {REDIS_HOST}:{REDIS_PORT} as {REDIS_USER!r}")
    print(f"  Flower container: {FLOWER_CONTAINER}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
