"""测试：目标状态机与滚动窗口指标。"""
from __future__ import annotations

import unittest

from netmon.models import ProbeResult, Target, TargetState


def make_state(window: int = 5) -> TargetState:
    target = Target(id="t1", name="测试目标", host="1.2.3.4", type="icmp", port=None)
    return TargetState(target, window)


class TargetStateTest(unittest.TestCase):
    def test_initial_state_is_pending_not_online(self):
        state = make_state()
        self.assertIsNone(state.online)
        self.assertEqual(state.status, "pending")

    def test_requires_threshold_before_down(self):
        state = make_state()
        self.assertIsNone(state.record(ProbeResult.failure("超时"), 100.0, 3))
        # 未到阈值也不能显示「在线」，先显示不稳定
        self.assertEqual(state.status, "degraded")
        self.assertEqual(state.consec_fails, 1)
        self.assertEqual(state.fail_started_ts, 100.0)

        self.assertIsNone(state.record(ProbeResult.failure("超时"), 103.0, 3))
        self.assertEqual(state.status, "degraded")

        transition = state.record(ProbeResult.failure("超时"), 106.0, 3)
        self.assertEqual(transition, "down")
        self.assertEqual(state.status, "down")
        self.assertFalse(state.online)
        # 故障起始时间应是连续失败的第一次
        self.assertEqual(state.fail_started_ts, 100.0)

    def test_recovery_transition(self):
        state = make_state()
        for i in range(3):
            state.record(ProbeResult.failure("超时"), 100.0 + i, 3)
        self.assertEqual(state.status, "down")
        transition = state.record(ProbeResult.success(12.5), 110.0, 3)
        self.assertEqual(transition, "up")
        self.assertEqual(state.status, "up")
        self.assertIsNone(state.current_fault_node)

    def test_single_failure_after_up_is_degraded(self):
        state = make_state()
        state.record(ProbeResult.success(10.0), 100.0, 3)
        self.assertEqual(state.status, "up")
        state.record(ProbeResult.failure("ICMP 超时"), 103.0, 3)
        self.assertEqual(state.status, "degraded")
        self.assertTrue(state.online)
        self.assertEqual(state.consec_fails, 1)

    def test_window_metrics(self):
        state = make_state(window=5)
        state.record(ProbeResult.success(10.0), 100.0, 3)
        state.record(ProbeResult.failure("超时"), 103.0, 3)
        state.record(ProbeResult.success(20.0), 106.0, 3)
        self.assertEqual(state.samples, 3)
        self.assertAlmostEqual(state.loss, 33.3, places=1)
        self.assertAlmostEqual(state.latency, 20.0)
        self.assertAlmostEqual(state.avg, 15.0)
        self.assertAlmostEqual(state.jitter, 10.0)

    def test_window_resize(self):
        state = make_state(window=5)
        for i, rtt in enumerate([10.0, 11.0, 12.0]):
            state.record(ProbeResult.success(rtt), 100.0 + i, 3)
        state.resize(2)
        self.assertEqual(list(state.history), [11.0, 12.0])
        self.assertEqual(state.samples, 2)

    def test_inconclusive_results_do_not_trigger_outage(self):
        state = make_state()
        for i in range(4):
            state.record(ProbeResult.failure("UDP 无响应（服务不回应）", inconclusive=True), 100.0 + i, 3)
        self.assertEqual(state.status, "unknown")
        self.assertIsNone(state.online)  # 始终没有被判定为断网
        self.assertEqual(state.consec_fails, 0)
        self.assertTrue(state.unknown)
        # 窗口里仍然有丢包记录
        self.assertGreater(state.loss, 0)

        state.record(ProbeResult.success(10.0), 200.0, 3)
        self.assertEqual(state.status, "up")
        self.assertFalse(state.unknown)

    def test_conclusive_failure_after_unknown_goes_down(self):
        state = make_state()
        for i in range(3):
            state.record(ProbeResult.failure("UDP 无响应", inconclusive=True), 100.0 + i, 3)
        self.assertEqual(state.status, "unknown")
        for i in range(3):
            state.record(ProbeResult.failure("UDP 端口不可达"), 200.0 + i, 3)
        self.assertEqual(state.status, "down")

    def test_to_dict_contains_ui_fields(self):
        state = make_state()
        state.record(ProbeResult.success(9.5), 100.0, 3)
        payload = state.to_dict()
        for key in ("id", "name", "host", "type", "status", "latency", "loss", "jitter", "last_check"):
            self.assertIn(key, payload)
        self.assertEqual(payload["status"], "up")


if __name__ == "__main__":
    unittest.main()
