"""配置文件的默认值、校验与读写。

所有可调参数集中在 DEFAULT_SETTINGS，新增参数时只需改这里即可。
配置文件使用 UTF-8 JSON，写入采用「临时文件 + 原子替换」，避免进程被杀时
把 config.json 写坏。
"""
from __future__ import annotations

import copy
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

DEFAULT_SETTINGS: Dict[str, Any] = {
    "check_interval": 10,         # 常规探测间隔（秒）：10s 足够发现断网，又不会刷屏
    "window_size": 30,            # 卡片上滚动统计窗口（次）：30 × 10s = 最近 5 分钟
    "fail_threshold": 5,          # 连续失败多少次判定为断网：约 50s 容忍，避免瞬时抖动误报
    "mtr_recheck_interval": 300,  # 断网期间路由追踪的复检间隔（秒）：5 分钟
    "mtr_ping_count": 5,          # 每次 mtr 每跳发包数
    "timeout": 3.0,               # 单次探测超时（秒）：给移动网络/慢链路留余量
    "raw_retention_days": 7,      # 原始探测点保留天数
    "rollup_retention_days": 180, # 分钟级聚合数据保留天数
}

DEFAULT_TARGETS: List[Dict[str, Any]] = [
    {"id": "dns_ali", "name": "阿里 DNS", "host": "223.5.5.5", "type": "icmp", "port": None, "check": "icmp"},
    {"id": "web_baidu", "name": "百度 HTTPS", "host": "www.baidu.com", "type": "tcp", "port": 443, "check": "tls"},
    {"id": "dns_google", "name": "Google DNS", "host": "8.8.8.8", "type": "udp", "port": 53, "check": "dns"},
]

DEFAULT_KNOWN_NODES: Dict[str, str] = {
    "192.168.1.1": "本地内网网关",
    "100.64.0.1": "运营商 CGNAT 节点",
    "202.97.33.1": "电信骨干网出口",
}

PROTOCOLS = ("icmp", "tcp", "udp")

# 每种协议可选的探测方式
CHECK_OPTIONS = {
    "icmp": ("icmp",),
    "tcp": ("tcp", "tls", "http", "https"),
    "udp": ("udp", "dns", "ntp"),
}
DEFAULT_CHECK = {"icmp": "icmp", "tcp": "tcp", "udp": "udp"}
_HEX_RE = re.compile(r"^[0-9a-f]+$")

# 参数 -> (最小值, 最大值)
INT_RANGES = {
    "check_interval": (1, 3600),
    "window_size": (2, 1000),
    "fail_threshold": (1, 100),
    "mtr_recheck_interval": (0, 86400),
    "mtr_ping_count": (1, 20),
    "raw_retention_days": (1, 365),
    "rollup_retention_days": (1, 3650),
}
FLOAT_RANGES = {
    "timeout": (0.2, 30.0),
}

_HOST_PORT_RE = re.compile(r"^([^:\s]+):(\d{1,5})$")


def default_config() -> Dict[str, Any]:
    """返回一份全新的默认配置（深拷贝，避免调用方改动全局常量）。"""
    return {
        "settings": dict(DEFAULT_SETTINGS),
        "targets": copy.deepcopy(DEFAULT_TARGETS),
        "known_nodes": dict(DEFAULT_KNOWN_NODES),
    }


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return fallback


def _to_float(value: Any, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def normalize_settings(raw: Any) -> Dict[str, Any]:
    """把任意来源的设置（HTML 表单字符串、旧配置文件）收敛为合法值。"""
    settings = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return settings
    for key, (low, high) in INT_RANGES.items():
        if key in raw:
            settings[key] = max(low, min(high, _to_int(raw[key], DEFAULT_SETTINGS[key])))
    for key, (low, high) in FLOAT_RANGES.items():
        if key in raw:
            settings[key] = round(max(low, min(high, _to_float(raw[key], DEFAULT_SETTINGS[key]))), 2)
    return settings


def apply_settings_patch(current: Dict[str, Any], patch: Any) -> Dict[str, Any]:
    """在当前设置上叠加一份局部修改，再统一校验。"""
    merged = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in current:
            merged[key] = current[key]
    if isinstance(patch, dict):
        for key, value in patch.items():
            if key in DEFAULT_SETTINGS:
                merged[key] = value
    return normalize_settings(merged)


def _clean_host(host: str) -> str:
    host = (host or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].strip()
    return host


def _split_host_port(host: str):
    """允许用户直接输入 ``1.2.3.4:443``，自动拆出端口（IPv6 不受影响）。"""
    match = _HOST_PORT_RE.match(host)
    if match:
        return match.group(1), int(match.group(2))
    return host, None


def normalize_hex(value: Any) -> Optional[str]:
    """把 ``00 1b ff`` / ``0x001b`` 之类的十六进制串规范成小写连续串。"""
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    text = text.replace("0x", "").replace(" ", "").replace(":", "").replace("-", "")
    if not text or len(text) % 2 or not _HEX_RE.match(text):
        return None
    try:
        bytes.fromhex(text)
    except ValueError:
        return None
    return text


def normalize_target(raw: Any, used_ids: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """校验单个监控目标；不合法时返回 None。"""
    if not isinstance(raw, dict):
        return None

    name = str(raw.get("name") or "").strip()
    host = _clean_host(str(raw.get("host") or ""))
    ptype = str(raw.get("type") or "icmp").strip().lower()
    if ptype not in PROTOCOLS:
        ptype = "icmp"

    host, host_port = _split_host_port(host)
    if not host:
        return None

    raw_port = raw.get("port")
    if raw_port in (None, "", 0, "0"):
        port = host_port
    else:
        port = _to_int(raw_port, 0) or host_port

    if ptype in ("tcp", "udp"):
        if not port or not (1 <= int(port) <= 65535):
            return None
        port = int(port)
    else:
        port = None

    if not name:
        name = host

    check = str(raw.get("check") or "").strip().lower()
    if check not in CHECK_OPTIONS[ptype]:
        check = DEFAULT_CHECK[ptype]

    sni = str(raw.get("sni") or "").strip() or None
    if check not in ("tls", "https"):
        sni = None

    path = str(raw.get("path") or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    if check not in ("http", "https"):
        path = "/"

    payload = normalize_hex(raw.get("payload")) if ptype == "udp" and check == "udp" else None
    expect = normalize_hex(raw.get("expect")) if ptype == "udp" and check == "udp" else None
    retries = 2
    if ptype == "udp" and check == "udp":
        retries = max(1, min(10, _to_int(raw.get("retries"), 2)))

    tid = str(raw.get("id") or "").strip() or "t_%s" % uuid.uuid4().hex[:8]
    if used_ids is not None:
        base = tid
        suffix = 2
        while tid in used_ids:
            tid = "%s_%d" % (base, suffix)
            suffix += 1

    return {
        "id": tid,
        "name": name,
        "host": host,
        "type": ptype,
        "port": port,
        "check": check,
        "sni": sni,
        "path": path,
        "payload": payload,
        "expect": expect,
        "retries": retries,
    }


def normalize_config(raw: Any) -> Dict[str, Any]:
    """把配置文件内容补全、纠错后返回完整配置。"""
    raw = raw if isinstance(raw, dict) else {}

    cfg: Dict[str, Any] = {}
    cfg["settings"] = normalize_settings(raw.get("settings"))

    targets: List[Dict[str, Any]] = []
    used_ids = set()
    if isinstance(raw.get("targets"), list):
        for item in raw["targets"]:
            target = normalize_target(item, used_ids)
            if target:
                used_ids.add(target["id"])
                targets.append(target)
    elif "targets" not in raw:
        # 首次运行（没有配置文件）才注入默认目标；
        # 用户把目标删光时应当保持为空，而不是又冒出来。
        targets = copy.deepcopy(DEFAULT_TARGETS)
    cfg["targets"] = targets

    if isinstance(raw.get("known_nodes"), dict):
        known = {}
        for ip, alias in raw["known_nodes"].items():
            ip = str(ip).strip()
            alias = str(alias).strip()
            if ip and alias:
                known[ip] = alias
        cfg["known_nodes"] = known
    else:
        cfg["known_nodes"] = dict(DEFAULT_KNOWN_NODES)

    return cfg


def load_config(path: str) -> Dict[str, Any]:
    """读取配置；文件缺失/损坏时回退默认值，并把规范化结果写回磁盘。"""
    raw = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:  # noqa: BLE001 - 配置损坏不应阻止服务启动
            print("读取配置文件失败，使用默认配置: %s" % exc)
            raw = None

    cfg = normalize_config(raw or {})
    if raw is None or cfg != raw:
        save_config(path, cfg)
    return cfg


def save_config(path: str, cfg: Dict[str, Any]) -> None:
    """原子写入配置文件。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception as exc:  # noqa: BLE001
        print("保存配置文件失败: %s" % exc)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
