"""Describe the installed compiler environment inside the benchmark container."""

import importlib.metadata
import json
import platform
import sys
from pathlib import Path


def identity(provenance_path=Path("/opt/source-provenance.json")):
    """Exclude model/ActivitySim settings: Sharrow hashes generated flow contents."""
    import llvmlite.binding as llvm

    names = ("sharrow", "numba", "llvmlite", "numpy")
    provenance = json.loads(provenance_path.read_text())
    return {
        "schema_version": 1,
        "python": [sys.implementation.name, *sys.version_info[:3]],
        "machine": platform.machine(),
        "system": platform.system(),
        "libc": platform.libc_ver(),
        "cpu": str(llvm.get_host_cpu_name()),
        "cpu_features": llvm.get_host_cpu_features().flatten(),
        "versions": {name: importlib.metadata.version(name) for name in names},
        "sources": {
            item["name"]: {
                key: item.get(key)
                for key in ("repository", "resolved_commit", "subdirectory")
            }
            for item in provenance
            if item["name"] in names
        },
    }


if __name__ == "__main__":
    print(json.dumps(identity(), sort_keys=True))
