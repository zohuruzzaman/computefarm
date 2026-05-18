"""
Example job handler — shows the interface your scripts must follow.

Rename this file to match your job type:
  solver.py    → handles {"type": "solver",   ...}
  train.py     → handles {"type": "train",    ...}
  ablation.py  → handles {"type": "ablation", ...}

The watcher on the RPi populates job["input_path"] and job["output_dir"]
automatically when it detects a new file in pending/.

Pattern: always copy files locally before processing (stage-in / stage-out).
Do not work directly on the UNC path — it's slower and fragile under load.
"""

import shutil
import tempfile
from pathlib import Path


def run(job: dict) -> dict:
    """
    job keys (always present, set by watcher):
        id          — short unique job ID
        type        — matches this filename (e.g. "solver")
        input_path  — UNC path to input file   e.g. \\rpi\storage\active\abc123__slope.gsz
        output_dir  — UNC path to results dir  e.g. \\rpi\storage\results\
        filename    — original filename before job_id prefix

    job keys (optional, from your JSON config or submit.ps1):
        any additional parameters your solver needs

    return: any dict — stored by coordinator, visible via status.ps1 -Results
    """

    input_unc  = job["input_path"]
    output_dir = Path(job["output_dir"])
    filename   = job["filename"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ── Stage in: copy file from RPi share to local temp ──────────────────
        local_input = tmp / filename
        shutil.copy2(input_unc, local_input)

        # ── Do real work here ──────────────────────────────────────────────────
        # Replace this block with your actual solver / trainer call.
        result_file = tmp / (Path(filename).stem + "_result.txt")
        result_file.write_text(f"processed: {filename}\n")

        # ── Stage out: copy results back to RPi share ─────────────────────────
        output_dir.mkdir(parents=True, exist_ok=True)
        for f in tmp.iterdir():
            if f.name == filename:
                continue  # don't copy the input back
            shutil.copy2(f, output_dir / f.name)

    return {
        "status":   "ok",
        "filename": filename,
        "output":   str(output_dir),
    }
