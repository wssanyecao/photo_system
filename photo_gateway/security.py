"""V1 简单认证（api-spec §6 认证流 / §7 · §8 未认证·权限响应语义）。

- 登录比对配置中的 password_hash（api-spec §6.1）。
- Hash 格式约定为本实现自描述 string：`scrypt$<salt_hex>$<n>$<r>$<p>$<dk_hex>`；
  生成用 `photo-gateway hash-password`。
- Session Token：登录取随机 32B hex，存于进程内 store（key→expiry 上海时区）。
  api-spec §6 明示"Session Token 不再从配置文件读取"，可用内存存储；跨重启失效可接受，
  记为改进点(可后续持久化)。
- 不引入用户表/RBAC（api-spec §6）。
历史注记：authentic 具体 hash 算法 spec 标"推荐 Argon2id"而非冻结；
本实现采用 stdlib scrypt(免额外依赖)，凭据格式可迁移。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field

# scrypt 参数（KDF 一次 login，成本可控）
_SCRYPT_PARAMS = {"n": 2 ** 14, "r": 8, "p": 1}


class AuthError(Exception):
    """认证产生的错误（登录失败 / token 无效等）。"""

    def __init__(self, code: str, message: str, http: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


class PgApiError(Exception):
    """业务 API 错误，由 HTTP 层转成统一信封（api-spec §9/§10 失败响应）。"""

    def __init__(self, code: str, message: str, http: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


def hash_password(password: str) -> str:
    """生成可存 config.password_hash 的字符串格式。"""
    salt = secrets.token_bytes(16)
    params = _SCRYPT_PARAMS
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=params["n"], r=params["r"], p=params["p"]
    )
    return "scrypt${}${}${}${}${}".format(
        salt.hex(), params["n"], params["r"], params["p"], dk.hex()
    )


def verify_password(password: str, stored: str) -> bool:
    """比对明文与 config 中 Hash。格式失败返回 False(不抛)。"""
    try:
        parts = stored.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        salt = bytes.fromhex(parts[1])
        params = {
            "n": int(parts[2]),
            "r": int(parts[3]),
            "p": int(parts[4]),
        }
        dk = bytes.fromhex(parts[5])
    except (ValueError, AttributeError):
        return False
    got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=params["n"], r=params["r"], p=params["p"])
    return secrets.compare_digest(got, dk)


@dataclass
class SessionStore:
    """进程内 Bearer Session Token 表（token→expiry epoch 秒，上海时间）。"""

    ttl_hours: int = 24
    _tokens: dict[str, float] = field(default_factory=dict)

    def create(self) -> str:
        tok = secrets.token_hex(32)
        self._tokens[tok] = time.time() + self.ttl_hours * 3600
        return tok

    def verify(self, token: str) -> bool:
        exp = self._tokens.get(token)
        if exp is None:
            return False
        if time.time() > exp:
            self._tokens.pop(token, None)
            return False
        return True

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)


def get_bearer_token(authorization: str | None) -> str | None:
    """从 Authorization: Bearer <token> 摘 token。"""
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer" or not rest.strip():
        return None
    return rest.strip()
