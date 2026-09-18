"""Small artifact helpers shared by the host tools."""

import json
import os
import tempfile


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    """Atomically replace metadata so Docker bind readers never see a rewrite."""
    content = json.dumps(value, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
