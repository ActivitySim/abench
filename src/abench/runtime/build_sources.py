"""Build exact source checkouts into wheels and resolve them together in Docker."""

import email
import importlib.metadata
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path


def command(args):
    """Use argument arrays throughout; profile values are never shell fragments."""
    return subprocess.check_output(args, text=True).strip()


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def install(manifest, directory=Path("/opt")):
    """Verify Git objects and wheel identities before installing all overrides."""
    directory.mkdir(parents=True, exist_ok=True)
    constraints = directory / "constraints.txt"
    constraints.write_text("\n".join(manifest.get("constraints", [])) + "\n")
    wheels = []
    provenance = []
    for item in manifest["sources"]:
        checkout = directory / "sources" / item["name"]
        checkout.mkdir(parents=True)
        command(["git", "init", str(checkout)])
        url = f"https://github.com/{item['repository']}.git"
        command(["git", "-C", str(checkout), "remote", "add", "origin", url])
        command(
            ["git", "-C", str(checkout), "fetch", "--tags", "origin", item["commit"]]
        )
        command(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"])
        resolved = command(["git", "-C", str(checkout), "rev-parse", "HEAD"])
        if resolved != item["commit"]:
            raise ValueError(f"Git commit mismatch for {item['name']}")
        package = (checkout / item.get("subdirectory", "")).resolve()
        if not package.is_relative_to(checkout.resolve()):
            raise ValueError("package subdirectory escapes checkout")
        wheel_dir = directory / "wheels" / item["name"]
        command(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(wheel_dir),
                str(package),
            ]
        )
        built = list(wheel_dir.glob("*.whl"))
        if len(built) != 1:
            raise ValueError(f"expected one wheel for {item['name']}")
        with zipfile.ZipFile(built[0]) as archive:
            metadata_path = next(
                n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
            )
            metadata = email.message_from_bytes(archive.read(metadata_path))
        if normalized(metadata["Name"]) != item["name"]:
            raise ValueError(
                f"wrong distribution: expected {item['name']}, got {metadata['Name']}"
            )
        extras = "[" + ",".join(item["extras"]) + "]" if item.get("extras") else ""
        wheels.append(str(built[0]) + extras)
        provenance.append(
            dict(item, version=metadata["Version"], resolved_commit=resolved)
        )
    # Supplying all local wheels in one transaction prevents dependency resolution
    # from quietly replacing a requested source package with a registry release.
    requirements = ["pyyaml", "pyarrow", *manifest.get("requirements", [])]
    command(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "-c",
            str(constraints),
            *wheels,
            *requirements,
        ]
    )
    command([sys.executable, "-m", "pip", "check"])
    for item in provenance:
        dist = importlib.metadata.distribution(item["name"])
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        expected = (directory / "wheels" / item["name"]).resolve().as_uri() + "/"
        if dist.version != item["version"] or not direct.get("url", "").startswith(
            expected
        ):
            raise ValueError(f"installed source identity mismatch: {item['name']}")
    (directory / "source-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    (directory / "pip-freeze.txt").write_text(
        command([sys.executable, "-m", "pip", "freeze"]) + "\n"
    )


if __name__ == "__main__":
    install(json.loads(Path(sys.argv[1]).read_text()))
