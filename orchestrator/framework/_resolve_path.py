"""
Resolve a local path to its UNC form if it's covered by a current SMB share
on this PC. Used by make_manifest.bat so manifests emit UNC paths
automatically in multi-worker setups.

Usage:
    python _resolve_path.py "Z:\raw"
Prints either:
    \\HOST\sharename\raw     (if the path is covered by an SMB share)
    Z:\raw                   (unchanged, if no share covers it)

Detection is best-effort via `net share`. If parsing fails, prints the
input unchanged - falls back to single-machine local-path mode.
"""
import socket
import subprocess
import sys
from pathlib import Path


def detect_share_for(target: Path) -> str:
    """Return a UNC string for `target` if some SMB share on this PC
    covers it (target == share root or target is a subpath of share root).
    Otherwise return the input target as a plain string.
    """
    try:
        result = subprocess.run(
            ["net", "share"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return str(target)

    if result.returncode != 0:
        return str(target)

    target_resolved = target.resolve()
    hostname = socket.gethostname()

    shares = []
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # net share output: "<sharename>    <local-path>    <remark>"
        # Skip header / footer lines.
        if line.startswith(("Share name", "-----", "The command")):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        share_name, share_path = parts[0], parts[1]
        # Skip admin shares (IPC$, ADMIN$, C$, etc.) - they end with $
        if share_name.endswith("$"):
            continue
        # Skip non-filesystem shares (e.g. printers)
        if not (len(share_path) >= 2 and share_path[1] == ":"):
            continue
        try:
            shares.append((share_name, Path(share_path).resolve()))
        except Exception:
            continue

    # Prefer the most specific (longest) matching share root
    shares.sort(key=lambda x: len(str(x[1])), reverse=True)

    for share_name, share_root in shares:
        try:
            rel = target_resolved.relative_to(share_root)
        except ValueError:
            continue
        rel_str = str(rel)
        if rel_str in (".", ""):
            return f"\\\\{hostname}\\{share_name}"
        return f"\\\\{hostname}\\{share_name}\\{rel_str}"

    return str(target)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python _resolve_path.py <local-path>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1])
    if not target.exists():
        print(str(target))  # let downstream caller surface the not-found error
        sys.exit(0)
    print(detect_share_for(target))
