"""photo-gateway 命令行入口。

用法：
    python -m photo_gateway --config path --check          # 校验配置 (spec §53)
    python -m photo_gateway hash-password                 # 生成 scrypt password_hash
    python -m photo_gateway serve --config path           # 启动 HTTP 服务(V1 Phase1 后)
    python -m photo_gateway maintenance --config path     # 手动执行一轮过期文件清理(tmp/缩略图)
    python -m photo_gateway cleanup --config path         # V2 Cleanup 预览/安全删除(默认 dry-run)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .security import hash_password


def _cmd_check(cfg_path: Path) -> int:
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        return 2  # 致命，拒绝启动 (spec §3.4)
    print(f"[config-ok] {cfg_path}")
    print(f"            host={cfg.server.host} port={cfg.server.port}")
    print(f"            auth.enabled={cfg.auth.enabled}")
    print(f"            devices={cfg.device_ids()}")
    print(f"            archive={cfg.storage.resolve(cfg.storage.archive)}")
    return 0


def _cmd_hash_password() -> int:
    pwd = input("password: ")
    if not pwd:
        print("[error] empty password", file=sys.stderr)
        return 1
    print(hash_password(pwd))
    print("# 把上面 scrypt$... 写入配置 auth.password_hash 并启用 auth.enabled=true", file=sys.stderr)
    return 0


def _cmd_serve(cfg_path: Path) -> int:
    from .http import boot, create_app
    import uvicorn

    cfg, dirs, logman, conn = boot(cfg_path)
    db_path = cfg.storage.resolved_database()
    app = create_app(cfg, db_path=db_path, logman=logman,
                     dirs={"incoming": cfg.storage.resolved_incoming(),
                           "archive": cfg.storage.resolved_archive()})
    logman.application().info("application started",
                              event="server_start", module="cli")
    print(f"[serve] http://{cfg.server.host}:{cfg.server.port} (auth={'on' if cfg.auth.enabled else 'off'})")
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")
    return 0


def _cmd_maintenance(cfg_path: Path, scan_missing: bool = False) -> int:
    """手动执行一轮过期文件清理（= serve 维护任务的同款逻辑，便于运维/测试）。

    清理 incoming/*.tmp（upload.tmp_cleanup）与缩略图缓存（thumbnails.cleanup），
    规则同配置；同时写 application.log maintenance_round。
    scan_missing：额外对账——把 DB 有记录但本地文件缺失的资产标记 file_missing_at
    （历史/外部删除兜底，照片页据此隐藏）。
    """
    from .http import boot, _run_maintenance
    from .worker import mark_missing_assets

    cfg, dirs, logman, conn = boot(cfg_path)
    counts = _run_maintenance(cfg, dirs, logman)
    print(f"[maintenance] tmp_removed={counts['tmp_removed']} "
          f"thumbnails_removed={counts['thumbnails_removed']}")
    if scan_missing:
        marked = mark_missing_assets(conn, cfg.storage.resolved_archive())
        print(f"[maintenance] marked_missing_assets={marked}")
    conn.close()
    return 0


def _cmd_cleanup(cfg_path: Path, apply: bool = False, yes: bool = False) -> int:
    """V2 Cleanup 人工安全删除入口（v2-spec §27-34）。

    默认 dry-run：打印各月份候选判定，绝不删除。仅当同时给出 `--apply --yes`
    才对满足全部条件（当前 NORMAL · 有 Task · 最新 SUCCESS · 达 synced_retention_days）
    的本地 `Photos/YYYY/YYYYMM` 执行 rmtree，并写 audit 日志（不可逆）。
    """
    from .http import boot
    from .v2cycle import cleanup_candidates, cleanup_expired

    try:
        cfg, dirs, logman, conn = boot(cfg_path)
    except Exception as exc:
        print(f"[cleanup-error] 配置/环境就绪失败: {exc}", file=sys.stderr)
        return 2
    if cfg.sync is None:
        print("[cleanup-error] 配置未提供 sync 块，无法确定 Cleanup 保留期", file=sys.stderr)
        conn.close()
        return 2
    retention = cfg.sync.cleanup.synced_retention_days
    archive = cfg.storage.resolved_archive()
    cands = cleanup_candidates(conn, archive, retention_days=retention)
    removable = [c for c in cands if c["remove"]]
    print(f"[cleanup] archive={archive} retention_days={retention}")
    if not cands:
        print("[cleanup] （归档内无月份目录）")
    for c in cands:
        if c["remove"]:
            print(f"  - {c['month']}  可删除 ({c['dir']})")
        else:
            extra = f" until={c.get('until')}" if "until" in c else ""
            print(f"  - {c['month']}  不删: {c.get('reason')}{extra}")
    if not removable:
        print("[cleanup] 无满足全部条件的月份，无需操作")
        conn.close()
        return 0
    if not apply:
        print(f"[cleanup] dry-run：{len(removable)} 个月份可删。"
              "加 --apply --yes 才真正删除（rmtree 不可逆）")
        conn.close()
        return 0
    if not yes:
        print("[cleanup] 安全确认：--apply 必须同时加 --yes 才会执行删除", file=sys.stderr)
        conn.close()
        return 2
    audits: list[str] = []

    def _audit(month: str, rel: str) -> None:
        audits.append(f"{month} ({rel})")
        if logman is not None:
            logman.audit().info(f"cleanup applied month={month} rel={rel}",
                                event="cleanup_applied", module="cli")

    removed = cleanup_expired(conn, archive, retention_days=retention,
                              dry_run=False, audit=_audit)
    print(f"[cleanup] 已删除 {len(removed)} 个月份: {removed}")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="photo-gateway",
        description="Photo Gateway V1 — 家庭照片导入网关",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="校验配置")
    p_check.add_argument("--config", type=Path, default=Path("/etc/photo-gateway/config.yaml"))

    p_hash = sub.add_parser("hash-password", help="生成 auth.password_hash")

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--config", type=Path, default=Path("/etc/photo-gateway/config.yaml"))

    p_maint = sub.add_parser("maintenance", help="手动执行一轮过期文件清理(tmp/缩略图)")
    p_maint.add_argument("--config", type=Path, default=Path("/etc/photo-gateway/config.yaml"))
    p_maint.add_argument("--scan-missing", action="store_true",
                         help="额外对账：标记本地文件缺失的资产(照片页隐藏，历史/外部删除兜底)")

    p_clean = sub.add_parser("cleanup", help="V2 Cleanup：预览/删除已达保留期的已同步月份")
    p_clean.add_argument("--config", type=Path, default=Path("/etc/photo-gateway/config.yaml"))
    p_clean.add_argument("--apply", action="store_true",
                         help="执行删除（默认仅 dry-run 预览，不删任何文件）")
    p_clean.add_argument("--yes", action="store_true",
                         help="确认删除（rmtree 不可逆，须与 --apply 同时使用）")

    p_backfill = sub.add_parser("backfill-exif", help="存量 photo_assets.exif_json 回填")
    p_backfill.add_argument("--config", type=Path, default=Path("/etc/photo-gateway/config.yaml"))

    p_align = sub.add_parser("align-mtime", help="存量资产磁盘 mtime 对齐到 date_taken")
    p_align.add_argument("--config", type=Path, default=Path("/etc/photo-gateway/config.yaml"))

    args = parser.parse_args(argv)
    default = Path("/etc/photo-gateway/config.yaml")
    if args.command == "check":
        return _cmd_check(getattr(args, "config", None) or default)
    if args.command == "hash-password":
        return _cmd_hash_password()
    if args.command == "serve":
        return _cmd_serve(getattr(args, "config", None) or default)
    if args.command == "maintenance":
        return _cmd_maintenance(getattr(args, "config", None) or default,
                                scan_missing=bool(getattr(args, "scan_missing", False)))
    if args.command == "cleanup":
        return _cmd_cleanup(getattr(args, "config", None) or default,
                            apply=bool(getattr(args, "apply", False)),
                            yes=bool(getattr(args, "yes", False)))
    if args.command == "backfill-exif":
        from .http import boot
        from .worker import backfill_exif_json
        cfg, dirs, logman, conn = boot(getattr(args, "config", None) or default)
        n = backfill_exif_json(conn, cfg.storage.resolved_archive())
        conn.close()
        print(f"[backfill-exif] updated {n}")
        return 0
    if args.command == "align-mtime":
        from .http import boot
        from .worker import align_asset_mtimes
        cfg, dirs, logman, conn = boot(getattr(args, "config", None) or default)
        n = align_asset_mtimes(conn, cfg.storage.resolved_archive())
        conn.close()
        print(f"[align-mtime] aligned {n}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
