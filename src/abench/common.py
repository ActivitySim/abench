"""Small artifact helpers shared by the host tools."""

import json


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
