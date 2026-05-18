"""
Generate a manifest from a folder of .gsz files.

Usage:
  python generate_manifest.py ..\raw
  python generate_manifest.py ..\raw -o ..\manifests\batch.yaml
"""

import argparse
from pathlib import Path
import yaml
from config import CFG


def generate(folder, output, queue, timeout, mesh_edge):
    folder = Path(folder).resolve()
    gsz_files = sorted(folder.glob("*.gsz"))

    if not gsz_files:
        print(f"No .gsz files in {folder}")
        return

    manifest = {
        "project": folder.name,
        "queue": queue,
        "type": "geostudio",
        "defaults": {
            "timeout_minutes": timeout,
            "mesh_edge": mesh_edge,
        },
        "jobs": [{"id": g.stem, "gsz_path": str(g)} for g in gsz_files],
    }

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)

    print(f"Manifest: {out}")
    print(f"Jobs: {len(manifest['jobs'])}")
    print(f"\nSubmit: python submit_manifest.py {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("folder", help="Folder with .gsz files")
    p.add_argument("-o", "--output",
                   default=str(CFG.manifests_dir / "generated.yaml"))
    p.add_argument("--queue", default="cpu")
    p.add_argument("--timeout", type=int, default=CFG.default_timeout)
    p.add_argument("--mesh-edge", type=float, default=CFG.default_mesh_edge)
    a = p.parse_args()
    generate(a.folder, a.output, a.queue, a.timeout, a.mesh_edge)
