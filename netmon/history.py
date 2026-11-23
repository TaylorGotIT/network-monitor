"""历史数据存储（SQLite）。

设计：

* ``probes``      —— 每次探测一行（原始点），按天保留，用于最近几小时的精细曲线；
* ``rollup_1m``   —— 分钟级聚合（次数 / 成功数 / 延迟和、极值 / 抖动差分），
  长期保留，用于 24 小时、7 天、30 天等长区间查询；
* ``outages`` / ``outage_checks`` —— 断网事件及每次 MTR 复检结果。

查询时按区间跨度自动选择数据源，并归并到目标数量的时间桶里，
这样前端拿到的点数始终可控（默认 240 个）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .timeutil import format_ts, parse_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    ok        INTEGER NOT NULL,
    rtt       REAL
);
CREATE INDEX IF NOT EXISTS idx_probes_target_ts ON probes(target_id, ts);

CREATE TABLE IF NOT EXISTS rollup_1m (
    target_id    TEXT    NOT NULL,
    bucket       INTEGER NOT NULL,
    samples      INTEGER NOT NULL DEFAULT 0,
    ok           INTEGER NOT NULL DEFAULT 0,
    sum_rtt      REAL    NOT NULL DEFAULT 0,
    min_rtt      REAL,
    max_rtt      REAL,
    sum_diff     REAL    NOT NULL DEFAULT 0,
    diff_samples INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (target_id, bucket)
);
CREATE INDEX IF NOT EXISTS idx_rollup_bucket ON rollup_1m(bucket);

CREATE TABLE IF NOT EXISTS outages (
    id          TEXT PRIMARY KEY,
    target_id   TEXT NOT NULL,
    target_name TEXT NOT NULL,
    host        TEXT NOT NULL,
    protocol    TEXT NOT NULL,
    start_ts    REAL NOT NULL,
    start_time  TEXT NOT NULL,
    end_ts      REAL,
    end_time    TEXT,
    status      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outages_start ON outages(start_ts);

CREATE TABLE IF NOT EXISTS outage_checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    outage_id  TEXT NOT NULL,
    ts         REAL NOT NULL,
    time_text  TEXT NOT NULL,
    fault_node TEXT,
    hops       TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_outage ON outage_checks(outage_id, ts);
"""

# 归并时间桶的候选值（秒），保证坐标轴刻度是「整」时间
NICE_BUCKETS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800)

RAW_QUERY_MAX_SPAN = 6 * 3600  # 6 小时以内直接查原始点


def choose_bucket(span_seconds: float, max_points: int = 240) -> int:
    """按区间跨度和期望点数选择归并粒度（秒）。"""
    max_points = max(30, int(max_points))
    raw = max(1.0, span_seconds / max_points)
    for bucket in NICE_BUCKETS:
        if bucket >= raw:
            return bucket
    return NICE_BUCKETS[-1]


class HistoryStore:
    """探测历史与断网事件的持久化存储。"""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # 记录每个目标最近一次成功探测的 RTT，用于跨桶计算抖动差分
        self._last_rtt: Dict[str, Optional[float]] = self._load_last_rtt()

    # ------------------------------------------------------------------ 写入
    def _load_last_rtt(self) -> Dict[str, Optional[float]]:
        result: Dict[str, Optional[float]] = {}
        try:
            rows = self._conn.execute(
                """
                SELECT p.target_id AS target_id, p.rtt AS rtt
                FROM probes p
                JOIN (SELECT target_id, MAX(id) AS max_id FROM probes GROUP BY target_id) latest
                  ON p.id = latest.max_id
                """
            ).fetchall()
        except sqlite3.Error:
            return result
        for row in rows:
            result[row["target_id"]] = row["rtt"]
        return result

    def record_probe(self, target_id: str, ts: float, rtt: Optional[float]) -> None:
        """写入一次探测，并同步更新分钟级聚合。"""
        ok = 0 if rtt is None else 1
        previous = self._last_rtt.get(target_id)
        diff: Optional[float] = None
        if ok and previous is not None:
            diff = abs(float(rtt) - float(previous))
        bucket = int(ts // 60)

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO probes(target_id, ts, ok, rtt) VALUES(?, ?, ?, ?)",
                (target_id, float(ts), ok, rtt),
            )
            self._conn.execute(
                """
                INSERT INTO rollup_1m(
                    target_id, bucket, samples, ok, sum_rtt, min_rtt, max_rtt, sum_diff, diff_samples
                ) VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id, bucket) DO UPDATE SET
                    samples      = samples + 1,
                    ok           = ok + excluded.ok,
                    sum_rtt      = sum_rtt + excluded.sum_rtt,
                    min_rtt      = CASE
                                     WHEN excluded.min_rtt IS NULL THEN min_rtt
                                     WHEN min_rtt IS NULL THEN excluded.min_rtt
                                     ELSE MIN(min_rtt, excluded.min_rtt)
                                   END,
                    max_rtt      = CASE
                                     WHEN excluded.max_rtt IS NULL THEN max_rtt
                                     WHEN max_rtt IS NULL THEN excluded.max_rtt
                                     ELSE MAX(max_rtt, excluded.max_rtt)
                                   END,
                    sum_diff     = sum_diff + excluded.sum_diff,
                    diff_samples = diff_samples + excluded.diff_samples
                """,
                (
                    target_id,
                    bucket,
                    ok,
                    float(rtt) if ok else 0.0,
                    float(rtt) if ok else None,
                    float(rtt) if ok else None,
                    float(diff) if diff is not None else 0.0,
                    1 if diff is not None else 0,
                ),
            )
        self._last_rtt[target_id] = float(rtt) if ok else None

    def open_outage(self, outage: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO outages(
                    id, target_id, target_name, host, protocol,
                    start_ts, start_time, end_ts, end_time, status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outage["id"],
                    outage["target_id"],
                    outage["target_name"],
                    outage["host"],
                    outage["protocol"],
                    float(outage["start_ts"]),
                    outage["start_time"],
                    outage.get("end_ts"),
                    outage.get("end_time"),
                    outage.get("status", "故障中"),
                ),
            )

    def close_outage(self, outage_id: str, ts: float, time_text: str, status: str = "已恢复") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE outages SET end_ts = ?, end_time = ?, status = ? WHERE id = ?",
                (float(ts), time_text, status, outage_id),
            )

    def record_outage_check(
        self, outage_id: str, ts: float, time_text: str, fault_node: Optional[str], hops: List[Dict[str, Any]]
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO outage_checks(outage_id, ts, time_text, fault_node, hops) VALUES(?, ?, ?, ?, ?)",
                (outage_id, float(ts), time_text, fault_node, json.dumps(hops or [], ensure_ascii=False)),
            )

    # ------------------------------------------------------------------ 查询
    def query_series(
        self, target_id: str, start: float, end: float, max_points: int = 240
    ) -> Dict[str, Any]:
        """查询一个目标在时间区间内的降采样曲线与汇总。"""
        start, end = float(start), float(end)
        span = max(1.0, end - start)
        bucket = choose_bucket(span, max_points)

        if span <= RAW_QUERY_MAX_SPAN:
            buckets = self._aggregate_raw(target_id, start, end, bucket)
            source = "raw"
        else:
            buckets = self._aggregate_rollup(target_id, start, end, bucket)
            source = "rollup"

        series: List[Dict[str, Any]] = []
        total_samples = total_ok = 0
        sum_rtt = 0.0
        sum_diff = 0.0
        diff_samples = 0
        min_rtt: Optional[float] = None
        max_rtt: Optional[float] = None

        for key in sorted(buckets):
            acc = buckets[key]
            samples = acc["samples"]
            ok = acc["ok"]
            avg = round(acc["sum"] / ok, 2) if ok else None
            loss = round((samples - ok) / samples * 100, 1) if samples else 0.0
            jitter = round(acc["sum_diff"] / acc["diff"], 2) if acc["diff"] else 0.0
            series.append(
                {
                    "t": key * bucket,
                    "avg": avg,
                    "min": round(acc["min"], 2) if acc["min"] is not None else None,
                    "max": round(acc["max"], 2) if acc["max"] is not None else None,
                    "loss": loss,
                    "jitter": jitter,
                    "samples": samples,
                }
            )

            total_samples += samples
            total_ok += ok
            sum_rtt += acc["sum"]
            sum_diff += acc["sum_diff"]
            diff_samples += acc["diff"]
            if acc["min"] is not None:
                min_rtt = acc["min"] if min_rtt is None else min(min_rtt, acc["min"])
            if acc["max"] is not None:
                max_rtt = acc["max"] if max_rtt is None else max(max_rtt, acc["max"])

        summary = {
            "samples": total_samples,
            "ok": total_ok,
            "loss": round((total_samples - total_ok) / total_samples * 100, 1) if total_samples else 0.0,
            "avg": round(sum_rtt / total_ok, 2) if total_ok else None,
            "min": round(min_rtt, 2) if min_rtt is not None else None,
            "max": round(max_rtt, 2) if max_rtt is not None else None,
            "jitter": round(sum_diff / diff_samples, 2) if diff_samples else 0.0,
            "uptime": round(total_ok / total_samples * 100, 2) if total_samples else None,
        }

        return {
            "target_id": target_id,
            "start": start,
            "end": end,
            "bucket": bucket,
            "source": source,
            "series": series,
            "summary": summary,
        }

    def _aggregate_raw(self, target_id: str, start: float, end: float, bucket: int) -> Dict[int, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, ok, rtt FROM probes WHERE target_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                (target_id, start, end),
            ).fetchall()

        buckets: Dict[int, Dict[str, Any]] = {}
        previous: Optional[float] = None
        for row in rows:
            key = int(row["ts"] // bucket)
            acc = buckets.get(key)
            if acc is None:
                acc = {"samples": 0, "ok": 0, "sum": 0.0, "min": None, "max": None, "sum_diff": 0.0, "diff": 0}
                buckets[key] = acc
            acc["samples"] += 1
            rtt = row["rtt"]
            if rtt is None:
                previous = None
                continue
            rtt = float(rtt)
            acc["ok"] += 1
            acc["sum"] += rtt
            acc["min"] = rtt if acc["min"] is None else min(acc["min"], rtt)
            acc["max"] = rtt if acc["max"] is None else max(acc["max"], rtt)
            if previous is not None:
                acc["sum_diff"] += abs(rtt - previous)
                acc["diff"] += 1
            previous = rtt
        return buckets

    def _aggregate_rollup(self, target_id: str, start: float, end: float, bucket: int) -> Dict[int, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT bucket, samples, ok, sum_rtt, min_rtt, max_rtt, sum_diff, diff_samples
                FROM rollup_1m
                WHERE target_id = ? AND bucket >= ? AND bucket < ?
                ORDER BY bucket
                """,
                (target_id, int(start // 60), int(end // 60) + 1),
            ).fetchall()

        buckets: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            key = int(row["bucket"] * 60 // bucket)
            acc = buckets.get(key)
            if acc is None:
                acc = {"samples": 0, "ok": 0, "sum": 0.0, "min": None, "max": None, "sum_diff": 0.0, "diff": 0}
                buckets[key] = acc
            acc["samples"] += row["samples"]
            acc["ok"] += row["ok"]
            acc["sum"] += row["sum_rtt"]
            acc["sum_diff"] += row["sum_diff"]
            acc["diff"] += row["diff_samples"]
            if row["min_rtt"] is not None:
                acc["min"] = row["min_rtt"] if acc["min"] is None else min(acc["min"], row["min_rtt"])
            if row["max_rtt"] is not None:
                acc["max"] = row["max_rtt"] if acc["max"] is None else max(acc["max"], row["max_rtt"])
        return buckets

    def open_outages(self) -> List[Dict[str, Any]]:
        """返回所有尚未闭环（``end_ts`` 为空）的断网事件。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outages WHERE end_ts IS NULL ORDER BY start_ts"
            ).fetchall()
        return [dict(row) for row in rows]

    def query_outages(
        self,
        target_id: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """查询断网事件（含每次 MTR 复检结果），按开始时间倒序。"""
        clauses = []
        params: List[Any] = []
        if target_id:
            clauses.append("target_id = ?")
            params.append(target_id)
        if start is not None:
            clauses.append("start_ts >= ?")
            params.append(float(start))
        if end is not None:
            clauses.append("start_ts < ?")
            params.append(float(end))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(500, int(limit))))

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outages%s "
                "ORDER BY CASE WHEN end_ts IS NULL THEN 0 ELSE 1 END, start_ts DESC LIMIT ?" % where,
                params,
            ).fetchall()
            checks_by_outage: Dict[str, List[Dict[str, Any]]] = {}
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                check_rows = self._conn.execute(
                    "SELECT outage_id, ts, time_text, fault_node, hops FROM outage_checks "
                    "WHERE outage_id IN (%s) ORDER BY ts" % placeholders,
                    ids,
                ).fetchall()
                for check in check_rows:
                    try:
                        hops = json.loads(check["hops"]) if check["hops"] else []
                    except (TypeError, ValueError):
                        hops = []
                    checks_by_outage.setdefault(check["outage_id"], []).append(
                        {
                            "ts": check["ts"],
                            "time": check["time_text"],
                            "fault_node": check["fault_node"],
                            "hops": hops,
                        }
                    )

        now = time.time()
        result: List[Dict[str, Any]] = []
        for row in rows:
            end_ts = row["end_ts"]
            duration = int((end_ts if end_ts is not None else now) - row["start_ts"])
            result.append(
                {
                    "id": row["id"],
                    "target_id": row["target_id"],
                    "target_name": row["target_name"],
                    "host": row["host"],
                    "protocol": row["protocol"],
                    "start_ts": row["start_ts"],
                    "start_time": row["start_time"],
                    "end_ts": end_ts,
                    "end_time": row["end_time"],
                    "status": row["status"],
                    "duration_sec": max(0, duration),
                    "checks": checks_by_outage.get(row["id"], []),
                }
            )
        return result

    # ------------------------------------------------------------------ 维护
    def prune(self, raw_retention_days: int, rollup_retention_days: int, now: Optional[float] = None) -> Dict[str, int]:
        """清理过期数据。"""
        now = time.time() if now is None else float(now)
        raw_cutoff = now - max(1, int(raw_retention_days)) * 86400
        rollup_cutoff = int((now - max(1, int(rollup_retention_days)) * 86400) // 60)

        with self._lock, self._conn:
            raw_deleted = self._conn.execute("DELETE FROM probes WHERE ts < ?", (raw_cutoff,)).rowcount
            rollup_deleted = self._conn.execute(
                "DELETE FROM rollup_1m WHERE bucket < ?", (rollup_cutoff,)
            ).rowcount
            orphan_checks = self._conn.execute(
                "DELETE FROM outage_checks WHERE outage_id NOT IN (SELECT id FROM outages)"
            ).rowcount
        return {
            "probes": max(0, raw_deleted),
            "rollup": max(0, rollup_deleted),
            "checks": max(0, orphan_checks),
        }

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            probes = self._conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
            rollups = self._conn.execute("SELECT COUNT(*) FROM rollup_1m").fetchone()[0]
            outages = self._conn.execute("SELECT COUNT(*) FROM outages").fetchone()[0]
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        return {
            "path": self.path,
            "size_bytes": size,
            "probes": probes,
            "rollup_rows": rollups,
            "outages": outages,
        }

    def import_legacy_logs(self, path: str) -> int:
        """把旧版 ``monitor_data.json`` 里的事件导入数据库（仅当库中还没有事件）。"""
        if not path or not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:  # noqa: BLE001
            return 0
        if not isinstance(data, list) or not data:
            return 0

        with self._lock:
            existing = self._conn.execute("SELECT COUNT(*) FROM outages").fetchone()[0]
            if existing:
                return 0

        imported = 0
        for log in data:
            if not isinstance(log, dict) or not log.get("id"):
                continue
            start_ts = parse_ts(log.get("start_time")) or 0.0
            end_text = log.get("end_time")
            end_ts = parse_ts(end_text) if end_text and end_text != "持续中..." else None
            try:
                self.open_outage(
                    {
                        "id": str(log["id"]),
                        "target_id": str(log.get("target_id") or str(log["id"]).split("_")[0]),
                        "target_name": str(log.get("target_name") or "未知目标"),
                        "host": str(log.get("host") or ""),
                        "protocol": str(log.get("protocol") or ""),
                        "start_ts": start_ts,
                        "start_time": log.get("start_time") or format_ts(start_ts) or "",
                        "end_ts": end_ts,
                        "end_time": end_text,
                        "status": log.get("status") or "已恢复",
                    }
                )
            except sqlite3.Error:
                continue
            for check in log.get("mtr_checks") or []:
                try:
                    self.record_outage_check(
                        str(log["id"]),
                        parse_ts(check.get("time")) or 0.0,
                        str(check.get("time") or ""),
                        check.get("fault_node"),
                        check.get("hops") or [],
                    )
                except sqlite3.Error:
                    continue
            imported += 1
        return imported

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
