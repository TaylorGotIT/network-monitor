"""端到端冒烟测试：真实启动服务、真实探测、真实 HTTP 查询，并验证重启后历史仍在。

运行方式::

    .\\venv\\Scripts\\python.exe tests\\smoke_server.py

默认使用本机 127.0.0.1:8888 上已在运行的服务作为「可连通目标」；
若该端口没有服务，则自动改为本地临时 TCP 监听。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
SMOKE_DIR = os.path.join(ROOT, ".smoke")


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


COOKIE = ""


def http_get(url: str, timeout: float = 5.0) -> bytes:
    headers = {"Cookie": COOKIE} if COOKIE else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def get_json(url: str, timeout: float = 5.0):
    return json.loads(http_get(url, timeout).decode("utf-8"))


def login(base_url: str, username: str = "admin", password: str = "admin@123") -> str:
    global COOKIE
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/api/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        COOKIE = response.headers.get("Set-Cookie", "").split(";")[0]
    return COOKIE


def wait_until(predicate, timeout: float, interval: float = 0.4):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(interval)
    raise AssertionError("等待超时，最后状态: %r" % (last,))


def start_server(port: int, config: str, db: str) -> subprocess.Popen:
    log_path = os.path.join(SMOKE_DIR, "server.log")
    log_file = open(log_path, "ab")
    proc = subprocess.Popen(
        [
            PYTHON, os.path.join(ROOT, "app.py"),
            "--host", "127.0.0.1",
            "--port", str(port),
            "--config", config,
            "--db", db,
            "--static", os.path.join(ROOT, "static"),
            "--legacy-logs", os.path.join(SMOKE_DIR, "none.json"),
        ],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    proc._log_file = log_file  # type: ignore[attr-defined]
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
    try:
        proc._log_file.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    if os.path.isdir(SMOKE_DIR):
        shutil.rmtree(SMOKE_DIR)
    os.makedirs(SMOKE_DIR)

    # 找一个可用的本地 TCP 服务作为「在线」目标
    live_sock = socket.socket()
    live_sock.bind(("127.0.0.1", 0))
    live_port = live_sock.getsockname()[1]
    live_sock.listen(8)

    port = free_port()
    config_path = os.path.join(SMOKE_DIR, "config.json")
    db_path = os.path.join(SMOKE_DIR, "monitor.db")

    config = {
        "settings": {
            "check_interval": 1,
            "window_size": 10,
            "fail_threshold": 2,
            "mtr_recheck_interval": 5,
            "mtr_ping_count": 1,
            "timeout": 1.0,
            "raw_retention_days": 7,
            "rollup_retention_days": 180,
        },
        "targets": [
            {"id": "tcp_live", "name": "本地监听端口", "host": "127.0.0.1", "type": "tcp", "port": live_port},
            {"id": "tcp_dead", "name": "未监听端口", "host": "127.0.0.1", "type": "tcp", "port": 1},
            {"id": "icmp_dead", "name": "ICMP 不可达", "host": "192.0.2.1", "type": "icmp", "port": None},
        ],
        "known_nodes": {},
    }
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)

    base_url = "http://127.0.0.1:%d" % port
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        print(("  [PASS] " if condition else "  [FAIL] ") + name + ((" — " + detail) if detail else ""))
        if not condition:
            raise AssertionError(name + " " + detail)

    proc = start_server(port, config_path, db_path)
    try:
        wait_until(lambda: http_get(base_url + "/login", 2), 20)
        print("服务已启动:", base_url)

        unauth_401 = False
        try:
            http_get(base_url + "/api/status", 2)
        except urllib.error.HTTPError as exc:
            unauth_401 = exc.code == 401
        check("未登录访问 API 返回 401", unauth_401)

        cookie = login(base_url)
        check("默认管理员 admin / admin@123 可登录", bool(cookie))
        wait_until(lambda: get_json(base_url + "/api/health", 2), 20)

        def settled():
            status = get_json(base_url + "/api/status")
            nodes = {node["id"]: node for node in status["nodes"]}
            if len(nodes) == 3 and all(nodes[tid]["status"] in ("up", "down") for tid in nodes):
                return nodes
            return None

        nodes = wait_until(settled, 25)
        print("目标状态:", {tid: (n["status"], n["last_error"]) for tid, n in nodes.items()})

        check("可连通 TCP 目标显示在线", nodes["tcp_live"]["status"] == "up", nodes["tcp_live"]["status"])
        check("可连通 TCP 有真实延迟", nodes["tcp_live"]["latency"] is not None, str(nodes["tcp_live"]["latency"]))
        check("不可连通 TCP 判定为断网", nodes["tcp_dead"]["status"] == "down", nodes["tcp_dead"]["status"])
        check(
            "不可连通 TCP 有失败原因",
            any(word in (nodes["tcp_dead"]["last_error"] or "") for word in ("拒绝", "超时", "失败")),
            str(nodes["tcp_dead"]["last_error"]),
        )
        check("不可 ping 的 ICMP 判定为断网", nodes["icmp_dead"]["status"] == "down", nodes["icmp_dead"]["status"])
        check(
            "ICMP 失败原因不是伪造延迟",
            nodes["icmp_dead"]["latency"] is None and bool(nodes["icmp_dead"]["last_error"]),
            str(nodes["icmp_dead"]["last_error"]),
        )

        time.sleep(3)

        history_live = get_json(base_url + "/api/history?target_id=tcp_live&range=1h")
        check(
            "历史查询返回真实点位",
            history_live["summary"]["samples"] >= 3 and history_live["summary"]["avg"] is not None,
            "samples=%s avg=%s" % (history_live["summary"]["samples"], history_live["summary"]["avg"]),
        )
        check("历史序列时间递增", all(
            history_live["series"][i]["t"] <= history_live["series"][i + 1]["t"]
            for i in range(len(history_live["series"]) - 1)
        ))

        history_dead = get_json(base_url + "/api/history?target_id=tcp_dead&range=1h")
        check(
            "断网目标历史丢包率为 100%",
            history_dead["summary"]["loss"] >= 99.0,
            "loss=%s" % history_dead["summary"]["loss"],
        )

        outages = get_json(base_url + "/api/outages?hours=24")["outages"]
        check("断网事件已落库", len(outages) >= 2, "outages=%d" % len(outages))
        check(
            "断网事件包含目标信息",
            all(item["target_name"] and item["start_ts"] for item in outages),
        )

        csv_body = http_get(base_url + "/api/history/export?target_id=tcp_live&range=1h").decode("utf-8")
        check("CSV 导出带 BOM 与表头", csv_body.startswith("\ufeff") and "平均延迟(ms)" in csv_body)

        index_body = http_get(base_url + "/").decode("utf-8")
        check("首页包含历史面板", "历史延迟 / 丢包" in index_body)
        check("静态脚本可访问", b"connectSse" in http_get(base_url + "/static/app.js"))
        check("静态样式可访问", b".chart-wrap" in http_get(base_url + "/static/app.css"))

        before_restart = history_live["summary"]["samples"]
    finally:
        stop_server(proc)

    # 重启服务，验证历史数据持久化
    proc = start_server(port, config_path, db_path)
    try:
        wait_until(lambda: http_get(base_url + "/login", 2), 20)
        login(base_url)
        wait_until(lambda: get_json(base_url + "/api/health", 2), 20)
        time.sleep(2)
        history_after = get_json(base_url + "/api/history?target_id=tcp_live&range=1h")
        check(
            "重启后历史数据仍然存在",
            history_after["summary"]["samples"] >= before_restart,
            "before=%s after=%s" % (before_restart, history_after["summary"]["samples"]),
        )
        check("重启后目标自动恢复监控", len(get_json(base_url + "/api/status")["nodes"]) == 3)

        outages_after = get_json(base_url + "/api/outages?hours=24")["outages"]
        open_tcp = [item for item in outages_after if item["target_id"] == "tcp_dead" and item["status"] == "故障中"]
        check(
            "重启后接管未闭环事件而不是重复创建",
            len(open_tcp) == 1,
            "open(tcp_dead)=%d" % len(open_tcp),
        )
    finally:
        stop_server(proc)
        live_sock.close()

    print("\n全部 %d 项检查通过。" % len(checks))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("\n冒烟测试失败:", exc)
        sys.exit(1)
