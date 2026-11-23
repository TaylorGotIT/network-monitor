"""领域模型：监控目标、探针结果、监控状态、全局设置。"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, Optional

from .config import normalize_settings
from .metrics import window_stats
from .timeutil import format_ts


@dataclass(frozen=True)
class Target:
    """一个监控目标（不可变，配置变更时替换对象）。

    ``check`` 决定具体探测方式：

    * ICMP：``icmp``
    * TCP：``tcp``（握手）、``tls``（TLS/证书）、``http``、``https``
    * UDP：``udp``（通用回包）、``dns``（校验 DNS 应答）、``ntp``（标准 NTP 请求）
    """

    id: str
    name: str
    host: str
    type: str
    port: Optional[int] = None
    check: str = "icmp"
    sni: Optional[str] = None
    path: str = "/"
    payload: Optional[str] = None
    expect: Optional[str] = None
    retries: int = 2

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Target":
        port = raw.get("port")
        target_type = str(raw.get("type") or "icmp").lower()
        default_check = {"icmp": "icmp", "tcp": "tcp", "udp": "udp"}.get(target_type, "icmp")
        try:
            retries = int(raw.get("retries") or 2)
        except (TypeError, ValueError):
            retries = 2
        sni = str(raw.get("sni") or "").strip() or None
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            host=str(raw["host"]),
            type=target_type,
            port=int(port) if port not in (None, "") else None,
            check=str(raw.get("check") or default_check),
            sni=sni,
            path=str(raw.get("path") or "/"),
            payload=raw.get("payload") or None,
            expect=raw.get("expect") or None,
            retries=retries,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "type": self.type,
            "port": self.port,
            "check": self.check,
            "sni": self.sni,
            "path": self.path,
            "payload": self.payload,
            "expect": self.expect,
            "retries": self.retries,
        }

    @property
    def endpoint(self) -> str:
        """用于展示和故障记录的地址，例如 ``www.baidu.com:443``。"""
        return "%s:%s" % (self.host, self.port) if self.port else self.host


@dataclass(frozen=True)
class ProbeResult:
    """一次探测的结果。

    失败时 ``rtt`` 必须为 None —— 绝不用耗时伪造延迟。
    ``inconclusive=True`` 表示「无法判定」（例如通用 UDP 没有回包），
    这类结果只记录丢包，不会单独触发断网告警。
    """

    ok: bool
    rtt: Optional[float] = None
    error: Optional[str] = None
    inconclusive: bool = False

    @classmethod
    def success(cls, rtt: float) -> "ProbeResult":
        return cls(True, round(float(rtt), 2), None)

    @classmethod
    def failure(cls, error: str, inconclusive: bool = False) -> "ProbeResult":
        return cls(False, None, error, inconclusive)


@dataclass
class Settings:
    """全局设置，字段与 config.DEFAULT_SETTINGS 保持一致。"""

    check_interval: int = 10
    window_size: int = 30
    fail_threshold: int = 5
    mtr_recheck_interval: int = 300
    mtr_ping_count: int = 5
    timeout: float = 3.0
    raw_retention_days: int = 7
    rollup_retention_days: int = 180

    @classmethod
    def from_dict(cls, raw: Any) -> "Settings":
        return cls(**normalize_settings(raw))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TargetState:
    """单个目标的运行状态与状态机。

    ``online`` 取值：``None``（还没有结果）、``True``、``False``（连续失败达到阈值）。
    ``unknown`` 用于通用 UDP 这类「无回包但无法证明故障」的情况，UI 显示「无响应」，
    不产生断网事件。
    """

    def __init__(self, target: Target, window_size: int) -> None:
        self.target = target
        self.history: Deque[Optional[float]] = deque(maxlen=max(2, int(window_size)))
        self.online: Optional[bool] = None
        self.unknown = False
        self.consec_fails = 0
        self.consec_ok = 0
        self.consec_unknown = 0
        self.fail_started_ts: Optional[float] = None
        self.last_probe_rtt: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_check_ts: Optional[float] = None
        self.last_ok_ts: Optional[float] = None
        self.current_fault_node: Optional[str] = None
        self.current_fault_alias: Optional[str] = None
        self.current_fault_ip: Optional[str] = None
        self.active_outage_id: Optional[str] = None
        self.last_mtr_ts = 0.0
        self.trace_running = False

        # 滚动窗口指标
        self.latency: Optional[float] = None
        self.avg: Optional[float] = None
        self.loss = 0.0
        self.jitter = 0.0
        self.samples = 0

    # ------------------------------------------------------------------ 状态
    def refresh_metrics(self) -> None:
        stats = window_stats(list(self.history))
        self.latency = stats["latency"]
        self.avg = stats["avg"]
        self.loss = stats["loss"]
        self.jitter = stats["jitter"]
        self.samples = stats["samples"]

    def resize(self, window_size: int) -> None:
        window_size = max(2, int(window_size))
        if window_size != self.history.maxlen:
            self.history = deque(list(self.history)[-window_size:], maxlen=window_size)
            self.refresh_metrics()

    def record(self, result: ProbeResult, ts: float, fail_threshold: int) -> Optional[str]:
        """写入一次探测结果，返回状态跃变：``"up"`` / ``"down"`` / ``None``。"""
        previous = self.online
        self.history.append(result.rtt)
        self.last_check_ts = ts
        self.last_probe_rtt = result.rtt
        transition: Optional[str] = None

        if result.ok:
            self.consec_fails = 0
            self.consec_ok += 1
            self.consec_unknown = 0
            self.unknown = False
            self.last_error = None
            self.last_ok_ts = ts
            self.fail_started_ts = None
            self.online = True
            self.current_fault_node = None
            self.current_fault_alias = None
            self.current_fault_ip = None
            if previous is False:
                transition = "up"
        elif result.inconclusive:
            # 无法判定的结果：只记录丢包，不累计失败、不触发断网
            self.consec_fails = 0
            self.consec_ok = 0
            self.consec_unknown += 1
            self.last_error = result.error
            if previous is not False and self.consec_unknown >= max(1, int(fail_threshold)):
                self.unknown = True
        else:
            self.consec_fails += 1
            self.consec_ok = 0
            self.consec_unknown = 0
            self.unknown = False
            self.last_error = result.error
            if self.consec_fails == 1:
                self.fail_started_ts = ts
            if previous is not False and self.consec_fails >= max(1, int(fail_threshold)):
                self.online = False
                transition = "down"

        self.refresh_metrics()
        return transition

    @property
    def status(self) -> str:
        """用于前端展示的状态：pending / up / degraded / unknown / down。"""
        if self.online is False:
            return "down"
        if self.unknown:
            return "unknown"
        if self.online is None:
            return "degraded" if self.consec_fails else "pending"
        if self.online:
            return "degraded" if self.consec_fails else "up"
        return "down"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.target.id,
            "name": self.target.name,
            "host": self.target.host,
            "type": self.target.type,
            "port": self.target.port,
            "check": self.target.check,
            "status": self.status,
            "online": self.online,
            "unknown": self.unknown,
            "latency": self.latency,
            "avg": self.avg,
            "loss": self.loss,
            "jitter": self.jitter,
            "samples": self.samples,
            "last_check_ts": self.last_check_ts,
            "last_check": format_ts(self.last_check_ts),
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
            "current_fault_node": self.current_fault_node,
            "current_fault_alias": self.current_fault_alias,
            "current_fault_ip": self.current_fault_ip,
            "consec_fails": self.consec_fails,
            "consec_ok": self.consec_ok,
            "consec_unknown": self.consec_unknown,
            "history": [None if x is None else round(float(x), 2) for x in list(self.history)],
        }
