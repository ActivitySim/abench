"""Real artifact generation and simulated GitHub transport; never post in tests."""

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from abench import cli, publishing
from abench.common import read_json, write_json

TARGET = {"repository": "ActivitySim/activitysim", "pr": 12, "baseline": "main"}
SNAPSHOT = {"headRefOid": "b" * 40, "baseRefOid": "a" * 40, "number": 12}
URL = "https://github.com/ActivitySim/activitysim/pull/12#issuecomment-123"


def record(path, *, seconds=10, valid=True, households=4):
    phase = path / "measured"
    phase.mkdir(parents=True)
    write_json(
        path / "experiment.json",
        {
            "schema_version": 2,
            "label": path.name,
            "households": households,
            "sources": [
                {
                    "name": "activitysim",
                    "repository": TARGET["repository"],
                    "commit": "a" * 40,
                }
            ],
        },
    )
    write_json(
        phase / "status.json",
        {"returncode": 0 if valid else 1, "elapsed_seconds": seconds},
    )
    write_json(phase / "docker-state.json", {"ExitCode": 0 if valid else 1})
    write_json(phase / "output-summary.json", {"households": {"rows": households}})
    (phase / "components-1.jsonl").write_text(
        json.dumps({"component": "work", "seconds": seconds / 2, "succeeded": True})
        + "\n"
    )
    (phase / "memory.csv").write_text(
        "elapsed_seconds,current_bytes,peak_bytes\n0,536870912,2147483648\n5,1073741824,2147483648\n10,805306368,2147483648\n"
    )


def saved_suite(tmp_path, *, valid=True):
    record(tmp_path / "main")
    record(tmp_path / "candidate", seconds=8, valid=valid)
    write_json(
        tmp_path / "suite.json",
        {
            "configuration": {"publish": {"github": TARGET}},
            "publication_pr": SNAPSHOT,
            "runs": [
                {"name": name, "output_dir": str(tmp_path / name)}
                for name in ("main", "candidate", "unstarted")
            ],
        },
    )
    return tmp_path


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"github": None},
        {"github": {**TARGET, "pr": True}},
        {"github": {**TARGET, "pr": 0}},
        {"github": {**TARGET, "repository": "https://github.com/a/b"}},
        {"github": {**TARGET, "baseline": "absent"}},
        {"github": {**TARGET, "baseline": []}},
        {"github": {**TARGET, "token": "secret"}},
    ],
)
def test_invalid_config(config):
    with pytest.raises(ValueError):
        publishing.configuration(config, ["main"])


def test_bundle_uses_real_results(tmp_path, monkeypatch):
    root = saved_suite(tmp_path)
    monkeypatch.setattr(
        publishing, "gh", lambda *a: pytest.fail("dry-run contacted GitHub")
    )
    assert cli.main(["publish", str(root), "--dry-run"]) == 0
    bundle = root / "publication"
    body = (bundle / "comment.md").read_text()
    assert "-20.0%" in body and "NOT RUN / NO RESULTS" in body
    assert SNAPSHOT["headRefOid"] in body and "a" * 40 in body
    for name in ("memory.svg", "runtimes.svg"):
        svg = ET.parse(bundle / name).getroot()
        assert svg.tag == "{http://www.w3.org/2000/svg}svg"
        nested = svg.findall(".//{http://www.w3.org/2000/svg}svg")
        assert all("height" in element.attrib for element in nested)
    before = read_json(bundle / "state.json")["id"]
    publishing.publish(root, dry_run=True)
    assert read_json(bundle / "state.json")["id"] == before


def test_invalid_runs_excluded(tmp_path):
    root = saved_suite(tmp_path, valid=False)
    bundle, _ = publishing.build_bundle(root)
    body = (bundle / "comment.md").read_text()
    assert "FAILED / INVALID" in body
    assert "-20.0%" not in body
    assert "candidate" not in (bundle / "runtimes.svg").read_text()
    assert "candidate" in (bundle / "memory.svg").read_text()


def test_different_settings_suppress_delta(tmp_path):
    root = saved_suite(tmp_path)
    path = root / "candidate/experiment.json"
    spec = read_json(path)
    spec["multiprocess"] = True
    write_json(path, spec)
    bundle, _ = publishing.build_bundle(root)
    assert "-20.0%" not in (bundle / "comment.md").read_text()


def fake_github(monkeypatch, *, comments=None, post_error=False, changed=False):
    calls = []
    monkeypatch.setattr(
        publishing,
        "preflight",
        lambda config: {**SNAPSHOT, "headRefOid": "c" * 40} if changed else SNAPSHOT,
    )

    def gh(*args, **kwargs):
        calls.append(args)
        if args[0] == "api":
            return json.dumps([comments or []])
        assert args[:2] == ("pr", "comment")
        if post_error:
            raise ValueError("upload unavailable")
        return URL

    monkeypatch.setattr(publishing, "gh", gh)
    return calls


def test_post_and_repeat_are_idempotent(tmp_path, monkeypatch):
    root = saved_suite(tmp_path)
    calls = fake_github(monkeypatch, changed=True)
    assert publishing.publish(root) == URL
    assert publishing.publish(root) == URL
    posts = [c for c in calls if c[0] == "pr"]
    assert len(posts) == 1
    assert posts[0].count("--attach") == 2
    upload = Path(posts[0][posts[0].index("--body-file") + 1]).read_text()
    assert "PR head has changed" in upload
    assert "./memory.svg" in upload
    state = read_json(root / "publication/state.json")
    assert state["comment_id"] == 123 and state["status"] == "published"


def test_retry_recovers_lost_response(tmp_path, monkeypatch):
    root = saved_suite(tmp_path)
    fake_github(monkeypatch, post_error=True)
    with pytest.raises(ValueError, match="Results retained"):
        publishing.publish(root)
    state = read_json(root / "publication/state.json")
    assert state["status"] == "failed"
    calls = fake_github(
        monkeypatch,
        comments=[
            {"id": 123, "html_url": URL, "body": f"<!-- abench:{state['id']} -->"}
        ],
    )
    assert publishing.publish(root) == URL
    assert all(c[0] == "api" for c in calls)


def test_retry_after_upload_failure(tmp_path, monkeypatch):
    root = saved_suite(tmp_path)
    fake_github(monkeypatch, post_error=True)
    with pytest.raises(ValueError):
        publishing.publish(root)
    fake_github(monkeypatch)
    assert publishing.publish(root) == URL


@pytest.mark.parametrize("attach,permission", [(False, "true"), (True, "false")])
def test_preflight_rejects_unsupported_or_unauthorized(monkeypatch, attach, permission):
    def gh(*args):
        return (
            ("--attach" if attach else "old gh") if args[-1] == "--help" else permission
        )

    monkeypatch.setattr(publishing, "gh", gh)
    with pytest.raises(ValueError):
        publishing.preflight(TARGET)


def test_empty_suite_bundle(tmp_path):
    root = saved_suite(tmp_path)
    plan = read_json(root / "suite.json")
    plan["runs"] = [{"name": "main", "output_dir": str(tmp_path / "missing")}]
    write_json(root / "suite.json", plan)
    bundle, _ = publishing.build_bundle(root)
    assert "No valid runtime results" in (bundle / "runtimes.svg").read_text()
    assert "NOT RUN" in (bundle / "comment.md").read_text()


def test_suite_preflight_precedes_assets_and_execution(tmp_path, monkeypatch):
    from test_experiments import suite

    from abench import experiments

    path = suite(tmp_path, publish={"github": TARGET})
    monkeypatch.setattr(
        publishing,
        "preflight",
        lambda _: (_ for _ in ()).throw(ValueError("auth failed")),
    )
    monkeypatch.setattr(
        experiments,
        "prepare_assets",
        lambda _: pytest.fail("prepared assets before preflight"),
    )
    with pytest.raises(ValueError, match="auth failed"):
        experiments.run_suite(path, lambda _: pytest.fail("ran without authentication"))


@pytest.mark.parametrize("dry_run,code", [(False, 0), (True, 0), (False, 7)])
def test_suite_finalizes_before_publishing(tmp_path, monkeypatch, dry_run, code):
    from test_experiments import suite

    from abench import experiments

    path = suite(tmp_path, publish={"github": TARGET})
    events = []
    monkeypatch.setattr(
        publishing, "preflight", lambda _: events.append("preflight") or SNAPSHOT
    )
    monkeypatch.setattr(experiments, "report", lambda *_: events.append("report"))

    def invoke(args):
        if args[0] == "run":
            output = Path(args[args.index("--output-dir") + 1])
            record(output, valid=not code)
            events.append("run")
            return code
        return 0

    def post(root, **kwargs):
        assert events[-1] == "report"
        assert kwargs["dry_run"] == dry_run
        events.append("publish")
        assert read_json(root / "suite.json").get("publication_pr") == (
            None if dry_run else SNAPSHOT
        )

    monkeypatch.setattr(publishing, "publish", post)
    assert experiments.run_suite(path, invoke, publish_dry_run=dry_run) == code
    assert ("preflight" in events) != dry_run
    assert events[-1] == "publish"


@pytest.mark.parametrize("code", [0, 7])
def test_posting_failure_preserves_benchmark_failure(
    tmp_path, monkeypatch, capsys, code
):
    from test_experiments import suite

    from abench import experiments

    path = suite(tmp_path, publish={"github": TARGET})
    monkeypatch.setattr(publishing, "preflight", lambda _: SNAPSHOT)
    monkeypatch.setattr(
        publishing,
        "publish",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("upload failed")),
    )
    if code:
        assert (
            experiments.run_suite(path, lambda args: code if args[0] == "run" else 0)
            == code
        )
        assert "Publication failed" in capsys.readouterr().out
    else:
        with pytest.raises(ValueError, match="upload failed"):
            experiments.run_suite(path, lambda _: 0)


def test_transport_uses_argv_and_bundle_directory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(
        publishing.subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append((args, kwargs)) or SimpleNamespace(stdout=URL)
        ),
    )
    assert publishing.gh("pr", "comment", "12", cwd=tmp_path) == URL
    args, kwargs = calls[0]
    assert args == ["gh", "pr", "comment", "12"]
    assert kwargs["cwd"] == tmp_path and kwargs["check"]
    assert not kwargs.get("shell")


def test_concurrent_publication_is_blocked(tmp_path, monkeypatch):
    import fcntl

    root = saved_suite(tmp_path)
    monkeypatch.setattr(
        publishing,
        "gh",
        lambda *a: pytest.fail("concurrent publisher contacted GitHub"),
    )
    with (root / ".publication.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="already running"):
            publishing.publish(root)


def test_comment_labels_cannot_inject_markdown():
    text = publishing.cell(
        "![unexpected](https://example.test/img) @someone | <script>\nnext"
    )
    assert "![" not in text and "@someone" not in text
    assert "<script>" not in text and "\n" not in text and "|" not in text
