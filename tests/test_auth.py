"""测试：账号、密码哈希与会话。"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from netmon.auth import AuthStore, DEFAULT_ADMIN, DEFAULT_PASSWORD, hash_password, verify_password


class PasswordHashTest(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = hash_password("secret-pass")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertNotIn("secret-pass", stored)
        self.assertTrue(verify_password(stored, "secret-pass"))
        self.assertFalse(verify_password(stored, "wrong-pass"))

    def test_malformed_hash(self):
        self.assertFalse(verify_password("", "x"))
        self.assertFalse(verify_password("plaintext", "x"))
        self.assertFalse(verify_password("pbkdf2_sha256$abc", "x"))


class AuthStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netmon-auth-")
        self.store = AuthStore(os.path.join(self.tmp, "auth.db"))
        self.created = self.store.ensure_default_admin()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_admin(self):
        self.assertTrue(self.created)
        self.assertFalse(self.store.ensure_default_admin())
        user = self.store.authenticate(DEFAULT_ADMIN, DEFAULT_PASSWORD)
        self.assertIsNotNone(user)
        self.assertTrue(user["is_admin"])
        self.assertTrue(user["must_change"])
        self.assertIsNone(self.store.authenticate(DEFAULT_ADMIN, "bad-password"))

    def test_create_user_validation(self):
        with self.assertRaises(ValueError):
            self.store.create_user("ab", "password123")  # 用户名太短
        with self.assertRaises(ValueError):
            self.store.create_user("viewer", "123")  # 密码太短
        user = self.store.create_user("viewer", "viewer123", is_admin=False)
        self.assertFalse(user["is_admin"])
        with self.assertRaises(ValueError):
            self.store.create_user("viewer", "viewer123")
        self.assertEqual(len(self.store.list_users()), 2)

    def test_sessions(self):
        token = self.store.create_session("admin")
        self.assertIsNotNone(self.store.get_session(token))
        self.store.delete_session(token)
        self.assertIsNone(self.store.get_session(token))
        self.assertIsNone(self.store.get_session("not-a-token"))

    def test_expired_session(self):
        token = self.store.create_session("admin")
        with self.store._lock, self.store._conn:  # noqa: SLF001 - 测试内部表
            self.store._conn.execute(
                "UPDATE sessions SET expires_ts = ? WHERE token = ?", (time.time() - 1, token)
            )
        self.assertIsNone(self.store.get_session(token))

    def test_change_password_invalidates_sessions(self):
        token = self.store.create_session("admin")
        with self.assertRaises(ValueError):
            self.store.change_password("admin", "wrong-old", "new-password-1")
        self.store.change_password("admin", DEFAULT_PASSWORD, "new-password-1")
        self.assertIsNone(self.store.get_session(token))
        self.assertIsNone(self.store.authenticate("admin", DEFAULT_PASSWORD))
        user = self.store.authenticate("admin", "new-password-1")
        self.assertIsNotNone(user)
        self.assertFalse(user["must_change"])

    def test_set_password_by_admin(self):
        self.store.create_user("viewer", "viewer123")
        token = self.store.create_session("viewer")
        self.store.set_password("viewer", "reset-pass-1")
        self.assertIsNone(self.store.get_session(token))
        self.assertIsNotNone(self.store.authenticate("viewer", "reset-pass-1"))
        self.assertTrue(self.store.get_user("viewer")["must_change"])
        with self.assertRaises(KeyError):
            self.store.set_password("nobody", "whatever-1")

    def test_delete_user_rules(self):
        with self.assertRaises(ValueError):
            self.store.delete_user("admin")  # 最后一个管理员不能删
        self.store.create_user("admin2", "admin2pass", is_admin=True)
        self.store.delete_user("admin")
        self.assertIsNone(self.store.get_user("admin"))
        with self.assertRaises(KeyError):
            self.store.delete_user("admin")


if __name__ == "__main__":
    unittest.main()
