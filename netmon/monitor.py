"""监控编排层：探测定时器、状态机、断网事件、SSE 广播。

探测与解析在 :mod:`netmon.probes` / :mod:`netmon.traceroute`，
持久化在 :mod:`netmon.history`，HTTP 在 :mod:`netmon.web`。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from . import config as config_mod
from .history import HistoryStore
from .models import ProbeResult, Settings, Target, TargetState
from .probes import make_probe
from .timeutil import format_ts, now_str
from .traceroute import RouteTracer

log = logging.getLogger(__name__)


def _port_failure_reason(error: Optional[str]) -> str:
    """把底层探测错误压缩成卡片上能读的一句话。"""
    error = (error or "探测失败").strip()
    if "TLS 证书" in error:
        return "TLS 证书校验失败"
    if "TLS" in error:
        return "TLS 握手失败"
    if error.startswith("HTTP 状态码"):
        return error
    if "拒绝" in error:
        return "连接被拒绝，服务未监听"
    if "超时" in error:
        return "连接超时，可能被防火墙拦截"
    if "不可达" in error:
        return "端口不可达"
    if "无响应" in error:
        return "无响应，可能被过滤或服务不回应"
    return error[:40]


class Monitor:
    """一个进程内的监控实例。"""

    def __init__(self, config_path: str, db_path: str, legacy_log_path: Optional[str] = None) -> None:
        self.config_path = config_path
        self.config = config_mod.load_config(config_path)
        self.settings = Settings.from_dict(self.config["settings"])
        self.store = HistoryStore(db_path)

        if legacy_log_path:
            try:
                imported = self.store.import_legacy_logs(legacy_log_path)
                if imported:
                    log.info("已从旧版日志导入 %d 条断网事件", imported)
            except Exception:  # noqa: BLE001 - 迁移失败不影响启动
                log.exception("导入旧版日志失败")

        self.targets: Dict[str, Target] = {}
        self.states: Dict[str, TargetState] = {}
        for raw in self.config["targets"]:
            target = Target.from_dict(raw)
            self.targets[target.id] = target
            self.states[target.id] = TargetState(target, self.settings.window_size)

        self.tasks: Dict[str, asyncio.Task] = {}
        self._trace_tasks: Set[asyncio.Task] = set()
        self._prune_task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self.sse_clients: Set[asyncio.Queue] = set()
        self.started_ts: Optional[float] = None
        self._adopt_open_outages()

    def _adopt_open_outages(self) -> None:
        """服务重启后接管未闭环的断网事件。

        目标仍存在时复用原事件（恢复时补上结束时间）；目标已被删除的事件
        直接以「已移除」收尾，避免永远停在「故障中」。
        """
        try:
            open_list = self.store.open_outages()
        except Exception:  # noqa: BLE001
            log.exception("读取未闭环断网事件失败")
            return
        if not open_list:
            return

        now = time.time()
        adopted: Set[str] = set()
        for outage in open_list:
            target_id = outage.get("target_id")
            state = self.states.get(target_id)
            if state is not None and target_id not in adopted:
                state.active_outage_id = outage["id"]
                adopted.add(target_id)
                continue
            try:
                self.store.close_outage(
                    outage["id"], now, format_ts(now) or now_str(), status="已移除"
                )
            except Exception:  # noqa: BLE001
                log.exception("关闭失效断网事件失败: %s", outage.get("id"))

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self._stop = asyncio.Event()
        self.started_ts = time.time()
        for index, target_id in enumerate(list(self.targets)):
            self.tasks[target_id] = asyncio.create_task(
                self._run_target(target_id, stagger=0.15 * index), name="probe:%s" % target_id
            )
        self._prune_task = asyncio.create_task(self._prune_loop(), name="prune")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        pending = list(self.tasks.values()) + list(self._trace_tasks)
        if self._prune_task is not None:
            pending.append(self._prune_task)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self._trace_tasks.clear()
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭数据库失败")

    # ---------------------------------------------------------------- 探测循环
    async def _run_target(self, target_id: str, stagger: float = 0.0) -> None:
        try:
            if stagger:
                await asyncio.sleep(stagger)
            while self._stop is not None and not self._stop.is_set():
                target = self.targets.get(target_id)
                state = self.states.get(target_id)
                if target is None or state is None:
                    return
                try:
                    result = await self._probe(target)
                    now = time.time()
                    transition = state.record(result, now, self.settings.fail_threshold)

                    try:
                        self.store.record_probe(target_id, now, result.rtt)
                    except Exception:  # noqa: BLE001
                        log.exception("写入探测历史失败: %s", target_id)

                    if transition == "down":
                        self._open_outage(state, target, now)
                    elif state.online is True and state.active_outage_id:
                        # 包含两种恢复：正常运行中的 down -> up，以及重启后接管的事件
                        self._close_outage(state, now)

                    if state.online is False:
                        self._maybe_trace(state, target, now)

                    self.broadcast()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 单次异常不应终止该目标的监控
                    log.exception("监控循环异常: %s", target_id)
                    await asyncio.sleep(2.0)
                await self._sleep_interval()
        except asyncio.CancelledError:
            raise

    async def _probe(self, target: Target) -> ProbeResult:
        try:
            probe = make_probe(target)
            return await probe.measure(self.settings.timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("探测执行失败: %s", target.endpoint)
            return ProbeResult.failure("探测异常: %s" % exc)

    async def _sleep_interval(self) -> None:
        if self._stop is None:
            return
        interval = max(1, int(self.settings.check_interval))
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    # ---------------------------------------------------------------- 断网事件
    def _open_outage(self, state: TargetState, target: Target, now: float) -> None:
        if state.active_outage_id:
            return
        start_ts = state.fail_started_ts or now
        outage_id = "%s_%d" % (target.id, int(now))
        state.active_outage_id = outage_id
        outage = {
            "id": outage_id,
            "target_id": target.id,
            "target_name": target.name,
            "host": target.endpoint,
            "protocol": target.type.upper(),
            "start_ts": start_ts,
            "start_time": format_ts(start_ts) or now_str(),
            "end_ts": None,
            "end_time": None,
            "status": "故障中",
        }
        try:
            self.store.open_outage(outage)
        except Exception:  # noqa: BLE001
            log.exception("写入断网事件失败: %s", outage_id)

    def _close_outage(self, state: TargetState, now: float, status: str = "已恢复") -> None:
        outage_id = state.active_outage_id
        if not outage_id:
            return
        state.active_outage_id = None
        state.current_fault_node = None
        state.current_fault_alias = None
        state.current_fault_ip = None
        try:
            self.store.close_outage(outage_id, now, format_ts(now) or now_str(), status=status)
        except Exception:  # noqa: BLE001
            log.exception("关闭断网事件失败: %s", outage_id)

    def _maybe_trace(self, state: TargetState, target: Target, now: float) -> None:
        """断网期间按周期做路由追踪；放到后台任务里，避免阻塞常规探测。"""
        if state.trace_running:
            return
        interval = max(5, int(self.settings.mtr_recheck_interval or 0))
        if state.last_mtr_ts and now - state.last_mtr_ts < interval:
            return
        state.last_mtr_ts = now
        state.trace_running = True
        task = asyncio.create_task(self._trace_target(state, target), name="trace:%s" % target.id)
        self._trace_tasks.add(task)
        task.add_done_callback(self._trace_tasks.discard)

    async def _trace_target(self, state: TargetState, target: Target) -> None:
        try:
            tracer = RouteTracer(self.config.get("known_nodes") or {}, self.settings.mtr_ping_count)
            result = await tracer.trace(target.host)
            fault_node = result.get("fault_node")
            if (
                state.online is False
                and target.type in ("tcp", "udp")
                and target.port
                and result.get("reached")
            ):
                # 路由能到达主机，但端口/TLS/HTTP 检查失败 —— 不能写成「已到达」了事
                fault_node = "[%s] 主机可达，端口 %d 不通（%s）" % (
                    result.get("tool") or "trace",
                    target.port,
                    _port_failure_reason(state.last_error),
                )
            if state.online is False:
                state.current_fault_node = fault_node
                state.current_fault_alias = result.get("fault_alias") or None
                state.current_fault_ip = result.get("fault_ip")
            if state.active_outage_id:
                self.store.record_outage_check(
                    state.active_outage_id,
                    time.time(),
                    now_str(),
                    fault_node,
                    result.get("hops") or [],
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("路由追踪失败: %s", target.endpoint)
        finally:
            state.trace_running = False
            self.broadcast()

    async def _prune_loop(self) -> None:
        while self._stop is not None and not self._stop.is_set():
            try:
                deleted = self.store.prune(
                    self.settings.raw_retention_days, self.settings.rollup_retention_days
                )
                if deleted.get("probes"):
                    log.info("已清理过期原始探测点 %d 条", deleted["probes"])
            except Exception:  # noqa: BLE001
                log.exception("清理历史数据失败")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------------- 快照/广播
    def snapshot(self) -> Dict[str, Any]:
        nodes = []
        known_nodes = self.config.get("known_nodes") or {}
        for raw in self.config["targets"]:
            state = self.states.get(raw["id"])
            if state is None:
                continue
            node = state.to_dict()
            # 别名可能在追踪之后才添加/修改，读取快照时实时补上
            if node.get("current_fault_ip") and not node.get("current_fault_alias"):
                node["current_fault_alias"] = known_nodes.get(node["current_fault_ip"]) or None
            nodes.append(node)
        return {
            "nodes": nodes,
            "config": self.config,
            "server_time": now_str(),
            "server_ts": time.time(),
            "sse_clients": len(self.sse_clients),
        }

    def snapshot_json(self) -> str:
        import json

        return json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":"))

    def broadcast(self) -> None:
        if not self.sse_clients:
            return
        payload = self.snapshot_json()
        for queue in list(self.sse_clients):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ---------------------------------------------------------------- 配置操作
    def add_target(
        self, name: str, host: str, target_type: str, port: Any, check: Optional[str] = None
    ) -> Dict[str, Any]:
        name = (name or "").strip()
        host = (host or "").strip()
        target_type = (target_type or "").strip().lower()
        if not name or not host:
            raise ValueError("名称和主机地址不能为空")
        if target_type not in config_mod.PROTOCOLS:
            raise ValueError("不支持的探测协议: %s" % target_type)
        if target_type in ("tcp", "udp") and port in (None, "", 0, "0"):
            raise ValueError("TCP/UDP 协议必须指定端口号")

        normalized = config_mod.normalize_target(
            {
                "name": name,
                "host": host,
                "type": target_type,
                "port": port,
                "check": check,
            }
        )
        if not normalized:
            raise ValueError("主机地址或端口不合法")

        while normalized["id"] in self.targets:
            normalized["id"] = "t_%s" % uuid.uuid4().hex[:8]

        self.config["targets"].append(normalized)
        target = Target.from_dict(normalized)
        self.targets[target.id] = target
        self.states[target.id] = TargetState(target, self.settings.window_size)
        config_mod.save_config(self.config_path, self.config)

        if self._stop is not None and not self._stop.is_set():
            self.tasks[target.id] = asyncio.create_task(
                self._run_target(target.id), name="probe:%s" % target.id
            )
        self.broadcast()
        return normalized

    def remove_target(self, target_id: str) -> None:
        if target_id not in self.targets:
            raise KeyError("目标不存在")
        task = self.tasks.pop(target_id, None)
        if task is not None:
            task.cancel()

        state = self.states.pop(target_id, None)
        if state is not None and state.active_outage_id:
            self._close_outage(state, time.time(), status="已移除")

        del self.targets[target_id]
        self.config["targets"] = [t for t in self.config["targets"] if t["id"] != target_id]
        config_mod.save_config(self.config_path, self.config)
        self.broadcast()

    def update_settings(self, patch: Any) -> Dict[str, Any]:
        previous_window = self.settings.window_size
        settings = Settings.from_dict(config_mod.apply_settings_patch(self.settings.to_dict(), patch))
        self.settings = settings
        self.config["settings"] = settings.to_dict()
        config_mod.save_config(self.config_path, self.config)

        if settings.window_size != previous_window:
            for state in self.states.values():
                state.resize(settings.window_size)
        self.broadcast()
        return settings.to_dict()

    def add_alias(self, ip: str, name: str) -> None:
        ip = (ip or "").strip()
        name = (name or "").strip()
        if not ip or not name:
            raise ValueError("IP 和名称不能为空")
        self.config["known_nodes"][ip] = name
        config_mod.save_config(self.config_path, self.config)
        self.broadcast()

    def remove_alias(self, ip: str) -> None:
        ip = (ip or "").strip()
        if ip in self.config["known_nodes"]:
            del self.config["known_nodes"][ip]
            config_mod.save_config(self.config_path, self.config)
            self.broadcast()

    # ---------------------------------------------------------------- 查询接口
    def history(self, target_id: str, start: float, end: float, max_points: int = 240) -> Dict[str, Any]:
        if target_id not in self.targets:
            raise KeyError("目标不存在")
        data = self.store.query_series(target_id, start, end, max_points)
        target = self.targets[target_id]
        data["target"] = target.to_dict()
        return data

    def outages(self, hours: float = 24.0, target_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        now = time.time()
        start = now - max(0.1, float(hours)) * 3600
        return self.store.query_outages(target_id=target_id or None, start=start, end=now + 1, limit=limit)

    def health(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "targets": len(self.targets),
            "sse_clients": len(self.sse_clients),
            "uptime_sec": int(time.time() - self.started_ts) if self.started_ts else 0,
            "settings": self.settings.to_dict(),
            "storage": self.store.stats(),
        }
