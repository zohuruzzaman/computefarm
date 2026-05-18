# ComputeFarm Setup

## Drop anywhere and run

The framework auto-detects where it lives. No drive letters to configure.

```
Put this folder anywhere:
  D:\MyFarm\              or  Z:\ComputeFarm\  or  \\SERVER\Share\Farm\
    framework\            <- auto-detects parent as root
      config.yaml         <- ONLY set redis_host (Pi's IP)
      setup.bat           <- first-time: creates subfolders, installs packages
      setup_check.bat     <- verify everything works
      start_workers.bat   <- fire it up
      ...
    tools\                <- pre-bundled: solve_gsz.ps1 + geostudio_automation\scripts\
      solve_gsz.ps1
      geostudio_automation\scripts\solve_and_extract.py
    manifests\
    raw\                  <- created by setup.bat
    solved\
    logs\
```

## Worker role (what runs on each worker PC)

Mesh + solve + push back. That's it.

```
raw\*.gsz  --copy-->  local scratch  --mesh+solve+save-->  copy back to solved\
```

No parquet, no CSVs leave the .gsz. Extraction is a separate step that runs
on the storage PC (or wherever) over the finished `solved\*.gsz` files.

## Retry policy

A `.gsz` that fails to solve is automatically requeued for another attempt -
up to **6 attempts total**, with **5 minutes between attempts**. The retry
is delivered through Redis to any worker subscribed to the queue (not
pinned to the worker that hit the failure), so a busy or licensing-
contended box doesn't block the job.

- Worker crashes mid-solve are handled separately (the message is
  redelivered to another worker) and do **not** count against the 6-attempt
  budget.
- A `<stem>_PARTIAL.gsz` is dropped into `solved\` only on the **final**
  failure - intermediate retries don't litter `solved\`.
- Per-attempt stdout/stderr are kept as `stdout.attempt1.log`,
  `stdout.attempt2.log`, ... under `logs\<job_id>\`.
- After 6 failed attempts the task ends in Celery `FAILURE` and
  `meta.txt` says `Status: FAILED after 6 attempts` - that's the
  manual-intervention signal.

Tune the retry count and delay at the top of `framework\tasks.py`:
`SOLVE_MAX_ATTEMPTS` and `SOLVE_RETRY_DELAY_SEC`.

## First time setup

1. Drop the `ComputeFarm` folder wherever you want it
2. Double-click `framework\setup.bat` - creates subfolders, installs packages, optionally shares on network
3. `framework\config.yaml` already points at the live Pi (`redis_host: <YOUR_PI_IP>`). Override only if the Pi moves.
4. Install the GeoStudio gsi wheel from `C:\Program Files\Seequent\GeoStudio 2025.2\API\gsi-*.whl`. (Optional: `pip install pandas` to enable the FoS min/max line in the post-solve summary.)
5. Double-click `framework\setup_check.bat` to verify
6. Double-click `framework\start_workers.bat` to begin

## On additional worker PCs

1. Map the shared drive: `net use Z: \\STORAGE-PC\ComputeFarm /persistent:yes`
2. Install packages: `pip install -r Z:\ComputeFarm\framework\requirements.txt`
3. Install the GeoStudio gsi wheel
4. Run `setup_check.bat`
5. Run `start_workers.bat`

## How paths work

`config.py` finds its own location on disk:

```
config.py is at:    Z:\ComputeFarm\framework\config.py
framework/ parent:  Z:\ComputeFarm\                      <- this becomes ROOT
raw_dir:            Z:\ComputeFarm\raw\                  <- ROOT / "raw"
solved_dir:         Z:\ComputeFarm\solved\               <- ROOT / "solved"
logs_dir:           Z:\ComputeFarm\logs\                 <- ROOT / "logs"
tools_dir:          Z:\ComputeFarm\tools\                <- ROOT / "tools"
solve_script:       Z:\ComputeFarm\tools\solve_gsz.ps1   <- tools / "solve_gsz.ps1"
```

Move the whole folder to `D:\Farm\` and it auto-adjusts:

```
ROOT:               D:\Farm\
raw_dir:            D:\Farm\raw\
solved_dir:         D:\Farm\solved\
...
```

Scratch auto-detects too:
- Local drive (D:\Farm\) -> scratch at D:\ComputeFarm_scratch\
- Network path (\\SERVER\...) -> scratch at C:\ComputeFarm_scratch\

## Submitting jobs

```bat
cd /d Z:\ComputeFarm\framework

python generate_manifest.py ..\raw -o ..\manifests\batch.yaml
python submit_manifest.py ..\manifests\batch.yaml
```

## Monitoring

Dashboard: `http://<PI_IP>:5555`

Logs: `<root>\logs\<job_id>\meta.txt` / `stdout.log` / `stderr.log`
