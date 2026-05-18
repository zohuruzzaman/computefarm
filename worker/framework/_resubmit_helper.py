"""
Helper for resubmit.bat: build a manifest of raw/ files that are
NOT yet present in solved/.

Usage:
    python _resubmit_helper.py <raw_dir> <solved_dir> <output_yaml>
"""
import sys
from pathlib import Path

import yaml

from config import CFG
from _resolve_path import detect_share_for


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)

    raw_dir = Path(sys.argv[1]).resolve()
    solved_dir = Path(sys.argv[2]).resolve()
    out_path = Path(sys.argv[3])

    raw = {p.stem: p for p in raw_dir.glob("*.gsz")}
    solved = {p.stem: p for p in solved_dir.glob("*.gsz")
              if not p.stem.endswith("_PARTIAL")}

    missing = sorted(stem for stem in raw if stem not in solved)
    done = sorted(stem for stem in raw if stem in solved)

    print(f"  raw/    : {len(raw):>4} .gsz files")
    print(f"  solved/ : {len(solved):>4} .gsz files")
    print(f"  done    : {len(done):>4} -> skipping")
    print(f"  missing : {len(missing):>4} -> resubmitting")

    if not missing:
        print()
        print("Nothing to resubmit. Everything in raw/ already has a solved/ twin.")
        sys.exit(0)

    # Resolve raw_dir to its UNC form if a share covers it - so jobs
    # are reachable from EVERY worker, not just this one.
    raw_unc = detect_share_for(raw_dir)
    if raw_unc != str(raw_dir):
        print(f"  share detected -> using UNC path: {raw_unc}")
        gsz_path_for = lambda stem: f"{raw_unc}\\{stem}.gsz"
    else:
        print(f"  no share -> using local paths (single-machine mode)")
        gsz_path_for = lambda stem: str(raw[stem])

    manifest = {
        "project": "resubmit",
        "queue": "cpu",
        "type": "geostudio",
        "defaults": {
            "timeout_minutes": CFG.default_timeout,
            "mesh_edge": float(CFG.default_mesh_edge),
        },
        "jobs": [
            {"id": stem, "gsz_path": gsz_path_for(stem)} for stem in missing
        ],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)

    print()
    print(f"Wrote {out_path} ({len(missing)} jobs)")


if __name__ == "__main__":
    main()
