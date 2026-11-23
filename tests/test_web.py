"""HTTP 层测试：登录鉴权、页面、静态资源与 JSON API。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

from tornado.testing import AsyncHTTPTestCase

from netmon.auth import AuthStore
from netmon.monitor import Monitor
from netmon.web import make_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "static")

CONFIG = {
    "settings": {
        "check_interval": 3,
        "window_size": 20,
        "fail_threshold": 3,
        "mtr_recheck_interval": 15,
        "mtr_ping_count": 3,
        "timeout": 2.0,
        "raw_retention_days": 7,
        "rollup_retention_days": 180,
    },
    "targets": [
        {"id": "dns_ali", "name": "阿里 DNS", "host": "223.5.5.5", "type": "icmp", "port": None}
    ],
    "known_nodes": {"192.168.1.1": "本地内网网关"},
}


class WebApiTest(AsyncHTTPTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netmon-web-")
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(CONFIG, handle, ensure_ascii=False)
        self.monitor = Monitor(config_path, os.path.join(self.tmp, "data", "monitor.db"))
        self.auth = AuthStore(os.path.join(self.tmp, "data", "auth.db"))
        self.auth.ensure_default_admin()
        base = int(time.time()) - 120
        for i in range(40):
            rtt = None if i == 39 else 10.0 + (i % 3)
            self.monitor.store.record_probe("dns_ali", base + i * 3, rtt)
        self.cookie = None
        super().setUp()
        self._login()

    def tearDown(self):
        self.monitor.store.close()
        self.auth.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def get_app(self):
        return make_app(self.monitor, STATIC_DIR, self.auth, "test-cookie-secret-0123456789abcdef")

    # ------------------------------------------------------------- 工具
    def fetch(self, path, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        if self.cookie:
            headers.setdefault("Cookie", self.cookie)
        return super().fetch(path, headers=headers, **kwargs)

    def _login(self, username="admin", password="admin@123"):
        response = self.fetch(
            "/api/login",
            method="POST",
            body=json.dumps({"username": username, "password": password}),
            headers={"Content-Type": "application/json"},
        )
        if response.code == 200:
            self.cookie = response.headers["Set-Cookie"].split(";")[0]
        return response

    def json(self, path, **kwargs):
        response = self.fetch(path, **kwargs)
        return response, json.loads(response.body.decode("utf-8"))

    # ------------------------------------------------------------- 鉴权
    def test_login_page(self):
        self.cookie = None
        response = self.fetch("/login")
        self.assertEqual(response.code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("初始账号", body)
        self.assertIn("login.js", body)

    def test_requires_login(self):
        self.cookie = None
        response = self.fetch("/api/status")
        self.assertEqual(response.code, 401)
        self.assertIn("未登录", response.body.decode("utf-8"))

        response = self.fetch("/", follow_redirects=False)
        self.assertEqual(response.code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_login_failure(self):
        self.cookie = None
        response = self._login("admin", "wrong-password")
        self.assertEqual(response.code, 401)
        self.assertIn("账号或密码错误", response.body.decode("utf-8"))

    def test_me(self):
        response, body = self.json("/api/auth/me")
        self.assertEqual(response.code, 200)
        self.assertEqual(body["username"], "admin")
        self.assertTrue(body["is_admin"])
        self.assertTrue(body["must_change"])

    # ------------------------------------------------------------- 页面
    def test_index_page(self):
        response = self.fetch("/")
        self.assertEqual(response.code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("历史延迟 / 丢包", body)
        self.assertIn("/static/app.js", body)
        self.assertIn("账号管理", body)

    def test_static_assets(self):
        self.cookie = None
        js = self.fetch("/static/app.js")
        self.assertEqual(js.code, 200)
        self.assertIn("connectSse", js.body.decode("utf-8"))

        css = self.fetch("/static/app.css")
        self.assertEqual(css.code, 200)
        self.assertIn(".chart-wrap", css.body.decode("utf-8"))

    # ------------------------------------------------------------- 状态
    def test_status_starts_pending(self):
        response, body = self.json("/api/status")
        self.assertEqual(response.code, 200)
        self.assertEqual(len(body["nodes"]), 1)
        self.assertEqual(body["nodes"][0]["status"], "pending")
        self.assertIsNone(body["nodes"][0]["online"])
        self.assertIn("config", body)

    def test_health(self):
        response, body = self.json("/api/health")
        self.assertEqual(response.code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["storage"]["probes"], 40)

    # ------------------------------------------------------------- 历史
    def test_history_series(self):
        response, body = self.json("/api/history?target_id=dns_ali&range=1h&points=240")
        self.assertEqual(response.code, 200)
        self.assertEqual(body["range"], "1h")
        self.assertEqual(body["source"], "raw")
        self.assertTrue(body["series"])
        self.assertEqual(body["summary"]["samples"], 40)
        self.assertAlmostEqual(body["summary"]["loss"], 2.5, places=1)
        self.assertEqual(body["target"]["id"], "dns_ali")

    def test_history_custom_range(self):
        now = int(time.time())
        response, body = self.json(
            "/api/history?target_id=dns_ali&range=custom&from=%d&to=%d" % (now - 600, now)
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(body["summary"]["samples"], 40)

    def test_history_requires_target(self):
        response, body = self.json("/api/history?range=1h")
        self.assertEqual(response.code, 400)
        self.assertIn("target_id", body["error"])

    def test_history_unknown_target(self):
        response, _ = self.json("/api/history?target_id=nope&range=1h")
        self.assertEqual(response.code, 404)

    def test_history_export_csv(self):
        response = self.fetch("/api/history/export?target_id=dns_ali&range=1h")
        self.assertEqual(response.code, 200)
        self.assertIn("text/csv", response.headers["Content-Type"])
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("history_dns_ali", response.headers["Content-Disposition"])
        text = response.body.decode("utf-8")
        self.assertTrue(text.startswith("\ufeff"))
        self.assertIn("平均延迟(ms)", text)

    # ------------------------------------------------------------- 目标/设置
    def test_add_and_delete_target(self):
        payload = {"name": "本地回环", "host": "127.0.0.1", "type": "icmp"}
        response, body = self.json(
            "/api/target", method="POST", body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("id", body["target"])

        _, status = self.json("/api/status")
        self.assertEqual(len(status["nodes"]), 2)

        target_id = body["target"]["id"]
        response = self.fetch("/api/target?id=%s" % target_id, method="DELETE")
        self.assertEqual(response.code, 200)
        _, status = self.json("/api/status")
        self.assertEqual(len(status["nodes"]), 1)

    def test_add_target_validation(self):
        response, body = self.json(
            "/api/target", method="POST", body=json.dumps({"name": "", "host": ""}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 400)
        self.assertFalse(body["ok"])

        response, _ = self.json(
            "/api/target", method="POST",
            body=json.dumps({"name": "tcp", "host": "1.2.3.4", "type": "tcp"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 400)

    def test_settings_are_clamped(self):
        response, body = self.json(
            "/api/settings", method="POST",
            body=json.dumps({"window_size": 999999, "check_interval": 0, "timeout": 999}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(body["settings"]["window_size"], 1000)
        self.assertEqual(body["settings"]["check_interval"], 1)
        self.assertEqual(body["settings"]["timeout"], 30.0)

    # ------------------------------------------------------------- 断网事件
    def test_outages_api(self):
        self.monitor.store.open_outage(
            {
                "id": "dns_ali_1",
                "target_id": "dns_ali",
                "target_name": "阿里 DNS",
                "host": "223.5.5.5",
                "protocol": "ICMP",
                "start_ts": time.time() - 300,
                "start_time": "2026-01-01 00:00:00",
                "end_ts": None,
                "end_time": None,
                "status": "故障中",
            }
        )
        response, body = self.json("/api/outages?hours=24")
        self.assertEqual(response.code, 200)
        self.assertEqual(len(body["outages"]), 1)
        self.assertEqual(body["outages"][0]["target_id"], "dns_ali")

    # ------------------------------------------------------------- 账号管理
    def test_user_crud_and_permissions(self):
        response, body = self.json(
            "/api/users", method="POST",
            body=json.dumps({"username": "viewer", "password": "viewer123", "is_admin": False}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(body["user"]["username"], "viewer")

        response, body = self.json("/api/users")
        self.assertEqual(response.code, 200)
        self.assertEqual(len(body["users"]), 2)

        admin_cookie = self.cookie
        self._login("viewer", "viewer123")
        response, _ = self.json("/api/users")
        self.assertEqual(response.code, 403)

        self.cookie = admin_cookie
        response = self.fetch("/api/users?username=viewer", method="DELETE")
        self.assertEqual(response.code, 200)
        _, body = self.json("/api/users")
        self.assertEqual(len(body["users"]), 1)

    def test_cannot_delete_self(self):
        response, _ = self.json("/api/users?username=admin", method="DELETE")
        self.assertEqual(response.code, 400)

    def test_change_password_forces_relogin(self):
        response, body = self.json(
            "/api/auth/password", method="POST",
            body=json.dumps({"old_password": "admin@123", "new_password": "admin@456789"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 200)
        self.assertTrue(body["relogin"])

        old_cookie = self.cookie
        self.cookie = old_cookie
        response = self.fetch("/api/status")
        self.assertEqual(response.code, 401)

        response = self._login("admin", "admin@456789")
        self.assertEqual(response.code, 200)
        _, me = self.json("/api/auth/me")
        self.assertFalse(me["must_change"])

    def test_reset_password(self):
        self._create_user("viewer", "viewer123")
        response, _ = self.json(
            "/api/users/password", method="POST",
            body=json.dumps({"username": "viewer", "password": "resetpass1"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 200)
        response = self._login("viewer", "resetpass1")
        self.assertEqual(response.code, 200)

    def _create_user(self, username, password):
        response, _ = self.json(
            "/api/users", method="POST",
            body=json.dumps({"username": username, "password": password, "is_admin": False}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 200)


if __name__ == "__main__":
    unittest.main()
