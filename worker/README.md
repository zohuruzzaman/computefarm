# ComputeFarm — operator guide

Distributed job farm: a Raspberry Pi orchestrator (Redis broker + Flower), N
Windows worker PCs (Celery, pulled jobs), and this SMB share as the single
storage hub. Software-agnostic: GeoStudio solves, ablation sweeps, and ML
training all run through the same framework — only the **driver script**
(`tools\`) differs per use case.

**Everything is operated through one command:** `farm.bat` (root of this
share). Run it from any machine that can reach the share + Redis.

```
farm status      farm stats        farm queue        farm assign
farm purge       farm manifest     farm submit       farm resume
farm retry-failed farm restart     farm stop/start   farm nuke    farm ingest
```

---

## 1. Folder layout

| Path | What it is |
|---|---|
| `farm.bat` | the one CLI — every operation below |
| `framework\` | Celery app, tasks, config.yaml, worker launch wrappers |
| `ops\` | farm.py CLI, node_watchdog.py + installer, share-mapping helpers |
| `tools\` | **driver scripts only** (solve_gsz_geocmd.ps1, run_training.ps1, …) |
| `control\` | active_batch.txt, stop sentinels, heartbeats\<HOST>.json |
| `manifests\` | job manifests (YAML) |
| `incoming\` → `raw\` → `solved\` | job payload flow (`farm ingest` moves them) |
| `logs\<job_id>\` | meta.txt, stdout/stderr per attempt, **progress.ndjson** (live beacon) |
| `results\`, `training\` | ML-training outputs |
| `archive\` | retired scripts, installers, pre-v3.1 logs, old raw inputs |
| `worker_local.yaml` | per-machine ML-training override (corpus_root) |

## 2. Daily quickstart

```bat
farm status                     :: who's online, queue depth, live job progress
farm stats                      :: each PC: CPU/RAM/disk/GPU/temp/uptime
farm manifest raw -o manifests\batch.yaml
farm submit manifests\batch.yaml --fresh    :: purge + new batch + submit
farm resume manifests\batch.yaml            :: submit ONLY unsolved jobs
```

`--fresh` is the safe default for a new campaign: it purges all queues,
supersedes every prior batch (stale messages get dropped by workers on
arrival), and submits.

## 3. The safety rails (why the old failure modes can't recur)

1. **Batch-stamp.** Every submission carries a `batch_id`;
   `control\active_batch.txt` names the live one. A worker that receives a
   job from a superseded batch **drops it on arrival** (logged
   `[stale-drop]`). Stale redelivered messages are harmless forever.
2. **Idempotent solve.** If `solved\<file>` already exists the worker skips
   the job instantly. Resubmitting a whole manifest never re-solves work
   (`farm submit --force` overrides deliberately).
3. **No hard time cap + progress watchdog.** A solve is killed only if its
   output stops growing for 30 min (`GEO_STALL_IDLE_MIN` env to change) —
   slow-but-progressing models run to completion. A 600-min backstop guards
   true hangs.
4. **Tree-kill everywhere.** Any timeout/stall kill takes the whole process
   tree (`taskkill /T`) — GeoCmd/SolveServer can no longer be orphaned into
   CPU-eating zombies.
5. **Single worker per box.** Celery's `--pidfile` refuses duplicate workers;
   the watchdog + worker boot also sweep any orphan solver processes.

## 4. Live progress & stall detection (`farm status`)

Workers append one JSON line per minute to `logs\<job>\progress.ndjson`
while a job runs (local output sizes, idle seconds, last driver log line).
`farm status` shows per-job: runtime, .gsz size so far, idle time, and flags
`[WARN idle >10m]` before the 30-min watchdog ever fires. You no longer wait
for the finished file to learn anything.

## 5. Queue control (the "dashboard gap")

Flower (`http://<pi>:5555`) shows workers + running tasks and can revoke a
task. It **cannot** see waiting messages, reorder them, or pin them to a PC
— these can:

```bat
farm queue                       :: list waiting jobs in run order
farm queue drop  <job_id>        :: remove one waiting job
farm queue next  <job_id>        :: this job runs next
farm assign <job_id> <HOST>      :: move job to HOST's personal queue (cpu.<HOST>)
farm submit m.yaml --to <HOST>   :: route a whole manifest to one PC
```

## 6. Worker lifecycle & remote start

Each PC runs the **node watchdog** (`ops\install_watchdog.bat`, run ONCE per
box): a plain ~180-line Python script Task Scheduler fires every 2 min. It
writes the heartbeat for `farm stats`, sweeps zombie solvers, and relaunches
the worker **unless a stop sentinel exists**. That gives remote control with
no extra services:

```bat
farm stop  <HOST>|all      :: sentinel + shutdown -> stays down
farm start <HOST>|all      :: delete sentinel -> watchdog relaunches in <=2 min
farm restart [HOST|all]    :: rolling restart (picks up config.yaml changes)
farm nuke --yes            :: stop fleet + revoke + purge + flush Redis
```

Sentinels: `WORKERS.stop` (root, fleet-wide) and `control\<HOST>.stop`
(per box). GPU workers are opt-in per box: create marker `control\<HOST>.gpu`.

## 7. Manifest schema (multi-purpose)

```yaml
# GeoStudio solve batch (type: geostudio -> tasks.solve_geostudio)
project: smoke_v31
queue: cpu
type: geostudio
defaults: { timeout_minutes: 600, mesh_edge: 0 }
jobs:
  - { id: cfg_a, gsz_path: \\<HUB>\ComputeFarm\raw\cfg_a.gsz, rel_path: cfg_a.gsz }
```

```yaml
# Generic command batch (ablation solver, ML training, anything CLI)
project: ablation_round2
queue: gpu            # or cpu
type: command
defaults: { timeout_minutes: 1440 }
jobs:
  - id: abl_lr3e4
    command: powershell
    args: [-ExecutionPolicy, Bypass, -File, "{root}\tools\run_training.ps1",
           -Config, "{root}\training\abl_lr3e4.yaml"]
```

`{root}` resolves to this share on whichever machine runs the job. Paths in
`gsz_path` must be full UNC — drive letters differ between boxes.
Both task types get the batch-stamp, beacon, and tree-kill rails.

To add a NEW tool: write a driver in `tools\` (exit 0 = success, nonzero =
fail, progress to stdout), point jobs at it. No framework changes.

## 8. New worker PC setup

1. Python 3.12 (`py -3.12` must resolve) + `framework\setup.bat` once.
2. Map the share (helpers in `ops\connect_drive.ps1`); GeoStudio 2025.2 at
   the standard path if the box will solve.
3. `ops\install_watchdog.bat` — the box now self-manages (auto-start worker,
   heartbeat, zombie sweep) and obeys `farm start/stop` remotely.
4. Details: `SETUP.md`.

## 9. Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `farm status` shows job `[WARN idle …]` | output flat >10 min; watchdog kills at 30 min idle. Check `logs\<job>\progress.ndjson` last lines |
| job logged `[stale-drop]` | it belonged to a superseded batch — intended; resubmit under the live batch if you actually want it |
| job logged `[skip] already solved` | output exists in `solved\`; use `farm submit --force` to re-solve |
| worker won't start, "Pidfile already exists" | a worker IS already running on that box (this is the duplicate-protection working) |
| box missing from `farm stats` | watchdog not installed there, or heartbeat STALE -> box offline/asleep |
| `farm queue` shows job but no worker takes it | its queue has no subscriber — check `farm status` workers list and the job's queue name |
| GeoCmd/SolveServer piling up | should no longer happen (tree-kill + sweeps); if seen: `farm stop <host>`, let watchdog sweep on next tick, `farm start <host>` |

## 10. Performance facts (GeoStudio deployment, measured)

- GeoCmd.exe `/solve` ≈ **160× faster** than per-analysis gsi gRPC. Always.
- 4 concurrent solves per box is the proven-good concurrency
  (`config.yaml: cpu_concurrency`). Change it, then `farm restart`.
- Local-scratch solving is mandatory; only start/end copies touch SMB.
- Coupled 365-day MCHS solves are *hours* each — that's physics, not a hang;
  the beacon + watchdog distinguish slow from stuck.
