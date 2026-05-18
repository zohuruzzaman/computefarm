"""
Submit a YAML job manifest.

Usage:
  python submit_manifest.py manifests/my_jobs.yaml
  python submit_manifest.py manifests/my_jobs.yaml --dry-run

Task IDs are stamped with the job id so Flower's UUID column reads as
'G_00018-a3f4b2c1' instead of an opaque random UUID. Makes the dashboard
self-describing even when args are truncated.
"""

import argparse
import uuid
import yaml
from tasks import run_command, solve_geostudio


def _make_task_id(job_id: str) -> str:
    """task_id readable in Flower: '<job_id>-<8-char-suffix>'.
    Suffix keeps it unique across retries / resubmits of the same job."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(job_id))
    return f"{safe}-{uuid.uuid4().hex[:8]}"


def submit(path: str, dry_run: bool = False):
    with open(path, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    project = manifest.get("project", "default")
    queue = manifest.get("queue", "default")
    job_type = manifest.get("type", "command")
    jobs = manifest["jobs"]
    defaults = manifest.get("defaults", {})

    print(f"Project : {project}")
    print(f"Queue   : {queue}")
    print(f"Type    : {job_type}")
    print(f"Jobs    : {len(jobs)}")
    if defaults:
        print(f"Defaults: {defaults}")
    print()

    for job in jobs:
        if "id" not in job:
            raise ValueError("Every job needs an 'id'")
        merged = {**defaults, **job}

        if dry_run:
            print(f"  [DRY RUN] {merged['id']} -> queue={queue}")
            continue

        task_id = _make_task_id(merged["id"])

        if job_type == "geostudio":
            if "gsz_path" not in merged:
                raise ValueError(f"Job {merged['id']} missing 'gsz_path'")
            result = solve_geostudio.apply_async(
                args=[merged], queue=queue, task_id=task_id,
            )
        else:
            if "command" not in merged:
                raise ValueError(f"Job {merged['id']} missing 'command'")
            result = run_command.apply_async(
                args=[merged], queue=queue, task_id=task_id,
            )

        print(f"  {merged['id']} -> {result.id}")

    if dry_run:
        print("\nDry run - nothing submitted.")
    else:
        print(f"\n{len(jobs)} jobs submitted to '{queue}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="Path to YAML manifest")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    submit(args.manifest, dry_run=args.dry_run)
