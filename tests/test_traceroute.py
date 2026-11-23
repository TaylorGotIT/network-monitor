"""测试：路由追踪输出解析与断点分析。"""
from __future__ import annotations

import unittest

from netmon.traceroute import analyze_hops, parse_mtr_output, parse_tracert_output, parse_traceroute_output

KNOWN = {"192.168.1.1": "本地内网网关", "100.64.0.1": "运营商 CGNAT 节点", "202.97.33.1": "电信骨干网出口"}

TRACERT_ZH_DEAD = """通过最多 30 个跃点跟踪
到 8.8.8.8 [8.8.8.8] 的路由:

  1     1 ms     1 ms     1 ms  192.168.1.1
  2     3 ms     3 ms     3 ms  100.64.0.1
  3     *        *        *     请求超时。
  4     *        *        *     请求超时。

跟踪完成。
"""

# 中间跳过滤 ICMP，但后续跳仍然可达：不能判成断点
TRACERT_FILTERED_MIDDLE = """Tracing route to 8.8.8.8 over a maximum of 30 hops

  1     1 ms     1 ms     1 ms  192.168.1.1
  2     *        *        *     Request timed out.
  3     5 ms     5 ms     5 ms  202.97.33.1
  4     6 ms     6 ms     6 ms  8.8.8.8

Trace complete.
"""

TRACEROUTE_OUT = """traceroute to 8.8.8.8 (8.8.8.8), 20 hops max, 60 byte packets
 1  192.168.1.1  1.234 ms  1.111 ms  1.001 ms
 2  * * *
 3  202.97.33.1  5.555 ms  5.444 ms  5.333 ms
 4  8.8.8.8  6.001 ms  6.002 ms  6.003 ms
"""

# 追踪在中途被阻断：最后一行只是返回 TTL 过期的中间路由器，并没有到达目标
TRACERT_NEVER_REACHED = """Tracing route to 100.64.0.1 over a maximum of 30 hops

  1     1 ms     1 ms     1 ms  192.168.1.1
  2     5 ms     5 ms     5 ms  10.30.135.182

Trace complete.
"""

MTR_OUT = """Start: 2026-11-23T19:00:00+0800
HOST: box                        Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.1.1                0.0%     3    1.2   1.3   1.1   1.4   0.1
  2.|-- 100.64.0.1                50.0%     3    5.0   5.0   4.0   6.0   0.5
  3.|-- ???                       100.0     3    0.0   0.0   0.0   0.0   0.0
"""


class TracertParseTest(unittest.TestCase):
    def test_parse_hops_and_aliases(self):
        hops = parse_tracert_output(TRACERT_ZH_DEAD, KNOWN)
        self.assertEqual(len(hops), 4)
        self.assertEqual(hops[0]["ip"], "192.168.1.1")
        self.assertEqual(hops[0]["alias"], "本地内网网关")
        self.assertEqual(hops[2]["ip"], "???")
        self.assertEqual(hops[2]["loss"], 100.0)

    def test_fault_after_last_responsive_hop(self):
        hops = parse_tracert_output(TRACERT_ZH_DEAD, KNOWN)
        result = analyze_hops(hops, "tracert", "8.8.8.8")
        self.assertFalse(result["reached"])
        self.assertIn("第 3 跳", result["fault_node"])
        self.assertIn("100.64.0.1", result["fault_node"])
        self.assertEqual(result["fault_ip"], "100.64.0.1")
        self.assertEqual(result["fault_alias"], "运营商 CGNAT 节点")

    def test_filtered_middle_hop_is_not_a_fault(self):
        hops = parse_tracert_output(TRACERT_FILTERED_MIDDLE, KNOWN)
        result = analyze_hops(hops, "tracert", "8.8.8.8")
        self.assertTrue(result["reached"])
        self.assertIn("已到达", result["fault_node"])

    def test_first_hop_dead(self):
        hops = [
            {"hop": 1, "ip": "???", "alias": "", "loss": 100.0, "avg": 0.0},
            {"hop": 2, "ip": "???", "alias": "", "loss": 100.0, "avg": 0.0},
        ]
        result = analyze_hops(hops, "tracert", None)
        self.assertFalse(result["reached"])
        self.assertIn("首跳", result["fault_node"])

    def test_last_response_is_not_target(self):
        hops = parse_tracert_output(TRACERT_NEVER_REACHED, KNOWN)
        result = analyze_hops(hops, "tracert", "100.64.0.1", {"100.64.0.1"})
        self.assertFalse(result["reached"])
        self.assertIn("未达目标", result["fault_node"])
        self.assertIn("10.30.135.182", result["fault_node"])


class TracerouteParseTest(unittest.TestCase):
    def test_parse(self):
        hops = parse_traceroute_output(TRACEROUTE_OUT, KNOWN)
        self.assertEqual([h["hop"] for h in hops], [1, 2, 3, 4])
        self.assertEqual(hops[1]["ip"], "???")
        self.assertAlmostEqual(hops[2]["avg"], 5.44, places=1)

    def test_analysis_reaches_target(self):
        hops = parse_traceroute_output(TRACEROUTE_OUT, KNOWN)
        result = analyze_hops(hops, "traceroute", "8.8.8.8")
        self.assertTrue(result["reached"])


class MtrParseTest(unittest.TestCase):
    def test_parse(self):
        hops = parse_mtr_output(MTR_OUT, KNOWN)
        self.assertEqual(len(hops), 3)
        self.assertEqual(hops[1]["loss"], 50.0)
        self.assertEqual(hops[2]["ip"], "???")

    def test_analysis(self):
        hops = parse_mtr_output(MTR_OUT, KNOWN)
        result = analyze_hops(hops, "mtr", "8.8.8.8")
        self.assertFalse(result["reached"])
        self.assertIn("第 3 跳", result["fault_node"])


class EmptyParseTest(unittest.TestCase):
    def test_empty(self):
        result = analyze_hops([], "tracert", None)
        self.assertFalse(result["reached"])
        self.assertIn("无法解析路由", result["fault_node"])


if __name__ == "__main__":
    unittest.main()
