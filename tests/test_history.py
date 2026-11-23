"""测试：SQLite 历史存储（原始点、分钟聚合、断网事件、清理）。"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from netmon.history import HistoryStore, choose_bucket


class HistoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netmon-test-")
        self.store = HistoryStore(os.path.join(self.tmp, "test.db"))
        self.base = 1700000000.0

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, target_id="t1", count=1200, interval=3):
        for i in range(count):
            rtt = None if i % 100 == 99 else 10.0 + (i % 5)
            self.store.record_probe(target_id, self.base + i * interval, rtt)

    def test_choose_bucket(self):
        self.assertEqual(choose_bucket(3600, 240), 15)
        self.assertEqual(choose_bucket(86400, 240), 600)
        self.assertEqual(choose_bucket(30 * 86400, 240), 10800)

    def test_raw_query(self):
        self._seed()
        data = self.store.query_series("t1", self.base, self.base + 3600)
        self.assertEqual(data["source"], "raw")
        self.assertEqual(data["bucket"], 15)
        self.assertEqual(data["summary"]["samples"], 1200)
        self.assertEqual(data["summary"]["ok"], 1188)
        self.assertAlmostEqual(data["summary"]["loss"], 1.0, places=1)
        self.assertAlmostEqual(data["summary"]["min"], 10.0)
        self.assertAlmostEqual(data["summary"]["max"], 14.0)
        self.assertLessEqual(len(data["series"]), 241)
        self.assertTrue(all("t" in point and "loss" in point for point in data["series"]))

    def test_rollup_query_for_long_range(self):
        self._seed()
        data = self.store.query_series("t1", self.base - 86400, self.base + 30 * 86400, max_points=240)
        self.assertEqual(data["source"], "rollup")
        self.assertEqual(data["summary"]["samples"], 1200)
        self.assertAlmostEqual(data["summary"]["loss"], 1.0, places=1)
        self.assertAlmostEqual(data["summary"]["avg"], 12.0, places=1)
        self.assertLessEqual(len(data["series"]), 240)

    def test_jitter_is_mean_absolute_difference(self):
        self.store.record_probe("t2", self.base, 10.0)
        self.store.record_probe("t2", self.base + 3, 12.0)
        self.store.record_probe("t2", self.base + 6, 12.0)
        data = self.store.query_series("t2", self.base - 60, self.base + 120)
        self.assertAlmostEqual(data["summary"]["jitter"], 1.0, places=2)

    def test_failure_resets_jitter_chain(self):
        self.store.record_probe("t3", self.base, 10.0)
        self.store.record_probe("t3", self.base + 3, None)
        self.store.record_probe("t3", self.base + 6, 100.0)
        data = self.store.query_series("t3", self.base - 60, self.base + 120)
        # 失败后的第一个成功点不与失败前的点算差分
        self.assertAlmostEqual(data["summary"]["jitter"], 0.0, places=2)
        self.assertAlmostEqual(data["summary"]["loss"], 33.3, places=1)

    def test_series_bucket_avg_and_loss(self):
        # 三个探测点放在同一个 5 秒桶内（base 能被 5 整除）
        self.store.record_probe("t4", self.base, 10.0)
        self.store.record_probe("t4", self.base + 1, 20.0)
        self.store.record_probe("t4", self.base + 2, None)
        data = self.store.query_series("t4", self.base - 60, self.base + 60)
        point = data["series"][0]
        self.assertEqual(point["samples"], 3)
        self.assertAlmostEqual(point["avg"], 15.0)
        self.assertAlmostEqual(point["loss"], 33.3, places=1)
        self.assertAlmostEqual(point["max"], 20.0)

    def test_outage_lifecycle(self):
        outage_id = "t1_100"
        self.store.open_outage(
            {
                "id": outage_id,
                "target_id": "t1",
                "target_name": "测试目标",
                "host": "1.2.3.4",
                "protocol": "ICMP",
                "start_ts": self.base,
                "start_time": "2023-11-14 22:13:20",
                "end_ts": None,
                "end_time": None,
                "status": "故障中",
            }
        )
        self.store.record_outage_check(
            outage_id,
            self.base + 15,
            "2023-11-14 22:13:35",
            "第 2 跳阻断: 1.2.3.4",
            [{"hop": 1, "ip": "192.168.1.1", "alias": "网关", "loss": 0.0, "avg": 1.2}],
        )
        self.store.close_outage(outage_id, self.base + 60, "2023-11-14 22:14:20")

        outages = self.store.query_outages(target_id="t1")
        self.assertEqual(len(outages), 1)
        outage = outages[0]
        self.assertEqual(outage["status"], "已恢复")
        self.assertEqual(outage["duration_sec"], 60)
        self.assertEqual(len(outage["checks"]), 1)
        self.assertEqual(outage["checks"][0]["hops"][0]["ip"], "192.168.1.1")

    def test_prune_removes_only_expired(self):
        old_ts = self.base - 30 * 86400
        self.store.record_probe("t5", old_ts, 12.0)
        self.store.record_probe("t5", self.base, 12.0)
        deleted = self.store.prune(7, 180, now=self.base + 60)
        self.assertEqual(deleted["probes"], 1)
        data = self.store.query_series("t5", self.base - 60, self.base + 60)
        self.assertEqual(data["summary"]["samples"], 1)

    def test_stats(self):
        self._seed(count=10)
        stats = self.store.stats()
        self.assertEqual(stats["probes"], 10)
        self.assertGreaterEqual(stats["rollup_rows"], 1)
        self.assertGreater(stats["size_bytes"], 0)

    def test_import_legacy_logs(self):
        import json

        legacy = [
            {
                "id": "t9_1700000000",
                "target_name": "旧目标",
                "host": "9.9.9.9",
                "protocol": "ICMP",
                "start_time": "2023-11-14 22:13:20",
                "end_time": "2023-11-14 22:15:20",
                "status": "已恢复",
                "mtr_checks": [
                    {"time": "2023-11-14 22:14:20", "fault_node": "第 1 跳阻断", "hops": []}
                ],
            }
        ]
        path = os.path.join(self.tmp, "monitor_data.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(legacy, handle, ensure_ascii=False)

        self.assertEqual(self.store.import_legacy_logs(path), 1)
        outages = self.store.query_outages()
        self.assertEqual(len(outages), 1)
        self.assertEqual(outages[0]["target_name"], "旧目标")
        self.assertEqual(len(outages[0]["checks"]), 1)
        # 重复导入不应产生重复记录
        self.assertEqual(self.store.import_legacy_logs(path), 0)


if __name__ == "__main__":
    unittest.main()
