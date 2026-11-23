"""Tornado HTTP 层：登录鉴权、页面、SSE 实时推送，以及 JSON API。"""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import tornado.ioloop
import tornado.iostream
import tornado.web

from .auth import AuthStore
from .monitor import Monitor
from .timeutil import clamp_range, format_ts, parse_ts

SESSION_COOKIE = "monitor_session"

# 允许的时间范围（秒）
RANGE_SECONDS = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "3h": 10800,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
    "3d": 259200,
    "7d": 604800,
    "30d": 2592000,
    "90d": 7776000,
}

# 登录失败限流（内存内，按来源 IP）
LOGIN_WINDOW = 300.0
LOGIN_MAX_FAILS = 5
_login_fails: Dict[str, List[float]] = {}


def _recent_failures(ip: str) -> List[float]:
    now = time.time()
    records = [stamp for stamp in _login_fails.get(ip, []) if now - stamp < LOGIN_WINDOW]
    _login_fails[ip] = records
    return records


class BaseHandler(tornado.web.RequestHandler):
    monitor: Monitor
    auth: AuthStore
    current_user: Optional[Dict[str, Any]] = None

    def initialize(self, monitor: Monitor, auth: AuthStore) -> None:
        self.monitor = monitor
        self.auth = auth

    # ------------------------------------------------------------------ 工具
    def write_json(self, payload: Any, status: int = 200) -> None:
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.set_header("Cache-Control", "no-store")
        self.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def write_error_json(self, message: str, status: int = 400) -> None:
        self.write_json({"ok": False, "error": message}, status=status)

    def read_json_body(self) -> Dict[str, Any]:
        if not self.request.body:
            return {}
        data = json.loads(self.request.body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def session_token(self) -> Optional[str]:
        raw = self.get_secure_cookie(SESSION_COOKIE)
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    def require_admin(self) -> bool:
        if not self.current_user or not self.current_user.get("is_admin"):
            self.write_error_json("需要管理员权限", 403)
            return False
        return True


class AuthenticatedHandler(BaseHandler):
    """所有需要登录的页面和接口。"""

    def prepare(self) -> None:
        self.current_user = self.auth.get_session(self.session_token())
        if self.current_user is not None:
            return
        if self.request.path.startswith("/api/"):
            self.write_error_json("未登录或登录已过期", 401)
            self.finish()
        else:
            self.redirect("/login?next=" + urllib.parse.quote(self.request.uri))


class LoginPageHandler(BaseHandler):
    def initialize(self, monitor: Monitor, auth: AuthStore, login_path: str) -> None:  # noqa: D401
        super().initialize(monitor, auth)
        self.login_path = login_path

    def get(self) -> None:
        if self.auth.get_session(self.session_token()):
            self.redirect("/")
            return
        try:
            with open(self.login_path, "r", encoding="utf-8") as handle:
                html = handle.read()
        except OSError as exc:
            self.set_status(500)
            self.write("登录页文件缺失: %s" % exc)
            return
        self.set_header("Content-Type", "text/html; charset=UTF-8")
        self.set_header("Cache-Control", "no-cache")
        self.write(html)


class LoginApiHandler(BaseHandler):
    def post(self) -> None:
        ip = self.request.remote_ip or "unknown"
        if len(_recent_failures(ip)) >= LOGIN_MAX_FAILS:
            self.write_error_json("尝试过于频繁，请 5 分钟后再试", 429)
            return

        try:
            data = self.read_json_body()
        except (ValueError, json.JSONDecodeError):
            data = {}
        username = str(data.get("username") or self.get_argument("username", "")).strip()
        password = str(data.get("password") or self.get_argument("password", ""))

        user = self.auth.authenticate(username, password)
        if user is None:
            _recent_failures(ip).append(time.time())
            self.write_error_json("账号或密码错误", 401)
            return

        _login_fails.pop(ip, None)
        token = self.auth.create_session(user["username"])
        self.set_secure_cookie(
            SESSION_COOKIE,
            token,
            expires_days=7,
            httponly=True,
            samesite="Lax",
        )
        self.write_json(
            {
                "ok": True,
                "username": user["username"],
                "is_admin": user["is_admin"],
                "must_change": user["must_change"],
            }
        )


class LogoutApiHandler(BaseHandler):
    def post(self) -> None:
        self.auth.delete_session(self.session_token())
        self.clear_cookie(SESSION_COOKIE)
        self.write_json({"ok": True})


class MeHandler(AuthenticatedHandler):
    def get(self) -> None:
        user = self.current_user or {}
        self.write_json(
            {
                "ok": True,
                "username": user.get("username"),
                "is_admin": bool(user.get("is_admin")),
                "must_change": bool(user.get("must_change")),
            }
        )


class ChangePasswordHandler(AuthenticatedHandler):
    def post(self) -> None:
        try:
            data = self.read_json_body()
            self.auth.change_password(
                self.current_user["username"],
                str(data.get("old_password") or ""),
                str(data.get("new_password") or ""),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return self.write_error_json(str(exc))
        except KeyError as exc:
            return self.write_error_json(str(exc), 404)
        # 修改密码后所有会话（含当前）失效，前端会跳回登录页
        self.clear_cookie(SESSION_COOKIE)
        self.write_json({"ok": True, "relogin": True})


class UsersHandler(AuthenticatedHandler):
    def get(self) -> None:
        if not self.require_admin():
            return
        self.write_json({"ok": True, "users": self.auth.list_users()})

    def post(self) -> None:
        if not self.require_admin():
            return
        try:
            data = self.read_json_body()
            user = self.auth.create_user(
                str(data.get("username") or ""),
                str(data.get("password") or ""),
                bool(data.get("is_admin")),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return self.write_error_json(str(exc))
        self.write_json({"ok": True, "user": user})

    def delete(self) -> None:
        if not self.require_admin():
            return
        username = self.get_argument("username", "").strip()
        if not username:
            return self.write_error_json("缺少 username 参数")
        if username == (self.current_user or {}).get("username"):
            return self.write_error_json("不能删除当前登录账号")
        try:
            self.auth.delete_user(username)
        except KeyError as exc:
            return self.write_error_json(str(exc), 404)
        except ValueError as exc:
            return self.write_error_json(str(exc))
        self.write_json({"ok": True})


class UserPasswordHandler(AuthenticatedHandler):
    def post(self) -> None:
        if not self.require_admin():
            return
        try:
            data = self.read_json_body()
            username = str(data.get("username") or "").strip()
            if username == (self.current_user or {}).get("username"):
                return self.write_error_json("修改自己的密码请使用「修改密码」")
            self.auth.set_password(username, str(data.get("password") or ""))
        except (ValueError, json.JSONDecodeError) as exc:
            return self.write_error_json(str(exc))
        except KeyError as exc:
            return self.write_error_json(str(exc), 404)
        self.write_json({"ok": True})


class IndexHandler(AuthenticatedHandler):
    def initialize(self, monitor: Monitor, auth: AuthStore, index_path: str) -> None:  # noqa: D401
        super().initialize(monitor, auth)
        self.index_path = index_path

    def get(self) -> None:
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                html = handle.read()
        except OSError as exc:
            self.set_status(500)
            self.write("前端文件缺失: %s" % exc)
            return
        self.set_header("Content-Type", "text/html; charset=UTF-8")
        self.set_header("Cache-Control", "no-cache")
        self.write(html)


class StatusHandler(AuthenticatedHandler):
    def get(self) -> None:
        self.write_json(self.monitor.snapshot())


class SseHandler(AuthenticatedHandler):
    """Server-Sent Events：服务端一有探测结果就推一份快照。"""

    def initialize(self, monitor: Monitor, auth: AuthStore) -> None:
        super().initialize(monitor, auth)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        self.connected = True

    async def get(self) -> None:  # type: ignore[override]
        self.set_header("Content-Type", "text/event-stream; charset=UTF-8")
        self.set_header("Cache-Control", "no-cache, no-store")
        self.set_header("Connection", "keep-alive")
        self.set_header("X-Accel-Buffering", "no")
        self.write("retry: 3000\n\n")
        self.write("data: %s\n\n" % self.monitor.snapshot_json())
        await self.flush()

        self.monitor.sse_clients.add(self.queue)
        try:
            while self.connected:
                try:
                    payload = await asyncio.wait_for(self.queue.get(), timeout=15.0)
                    self.write("data: %s\n\n" % payload)
                    await self.flush()
                except asyncio.TimeoutError:
                    self.write(": keep-alive\n\n")
                    await self.flush()
        except (tornado.iostream.StreamClosedError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.monitor.sse_clients.discard(self.queue)

    def on_connection_close(self) -> None:
        self.connected = False
        self.monitor.sse_clients.discard(self.queue)


class SettingsHandler(AuthenticatedHandler):
    def post(self) -> None:
        try:
            payload = self.read_json_body()
            settings = self.monitor.update_settings(payload)
            self.write_json({"ok": True, "settings": settings})
        except (ValueError, json.JSONDecodeError) as exc:
            self.write_error_json("参数错误: %s" % exc)
        except Exception as exc:  # noqa: BLE001
            self.write_error_json("保存设置失败: %s" % exc, 500)


class TargetHandler(AuthenticatedHandler):
    def post(self) -> None:
        try:
            payload = self.read_json_body()
            extra = {
                key: payload[key]
                for key in ("sni", "path", "payload", "expect", "query", "retries")
                if key in payload
            }
            target = self.monitor.add_target(
                payload.get("name", ""),
                payload.get("host", ""),
                payload.get("type", "icmp"),
                payload.get("port"),
                payload.get("check"),
                **extra
            )
            self.write_json({"ok": True, "target": target})
        except (ValueError, json.JSONDecodeError) as exc:
            self.write_error_json(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.write_error_json("添加目标失败: %s" % exc, 500)

    def delete(self) -> None:
        target_id = self.get_argument("id", "")
        if not target_id:
            return self.write_error_json("缺少 id 参数")
        try:
            self.monitor.remove_target(target_id)
        except KeyError as exc:
            return self.write_error_json(str(exc), 404)
        self.write_json({"ok": True})


class AliasHandler(AuthenticatedHandler):
    def post(self) -> None:
        try:
            payload = self.read_json_body()
            self.monitor.add_alias(payload.get("ip", ""), payload.get("name", ""))
            self.write_json({"ok": True})
        except (ValueError, json.JSONDecodeError) as exc:
            self.write_error_json(str(exc))

    def delete(self) -> None:
        self.monitor.remove_alias(self.get_argument("ip", ""))
        self.write_json({"ok": True})


def _resolve_range(handler: BaseHandler) -> Tuple[float, float, str]:
    """把 ``range`` / ``from`` / ``to`` 参数解析成时间戳区间。"""
    range_key = (handler.get_argument("range", "1h") or "1h").strip()
    if range_key == "custom":
        start = parse_ts(handler.get_argument("from", ""))
        end = parse_ts(handler.get_argument("to", ""))
        if start is None or end is None:
            raise ValueError("自定义时间范围需要合法的 from / to")
        start, end = clamp_range(start, end)
        return start, end, range_key

    seconds = RANGE_SECONDS.get(range_key)
    if seconds is None:
        raise ValueError("未知的时间范围: %s" % range_key)
    now = time.time()
    return now - seconds, now, range_key


class HistoryHandler(AuthenticatedHandler):
    def get(self) -> None:
        target_id = self.get_argument("target_id", "")
        if not target_id:
            return self.write_error_json("缺少 target_id 参数")
        try:
            start, end, range_key = _resolve_range(self)
            points = int(self.get_argument("points", "240"))
        except ValueError as exc:
            return self.write_error_json(str(exc))

        try:
            data = self.monitor.history(target_id, start, end, max(30, min(600, points)))
        except KeyError as exc:
            return self.write_error_json(str(exc), 404)

        data["range"] = range_key
        self.write_json(data)


class HistoryExportHandler(AuthenticatedHandler):
    def get(self) -> None:
        target_id = self.get_argument("target_id", "")
        if not target_id:
            return self.write_error_json("缺少 target_id 参数")
        try:
            start, end, range_key = _resolve_range(self)
            data = self.monitor.history(target_id, start, end, 2000)
        except ValueError as exc:
            return self.write_error_json(str(exc))
        except KeyError as exc:
            return self.write_error_json(str(exc), 404)

        lines = ["时间,平均延迟(ms),最小延迟(ms),最大延迟(ms),丢包率(%),抖动(ms),样本数"]
        for point in data["series"]:
            lines.append(
                "%s,%s,%s,%s,%s,%s,%s"
                % (
                    format_ts(point["t"]) or "",
                    "" if point["avg"] is None else point["avg"],
                    "" if point["min"] is None else point["min"],
                    "" if point["max"] is None else point["max"],
                    point["loss"],
                    point["jitter"],
                    point["samples"],
                )
            )
        filename = "history_%s_%s.csv" % (target_id, time.strftime("%Y%m%d_%H%M%S"))
        self.set_header("Content-Type", "text/csv; charset=UTF-8")
        self.set_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.set_header("Cache-Control", "no-store")
        # BOM 让 Excel 正确识别 UTF-8 中文
        self.write("\ufeff" + "\r\n".join(lines) + "\r\n")


class OutagesHandler(AuthenticatedHandler):
    def get(self) -> None:
        try:
            hours = float(self.get_argument("hours", "24"))
            limit = int(self.get_argument("limit", "100"))
        except ValueError:
            return self.write_error_json("hours / limit 参数不合法")
        target_id = self.get_argument("target_id", "") or None
        self.write_json({"outages": self.monitor.outages(hours=hours, target_id=target_id, limit=limit)})


class HealthHandler(AuthenticatedHandler):
    def get(self) -> None:
        self.write_json(self.monitor.health())


def make_app(
    monitor: Monitor,
    static_dir: str,
    auth: AuthStore,
    cookie_secret: str,
    debug: bool = False,
) -> tornado.web.Application:
    static_dir = os.path.abspath(static_dir)
    index_path = os.path.join(static_dir, "index.html")
    login_path = os.path.join(static_dir, "login.html")
    handlers = [
        (r"/", IndexHandler, {"monitor": monitor, "auth": auth, "index_path": index_path}),
        (r"/login", LoginPageHandler, {"monitor": monitor, "auth": auth, "login_path": login_path}),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": static_dir}),
        (r"/api/login", LoginApiHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/logout", LogoutApiHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/auth/me", MeHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/auth/password", ChangePasswordHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/users", UsersHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/users/password", UserPasswordHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/status", StatusHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/events", SseHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/settings", SettingsHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/target", TargetHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/alias", AliasHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/history", HistoryHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/history/export", HistoryExportHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/outages", OutagesHandler, {"monitor": monitor, "auth": auth}),
        (r"/api/health", HealthHandler, {"monitor": monitor, "auth": auth}),
    ]
    return tornado.web.Application(
        handlers,
        debug=debug,
        autoreload=debug,
        compress_response=False,
        cookie_secret=cookie_secret,
        login_url="/login",
    )
