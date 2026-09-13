"""A verified requirement must describe an unchanged, actually passing run."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[2] / ".planning/tools"


def command(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, check=False)


def manifest(root: Path) -> dict[str, Any]:
    result = command(
        root,
        sys.executable,
        "-c",
        "import json,sys; from pathlib import Path; "
        "sys.path.insert(0,'.planning/tools'); from evidence_source import capture_source; "
        "print(json.dumps(capture_source(Path.cwd())))",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def metadata(root: Path) -> dict[str, Any]:
    return json.loads((root / ".planning/evidence/latest.json").read_text())  # type: ignore[no-any-return]


def save(root: Path, report: dict[str, Any]) -> None:
    (root / ".planning/evidence/latest.json").write_text(json.dumps(report))


def check(root: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return command(root, sys.executable, ".planning/tools/check_traceability.py", *flags)


@pytest.fixture
def evidence_repo(tmp_path: Path) -> Path:
    for directory in (".planning/tools", ".planning/evidence", "src", "tests"):
        (tmp_path / directory).mkdir(parents=True)
    for name in ("check_traceability.py", "collect_evidence.py", "evidence_source.py"):
        shutil.copyfile(TOOLS / name, tmp_path / ".planning/tools" / name)
    (tmp_path / "src/app.py").write_text("value = 1\n")
    (tmp_path / "tests/test_fake.py").write_text("def test_ok(): pass\n")
    (tmp_path / "pyproject.toml").write_text("# test configuration\n")
    (tmp_path / ".planning/requirements.yaml").write_text(
        "- id: A01\n  status: verified\n  verification:\n"
        "    - tests/test_fake.py::test_ok\n  evidence:\n"
        "    - .planning/evidence/latest.json\n"
    )
    for args in (
        ("git", "init", "-q"),
        ("git", "add", "src/app.py", "tests/test_fake.py", "pyproject.toml"),
        (
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "source",
        ),
    ):
        result = command(tmp_path, *args)
        assert result.returncode == 0, result.stderr
    junit = tmp_path / ".planning/evidence/latest-junit.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_fake" '
        'name="test_ok"/></testsuite></testsuites>'
    )
    source = manifest(tmp_path)
    save(
        tmp_path,
        {
            "git_revision": command(tmp_path, "git", "rev-parse", "HEAD").stdout.strip(),
            "source_before": source,
            "source_after": source,
            "checks": {"tests": {"result": "PASS"}},
            "test_report": {
                "path": ".planning/evidence/latest-junit.xml",
                "sha256": hashlib.sha256(junit.read_bytes()).hexdigest(),
            },
        },
    )
    return tmp_path


def test_valid_run_and_report_only_commit_remain_valid(evidence_repo: Path) -> None:
    root = evidence_repo
    assert check(root).returncode == 0
    (root / "report.md").write_text("Audit report\n")
    command(root, "git", "add", "report.md")
    result = command(
        root,
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-qm",
        "report only",
    )
    assert result.returncode == 0
    assert check(root).returncode == 0


@pytest.mark.parametrize(
    "mutation", ["empty", "malformed", "missing", "digest", "commit", "unknown_commit"]
)
def test_invalid_run_is_rejected_even_with_stale_flag(evidence_repo: Path, mutation: str) -> None:
    root = evidence_repo
    report = metadata(root)
    junit = root / ".planning/evidence/latest-junit.xml"
    if mutation == "empty":
        junit.write_text("<testsuites/>")
        report["test_report"]["sha256"] = hashlib.sha256(junit.read_bytes()).hexdigest()
    elif mutation == "malformed":
        junit.write_text("<broken")
        report["test_report"]["sha256"] = hashlib.sha256(junit.read_bytes()).hexdigest()
    elif mutation == "missing":
        junit.unlink()
    elif mutation == "digest":
        junit.write_text(junit.read_text().replace("test_ok", "test_other"))
    elif mutation == "commit":
        report.pop("git_revision")
    else:
        report["git_revision"] = "f" * 40
    save(root, report)
    result = check(root, "--allow-stale-run")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "tests/test_fake.py",
        "uv.lock",
        "Dockerfile",
        "pyproject.toml",
        ".planning/tools/extra.py",
        "src/new.py",
    ],
)
def test_current_content_changes_invalidate_run(evidence_repo: Path, path: str) -> None:
    (evidence_repo / path).write_text("# changed content\n")
    assert check(evidence_repo).returncode == 1


def test_same_dirty_status_different_content_is_rejected(evidence_repo: Path) -> None:
    root = evidence_repo
    source_file = root / "src/app.py"
    source_file.write_text("value = 2\n")
    report = metadata(root)
    report["source_before"] = report["source_after"] = manifest(root)
    save(root, report)
    assert check(root).returncode == 0
    status = command(root, "git", "status", "--porcelain").stdout
    source_file.write_text("value = 3\n")
    assert command(root, "git", "status", "--porcelain").stdout == status
    assert check(root).returncode == 1


def test_mid_run_change_is_rejected_even_if_current_matches_after(evidence_repo: Path) -> None:
    report = metadata(evidence_repo)
    (evidence_repo / "src/app.py").write_text("value = 99\n")
    report["source_after"] = manifest(evidence_repo)
    save(evidence_repo, report)
    result = check(evidence_repo)
    assert result.returncode == 1
    assert "во время прогона" in result.stdout


def test_failed_or_skipped_outcome_cannot_verify(evidence_repo: Path) -> None:
    root = evidence_repo
    for outcome in ("failure", "error", "skipped"):
        junit = root / ".planning/evidence/latest-junit.xml"
        junit.write_text(
            '<testsuites><testsuite><testcase classname="tests.test_fake" '
            f'name="test_ok"><{outcome}/></testcase></testsuite></testsuites>'
        )
        report = metadata(root)
        report["test_report"]["sha256"] = hashlib.sha256(junit.read_bytes()).hexdigest()
        save(root, report)
        assert check(root).returncode == 1


@pytest.mark.parametrize("mutation", ["changed_source", "missing_xml"])
def test_collector_rejects_mutated_source_and_never_reuses_old_xml(
    evidence_repo: Path, mutation: str
) -> None:
    root = evidence_repo
    driver = (
        "import sys; from pathlib import Path; sys.path.insert(0,'.planning/tools'); "
        "import collect_evidence as collector\n"
        "def fake_run(command, extra_env=None):\n"
        "    if command[0] == 'git':\n"
        "        return 0, '" + metadata(root)["git_revision"] + "'\n"
        "    if command[0].endswith('pytest'):\n"
        "        if sys.argv[1] == 'changed_source':\n"
        "            Path('src/app.py').write_text('value = 17\\n')\n"
        "            Path(collector.JUNIT_NAME).write_text('<testsuites/>')\n"
        "    return 0, 'fake check passed'\n"
        "collector.run = fake_run\n"
        "raise SystemExit(collector.main())\n"
    )
    result = command(root, sys.executable, "-c", driver, mutation)
    assert result.returncode == 1, result.stdout + result.stderr
    report = metadata(root)
    assert report["overall"] == "FAIL"
    if mutation == "changed_source":
        assert report["source_before"] != report["source_after"]
    else:
        assert report["test_report"]["sha256"] is None
        assert not (root / ".planning/evidence/latest-junit.xml").exists()
