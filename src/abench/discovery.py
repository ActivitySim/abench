"""Discover and choose experiment instructions stored inside a model directory."""

from .menus import choose


def instruction_files(directory):
    """List immediate YAML files deterministically without loading any suite."""
    folder = directory.expanduser() / ".abench"
    if not folder.is_dir():
        raise ValueError(f"No experiment directory found: {folder}")
    files = sorted(
        (
            p
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
        ),
        key=lambda p: (p.name.casefold(), p.name),
    )
    if not files:
        raise ValueError(f"No YAML experiment files found in {folder}")
    return files


def directory_help(directory):
    """Help lists available files without requiring an interactive selection."""
    files = instruction_files(directory)
    return (
        "Available experiments:\n"
        + "\n".join(f"  {p.name}" for p in files)
        + (
            "\n\nRun this directory to choose an experiment, or pass a file directly."
            "\nRelative paths in each experiment are relative to its YAML file."
        )
    )


def select_experiment(path, interactive):
    """Choose a suite before resolving its inputs, sources, or data assets."""
    path = path.expanduser()
    if not path.is_dir():
        return path
    files = instruction_files(path)
    if not interactive:
        if len(files) == 1:
            return files[0]
        raise ValueError(
            f"Multiple experiments found in {path / '.abench'}: "
            + ", ".join(p.name for p in files)
            + ". Run in a terminal to choose, or pass the experiment YAML path directly."
        )
    selected = choose(
        f"Experiment in {path / '.abench'}", [file.name for file in files]
    )
    return files[selected]
