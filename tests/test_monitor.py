"""测试：监控实例（重启后事件接管、目标增删、设置热更新）。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

from netmon.history import HistoryStore
from netmon.models import ProbeResult
from netmon.monitor import Monitor

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
        {"id": "t1", "name": "测试目标", "host": "127.0.0.1", "type": "icmp", "port": None}
    ],
    "known_nodes": {},
}


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netmon-monitor-")
        self.config_path = os.path.join(self.tmp, "config.json")
        self.db_path = os.path.join(self.tmp, "data", "monitor.db")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(CONFIG, handle, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_open_outage(self):
        store = HistoryStore(self.db_path)
        store.open_outage(
            {
                "id": "t1_100",
                "target_id": "t1",
                "target_name": "测试目标",
                "host": "127.0.0.1",
                "protocol": "ICMP",
                "start_ts": 100.0,
                "start_time": "1970-01-01 08:01:40",
                "end_ts": None,
                "end_time": None,
                "status": "故障中",
            }
        )
        store.open_outage(
            {
                "id": "ghost_100",
                "target_id": "ghost",
                "target_name": "已删除目标",
                "host": "9.9.9.9",
                "protocol": "ICMP",
                "start_ts": 100.0,
                "start_time": "1970-01-01 08:01:40",
                "end_ts": None,
                "end_time": None,
                "status": "故障中",
            }
        )
        store.close()

    def test_adopts_open_outage_and_closes_orphan(self):
        self._seed_open_outage()
        monitor = Monitor(self.config_path, self.db_path)
        try:
            self.assertEqual(monitor.states["t1"].active_outage_id, "t1_100")
            outages = {item["id"]: item for item in monitor.store.query_outages()}
            self.assertEqual(outages["t1_100"]["status"], "故障中")
            self.assertEqual(outages["ghost_100"]["status"], "已移除")
            self.assertIsNotNone(outages["ghost_100"]["end_ts"])

            state = monitor.states["t1"]
            state.record(ProbeResult.success(10.0), 200.0, 3)
            monitor._close_outage(state, 200.0)
            self.assertEqual(monitor.store.query_outages(target_id="t1")[0]["status"], "已恢复")
        finally:
            monitor.store.close()

    def test_add_remove_target(self):
        monitor = Monitor(self.config_path, self.db_path)
        try:
            target = monitor.add_target("回环", "127.0.0.1", "icmp", None)
            self.assertIn(target["id"], monitor.targets)
            self.assertEqual(len(monitor.config["targets"]), 2)

            monitor.remove_target(target["id"])
            self.assertNotIn(target["id"], monitor.targets)
            self.assertEqual(len(monitor.config["targets"]), 1)

            with open(self.config_path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(len(saved["targets"]), 1)
        finally:
            monitor.store.close()

    def test_add_target_validation(self):
        monitor = Monitor(self.config_path, self.db_path)
        try:
            with self.assertRaises(ValueError):
                monitor.add_target("", "1.2.3.4", "icmp", None)
            with self.assertRaises(ValueError):
                monitor.add_target("端口", "1.2.3.4", "tcp", None)
            with self.assertRaises(ValueError):
                monitor.add_target("协议", "1.2.3.4", "smtp", None)
        finally:
            monitor.store.close()

    def test_settings_patch_persists(self):
        monitor = Monitor(self.config_path, self.db_path)
        try:
            settings = monitor.update_settings({"window_size": 5, "timeout": 1.5})
            self.assertEqual(settings["window_size"], 5)
            self.assertAlmostEqual(settings["timeout"], 1.5)
            self.assertEqual(monitor.states["t1"].history.maxlen, 5)

            with open(self.config_path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["settings"]["window_size"], 5)
        finally:
            monitor.store.close()

    def test_snapshot_shape(self):
        monitor = Monitor(self.config_path, self.db_path)
        try:
            snapshot = monitor.snapshot()
            self.assertEqual(len(snapshot["nodes"]), 1)
            self.assertEqual(snapshot["nodes"][0]["status"], "pending")
            self.assertIn("server_time", snapshot)
            self.assertIn("settings", snapshot["config"])
            json.dumps(snapshot, ensure_ascii=False)
        finally:
            monitor.store.close()

    def test_snapshot_enriches_fault_alias(self):
        monitor = Monitor(self.config_path, self.db_path)
        try:
            monitor.config["known_nodes"]["10.30.135.182"] = "电信出口"
            state = monitor.states["t1"]
            state.online = False
            state.current_fault_node = "[tracert] 停在 10.30.135.182（未达目标）"
            state.current_fault_ip = "10.30.135.182"
            node = monitor.snapshot()["nodes"][0]
            self.assertEqual(node["current_fault_alias"], "电信出口")
        finally:
            monitor.store.close()

    def test_history_rejects_unknown_target(self):
        monitor = Monitor(self.config_path, self.db_path)
        try:
            with self.assertRaises(KeyError):
                monitor.history("nope", time.time() - 60, time.time())
        finally:
            monitor.store.close()


if __name__ == "__main__":
    unittest.main()
