# submit.ps1 — Submit a job to the Ray cluster from any Windows machine
#
# Usage:
#   .\submit.ps1 -Type solver   -Config .\slope_run.json
#   .\submit.ps1 -Type train    -Config .\train_config.json
#   .\submit.ps1 -Type ablation -Config .\sweep.json
#
# The JSON file can contain any fields your handler needs.
# "type" is added automatically from -Type.

param(
    [Parameter(Mandatory)] [string]$Type,
    [Parameter(Mandatory)] [string]$Config,
    [string]$HeadIP = "<RPI_IP>"   # <<< update to your RPi IP
)

if (-not (Test-Path $Config)) {
    Write-Error "Config file not found: $Config"
    exit 1
}

# Pass values through environment variables — avoids all quoting/escaping issues
$env:_RAY_HEAD = $HeadIP
$env:_JOB_TYPE = $Type
$env:_JOB_CFG  = (Resolve-Path $Config).Path

$py = @'
import ray, json, os, sys

head  = os.environ["_RAY_HEAD"]
jtype = os.environ["_JOB_TYPE"]
cfg   = os.environ["_JOB_CFG"]

try:
    ray.init(address=f"ray://{head}:10001", ignore_reinit_error=True)
    coord = ray.get_actor("coordinator")

    with open(cfg) as f:
        job = json.load(f)
    job["type"] = jtype

    jid = ray.get(coord.submit.remote(job))
    print(f"Submitted  job_id={jid}  type={jtype}")

except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
finally:
    ray.shutdown()
'@

$py | python -

Remove-Item Env:\_RAY_HEAD, Env:\_JOB_TYPE, Env:\_JOB_CFG -ErrorAction SilentlyContinue
