"""实际探测实现：ICMP / TCP / UDP。

这里有两个关键点（旧版本的两个 bug 就出在这）：

1. Windows 的 ``ping.exe`` 输出是 **控制台 OEM 代码页**（简体中文系统为
   cp936/GBK）。旧版本按 UTF-8 解码，中文「时间=16ms」直接变成乱码，
   正则永远匹配不到，于是走进 ``else: return round(elapsed, 2)`` 的分支——
   任何退出码为 0 的目标都被当成「在线」，延迟则是进程启动耗时（Windows
   下量化成 15.6ms 的整数倍）。
2. Windows 的 ``ping`` 在收到「无法访问目标主机 / 传输中过期」等 ICMP 错误
   时 **退出码仍然是 0**，且统计行还写着 ``已接收 = 1, 丢失 = 0``。因此不能
   只看退出码或丢包率，必须识别明确的失败标记。

现在的原则：**只有真正解析到回显 RTT 才算成功，绝不伪造延迟。**
"""
from __future__ import annotations

import asyncio
import errno
import locale
import os
import platform
import random
import re
import socket
import struct
import time
from typing import List, Optional, Sequence

from .models import ProbeResult, Target

# Windows 下隐藏子进程控制台窗口，避免每 3 秒闪一次黑框
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}

# 这些字符串一旦出现，说明探测失败（即使 ping 的退出码为 0）
FAILURE_MARKERS: Sequence[str] = (
    # 英文
    "request timed out",
    "timed out",
    "transmit failed",
    "destination host unreachable",
    "destination net unreachable",
    "destination port unreachable",
    "host unreachable",
    "network is unreachable",
    "ttl expired in transit",
    "general failure",
    "could not find host",
    "unknown host",
    "name or service not known",
    # 中文（简体 Windows）
    "请求超时",
    "传输失败",
    "一般故障",
    "常见故障",
    "无法访问目标主机",
    "无法访问目标网络",
    "目标主机不可达",
    "目标网络不可达",
    "TTL 传输中过期",
    "TTL传输中过期",
    "找不到主机",
    "未知的主机",
    # 繁体 Windows
    "要求逾時",
    "無法存取目標主機",
    "目的地主機無法連線",
)

_RTT_RE = re.compile(r"(?:time|时间|時間)\s*([=<])\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
_LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:packet\s+)?(?:loss|丢失|遺失)", re.IGNORECASE)

# 仅用于判断「这段字节用哪种编码解出来才像 ping 输出」
_DECODE_HINTS = (
    "time=",
    "reply from",
    "ping statistics",
    "packets:",
    "ttl=",
    "bytes=",
    "来自",
    "时间",
    "時間",
    "字节",
    "数据包",
    "丢失",
    "正在 ping",
    "统计信息",
)

_encoding_cache: Optional[List[str]] = None


def _encoding_candidates() -> List[str]:
    """按优先级列出候选编码：UTF-8 -> 控制台代码页 -> 系统首选编码。"""
    global _encoding_cache
    if _encoding_cache is not None:
        return _encoding_cache

    candidates: List[str] = ["utf-8"]
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            for codepage in (kernel32.GetOEMCP(), kernel32.GetACP()):
                if codepage:
                    candidates.append("cp%d" % int(codepage))
        except Exception:  # noqa: BLE001 - 取不到代码页就退回常见值
            pass
        candidates.extend(["gb18030", "cp936", "big5", "cp932"])

    preferred = locale.getpreferredencoding(False)
    if preferred:
        candidates.append(preferred)
    candidates.extend(["latin-1"])

    ordered: List[str] = []
    seen = set()
    for name in candidates:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            "".encode(name)
        except LookupError:
            continue
        ordered.append(name)
    _encoding_cache = ordered
    return ordered


def decode_console_bytes(data: bytes) -> str:
    """把 ping / tracert 的原始输出解码成文本。

    GBK 字节有时恰好也是合法的 UTF-8（例如「时间」的 GBK 编码），所以不能
    只按顺序试解码，而要选择「命中 ping 关键词最多」的那种解码结果。
    """
    if not data:
        return ""

    best_text = ""
    best_score = -1
    for encoding in _encoding_candidates():
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        lowered = text.lower()
        score = sum(1 for hint in _DECODE_HINTS if hint in lowered)
        if score > best_score:
            best_text, best_score = text, score
        if score >= 4:
            break
    if best_score <= 0:
        return data.decode("latin-1", errors="replace")
    return best_text


def parse_ping_output(text: str, returncode: int) -> ProbeResult:
    """解析 ping 输出。只有拿到有效回显才返回成功。"""
    if not text.strip():
        return ProbeResult.failure("ICMP 无输出（ping 命令执行失败）")

    lowered = text.lower()
    for marker in FAILURE_MARKERS:
        if marker.lower() in lowered:
            detail = _first_line_with(text, marker) or marker
            return ProbeResult.failure("ICMP: %s" % _shorten(detail))

    loss_match = _LOSS_RE.search(text)
    loss_pct = float(loss_match.group(1)) if loss_match else None

    if returncode != 0:
        detail = "ICMP 探测失败（退出码 %s）" % returncode
        if loss_pct is not None:
            detail += "，丢包 %g%%" % loss_pct
        return ProbeResult.failure(detail)

    if loss_pct is not None and loss_pct >= 100.0:
        return ProbeResult.failure("ICMP 100% 丢包（目标无响应）")

    match = _RTT_RE.search(text)
    if match:
        operator, value = match.group(1), float(match.group(2))
        # Windows 的「时间<1ms」：记录 0.5ms，避免把 0 当成有效值
        rtt = value * 0.5 if operator == "<" else value
        return ProbeResult.success(rtt)

    if loss_pct is not None and loss_pct > 0:
        return ProbeResult.failure("ICMP 丢包 %g%%（未收到有效回显）" % loss_pct)

    return ProbeResult.failure("ICMP 未收到有效回显（目标不响应或被防火墙过滤）")


def _first_line_with(text: str, needle: str) -> Optional[str]:
    needle_lower = needle.lower()
    for line in text.splitlines():
        if needle_lower in line.lower():
            return line.strip()
    return None


def _shorten(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ping_command(host: str, timeout: float) -> List[str]:
    """按平台拼装单次 ping 命令。"""
    system = platform.system().lower()
    if system == "windows":
        return ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    if system == "darwin":
        return ["ping", "-c", "1", "-W", str(int(timeout * 1000)), host]
    # Linux / *BSD：-W 单位为秒
    return ["ping", "-c", "1", "-W", str(max(1, int(round(timeout)))), "-n", host]


def _dns_query(name: str = "www.baidu.com") -> bytes:
    """构造一个最小 DNS A 查询，用于 UDP:53 探测。"""
    txid = random.randrange(0, 65536)
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    question = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split("."))
    question += b"\x00" + struct.pack("!HH", 1, 1)
    return header + question


class IcmpProbe:
    """调用系统 ping 命令做一次 ICMP 探测。"""

    kind = "icmp"

    def __init__(self, host: str) -> None:
        self.host = host

    async def measure(self, timeout: float) -> ProbeResult:
        cmd = ping_command(self.host, timeout)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_SUBPROCESS_KWARGS
            )
        except FileNotFoundError:
            return ProbeResult.failure("系统缺少 ping 命令")
        except Exception as exc:  # noqa: BLE001
            return ProbeResult.failure("无法执行 ping: %s" % exc)

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2.0)
        except asyncio.TimeoutError:
            await _terminate_process(proc)
            return ProbeResult.failure("ICMP 探测超时（>%.1fs）" % timeout)

        text = decode_console_bytes(stdout)
        if stderr.strip():
            text += "\n" + decode_console_bytes(stderr)
        return parse_ping_output(text, proc.returncode)


async def _terminate_process(proc) -> None:
    """超时后杀掉 ping 子进程，避免残留进程越积越多。"""
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


class TcpProbe:
    """TCP 建连耗时（SYN -> SYN/ACK）。"""

    kind = "tcp"

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = int(port)

    async def measure(self, timeout: float) -> ProbeResult:
        start = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=timeout
            )
        except asyncio.TimeoutError:
            return ProbeResult.failure("TCP 连接超时")
        except ConnectionRefusedError:
            return ProbeResult.failure("TCP 连接被拒绝（端口未监听）")
        except socket.gaierror as exc:
            return ProbeResult.failure("域名解析失败: %s" % exc)
        except OSError as exc:
            if exc.errno in (errno.ETIMEDOUT, 10060):  # 10060 = WSAETIMEDOUT
                return ProbeResult.failure("TCP 连接超时")
            return ProbeResult.failure("TCP 连接失败: %s" % (exc.strerror or exc))
        except Exception as exc:  # noqa: BLE001
            return ProbeResult.failure("TCP 探测异常: %s" % exc)

        rtt = (time.perf_counter() - start) * 1000.0
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return ProbeResult.success(rtt)


class UdpProbe:
    """UDP 探测：发出报文后等待任意回包。

    注意：UDP 没有连接语义，目标端口不开放时通常只会静默丢弃，
    因此「超时」不一定代表网络故障，可能只是端口被过滤。
    """

    kind = "udp"

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = int(port)

    async def measure(self, timeout: float) -> ProbeResult:
        loop = asyncio.get_running_loop()
        try:
            # 显式按 IPv4 解析，避免域名同时有 AAAA 记录时套接字族不匹配
            infos = await asyncio.wait_for(
                loop.getaddrinfo(self.host, self.port, family=socket.AF_INET, type=socket.SOCK_DGRAM),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ProbeResult.failure("UDP 域名解析超时")
        except socket.gaierror as exc:
            return ProbeResult.failure("域名解析失败: %s" % exc)
        if not infos:
            return ProbeResult.failure("域名解析失败: 无可用地址")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        start = time.perf_counter()
        try:
            await asyncio.wait_for(loop.sock_connect(sock, infos[0][4]), timeout=timeout)
            payload = _dns_query() if self.port == 53 else b"\x00"
            await asyncio.wait_for(loop.sock_sendall(sock, payload), timeout=timeout)
            await asyncio.wait_for(loop.sock_recv(sock, 2048), timeout=timeout)
            rtt = (time.perf_counter() - start) * 1000.0
            return ProbeResult.success(rtt)
        except asyncio.TimeoutError:
            return ProbeResult.failure("UDP 无响应（端口未开放或被过滤）")
        except ConnectionRefusedError:
            return ProbeResult.failure("UDP 端口不可达")
        except OSError as exc:
            return ProbeResult.failure("UDP 探测失败: %s" % (exc.strerror or exc))
        except Exception as exc:  # noqa: BLE001
            return ProbeResult.failure("UDP 探测异常: %s" % exc)
        finally:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


def make_probe(target: Target):
    """按目标类型创建探针。"""
    if target.type == "tcp" and target.port:
        return TcpProbe(target.host, target.port)
    if target.type == "udp" and target.port:
        return UdpProbe(target.host, target.port)
    return IcmpProbe(target.host)
