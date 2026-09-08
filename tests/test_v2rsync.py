"""真实 RsyncNas adapter 测试：命令构造/禁 --delete/退出码。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import yaml as _y

from photo_gateway.config import load_yaml_text
from photo_gateway.v2rsync_transport import RsyncNas


def _sync_cfg(enabled=True):
    return load_yaml_text(_y.safe_dump({
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "a"}], "storage": {"root": "/tmp/pg"},
        "sync": {"enabled": enabled, "nas": {
            "host": "nas.local", "ssh_port": 22, "ssh_user": "photo-sync",
            "ssh_key": "/root/.ssh/pg", "target_root": "/data/Photos",
        }},
    }, sort_keys=False)).sync


def _adapter():
    return RsyncNas(_sync_cfg(), Path("/mnt/sda1/photo-gateway/Photos"), timeout=42)


@mock.patch("subprocess.run")
def test_failed_alive_returns_false(run):
    run.return_value = subprocess.CompletedProcess(["?"], 1, stdout="", stderr="denied")
    assert _adapter().alive() is False


@mock.patch("subprocess.run")
def test_alive_ok_uses_batchmode(run):
    run.return_value = subprocess.CompletedProcess(["?"], 0, stdout="", stderr="")
    assert _adapter().alive() is True
    argv = run.call_args[0][0]
    assert "ssh" in argv and any("BatchMode" in a for a in argv)


@mock.patch("subprocess.run")
def test_sync_never_uses_delete(run):
    run.return_value = subprocess.CompletedProcess(["?"], 0, stdout="", stderr="")
    ad = _adapter()
    src = Path("/mnt/sda1/photo-gateway/Photos/2026/202608")
    rc, *_ = ad.sync(src, Path("/data/Photos/2026/202608"))
    argv = run.call_args[0][0]
    joined = " ".join(argv)
    assert "rsync" in argv[0]
    assert "--delete" not in joined
    assert "-rt" in argv                 # 单向递归+保留时间
    assert photo_sync_user in joined     # remote user@host 出现在 receiver 目标


photo_sync_user = "photo-sync@nas.local"


@mock.patch("subprocess.run")
def test_needs_sync_uses_dry_run(run):
    run.return_value = subprocess.CompletedProcess(["?"], 0, stdout="pic.jpg\n", stderr="")
    ad = _adapter()
    assert ad.needs_sync(Path("/Photos/2026/202608"), Path("/x")) is True
    argv = run.call_args[0][0]
    assert any("rtn" in a for a in argv)  # dry-run diff(-n)
    assert any(a == "-rtni" for a in argv)  # -i itemize：rsync≥3.4 无 -v/-i 时 dry-run 无输出
    assert argv[0].endswith("rsync")
