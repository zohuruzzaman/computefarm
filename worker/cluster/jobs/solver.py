r"""
GeoStudio solver job handler.

Pulls one .gsz job, solves it headlessly via gsi (auto-meshes via the
DisplayMeshDefaultEdgeLength dirty-bit trick), extracts every analysis /
step / source CSV into a long-format parquet, runs V2 sidecar enrichment
(node_gauss + node_static + gauss_geom + node_geom + seep_gauss), and
stages the whole bundle back to the RPi share as one .tar.gz per template.

Job dict (set by watcher):
    id           short unique job ID
    type         "solver"
    input_path   UNC path to the .gsz                e.g. \\rpi\storage\active\abc__G_00000.gsz
    output_dir   UNC path to write results into       e.g. \\rpi\storage\results\
    filename     original .gsz filename               e.g. G_00000.gsz

Optional job keys (from submit.ps1 or job JSON):
    mesh_edge        float, override global element size (project units)
    skip_enrich      bool, skip V2 enrichment if True
    bundle           bool, default True — wrap outputs into one .tar.gz
    keep_solved_gsz  bool, default True — include the solved .gsz in the bundle

Per-template wall time: typically 3–5 min for the canonical 15-analysis
chain at edge=5 ft. Driven entirely by the gsi solver — this handler adds
~5 s of overhead for stage-in + extract + enrich + stage-out.

Result dict (stored by coordinator, visible via status.ps1):
    status                "ok" | "fail"
    filename              echoed input
    bundle                bundle path on the RPi share (if bundled)
    outputs               list of artifact filenames written to output_dir
    stage_in_s            seconds spent copying .gsz from UNC
    solve_s               seconds inside SolveAnalyses
    extract_s             seconds in parquet extraction
    enrich_s              seconds in V2 sidecar emission (0 if skipped)
    bundle_s              seconds tarring + staging back
    elapsed_s             total handler runtime
    error                 error message string (only when status="fail")
"""

import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import traceback
from pathlib import Path


# ── Where the GeoStudio skill scripts live on each worker ──────────────
# Patch this to match how you've deployed the skill on the worker PCs.
# Either a fixed path (e.g. C:\geostudio_automation\scripts) or, if you
# install via Claude Code, the user-skills path below.
GEO_SCRIPTS_DIR = os.environ.get(
    "GEO_SCRIPTS_DIR",
    str(Path.home() / ".claude" / "skills" / "geostudio_automation" / "scripts"),
)


def _ensure_skill_path():
    """Make solve_and_extract / enrich_gauss importable from this handler."""
    p = Path(GEO_SCRIPTS_DIR)
    if not p.is_dir():
        raise RuntimeError(
            f"GEO_SCRIPTS_DIR not found: {p}. "
            f"Set the GEO_SCRIPTS_DIR env var on each worker (or edit this "
            f"file) to point at the geostudio_automation/scripts/ directory."
        )
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)


# ── Main entrypoint ────────────────────────────────────────────────────

def run(job: dict) -> dict:
    t0 = time.perf_counter()
    timings: dict[str, float] = {
        "stage_in_s": 0.0, "solve_s": 0.0, "extract_s": 0.0,
        "enrich_s": 0.0, "bundle_s": 0.0,
    }

    filename = job["filename"]
    input_unc = job["input_path"]
    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    # Job options (with sensible defaults)
    mesh_edge: float | None = job.get("mesh_edge")
    skip_enrich: bool = bool(job.get("skip_enrich", False))
    bundle: bool = bool(job.get("bundle", True))
    keep_solved_gsz: bool = bool(job.get("keep_solved_gsz", True))

    try:
        _ensure_skill_path()
        import solve_and_extract as se
        if not skip_enrich:
            import enrich_gauss as eg
        else:
            eg = None
    except Exception as e:
        return _fail(filename, f"import failed: {e}\n{traceback.format_exc(limit=4)}",
                     t0, timings)

    with tempfile.TemporaryDirectory(prefix="gs_solver_") as tmp:
        tmp = Path(tmp)

        # ── 1. Stage in ──────────────────────────────────────────────
        t = time.perf_counter()
        local_gsz = tmp / filename
        try:
            shutil.copy2(input_unc, local_gsz)
        except Exception as e:
            return _fail(filename, f"stage-in copy failed: {e}", t0, timings)
        timings["stage_in_s"] = round(time.perf_counter() - t, 2)

        stem = local_gsz.stem

        # ── 2. Solve (auto-mesh via gsi.Set dirty-bit) ───────────────
        t = time.perf_counter()
        try:
            ok, solve_errs = se.solve_gsz(local_gsz, mesh_edge=mesh_edge)
        except Exception as e:
            return _fail(filename, f"solve exception: {e!r}\n"
                                   f"{traceback.format_exc(limit=4)}",
                         t0, timings)
        timings["solve_s"] = round(time.perf_counter() - t, 2)
        if not ok:
            return _fail(filename, "solve failed: " + " | ".join(solve_errs),
                         t0, timings)

        # ── 3. Extract parquet + meta ────────────────────────────────
        t = time.perf_counter()
        try:
            df, meta = se.extract_all(local_gsz)
            pq_path = local_gsz.with_suffix(".parquet")
            meta_path = local_gsz.with_name(f"{stem}_meta.json")
            df.to_parquet(pq_path, index=False)
            meta_path.write_text(json.dumps(meta, indent=2))
        except Exception as e:
            return _fail(filename, f"extract failed: {e!r}\n"
                                   f"{traceback.format_exc(limit=4)}",
                         t0, timings)
        timings["extract_s"] = round(time.perf_counter() - t, 2)

        # ── 4. V2 enrichment sidecars (non-fatal if it fails) ────────
        enrich_errors: list[str] = []
        if eg is not None:
            t = time.perf_counter()
            try:
                frames = eg.enrich_one(local_gsz, write_parquet=False,
                                       validate=False)
                for tag, frame in frames.items():
                    if frame is None or len(frame) == 0:
                        continue
                    side = local_gsz.with_name(f"{stem}_{tag}.parquet")
                    frame.to_parquet(side, index=False)
            except Exception as e:
                enrich_errors.append(f"enrich: {e!r}")
            timings["enrich_s"] = round(time.perf_counter() - t, 2)

        # ── 5. Bundle + stage out ────────────────────────────────────
        t = time.perf_counter()
        outputs: list[str] = []

        if bundle:
            bundle_local = tmp / f"{stem}.tar.gz"
            try:
                with tarfile.open(bundle_local, mode="w:gz", compresslevel=6) as tar:
                    for p in sorted(tmp.iterdir()):
                        if p == bundle_local:
                            continue
                        if p.name == filename and not keep_solved_gsz:
                            continue
                        tar.add(p, arcname=p.name)
                bundle_unc = output_dir / bundle_local.name
                shutil.copy2(bundle_local, bundle_unc)
                outputs.append(bundle_local.name)
            except Exception as e:
                return _fail(filename, f"bundle failed: {e!r}", t0, timings)
        else:
            # Stage each artifact individually
            try:
                for p in sorted(tmp.iterdir()):
                    if p.name == filename and not keep_solved_gsz:
                        continue
                    shutil.copy2(p, output_dir / p.name)
                    outputs.append(p.name)
            except Exception as e:
                return _fail(filename, f"stage-out failed: {e!r}", t0, timings)
        timings["bundle_s"] = round(time.perf_counter() - t, 2)

    elapsed = round(time.perf_counter() - t0, 2)
    return {
        "status": "ok",
        "filename": filename,
        "outputs": outputs,
        "bundle": str(output_dir / f"{stem}.tar.gz") if bundle else "",
        "enrich_warnings": enrich_errors,
        "elapsed_s": elapsed,
        **timings,
    }


# ── Helpers ────────────────────────────────────────────────────────────

def _fail(filename: str, msg: str, t0: float, timings: dict) -> dict:
    return {
        "status": "fail",
        "filename": filename,
        "error": msg[:2000],
        "elapsed_s": round(time.perf_counter() - t0, 2),
        **timings,
    }
