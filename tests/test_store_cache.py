"""歌词缓存 TTL / 容量清理回归（离线，零网络）。

覆盖：
  - 负缓存（空串歌词）24 小时过期、正缓存（有歌词）90 天过期
  - 容量淘汰：超过上限按 fetched_at 最旧先删
  - 清理幂等：连跑两次第二次删 0 行
  - 连接关闭：run_netease_lyric 必须 close（文本断言 + 连接行为）

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_store_cache.py
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import store

RUN_DIR = os.path.join(ROOT, "tests", ".run")
NOW = int(time.time())
HOUR = 3600
DAY = 24 * HOUR


def _fresh_db(tag: str) -> str:
    """每个用例一个临时库（带 pid，避免并发互踩）。"""
    os.makedirs(RUN_DIR, exist_ok=True)
    db = os.path.join(RUN_DIR, f"store_cache_{os.getpid()}_{tag}.sqlite3")
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


def _put(idx: store.Index, song_id: int, lrc: str, fetched_at: int) -> None:
    """直接写库：set_lyric 只写 now，时间旅行得绕过它。"""
    idx.conn.execute(
        "INSERT INTO lyrics(song_id, lrc, fetched_at) VALUES(?,?,?) "
        "ON CONFLICT(song_id) DO UPDATE SET lrc=excluded.lrc, fetched_at=excluded.fetched_at",
        (int(song_id), lrc, int(fetched_at)))
    idx.conn.commit()


def case_negative_ttl() -> tuple[bool, dict]:
    """空串 = 负缓存：过期后必须重抓；未过期仍返回空串（不重抓）。"""
    db = _fresh_db("neg")
    idx = store.Index(db)
    try:
        _put(idx, 1, "", NOW - 25 * HOUR)   # 25 小时前抓过，没抓到
        _put(idx, 2, "", NOW - 1 * HOUR)    # 1 小时前抓过，没抓到
        expired = idx.get_lyric(1)
        fresh = idx.get_lyric(2)
    finally:
        idx.conn.close()
        _drop_db(db)
    ok = expired is None and fresh == "" and fresh is not None
    return ok, {"25h": expired, "1h": fresh}


def case_positive_ttl() -> tuple[bool, dict]:
    """有歌词 = 正缓存：90 天过期，89 天仍命中。"""
    db = _fresh_db("pos")
    idx = store.Index(db)
    try:
        _put(idx, 1, "[00:00.00]过期歌词", NOW - 91 * DAY)
        _put(idx, 2, "[00:00.00]新鲜歌词", NOW - 89 * DAY)
        old = idx.get_lyric(1)
        kept = idx.get_lyric(2)
    finally:
        idx.conn.close()
        _drop_db(db)
    ok = old is None and kept == "[00:00.00]新鲜歌词"
    return ok, {"91d": old, "89d": kept}


def _seed_capacity(idx: store.Index) -> None:
    """8 行，id 越大越新（id 8 最新、id 1 最旧）。"""
    for i in range(1, 9):
        _put(idx, i, f"[00:00.00]第{i}行", NOW - (9 - i) * HOUR)


def case_capacity_evict() -> tuple[bool, dict]:
    """8 行上限 5 → 删 3 行，留下的是最新 5 条。"""
    db = _fresh_db("cap")
    idx = store.Index(db)
    try:
        _seed_capacity(idx)
        deleted = idx.prune_lyrics(max_rows=5)
        left = [r[0] for r in idx.conn.execute(
            "SELECT song_id FROM lyrics ORDER BY song_id").fetchall()]
    finally:
        idx.conn.close()
        _drop_db(db)
    ok = deleted == 3 and left == [4, 5, 6, 7, 8]
    return ok, {"deleted": deleted, "left": left}


def case_cleanup_idempotent() -> tuple[bool, dict]:
    """连跑两次：第二次删 0 行，行数不再变化。"""
    db = _fresh_db("idem")
    idx = store.Index(db)
    try:
        _seed_capacity(idx)
        first = idx.prune_lyrics(max_rows=5)
        rows1 = idx.conn.execute("SELECT COUNT(*) FROM lyrics").fetchone()[0]
        second = idx.prune_lyrics(max_rows=5)
        rows2 = idx.conn.execute("SELECT COUNT(*) FROM lyrics").fetchone()[0]
    finally:
        idx.conn.close()
        _drop_db(db)
    ok = first == 3 and second == 0 and rows1 == 5 and rows2 == 5
    return ok, {"first": first, "second": second, "rows1": rows1, "rows2": rows2}


def case_lyric_conn_closed() -> tuple[bool, dict]:
    """Index 关闭后连接不可用；run_netease_lyric 在下一个顶层 def 前 close。

    只读源码做文本断言，不调网易云接口。
    """
    db = _fresh_db("conn")
    idx = store.Index(db)
    try:
        idx.conn.close()
        raised = False
        try:
            idx.conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            raised = True
    finally:
        _drop_db(db)

    path = os.path.join(ROOT, "src", "usable_search", "server.py")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    start = text.find("def run_netease_lyric")
    end = -1
    closed_in_func = False
    if start >= 0:
        body = text[start:]
        nxt = re.search(r"^def ", body[1:], re.M)
        end = start + 1 + nxt.start() if nxt else len(text)
        closed_in_func = "idx.conn.close()" in text[start:end]
    ok = raised and closed_in_func and end > start
    return ok, {"ProgrammingError": raised, "close_in_func": closed_in_func,
                "span": [start, end]}


CASES = [
    ("negative_ttl", "空串负缓存 24h 过期", case_negative_ttl),
    ("positive_ttl", "有歌词正缓存 90 天过期", case_positive_ttl),
    ("capacity_evict", "超上限按最旧淘汰", case_capacity_evict),
    ("cleanup_idempotent", "清理幂等", case_cleanup_idempotent),
    ("lyric_conn_closed", "歌词路径关闭连接", case_lyric_conn_closed),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== 歌词缓存 TTL/清理回归（{len(CASES)} 条，离线）==")
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
