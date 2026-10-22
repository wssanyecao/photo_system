"""版本一致性与公开资料隔离的最小质量门禁。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from photo_gateway import __version__
from photo_gateway.config import load_yaml_text
from photo_gateway.http import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_software_version_follows_semver_without_prefix():
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", __version__)


def test_cli_matches_package_version():
    result = subprocess.run(
        [sys.executable, "-m", "photo_gateway", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"photo-gateway {__version__}"


def test_api_metadata_and_health_match_package_version(tmp_path):
    cfg = load_yaml_text(
        "version: 1\n"
        "auth:\n  enabled: false\n"
        "devices:\n  - id: example\n    name: Demo\n"
        f"storage:\n  root: {tmp_path}\n"
    )
    app = create_app(cfg)
    assert app.title == "Photo Gateway"
    assert app.version == __version__
    response = TestClient(app).get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["data"]["version"] == __version__


def test_internal_files_are_ignored_by_git_and_docker():
    git_excludes = [
        "AGENTS.md", ".env", "config/config.yaml", "docs/HANDOFF.md",
        "DEVELOPMENT.md", "ENVIRONMENT.md", "deploy/docker-host/load_start.sh",
    ]
    for name in git_excludes:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", name],
            cwd=ROOT,
        )
        assert result.returncode == 0, f"Git should ignore {name}"
    docker_ignores = set((ROOT / ".dockerignore").read_text().splitlines())
    for pattern in (
        "/AGENTS.md", "/.env", "/docs/HANDOFF.md", "/deploy/docker-host/",
        "/ENVIRONMENT.md", "/DEVELOPMENT.md", "/secrets/",
    ):
        assert pattern in docker_ignores, f"Docker should ignore {pattern}"
