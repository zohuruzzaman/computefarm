"""Worker-side helpers — mesh + solve only. NO extraction.

This is a slim drop-in for the worker PCs in the ComputeFarm. The
`solve_gsz.ps1` wrapper imports `discover_chain` and `_force_remesh` from
this module by name; everything else (parquet extraction, FoS rollup,
node/element CSV harvesting) deliberately lives elsewhere and runs on
the storage PC over the `solved/*.gsz` files after the farm finishes.

Worker contract:
    raw/*.gsz  --copy-->  local scratch  --mesh+solve+save-->  copy back to solved/

That's it. No parquet, no CSVs leave the .gsz.

Dependencies: stdlib + the gsi wheel only. No pandas / pyarrow / plyfile.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from gsi.protobuf import gsi_project_pb2 as _gsi_pb
from google.protobuf.struct_pb2 import Value as _gsi_Value


MESH_EDGE_OBJ = "CurrentAnalysis.GeometryItems.Mesh.DisplayMeshDefaultEdgeLength"


def discover_chain(gsz_path: Path) -> list[str]:
    """Return analysis names in dependency order (parent first)."""
    with zipfile.ZipFile(gsz_path) as z:
        xml_name = next(n for n in z.namelist() if n.endswith(".xml") and "/" not in n)
        xml = z.read(xml_name).decode("utf-8")
    analyses = []
    for m in re.finditer(
        r'<Analysis>\s*<ID>(\d+)</ID>\s*<Name>([^<]+)</Name>\s*<Kind>[^<]+</Kind>'
        r'(?:\s*<ParentID>(\d+)</ParentID>)?',
        xml,
    ):
        analyses.append((int(m.group(1)), m.group(2),
                         int(m.group(3)) if m.group(3) else None))
    by_id = {aid: (name, pid) for aid, name, pid in analyses}
    ordered: list[str] = []
    seen: set[int] = set()

    def emit(aid: int):
        if aid in seen:
            return
        name, pid = by_id[aid]
        if pid is not None and pid in by_id:
            emit(pid)
        seen.add(aid)
        ordered.append(name)

    for aid, _name, _pid in analyses:
        emit(aid)
    return ordered


def _force_remesh(proj, analysis_name: str, edge_override: float | None = None) -> float:
    """Force the gsi server to mark the mesh dirty so SolveAnalyses regenerates
    mesh_1.ply. Required when the .gsz has no mesh_1.ply yet (fresh corpus output).

    A value-changing Set on DisplayMeshDefaultEdgeLength flips the in-memory
    dirty bit; a no-op Set does not. Either set to a user-supplied target
    (edge_override) or toggle current -> current+epsilon -> current.

    Returns the final edge length value (in the project's length unit, e.g. ft).
    """
    resp = proj.Get(_gsi_pb.GetRequest(analysis=analysis_name, object=MESH_EDGE_OBJ))
    cur = float(resp.data.struct_value["Value"])

    if edge_override is not None and abs(edge_override - cur) > 1e-9:
        proj.Set(_gsi_pb.SetRequest(
            analysis=analysis_name, object=MESH_EDGE_OBJ,
            data=_gsi_Value(number_value=float(edge_override))))
        return float(edge_override)

    eps = max(abs(cur) * 1e-6, 1e-6)
    proj.Set(_gsi_pb.SetRequest(
        analysis=analysis_name, object=MESH_EDGE_OBJ,
        data=_gsi_Value(number_value=cur + eps)))
    proj.Set(_gsi_pb.SetRequest(
        analysis=analysis_name, object=MESH_EDGE_OBJ,
        data=_gsi_Value(number_value=cur)))
    return cur
