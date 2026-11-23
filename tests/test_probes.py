"""测试：ping 输出解码与解析（重点覆盖「不可达却显示在线」的回归）。"""
from __future__ import annotations

import unittest

from netmon.models import Target
from netmon.probes import (
    DnsProbe,
    HttpProbe,
    IcmpProbe,
    NtpProbe,
    TcpProbe,
    TlsProbe,
    UdpProbe,
    decode_console_bytes,
    make_probe,
    parse_ping_output,
)

ZH_PING_OK = """正在 Ping 223.5.5.5 具有 32 字节的数据:
来自 223.5.5.5 的回复: 字节=32 时间=16ms TTL=117

223.5.5.5 的 Ping 统计信息:
    数据包: 已发送 = 1，已接收 = 1，丢失 = 0 (0% 丢失)，
往返行程的估计时间(以毫秒为单位):
    最短 = 16ms，最长 = 16ms，平均 = 16ms
"""

# 关键回归用例：Windows ping 对「目标主机不可达」的回复退出码为 0，
# 统计行还是「已接收 = 1, 丢失 = 0」，旧版因此判定为在线。
ZH_PING_UNREACHABLE = """正在 Ping 100.64.0.1 具有 32 字节的数据:
来自 192.168.1.1 的回复: 无法访问目标主机。

100.64.0.1 的 Ping 统计信息:
    数据包: 已发送 = 1，已接收 = 1，丢失 = 0 (0% 丢失)，
"""

ZH_PING_TRANSMIT_FAILED = """正在 Ping 100.64.0.1 具有 32 字节的数据:
PING: 传输失败。常见故障。

100.64.0.1 的 Ping 统计信息:
    数据包: 已发送 = 1，已接收 = 0，丢失 = 1 (100% 丢失)，
"""

EN_PING_TIMEOUT = """Pinging 10.255.255.1 with 32 bytes of data:
Request timed out.

Ping statistics for 10.255.255.1:
    Packets: Sent = 1, Received = 0, Lost = 1 (100% loss),
"""

EN_PING_OK_SUB_MS = """Pinging 192.168.1.1 with 32 bytes of data:
Reply from 192.168.1.1: bytes=32 time<1ms TTL=64

Ping statistics for 192.168.1.1:
    Packets: Sent = 1, Received = 1, Lost = 0 (0% loss),
"""

EN_PING_UNREACHABLE = """Pinging 100.64.0.1 with 32 bytes of data:
Reply from 192.168.1.1: Destination host unreachable.

Ping statistics for 100.64.0.1:
    Packets: Sent = 1, Received = 1, Lost = 0 (0% loss),
"""

LINUX_PING_OK = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=14.7 ms

--- 8.8.8.8 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
"""

LINUX_PING_UNREACHABLE = """PING 10.1.1.1 (10.1.1.1) 56(84) bytes of data.
From 192.168.1.1 icmp_seq=1 Destination Host Unreachable

--- 10.1.1.1 ping statistics ---
1 packets transmitted, 0 received, +1 errors, 100% packet loss, time 0ms
"""


class DecodeTest(unittest.TestCase):
    def test_gbk_output_is_decoded(self):
        raw = ZH_PING_OK.encode("cp936")
        text = decode_console_bytes(raw)
        self.assertIn("时间=16ms", text)
        self.assertNotIn("\ufffd", text)

    def test_utf8_output_is_decoded(self):
        raw = EN_PING_OK_SUB_MS.encode("utf-8")
        self.assertIn("time<1ms", decode_console_bytes(raw))

    def test_empty_bytes(self):
        self.assertEqual(decode_console_bytes(b""), "")


class ParsePingTest(unittest.TestCase):
    def test_chinese_success(self):
        result = parse_ping_output(ZH_PING_OK, 0)
        self.assertTrue(result.ok)
        self.assertEqual(result.rtt, 16.0)

    def test_chinese_unreachable_exit_code_zero_must_fail(self):
        result = parse_ping_output(ZH_PING_UNREACHABLE, 0)
        self.assertFalse(result.ok)
        self.assertIsNone(result.rtt)
        self.assertIn("无法访问目标主机", result.error)

    def test_transmit_failed_must_fail(self):
        result = parse_ping_output(ZH_PING_TRANSMIT_FAILED, 1)
        self.assertFalse(result.ok)
        self.assertIn("传输失败", result.error)

    def test_english_timeout(self):
        result = parse_ping_output(EN_PING_TIMEOUT, 1)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error.lower())

    def test_english_unreachable(self):
        result = parse_ping_output(EN_PING_UNREACHABLE, 0)
        self.assertFalse(result.ok)
        self.assertIn("unreachable", result.error.lower())

    def test_sub_ms_reply(self):
        result = parse_ping_output(EN_PING_OK_SUB_MS, 0)
        self.assertTrue(result.ok)
        self.assertEqual(result.rtt, 0.5)

    def test_linux_success(self):
        result = parse_ping_output(LINUX_PING_OK, 0)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.rtt, 14.7, places=2)

    def test_linux_unreachable(self):
        result = parse_ping_output(LINUX_PING_UNREACHABLE, 1)
        self.assertFalse(result.ok)

    def test_no_output(self):
        self.assertFalse(parse_ping_output("", 0).ok)

    def test_reply_without_rtt_is_not_success(self):
        # 只有「回复」字样、没有 RTT 的输出不应被当成成功
        result = parse_ping_output("来自 1.2.3.4 的回复: 字节=32 TTL=64", 0)
        self.assertFalse(result.ok)
        self.assertIsNone(result.rtt)


class ProbeFactoryTest(unittest.TestCase):
    def _target(self, target_type, check, port=None, **kwargs):
        return Target(
            id="t", name="测试", host="example.com", type=target_type, port=port, check=check, **kwargs
        )

    def test_tcp_variants(self):
        self.assertIsInstance(make_probe(self._target("tcp", "tcp", 443)), TcpProbe)
        self.assertIsInstance(make_probe(self._target("tcp", "tls", 443)), TlsProbe)

        http = make_probe(self._target("tcp", "http", 80))
        self.assertIsInstance(http, HttpProbe)
        self.assertFalse(http.use_tls)

        https = make_probe(self._target("tcp", "https", 443))
        self.assertIsInstance(https, HttpProbe)
        self.assertTrue(https.use_tls)

    def test_udp_variants(self):
        generic = make_probe(self._target("udp", "udp", 5000, payload="00ff", retries=3))
        self.assertIsInstance(generic, UdpProbe)
        self.assertEqual(generic.payload, b"\x00\xff")
        self.assertEqual(generic.retries, 3)

        self.assertIsInstance(make_probe(self._target("udp", "dns", 53)), DnsProbe)
        self.assertIsInstance(make_probe(self._target("udp", "ntp", 123)), NtpProbe)

    def test_icmp(self):
        self.assertIsInstance(make_probe(self._target("icmp", "icmp")), IcmpProbe)


if __name__ == "__main__":
    unittest.main()
