"""断点追踪：mtr / tracert / traceroute / tracepath 的调用与解析。

解析结果统一为 ``{"tool", "fault_node", "hops", "reached"}``：

* ``hops`` 是按跳序排列的列表（无响应的跳记为 ``ip="???"``）
* ``fault_node`` 是给人看的断点描述
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import socket
import statistics
from typing import Any, Dict, List, Optional, Tuple

from .probes import decode_console_bytes

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}

_IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
_IPV4_EXACT_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_RTT_RE = re.compile(r"(?:<\s*)?(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
_HOP_RE = re.compile(r"^\s*(\d+)[.:\s]\s*(.*)$")
_MTR_RE = re.compile(r"^\s*(\d+)\.\|--\s+(\S+)\s+([\d.]+)%?\s+\d+\s+([\d.]+)")


def _alias_of(ip: str, known_nodes: Dict[str, str]) -> str:
    return (known_nodes or {}).get(ip, "")


def parse_mtr_output(raw_text: str, known_nodes: Dict[str, str]) -> List[Dict[str, Any]]:
    hops: List[Dict[str, Any]] = []
    for line in raw_text.splitlines():
        match = _MTR_RE.search(line)
        if not match:
            continue
        ip = match.group(2)
        hops.append(
            {
                "hop": int(match.group(1)),
                "ip": ip,
                "alias": _alias_of(ip, known_nodes),
                "loss": float(match.group(3)),
                "avg": float(match.group(4)),
            }
        )
    return hops


def parse_tracert_output(raw_text: str, known_nodes: Dict[str, str]) -> List[Dict[str, Any]]:
    """解析 Windows ``tracert`` 输出（含中英文）。"""
    hops: List[Dict[str, Any]] = []
    for line in raw_text.splitlines():
        match = _HOP_RE.match(line)
        if not match:
            continue
        hop_no = int(match.group(1))
        rest = match.group(2)
        if not rest.strip():
            continue

        ip_match = _IP_RE.search(rest)
        rtts = [float(value) for value in _RTT_RE.findall(rest)]
        stars = rest.count("*")

        if ip_match:
            ip = ip_match.group(1)
            total = len(rtts) + stars
            loss = round(stars / total * 100, 1) if total else 0.0
            avg = round(statistics.mean(rtts), 2) if rtts else 0.0
            hops.append(
                {"hop": hop_no, "ip": ip, "alias": _alias_of(ip, known_nodes), "loss": loss, "avg": avg}
            )
        elif stars or "timed out" in rest.lower() or "超时" in rest or "逾時" in rest:
            hops.append({"hop": hop_no, "ip": "???", "alias": "", "loss": 100.0, "avg": 0.0})
    return hops


def parse_traceroute_output(raw_text: str, known_nodes: Dict[str, str]) -> List[Dict[str, Any]]:
    """解析 traceroute / tracepath 输出。"""
    hops: List[Dict[str, Any]] = []
    for line in raw_text.splitlines():
        match = _HOP_RE.match(line)
        if not match:
            continue
        hop_no = int(match.group(1))
        rest = match.group(2)
        if not rest.strip():
            continue

        ip_match = _IP_RE.search(rest)
        rtts = [float(value) for value in _RTT_RE.findall(rest)]
        stars = rest.count("*")

        if ip_match:
            ip = ip_match.group(1)
            total = len(rtts) + stars
            loss = round(stars / total * 100, 1) if total else 0.0
            avg = round(statistics.mean(rtts), 2) if rtts else 0.0
            hops.append(
                {"hop": hop_no, "ip": ip, "alias": _alias_of(ip, known_nodes), "loss": loss, "avg": avg}
            )
        elif stars or "no reply" in rest.lower() or "no answer" in rest.lower():
            hops.append({"hop": hop_no, "ip": "???", "alias": "", "loss": 100.0, "avg": 0.0})
    return hops


def analyze_hops(
    hops: List[Dict[str, Any]],
    tool: str,
    target_host: Optional[str] = None,
    target_ips: Optional[set] = None,
) -> Dict[str, Any]:
    """根据跳表判断断点位置。

    规则：找到最后一个「有响应」的跳；它之后的第一个无响应跳就是断点。
    只按"第一跳 100% 丢包"判断会把「中间跳过滤 ICMP、后续仍可达」误报为故障。

    ``target_ips`` 用于判断是否真的到达目标：tracert 在网络中途被阻断时，
    最后一行往往只是某个返回 TTL 过期的中间路由器，并不等于目标可达。
    """
    if not hops:
        return {
            "fault_node": "[%s] 无法解析路由" % tool,
            "hops": [],
            "reached": False,
            "fault_ip": None,
            "fault_alias": "",
        }

    resolved = {ip for ip in (target_ips or set()) if ip}
    if not resolved and target_host and _IPV4_EXACT_RE.match(target_host or ""):
        resolved = {target_host}

    def result(message: str, reached: bool, hop: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        hop = hop or {}
        return {
            "fault_node": message,
            "hops": hops,
            "reached": reached,
            "fault_ip": hop.get("ip"),
            "fault_alias": hop.get("alias") or "",
        }

    responsive = [index for index, hop in enumerate(hops) if hop["ip"] != "???" and hop["loss"] < 100.0]
    if not responsive:
        return result("[%s] 首跳无响应" % tool, False, hops[0])

    last_index = responsive[-1]
    last_hop = hops[last_index]
    if resolved:
        reached = last_hop["ip"] in resolved
    elif target_host:
        reached = last_hop["ip"] == target_host
    else:
        reached = last_index == len(hops) - 1

    if reached:
        return result("[%s] 已到达 %s" % (tool, last_hop["ip"]), True, last_hop)

    if last_index == len(hops) - 1:
        return result("[%s] 停在 %s（未达目标）" % (tool, last_hop["ip"]), False, last_hop)

    next_hop = hops[last_index + 1]
    if next_hop["ip"] == "???":
        return result(
            "[%s] 第 %d 跳无响应（%s 之后）" % (tool, next_hop["hop"], last_hop["ip"]),
            False,
            last_hop,
        )
    return result("[%s] 第 %d 跳阻断：%s" % (tool, next_hop["hop"], next_hop["ip"]), False, next_hop)


class RouteTracer:
    """依次尝试可用的路由追踪工具，返回统一的断点分析结果。"""

    def __init__(self, known_nodes: Dict[str, str], ping_count: int = 3, timeout: float = 25.0) -> None:
        self.known_nodes = known_nodes or {}
        self.ping_count = max(1, int(ping_count))
        self.timeout = timeout

    def _candidates(self) -> List[Tuple[str, List[str]]]:
        count = str(self.ping_count)
        system = platform.system().lower()
        candidates: List[Tuple[str, List[str]]] = []

        if shutil.which("mtr"):
            candidates.append(("mtr", ["mtr", "-n", "-r", "-c", count, "-w"]))
        if system == "windows" and shutil.which("tracert"):
            candidates.append(("tracert", ["tracert", "-d", "-w", "1000", "-h", "20"]))
        if shutil.which("traceroute"):
            candidates.append(("traceroute", ["traceroute", "-n", "-w", "1", "-q", count, "-m", "20"]))
        if shutil.which("tracepath"):
            candidates.append(("tracepath", ["tracepath", "-n", "-m", "20"]))
        return candidates

    def _resolve_target_ips(self, host: str) -> set:
        """解析目标 IP，用于判断追踪是否真的到达目标。"""
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError):
            return set()
        return {info[4][0] for info in infos}

    async def trace(self, host: str) -> Dict[str, Any]:
        candidates = self._candidates()
        if not candidates:
            return {
                "tool": "",
                "fault_node": "未安装 mtr / tracert / traceroute",
                "hops": [],
                "reached": False,
            }

        target_ips = self._resolve_target_ips(host)
        last_result: Optional[Dict[str, Any]] = None
        for tool, cmd in candidates:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    host,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_SUBPROCESS_KWARGS
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                last_result = {
                    "tool": tool,
                    "fault_node": "[%s] 追踪超时" % tool,
                    "hops": [],
                    "reached": False,
                }
                continue
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001
                last_result = {
                    "tool": tool,
                    "fault_node": "[%s] 执行异常: %s" % (tool, exc),
                    "hops": [],
                    "reached": False,
                }
                continue

            text = decode_console_bytes(stdout)
            if not stdout.strip() and stderr.strip():
                text = decode_console_bytes(stderr)

            if tool == "mtr":
                hops = parse_mtr_output(text, self.known_nodes)
            elif tool == "tracert":
                hops = parse_tracert_output(text, self.known_nodes)
            else:
                hops = parse_traceroute_output(text, self.known_nodes)

            result = analyze_hops(hops, tool, host, target_ips)
            result["tool"] = tool
            if hops:
                return result
            last_result = result

        return last_result or {
            "tool": "",
            "fault_node": "路由追踪无结果",
            "hops": [],
            "reached": False,
        }
