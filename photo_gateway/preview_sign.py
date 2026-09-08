"""短期签名预览 URL（供 <img>/<a> 等无法带 Authorization 的场景使用）。

背景：V1 认证为 Bearer(api-spec §6)，浏览器原生资源请求不能带自定义头。
方案（用户已选）：登录态下的 WebUI/客户端向后端拿“带签名的短时 URL”用于预览/下载；
服务端资源端点校验签名后放行。不改变 Bearer 认证契约，仅为只读照片端点提供旁路。
约束：
- 签名 = HMAC-SHA256(secret, f"{asset_id}:{expires}")
- 过期后失效；asset_id 与路径必须一致；签名由持有 secret 的 server 生成(secret 进程内随机)。
"""

from __future__ import annotations

import hmac
import hashlib
import secrets
import time


def new_secret() -> str:
    """进程内随机 secret；服务重启后旧签名失效（前端重新拉取即可）。"""
    return secrets.token_hex(32)


def make_preview_token(secret: str, asset_id: int, ttl_seconds: int = 600) -> str:
    """生成 `exp:sig`（asset 绑定在消息里，防串用）。"""
    expires = int(time.time()) + ttl_seconds
    msg = f"{asset_id}:{expires}"
    sig = hmac.new(secret.encode("ascii"), msg.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{expires}:{sig}"


def verify_preview_token(secret: str, asset_id: int, token: str | None) -> bool:
    """校验 `exp:sig`；过期/格式错/asset 不符均 False。"""
    if not token:
        return False
    parts = token.split(":")
    if len(parts) != 2:
        return False
    expires_s, sig = parts
    try:
        expires = int(expires_s)
    except ValueError:
        return False
    if time.time() > expires:
        return False
    msg = f"{asset_id}:{expires_s}"
    expect = hmac.new(secret.encode("ascii"), msg.encode("ascii"),
                      hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig)
