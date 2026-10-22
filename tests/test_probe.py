"""非官方接口探针回归（离线，零网络）。

覆盖：
  - 记录与统计：总数 / 成功 / 失败 / 风控 / 成功率
  - 分位：1..100ms 各一条，p50 与 p95 必须落在 ±1 内
  - recent 倒序 + limit 生效
  - prune：40 天前的行被删，30 天内的留着
  - record 绝不上抛：库路径不可写时也只吞掉并打印一行

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_probe.py
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import probe

RUN_DIR = os.path.join(ROOT, "tests", ".run")
DAY = 86400


def _fresh_db(tag: str) -> str:
    """每个用例一个临时库（带 pid，避免并发互踩）。"""
    os.makedirs(RUN_DIR, exist_ok=True)
    db = os.path.join(RUN_DIR, f"probe_{os.getpid()}_{tag}.sqlite3")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    return db


def _drop_db(db: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass


def _touch_ts(db: str, row_id: int, ts: int) -> None:
    """时间旅行：record 只写 now，测 prune 得直接改 ts。"""
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute("UPDATE probes SET ts=? WHERE id=?", (int(ts), int(row_id)))
        conn.commit()
    finally:
        conn.close()


def _last_id(db: str) -> int:
    conn = sqlite3.connect(db, timeout=10)
    try:
        return int(conn.execute("SELECT MAX(id) FROM probes").fetchone()[0])
    finally:
        conn.close()


def case_record_and_stats() -> tuple[bool, dict]:
    """5 条：4 成 1 败，其中 1 条风控。"""
    db = _fresh_db("stat")
    try:
        for i in range(4):
            probe.record("bilibili", f"/x/test/{i}", ok=True, ms=100 + i, db_path=db)
        probe.record("bilibili", "/x/test/fail", ok=False, code="412", ms=900,
                     risk=True, db_path=db)
        st = probe.stats(days=7, db_path=db)
    finally:
        _drop_db(db)
    src = (st.get("sources") or {}).get("bilibili") or {}
    ok = (
        st.get("total") == 5
        and st.get("days") == 7
        and src.get("total") == 5
        and src.get("ok") == 4
        and src.get("fail") == 1
        and src.get("risk") == 1
        and src.get("ok_rate") == 0.8
    )
    return ok, {"total": st.get("total"), "src": src}


def case_percentile() -> tuple[bool, dict]:
    """1..100ms 各一条：p50≈50，p95≈95。"""
    db = _fresh_db("pct")
    try:
        for ms in range(1, 101):
            probe.record("netease", "/api/song/lyric", ok=True, ms=ms, db_path=db)
        st = probe.stats(days=7, db_path=db)
    finally:
        _drop_db(db)
    src = (st.get("sources") or {}).get("netease") or {}
    p50, p95 = src.get("p50_ms"), src.get("p95_ms")
    ok = (
        src.get("total") == 100
        and isinstance(p50, int) and abs(p50 - 50) <= 1
        and isinstance(p95, int) and abs(p95 - 95) <= 1
    )
    return ok, {"p50": p50, "p95": p95}


def case_single_sample() -> tuple[bool, dict]:
    """只有 1 条样本时 p50/p95 直接取该值，不报错。"""
    db = _fresh_db("one")
    try:
        probe.record("bilibili", "/x/web-interface/view", ok=True, ms=321, db_path=db)
        st = probe.stats(days=7, db_path=db)
    finally:
        _drop_db(db)
    src = (st.get("sources") or {}).get("bilibili") or {}
    ok = src.get("p50_ms") == 321 and src.get("p95_ms") == 321
    return ok, {"p50": src.get("p50_ms"), "p95": src.get("p95_ms")}


def case_recent_order() -> tuple[bool, dict]:
    """recent 按 ts 倒序（同秒按 id 倒序），limit 生效。"""
    db = _fresh_db("recent")
    try:
        for tag in ("a", "b", "c"):
            probe.record("bilibili", f"/x/{tag}", ok=True, ms=10, db_path=db)
        all_rows = probe.recent(limit=50, db_path=db)
        two = probe.recent(limit=2, db_path=db)
    finally:
        _drop_db(db)
    order = [r.get("endpoint") for r in all_rows]
    ok = (
        order == ["/x/c", "/x/b", "/x/a"]
        and [r.get("endpoint") for r in two] == ["/x/c", "/x/b"]
        and len(all_rows) == 3
    )
    return ok, {"order": order, "limit2": [r.get("endpoint") for r in two]}


def case_prune() -> tuple[bool, dict]:
    """两条 40 天前、两条全新的：prune(30) 删 2，只留新的。"""
    db = _fresh_db("prune")
    now = int(time.time())
    try:
        probe.record("bilibili", "/old/1", ok=True, ms=1, db_path=db)
        old1 = _last_id(db)
        probe.record("bilibili", "/old/2", ok=False, code="412", ms=1, db_path=db)
        old2 = _last_id(db)
        probe.record("netease", "/new/1", ok=True, ms=1, db_path=db)
        probe.record("netease", "/new/2", ok=True, ms=1, db_path=db)
        _touch_ts(db, old1, now - 40 * DAY)
        _touch_ts(db, old2, now - 40 * DAY)
        deleted = probe.prune(keep_days=30, db_path=db)
        left = [r.get("endpoint") for r in probe.recent(limit=50, db_path=db)]
    finally:
        _drop_db(db)
    ok = deleted == 2 and sorted(left) == ["/new/1", "/new/2"]
    return ok, {"deleted": deleted, "left": left}


def case_record_never_raises() -> tuple[bool, dict]:
    """库路径不可写：record 只打日志，绝不抛。"""
    raised = ""
    try:
        probe.record("bilibili", "/x/web-interface/view", ok=True, ms=5,
                     db_path="Z:/nope/x.sqlite3")
    except Exception as exc:  # noqa: BLE001
        raised = repr(exc)
    return (raised == ""), {"raised": raised}


CASES = [
    ("record_and_stats", "记录 + 统计口径", case_record_and_stats),
    ("percentile", "p50/p95 分位", case_percentile),
    ("single_sample", "单样本分位不报错", case_single_sample),
    ("recent_order", "recent 倒序与 limit", case_recent_order),
    ("prune", "按保留天数清理", case_prune),
    ("record_never_raises", "record 绝不上抛", case_record_never_raises),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== 接口探针回归（{len(CASES)} 条，离线）==")
    all_ok = True
    passed = 0
    for cid, label, fn in CASES:
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, {"error": repr(exc)}
        all_ok = all_ok and ok
        passed += 1 if ok else 0
        print(f"[{cid}] {label} —— {'PASS' if ok else 'FAIL'}"
              f"（{round(time.time() - t0, 2)}s）  {detail}")
    print(f"\n== 汇总: {passed}/{len(CASES)} PASS ==")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
