"""非官方接口的长期可用性探针（自包含，零第三方依赖）.

目的：B站 / 网易云的公开 Web 接口一旦改版或风控收紧，当天就能从本地数据看出来，
而不是等用户发现搜不出东西。

- 每次请求落一行：时间 / 来源 / 端点路径（不含参数）/ 成败 / HTTP 码或异常名 / 耗时 / 是否风控
- 只记路径不记查询串：搜索词留在用户那里，不进库
- 任何异常都内部吞掉：探针永远不能拖垮主流程
- 库在 data/probe.sqlite3（gitignore，不入仓），只保留最近 MAX_KEEP_DAYS 天
"""
from __future__ import annotations

import os
import sqlite3
import time

PROBE_DB = "data/probe.sqlite3"
MAX_KEEP_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    source   TEXT,
    endpoint TEXT,
    ok       INTEGER,
    code     TEXT,
    ms       INTEGER,
    risk     INTEGER
);
CREATE INDEX IF NOT EXISTS probes_source_ts ON probes(source, ts);
CREATE INDEX IF NOT EXISTS probes_ts ON probes(ts);
"""


def _conn(db_path: str | None = None) -> sqlite3.Connection:
    """打开（必要时建库建表）：父目录自动建，超时 5 秒。"""
    path = db_path or PROBE_DB
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.executescript(_SCHEMA)
    return conn


def record(source: str, endpoint: str, *, ok: bool, code: str = "", ms: int = 0,
           risk: bool = False, db_path: str | None = None) -> None:
    """记一条探针。失败只打一行日志，绝不向上抛（探针是旁路，不是主流程）。"""
    try:
        conn = _conn(db_path)
        try:
            conn.execute(
                "INSERT INTO probes(ts, source, endpoint, ok, code, ms, risk) "
                "VALUES(?,?,?,?,?,?,?)",
                (int(time.time()), str(source), str(endpoint or "/"),
                 1 if ok else 0, str(code or "")[:120], int(ms or 0),
                 1 if risk else 0))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 故意全吞
        print(f"[probe] record failed: {exc!r}", flush=True)


def _percentile(values: list[int], p: float) -> int:
    """排序后取分位（线性插值取最近整数）；空列表返回 0，单样本直接返回该值。"""
    if not values:
        return 0
    if len(values) == 1:
        return int(values[0])
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * p))
    return int(ordered[max(0, min(len(ordered) - 1, idx))])


def stats(days: int = 7, db_path: str | None = None) -> dict:
    """最近 days 天的汇总：每个 source 的成败 / 风控 / p50 / p95。"""
    days = max(1, int(days))
    since = int(time.time()) - days * 86400
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT source, ok, risk, ms FROM probes WHERE ts >= ?", (since,)
        ).fetchall()
    finally:
        conn.close()

    buckets: dict[str, dict] = {}
    for source, ok, risk, ms in rows:
        name = str(source or "")
        b = buckets.setdefault(name, {"total": 0, "ok": 0, "fail": 0, "risk": 0, "ms": []})
        b["total"] += 1
        if ok:
            b["ok"] += 1
        else:
            b["fail"] += 1
        if risk:
            b["risk"] += 1
        b["ms"].append(int(ms or 0))

    sources: dict[str, dict] = {}
    for name, b in buckets.items():
        sources[name] = {
            "total": b["total"],
            "ok": b["ok"],
            "fail": b["fail"],
            "ok_rate": round(b["ok"] / b["total"], 4) if b["total"] else 0.0,
            "risk": b["risk"],
            "p50_ms": _percentile(b["ms"], 0.50),
            "p95_ms": _percentile(b["ms"], 0.95),
        }
    return {
        "since": since,
        "days": days,
        "sources": sources,
        "total": sum(b["total"] for b in buckets.values()),
    }


def recent(limit: int = 50, db_path: str | None = None) -> list[dict]:
    """最近的探针明细（新→旧）。"""
    limit = max(1, min(1000, int(limit or 50)))
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, ts, source, endpoint, ok, code, ms, risk FROM probes "
            "ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "ts": r[1], "source": r[2], "endpoint": r[3],
         "ok": bool(r[4]), "code": r[5] or "", "ms": int(r[6] or 0),
         "risk": bool(r[7])}
        for r in rows
    ]


def prune(keep_days: int = MAX_KEEP_DAYS, db_path: str | None = None) -> int:
    """删掉 ts 早于 keep_days 的行，返回删除行数。"""
    keep_days = max(1, int(keep_days))
    cutoff = int(time.time()) - keep_days * 86400
    conn = _conn(db_path)
    try:
        cur = conn.execute("DELETE FROM probes WHERE ts < ?", (cutoff,))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()
