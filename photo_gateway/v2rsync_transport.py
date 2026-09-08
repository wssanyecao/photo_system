"""V2 真实传输适配：RFC R5S→NAS 单向 rsync over SSH（对齐 v2-spec §8·18-20）。

- 方向固定单向；任何路径不得追加 `--delete` 等可能清理 NAS 的开关（§19）。
- nas.ready/alive 通过 ssh BatchMode 探测；Difference 使用 rsync `-rtn`(dry-run) 计差异（§9 建议 dry-run）。
- 正式同步 `rsync -rtn --dry` 关 → `rsync -rt [---rsh ssh]`（-t 保留 mtime；r 递归），无 delete。
- 每个调用带 timeout，异常上升由 worker 捕获（确保 worker 不退出）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import _SyncConfig as SyncConfig, _SyncNas as SyncNas


class RsyncNas:
    """基于配置 sync.nas 的真实 rsync-over-SSH 后端。"""

    def __init__(self, cfg: SyncConfig, source_root: Path, timeout: int = 600):
        self.cfg = cfg
        self.nas: SyncNas = cfg.nas
        self.source_root = Path(source_root)
        self.timeout = timeout
        self._ssh = shutil.which("ssh") or "ssh"
        self._rsync = shutil.which("rsync") or "rsync"

    # --- 小工具 ---
    def _ssh_base(self) -> list[str]:
        args = ["ssh", "-p", str(self.nas.ssh_port), "-i", self.nas.ssh_key,
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "ControlMaster=no",     # 禁用 ControlMaster：不依赖/绑定 ~/.ssh/con-* socket
                f"{self.nas.ssh_user}@{self.nas.host}"]
        return args

    def _rsync_rsh_args(self) -> list[str]:
        """rsync `--rsh=` 用的 ssh 前缀：**不含** `user@host`。

        rsync 会把 `--rsh` 串交给 shell 再自行追加远端 receiver（`user@host:`）。
        若把 `user@host` 拼进 rsh，远端主机名会被 shell 当命令执行（rc 127）。
        """
        return ["ssh", "-p", str(self.nas.ssh_port), "-i", self.nas.ssh_key,
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "ControlMaster=no"]

    def _target_remote(self, rel: str) -> str:
        # remote = user@host:/.../Photos/YYYY/YYYYMM
        tgt = f"{self.nas.target_root.rstrip('/')}/{rel}"
        return f"{self.nas.ssh_user}@{self.nas.host}:{tgt}"

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        """执行 argv 并返回 (rc, stdout, stderr)。异常→rc 255。"""
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
            return cp.returncode, cp.stdout, cp.stderr
        except (subprocess.TimeoutExpired, OSError) as exc:
            return 255, "", f"{type(exc).__name__}: {exc}"

    # --- 接口：NasBackend-like（alive/ready/needs_sync/sync） ---
    def alive(self) -> bool:
        if not self._ssh:
            return False
        rc, _, _ = self._run([*self._ssh_base(), "-o", "ConnectTimeout=4", "true"])
        return rc == 0

    def ready(self) -> bool:
        # Ready 探测是否 target_root 挂载目录可写路径可达：ssh test -d
        rc, _, _ = self._run(self._ssh_base() + ["test", "-d", self.nas.target_root])
        return rc == 0

    def _mkdir_remote(self, rel: str) -> tuple[int, str, str]:
        tgt = self.nas.target_root.rstrip("/") + "/" + rel
        return self._run(self._ssh_base() + ["mkdir", "-p", tgt])

    def needs_sync(self, source: Path, target: Path) -> bool:
        """RSYNC dry-run 差异探测（输出行>0 视为有差异；不删除 NAS §19）。

        - 先幂等创建远端月份目录：首次同步远端目录尚不存在时 dry-run 会报错，
          不能据此判定"无差异"（否则首轮恒不同步）；
        - 用 `-i`（itemize）输出变更明细：rsync ≥3.4 无 `-v`/`-i` 时 dry-run
          stdout 为空，无法据输出判定差异（实测 homebrew rsync 3.4.3）。
        """
        rel = _rel_for(source)
        self._mkdir_remote(rel)
        src = str(source) if source.is_absolute() else str(self.source_root / source)
        args = [
            self._rsync, "-rtni",
            "--rsh=" + " ".join(self._rsync_rsh_args()),
            "--", src.rstrip("/") + "/", self._target_remote(rel),
        ]
        rc, out, _ = self._run(args)
        return rc == 0 and bool(out.strip())

    def sync(self, source: Path, target: Path) -> tuple[int, str, str]:
        """单向正式同步。source 为 YYYY/YYYYMM 源，target 为同 rel(记录用). 不改 NAS 数据(无 --delete)。"""
        rel = _rel_for(source)
        self._mkdir_remote(rel)
        src = str(source) if source.is_absolute() else str(self.source_root / source)
        argv = [
            self._rsync, "-rt",
            "--rsh=" + " ".join(self._rsync_rsh_args()),
            "--", src.rstrip("/") + "/", self._target_remote(rel),
        ]
        return self._run(argv)


def _rel_for(source: Path) -> str:
    """源即 '.../Photos/YYYY/YYYYMM' → 取相对 'Photos/' 的 YYYY/YYYYMM。"""
    parts = list(Path(source).parts)
    # 找 'Photos' 段之后的子路径
    try:
        i = parts.index("Photos")
        rel = "/".join(parts[i + 1:])
        return rel
    except ValueError:
        return str(Path(source).name)
