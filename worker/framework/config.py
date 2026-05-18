"""
ComputeFarm config loader.

Auto-detects the ComputeFarm root by looking at where this file lives:
  <root>/framework/config.py  ->  root = parent of framework/

All paths are derived from that root. No hardcoded drive letters.
Drop the folder anywhere and it works.

Usage:
    from config import CFG
    print(CFG.root)        # auto-detected
    print(CFG.raw_dir)     # root / "raw"
    print(CFG.redis_host)  # from config.yaml
"""

import os
import socket
from pathlib import Path

import yaml

# framework/ is where this file lives. Root is one level up.
FRAMEWORK_DIR = Path(__file__).resolve().parent
ROOT = FRAMEWORK_DIR.parent


class _Config:
    def __init__(self):
        config_path = FRAMEWORK_DIR / "config.yaml"
        if not config_path.exists():
            raise FileNotFoundError(
                f"config.yaml not found at {config_path}\n"
                "It should be next to config.py in the framework folder."
            )

        with open(config_path, "r", encoding="utf-8") as f:
            self._raw = yaml.safe_load(f)

        # Auto-detected root
        self.root = ROOT

        # Redis
        self.redis_host = self._raw["redis_host"]
        self.redis_port = int(self._raw.get("redis_port", 6379))

        # Auth: config.yaml fields are baseline, REDIS_USER/REDIS_PASSWORD env
        # vars override (useful for not putting secrets in a shared file).
        # The Pi-side ACL setup uses user `computefarm` with no password.
        self.redis_user = os.environ.get("REDIS_USER") or self._raw.get("redis_user", "") or ""
        self.redis_password = os.environ.get("REDIS_PASSWORD") or self._raw.get("redis_password", "") or ""

        if self.redis_user and self.redis_password:
            _auth = f"{self.redis_user}:{self.redis_password}@"
        elif self.redis_user:
            _auth = f"{self.redis_user}@"
        elif self.redis_password:
            _auth = f":{self.redis_password}@"
        else:
            _auth = ""

        self.redis_broker = f"redis://{_auth}{self.redis_host}:{self.redis_port}/0"
        self.redis_backend = f"redis://{_auth}{self.redis_host}:{self.redis_port}/1"

        # Folders - all relative to auto-detected root
        self.raw_dir = self.root / self._raw.get("raw_folder", "raw")
        self.solved_dir = self.root / self._raw.get("solved_folder", "solved")
        self.logs_dir = self.root / self._raw.get("logs_folder", "logs")
        self.tools_dir = self.root / self._raw.get("tools_folder", "tools")
        self.manifests_dir = self.root / self._raw.get("manifests_folder", "manifests")

        # GeoStudio tools - relative to tools_dir
        self.solve_script = str(
            self.tools_dir / self._raw.get("solve_script", "solve_gsz.ps1"))
        self.scripts_dir = str(
            self.tools_dir / self._raw.get("scripts_subdir",
                                           r"geostudio_automation\scripts"))

        # Local scratch - auto-detect if not set
        scratch = self._raw.get("local_scratch", "")
        if scratch:
            self.local_scratch = Path(scratch)
        else:
            # Create scratch next to root, on the same drive if local,
            # or on C: if root is a network path (UNC)
            if str(self.root).startswith("\\\\"):
                # Network path - use local C: drive for scratch
                self.local_scratch = Path(r"C:\ComputeFarm_scratch")
            else:
                # Local path - scratch next to root
                self.local_scratch = self.root.parent / "ComputeFarm_scratch"

        # GeoStudio defaults
        self.default_mesh_edge = float(self._raw.get("default_mesh_edge", 0))
        self.default_timeout = int(self._raw.get("default_timeout_minutes", 300))

        # Worker
        self.cpu_concurrency = int(self._raw.get("cpu_concurrency", 1))
        self.gpu_concurrency = int(self._raw.get("gpu_concurrency", 1))
        self.result_expires = int(self._raw.get("result_expires", 60 * 60 * 24 * 14))

    def print_summary(self):
        """Print resolved paths for debugging."""
        print(f"  Root        : {self.root}")
        auth_summary = "no auth"
        if self.redis_user and self.redis_password:
            auth_summary = f"user={self.redis_user} (with password)"
        elif self.redis_user:
            auth_summary = f"user={self.redis_user} (nopass)"
        elif self.redis_password:
            auth_summary = "default user (with password)"
        print(f"  Redis       : {self.redis_host}:{self.redis_port}  [{auth_summary}]")
        print(f"  Raw         : {self.raw_dir}")
        print(f"  Solved      : {self.solved_dir}")
        print(f"  Logs        : {self.logs_dir}")
        print(f"  Tools       : {self.tools_dir}")
        print(f"  Scratch     : {self.local_scratch}")
        print(f"  Solve script: {self.solve_script}")
        print(f"  Scripts dir : {self.scripts_dir}")
        print(f"  CPU conc.   : {self.cpu_concurrency}")
        print(f"  Hostname    : {socket.gethostname()}")


CFG = _Config()
