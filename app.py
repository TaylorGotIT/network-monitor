"""启动入口：``python app.py``。"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import signal
import sys
from pathlib import Path

from netmon.auth import AuthStore
from netmon.monitor import Monitor
from netmon.web import make_app

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_DB = ROOT / "data" / "monitor.db"
DEFAULT_STATIC = ROOT / "static"
DEFAULT_LEGACY_LOGS = ROOT / "monitor_data.json"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="网络质量与断点监控")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（仅本机访问可设为 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8888, help="监听端口，默认 8888")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="历史数据库路径")
    parser.add_argument("--static", default=str(DEFAULT_STATIC), help="前端静态文件目录")
    parser.add_argument("--legacy-logs", default=str(DEFAULT_LEGACY_LOGS), help="旧版断网日志（用于一次性迁移）")
    parser.add_argument("--debug", action="store_true", help="启用 Tornado 调试模式（自动重载模板/静态文件）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def load_or_create_secret(path: str) -> str:
    """会话 Cookie 的签名密钥；首次启动生成并持久化，避免重启后登录态全部失效。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            secret = handle.read().strip()
        if len(secret) >= 32:
            return secret
    except OSError:
        pass

    secret = secrets.token_urlsafe(48)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(secret + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        print("警告: 无法保存会话密钥（重启后需要重新登录）: %s" % exc)
    return secret


async def serve(args: argparse.Namespace) -> int:
    monitor = Monitor(
        config_path=os.path.abspath(args.config),
        db_path=os.path.abspath(args.db),
        legacy_log_path=args.legacy_logs if os.path.exists(args.legacy_logs) else None,
    )

    data_dir = os.path.dirname(os.path.abspath(args.db))
    auth = AuthStore(os.path.join(data_dir, "auth.db"))
    created_admin = auth.ensure_default_admin()
    cookie_secret = load_or_create_secret(os.path.join(data_dir, "cookie_secret"))

    await monitor.start()

    application = make_app(monitor, args.static, auth, cookie_secret, debug=args.debug)
    try:
        application.listen(args.port, address=args.host)
    except OSError as exc:
        print("启动失败: %s" % exc)
        print("端口 %d 可能已被占用，请先停止旧进程或使用 --port 指定其它端口。" % args.port)
        await monitor.stop()
        auth.close()
        return 1

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop_event.set))
            except (ValueError, OSError):
                pass

    print("网络监控服务已启动: http://127.0.0.1:%d" % args.port)
    print("登录地址: http://127.0.0.1:%d/login" % args.port)
    print("配置文件: %s" % monitor.config_path)
    print("历史数据库: %s" % monitor.store.path)
    print("账号数据库: %s" % auth.path)
    if created_admin:
        print("初始账号: admin / admin@123（登录后请立即修改密码）")
    print("监控目标: %d 个（间隔 %ds，超时 %.1fs）" % (
        len(monitor.targets), monitor.settings.check_interval, monitor.settings.timeout
    ))

    try:
        await stop_event.wait()
    finally:
        await monitor.stop()
        auth.close()
        print("已停止。")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Windows 上 asyncio 的默认事件循环（Proactor）才支持子进程，无需额外设置
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
