# status.ps1 — Check cluster queue and job status
#
# Usage:
#   .\status.ps1                  — queue counts + active workers
#   .\status.ps1 -Results         — also show last 20 completed jobs
#   .\status.ps1 -Failed          — also show failed jobs with errors
#   .\status.ps1 -Results -Failed — show everything

param(
    [string]$HeadIP = "<RPI_IP>",   # <<< update to your RPi IP
    [switch]$Results,
    [switch]$Failed
)

$env:_RAY_HEAD     = $HeadIP
$env:_SHOW_RESULTS = if ($Results) { "1" } else { "0" }
$env:_SHOW_FAILED  = if ($Failed)  { "1" } else { "0" }

$py = @'
import ray, json, os

head         = os.environ["_RAY_HEAD"]
show_results = os.environ["_SHOW_RESULTS"] == "1"
show_failed  = os.environ["_SHOW_FAILED"]  == "1"

ray.init(address=f"ray://{head}:10001", ignore_reinit_error=True)
coord  = ray.get_actor("coordinator")
status = ray.get(coord.status.remote())

print("")
print("=== Cluster Status ===")
print(f"  Pending   : {status['pending']}")
print(f"  Active    : {status['active']}")
print(f"  Completed : {status['completed']}")
print(f"  Failed    : {status['failed']}")
if status["workers"]:
    print(f"  Busy      : {', '.join(status['workers'])}")
print("")

if show_results:
    results = ray.get(coord.get_results.remote())
    print("=== Completed Jobs (last 20) ===")
    for r in results[-20:]:
        j = r["job"]
        out = json.dumps(r["result"])[:100]
        print(f"  [{r['worker']:<15s}] {j['id']}  {j.get('type','?'):<10s}  {j.get('filename', '')}  {out}")
    print("")

if show_failed:
    failed = ray.get(coord.get_failed.remote())
    print("=== Failed Jobs ===")
    for f in failed:
        j = f["job"]
        print(f"  [{f['worker']:<15s}] {j['id']}  {j.get('type','?'):<10s}  {j.get('filename', '')}")
        print(f"    {f['error'][:300]}")
    print("")

ray.shutdown()
'@

$py | python -

Remove-Item Env:\_RAY_HEAD, Env:\_SHOW_RESULTS, Env:\_SHOW_FAILED -ErrorAction SilentlyContinue
