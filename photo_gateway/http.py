"""FastAPI 装配 / 统一响应 / 简单认证中间与最小管理 API。

依据 api-spec：前缀 /api/v1、统一成功/失败信封(§9 §10)、HTTP 语义(§11)、
Bearer 认证(§6)、未认证 401 AUTH_REQUIRED(§7)。

当前覆盖(Phase1)：
  POST /api/v1/auth/login
  POST /api/v1/auth/logout
  GET  /api/v1/devices            // 来自配置(受认证保护, 若 auth.enabled)
后续里程碑(M2+)在相同信封下扩展上传/照片等 router。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import ConfigError
from .security import (
    AuthError,
    PgApiError,
    SessionStore,
    get_bearer_token,
    verify_password,
)

SH = timezone(timedelta(hours=8))

# 统一信封（api-spec §9）
def ok(data=None) -> dict:
    return {"success": True, "data": {} if data is None else data}


def err_response(code: str, message: str, http: int) -> JSONResponse:
    body = {"success": False, "error": {"code": code, "message": message}}
    return JSONResponse(status_code=http, content=body)


# 维护任务周期：启动先执行一轮，之后每 24h（config-spec §63.3 / upload.tmp_cleanup）
MAINTENANCE_INTERVAL_SECONDS = 24 * 3600


def _run_maintenance(cfg, dirs, logman=None) -> dict:
    """维护轮：过期文件清理（serve 周期任务调用，返回各清理计数）。

    接线两套过期清理规则（此前仅配置+原语、未在运行时执行）：
    - `upload.tmp_cleanup` → incoming/*.tmp（reliability.cleanup_tmp，默认 7 天）；
    - `thumbnails.cleanup` → 缩略图缓存 *.jpg（thumbnails.cleanup_thumbnails，默认 90 天）。
    各自 enabled=false 时跳过；单轮异常由调用方守护（Worker 不退出）。
    """
    from .reliability import cleanup_tmp
    from .thumbnails import cleanup_thumbnails

    counts = {"tmp_removed": 0, "thumbnails_removed": 0}
    tc = cfg.upload.tmp_cleanup
    if tc.enabled:
        counts["tmp_removed"] = cleanup_tmp(dirs["incoming"], tc.max_age_days)
    th = cfg.thumbnails
    if th.cleanup.enabled:
        counts["thumbnails_removed"] = cleanup_thumbnails(
            cfg.resolved_thumbnails(), th.cleanup.max_age_days)
    if logman is not None:
        logman.application().info(
            "maintenance round", event="maintenance_round",
            interval_seconds=MAINTENANCE_INTERVAL_SECONDS, **counts,
        )
    return counts


# Pydantic 请求模型
class _LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


def _auth_enabled(state) -> bool:
    return bool(state.get("cfg").auth.enabled)


def _require_auth(state, authorization: str | None, store: SessionStore) -> None:
    """受保护入口校验：未开认证直接放行；否则校验 Bearer。"""
    if not _auth_enabled(state):
        return
    token = get_bearer_token(authorization)
    if token is None or not store.verify(token):
        raise AuthError("AUTH_REQUIRED", "authentication required", 401)


def create_app(cfg, db_conn=None, db_path=None, dirs=None, logman=None) -> FastAPI:
    """装配应用。

    db_conn/db_path 二选一：既有测试传 db_conn(未落盘)；运行服务传 db_path + dirs(每次请求重开连接)。
    dirs 提供 incoming/archive/logs 等已解析绝对路径；提供时开启后台 Worker 单传(Ticker)让上传文件自动归档。
    logman：LoggingManager 实例；提供后 Worker/upload 会写入 processing.log / upload.log（serve 场景）。
    """
    import asyncio

    store = SessionStore(ttl_hours=cfg.auth.session_ttl_hours)
    _worker: asyncio.Task | None = None
    _synctask: asyncio.Task | None = None
    _maintask: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal _worker, _synctask, _maintask
        if db_path and dirs and db_path is not None:
            from .db import connect as _connect

            async def _tick():
                while True:
                    try:
                        c = _connect(db_path)
                        try:
                            from .worker import run_round
                            run_round(c, cfg, dirs, logman)   # logman=None 时静默（测试场景）
                        finally:
                            c.close()
                    except Exception:
                        pass  # 单次失败不中断 worker；由日志层/恢复兜底
                    await asyncio.sleep(2)

            _worker = asyncio.create_task(_tick())

            # 维护任务：tmp / 缩略图过期清理（启动先执行一轮，之后每 24h）
            async def _mtick():
                while True:
                    try:
                        _run_maintenance(cfg, dirs, logman)
                    except Exception:
                        pass  # 单次失败不中断；下次周期再试
                    await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)

            _maintask = asyncio.create_task(_mtick())

            # V2：启用了 sync 时按 cycle_interval_seconds 周期跑一个 run_cycle
            archive = dirs.get("archive") or cfg.storage.resolved_archive()
            if cfg.sync is not None and cfg.sync.enabled and archive:
                from .v2runner import run_once
                plog = logman.processing() if logman is not None else None

                async def _vtick():
                    interval = cfg.sync.worker.cycle_interval_seconds
                    while True:
                        try:
                            c = _connect(db_path)
                            try:
                                run_once(c, cfg, archive,
                                         cleanup=cfg.sync.cleanup.enabled,
                                         dry_run_cleanup=True,  # 周期自动清理仍走 dry(实际删除留 flag/人工)
                                         log=plog)
                            finally:
                                c.close()
                        except Exception as exc:
                            # 周期任务不退出；异常入 processing 日志（v2-spec §42）
                            if plog is not None:
                                plog.error(f"v2 cycle error: {exc}",
                                           event="v2_cycle_error")
                        await asyncio.sleep(interval)

                _synctask = asyncio.create_task(_vtick())
        try:
            yield
        finally:
            if _synctask:
                _synctask.cancel()
            if _worker:
                _worker.cancel()
            if _maintask:
                _maintask.cancel()

    app = FastAPI(title="Photo Gateway V1", version=__version__, lifespan=lifespan)
    from .preview_sign import new_secret
    app.state.pg = {"cfg": cfg, "store": store, "db": db_conn, "db_path": db_path,
                    "dirs": dirs, "logman": logman,
                    "preview_secret": new_secret()}

    # --- 统一错误处理：将 Auth/Config/业务错误转信封 ---
    @app.exception_handler(AuthError)
    async def _on_auth_error(_: Request, exc: AuthError):
        return err_response(exc.code, exc.message, exc.http)

    @app.exception_handler(PgApiError)
    async def _on_api_error(_: Request, exc: PgApiError):
        return err_response(exc.code, exc.message, exc.http)

    @app.exception_handler(ConfigError)
    async def _on_config_error(_: Request, exc: ConfigError):
        return err_response("BAD_CONFIG", str(exc), 500)

    @app.exception_handler(Exception)
    async def _on_unexpected(_: Request, exc: Exception):
        # 未知异常：以 500 内部错误信封返回（日志由 logging 层负责）
        return err_response("INTERNAL_ERROR", "internal server error", 500)

    @app.get("/api/v1/health")
    def health():
        return ok({"service": "photo-gateway", "version": __version__, "status": "ok"})

    @app.get("/")
    async def webui_index():
        """WebUI 上传控制台（静态入口；功能面板逐里程碑完善）。"""
        from fastapi.responses import HTMLResponse
        p = Path(__file__).parent / "webui" / "index.html"
        return HTMLResponse(p.read_text(encoding="utf-8"))

    # --- 认证 ---
    @app.post("/api/v1/auth/login")
    def login(body: _LoginBody):
        if not _auth_enabled(app.state.pg):
            raise AuthError("AUTH_DISABLED", "authentication is disabled", 403)
        cfg_auth = app.state.pg["cfg"].auth
        if body.username != cfg_auth.username or not verify_password(body.password, cfg_auth.password_hash):
            raise AuthError("AUTH_FAILED", "invalid username or password", 401)
        token = app.state.pg["store"].create()
        expires_at = datetime.now(SH).strftime("%Y-%m-%d %H:%M:%S")
        return ok({"token": token, "expires_at": expires_at})

    @app.post("/api/v1/auth/logout")
    def logout(authorization: str = Header(default=None)):
        _require_auth(app.state.pg, authorization, app.state.pg["store"])
        token = get_bearer_token(authorization)
        if token:
            app.state.pg["store"].revoke(token)
        return ok({})

    # --- 管理：设备列表（读配置；受认证保护） ---
    @app.get("/api/v1/devices")
    def devices(authorization: str = Header(default=None)):
        _require_auth(app.state.pg, authorization, app.state.pg["store"])
        cfg_dev = app.state.pg["cfg"].devices
        return ok({
            "devices": [
                {"id": d.id, "name": d.name, "enabled": d.enabled} for d in cfg_dev
            ]
        })

    # --- 会话/Precheck API（M2）：需要数据库存在 ---
    if db_conn is not None or db_path is not None:
        from . import api_sessions as _sess
        app.include_router(_sess.router)

    return app


def boot(cfg_path: str | Path):
    """开发/跑服务的便捷自举：load config → 建目录 → logging → db。返回 cfg。"""
    from .config import load_config as _lc
    from .directories import ensure_storage_dirs
    from .logging_util import build_logging
    from .db import init_db

    cfg = _lc(cfg_path)
    dirs = ensure_storage_dirs(cfg)
    logman = build_logging(dirs["logs"], level=cfg.logging.level,
                           retention_days=cfg.logging.rotation.retention_days,
                           max_bytes=cfg.logging.rotation.max_size_mb * 1024 * 1024)
    conn = init_db(dirs["database"])
    return cfg, dirs, logman, conn
