"""账号与会话管理。

* 密码使用 PBKDF2-HMAC-SHA256（20 万次迭代 + 随机盐）存储，绝不存明文；
* 登录成功后生成随机会话 token，写入 SQLite，并通过签名 Cookie 保存；
* 修改/重置密码会删除该用户的全部会话，旧 Cookie 立即失效；
* 首次启动自动创建默认管理员 ``admin / admin@123``。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from .timeutil import format_ts

DEFAULT_ADMIN = "admin"
DEFAULT_PASSWORD = "admin@123"
SESSION_DAYS = 7
ITERATIONS = 200_000
MIN_PASSWORD_LEN = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    must_change   INTEGER NOT NULL DEFAULT 0,
    created_ts    REAL NOT NULL,
    last_login_ts REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);
"""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (ITERATIONS, salt.hex(), digest.hex())


def verify_password(stored: str, password: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = (stored or "").split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ValueError("用户名需为 3-32 位字母、数字、下划线、点或短横线")
    return username


def validate_password(password: str) -> str:
    password = password or ""
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError("密码至少需要 %d 位" % MIN_PASSWORD_LEN)
    return password


class AuthStore:
    """用户与会话的持久化存储。"""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.purge_expired_sessions()

    # ------------------------------------------------------------------ 用户
    @staticmethod
    def _to_user(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
            "must_change": bool(row["must_change"]),
            "created_ts": row["created_ts"],
            "created_time": format_ts(row["created_ts"]),
            "last_login_ts": row["last_login_ts"],
            "last_login": format_ts(row["last_login_ts"]) or "--",
        }

    def ensure_default_admin(self) -> bool:
        """没有任何用户时创建默认管理员，返回是否新建。"""
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count:
                return False
            self._conn.execute(
                "INSERT INTO users(username, password_hash, is_admin, must_change, created_ts) "
                "VALUES(?, ?, 1, 1, ?)",
                (DEFAULT_ADMIN, hash_password(DEFAULT_PASSWORD), time.time()),
            )
            self._conn.commit()
        return True

    def count_users(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return self._to_user(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY is_admin DESC, username").fetchall()
        return [self._to_user(row) for row in rows]

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        username = (username or "").strip()
        if not username or not password:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if row is None or not verify_password(row["password_hash"], password):
                return None
            self._conn.execute(
                "UPDATE users SET last_login_ts = ? WHERE username = ?", (time.time(), username)
            )
            self._conn.commit()
        return self._to_user(row)

    def create_user(self, username: str, password: str, is_admin: bool = False) -> Dict[str, Any]:
        username = validate_username(username)
        validate_password(password)
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if exists:
                raise ValueError("用户名已存在")
            self._conn.execute(
                "INSERT INTO users(username, password_hash, is_admin, must_change, created_ts) "
                "VALUES(?, ?, ?, 1, ?)",
                (username, hash_password(password), 1 if is_admin else 0, time.time()),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return self._to_user(row)

    def delete_user(self, username: str) -> None:
        username = (username or "").strip()
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                raise KeyError("用户不存在")
            if row["is_admin"]:
                admins = int(
                    self._conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]
                )
                if admins <= 1:
                    raise ValueError("至少需要保留一个管理员")
            self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
            self._conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            self._conn.commit()

    def set_password(self, username: str, password: str, must_change: bool = True) -> None:
        """管理员重置密码（会踢掉该用户所有已登录会话）。"""
        validate_password(password)
        with self._lock:
            if self._conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is None:
                raise KeyError("用户不存在")
            self._conn.execute(
                "UPDATE users SET password_hash = ?, must_change = ? WHERE username = ?",
                (hash_password(password), 1 if must_change else 0, username),
            )
            self._conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            self._conn.commit()

    def change_password(self, username: str, old_password: str, new_password: str) -> None:
        """用户自己修改密码：校验旧密码，成功后同样踢掉全部会话。"""
        validate_password(new_password)
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                raise KeyError("用户不存在")
            if not verify_password(row["password_hash"], old_password or ""):
                raise ValueError("当前密码不正确")
            self._conn.execute(
                "UPDATE users SET password_hash = ?, must_change = 0 WHERE username = ?",
                (hash_password(new_password), username),
            )
            self._conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            self._conn.commit()

    # ------------------------------------------------------------------ 会话
    def create_session(self, username: str, days: int = SESSION_DAYS) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions(token, username, created_ts, expires_ts) VALUES(?, ?, ?, ?)",
                (token, username, now, now + days * 86400),
            )
        return token

    def get_session(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.username = s.username "
                "WHERE s.token = ? AND s.expires_ts > ?",
                (token, time.time()),
            ).fetchone()
            if row is None:
                self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                self._conn.commit()
                return None
        return self._to_user(row)

    def delete_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def delete_user_sessions(self, username: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE username = ?", (username,))

    def purge_expired_sessions(self) -> int:
        with self._lock, self._conn:
            deleted = self._conn.execute(
                "DELETE FROM sessions WHERE expires_ts <= ?", (time.time(),)
            ).rowcount
        return max(0, deleted)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
