"""FTS5 / trigram 能力探测 + LIKE 基线比对（离线，零网络）。

E4-FTS5 的 P1：只采集事实、钉死语义，不改任何生产代码。
为 P2（影子索引）提供依据，覆盖：
  - fts5 模块在本机解释器里到底可不可用（不可用记 SKIP，不记 FAIL）
  - trigram 分词器语义：≥3 字子串能命中、单字查询不命中、2 字查询只记录
  - 现有 LIKE 路径的 11 列返回契约 + fetched_at 降序（P2 的「接口不变」基线）
  - 同一批固定数据上 LIKE 与 trigram FTS 的 id 集合差异（只如实报，不判失败）

所有数据写死在文件里，不读 data/ 目录的真实库；全程内存库或 tests/.run 临时库。

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_store_fts.py
退出码：有 FAIL 返回 1；只有 PASS/SKIP 返回 0（整条 SKIP 不算失败）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import store  # noqa: E402

RUN_DIR = os.path.join(ROOT, "tests", ".run")

PASS, SKIP, FAIL = "PASS", "SKIP", "FAIL"

# 固定 5 首（写死，不碰真实库）。artists 统一带 MyGO 便于整表命中做顺序断言。
SONGS = [
    {"source": "netease", "id": 101, "title": "春日影", "artists": "MyGO!!!!!",
     "album": "春日影", "album_id": 9001, "cover": "https://example.invalid/101.jpg",
     "duration": 240000, "fee": 0, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 102, "title": "今天唱了春日影", "artists": "MyGO!!!!!",
     "album": "诗超绊", "album_id": 9002, "cover": "", "duration": 260000,
     "fee": 1, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 103, "title": "キラキラだとか夢だとか", "artists": "MyGO!!!!!",
     "album": "", "album_id": None, "cover": "", "duration": 210000,
     "fee": 0, "origin": "fixture", "mine": 1},
    {"source": "netease", "id": 104, "title": "春", "artists": "MyGO!!!!!",
     "album": "", "album_id": None, "cover": "https://example.invalid/104.jpg",
     "duration": 180000, "fee": None, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 105, "title": "Haruhikage", "artists": "MyGO!!!!!",
     "album": "春日", "album_id": 9003, "cover": "", "duration": 250000,
     "fee": 0, "origin": "fixture", "mine": 0},
]

# 固定基准时间戳 + 手工偏移：顺序不依赖运行时时钟，且故意打乱插入序。
BASE_TS = 1700000000
FETCH_OFFSET = {101: 30, 102: 20, 103: 40, 104: 10, 105: 50}
EXPECT_ORDER = [105, 103, 101, 102, 104]  # fetched_at 降序

# store.search() 返回的 11 个列（P2 必须保持不变的接口）
EXPECT_KEYS = {
    "source", "id", "title", "artists", "album", "album_id",
    "cover", "duration", "fee", "origin", "mine",
}


def _fresh_db(tag: str) -> str:
    """每个用例一个临时库（带 pid，避免并发互踩）。"""
    os.makedirs(RUN_DIR, exist_ok=True)
    db = os.path.join(RUN_DIR, f"store_fts_{os.getpid()}_{tag}.sqlite3")
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


def _seed_index(tag: str) -> tuple[str, "store.Index"]:
    """建临时库 + 写死 5 首 + 手工钉死 fetched_at。"""
    db = _fresh_db(tag)
    idx = store.Index(db)
    idx.upsert_songs(SONGS)
    idx.conn.executemany(
        "UPDATE songs SET fetched_at=? WHERE source=? AND id=?",
        [(BASE_TS + FETCH_OFFSET[s["id"]], s["source"], s["id"]) for s in SONGS])
    idx.conn.commit()
    return db, idx


def _meta() -> dict:
    """解释器与 SQLite 版本；sqlite3.version 在新解释器上可能已移除。"""
    return {
        "python": sys.version.split()[0],
        "sqlite3.sqlite_version": sqlite3.sqlite_version,
        "sqlite3.version": getattr(sqlite3, "version", "已移除"),
    }


def _is_missing_fts5(exc: sqlite3.OperationalError) -> bool:
    """判断 OperationalError 是不是「本机没有 fts5」而不是用法错误。"""
    msg = str(exc).lower()
    return "no such module" in msg or "fts5" in msg


def _match_count(conn: sqlite3.Connection, table: str, q: str) -> int:
    """带引号的 MATCH 计数：裸写 !!!! 这类查询会触发 fts5 语法错误。

    trigram 下「词」就是子串，加双引号只是把特殊标点转义成普通字符。
    """
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (f'"{q}"',)).fetchone()[0]


# ---------------------------------------------------------------- 用例 1
def case_fts5_capability() -> tuple[str, object]:
    """fts5 模块可用性：缺模块记 SKIP（带版本信息），可用记 PASS。"""
    meta = _meta()
    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.execute("CREATE VIRTUAL TABLE cap USING fts5(x)")
            conn.execute("INSERT INTO cap(x) VALUES('hello fts5 world')")
            n = conn.execute(
                "SELECT COUNT(*) FROM cap WHERE cap MATCH 'fts5'").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if _is_missing_fts5(exc) and "syntax" not in str(exc).lower():
                return SKIP, {"SKIP 原因": str(exc), **meta}
            raise
        opts = [r[0] for r in conn.execute("PRAGMA compile_options").fetchall()]
        compiled = any("FTS5" in o.upper() for o in opts)
    finally:
        conn.close()
    detail = {**meta, "ENABLE_FTS5 编译项": compiled, "MATCH 命中": n}
    return (PASS if n == 1 else FAIL), detail


# ---------------------------------------------------------------- 用例 2
def case_trigram_semantics() -> tuple[str, object]:
    """trigram 语义：≥3 字子串命中；单字 0 命中；2 字仅记录不判成败。"""
    texts = ["今天唱了春日影", "キラキラだとか夢だとか", "MyGO!!!!!"]
    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.execute("CREATE VIRTUAL TABLE tri USING fts5(x, tokenize='trigram')")
        except sqlite3.OperationalError as exc:
            return SKIP, {"SKIP 原因": f"trigram 不可用: {exc}", **_meta()}
        conn.executemany("INSERT INTO tri(x) VALUES(?)", [(t,) for t in texts])
        three = _match_count(conn, "tri", "春日影")
        kira = _match_count(conn, "tri", "キラキラ")
        mygo = _match_count(conn, "tri", "MyGO")
        one = _match_count(conn, "tri", "春")
        two = _match_count(conn, "tri", "春日")  # 2 字：只记录，待 P2 决策
    finally:
        conn.close()
    ok = three == 1 and kira == 1 and mygo == 1 and one == 0
    detail = {
        "春日影(3字)": three, "キラキラ(4字)": kira, "MyGO(4字)": mygo,
        "春(1字)": one, "春日(2字,仅记录)": two,
    }
    return (PASS if ok else FAIL), detail


# ---------------------------------------------------------------- 用例 3
def case_like_baseline() -> tuple[str, object]:
    """现有 LIKE 路径契约：11 个键、fetched_at 降序（P2 接口不变基线）。"""
    db, idx = _seed_index("like")
    try:
        rows = idx.search("MyGO", limit=1000)
    finally:
        idx.conn.close()
        _drop_db(db)
    keys_ok = bool(rows) and all(set(r) == EXPECT_KEYS for r in rows)
    order = [r["id"] for r in rows]
    ok = len(rows) == len(SONGS) and keys_ok and order == EXPECT_ORDER
    detail = {
        "命中": f"{len(rows)}/{len(SONGS)}",
        "11 键一致": keys_ok,
        "顺序": order,
        "期望顺序": EXPECT_ORDER,
        "实际键序": list(rows[0]) if rows else [],
    }
    return (PASS if ok else FAIL), detail


# ---------------------------------------------------------------- 用例 4
def case_like_vs_fts_equivalence() -> tuple[str, object]:
    """同一批数据、同一个 ≥3 字中文词：LIKE 与 trigram FTS 的 id 集合差异。

    P1 只采集事实：不等价也照实输出差集，不判失败。
    """
    query = "春日影"
    db, idx = _seed_index("eq")
    try:
        like_ids = {r["id"] for r in idx.search(query, limit=1000)}
        try:
            idx.conn.execute(
                "CREATE VIRTUAL TABLE songs_fts_probe USING fts5("
                "id UNINDEXED, title, artists, album, tokenize='trigram')")
            idx.conn.execute(
                "INSERT INTO songs_fts_probe(id, title, artists, album) "
                "SELECT id, title, artists, album FROM songs")
            fts_ids = {r[0] for r in idx.conn.execute(
                "SELECT id FROM songs_fts_probe WHERE songs_fts_probe MATCH ?", (f'"{query}"',)).fetchall()}
            fts_rows = idx.conn.execute("SELECT COUNT(*) FROM songs_fts_probe").fetchone()[0]
            like_one_char = idx.conn.execute(
                "SELECT COUNT(*) FROM songs WHERE title LIKE '%春%'").fetchone()[0]
            fts_one_char = _match_count(idx.conn, "songs_fts_probe", "春")
        except sqlite3.OperationalError as exc:
            return SKIP, {"SKIP 原因": f"trigram 不可用: {exc}", **_meta()}
    finally:
        idx.conn.close()
        _drop_db(db)

    only_like = sorted(like_ids - fts_ids)
    only_fts = sorted(fts_ids - like_ids)
    detail = {
        "查询": query,
        "LIKE": sorted(like_ids),
        "FTS": sorted(fts_ids),
        "集合相等": like_ids == fts_ids,
        "A-B(仅 LIKE 有)": only_like,
        "B-A(仅 FTS 有)": only_fts,
        "fts 行数/原表行数": f"{fts_rows}/{len(SONGS)}",
        # 结构性差异参考：1 字文档 LIKE 能中、trigram 不索引（单字<3 无 token）
        "单字「春」LIKE/FTS": f"{like_one_char}/{fts_one_char}",
    }
    return PASS, detail  # 只报事实，P1 不据此判失败


CASES = [
    ("fts5_capability", "fts5 模块可用性（缺模块记 SKIP）", case_fts5_capability),
    ("trigram_semantics", "trigram 子串语义：≥3 字命中 / 单字不命中", case_trigram_semantics),
    ("like_baseline", "现有 LIKE 路径 11 列契约 + fetched_at 降序", case_like_baseline),
    ("like_vs_fts_equivalence", "LIKE 与 trigram FTS 结果集差异（只报事实）",
     case_like_vs_fts_equivalence),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== FTS5 能力探测（{len(CASES)} 条，离线）==")
    counts = {PASS: 0, SKIP: 0, FAIL: 0}
    for cid, label, fn in CASES:
        t0 = time.time()
        try:
            status, detail = fn()
        except Exception as exc:  # noqa: BLE001
            status, detail = FAIL, {"error": repr(exc)}
        if status not in counts:
            status, detail = FAIL, {"error": f"未知状态 {status!r}: {detail}"}
        counts[status] += 1
        print(f"[{cid}] {label} —— {status}（{round(time.time() - t0, 2)}s）  {detail}")
    total = len(CASES)
    # 汇总行必须带 "SKIP M" 与 "FAIL 0"：tools/healthcheck.py 靠这两个片段判 SKIP/PASS
    print(f"\n== 汇总: {counts[PASS]}/{total} PASS · SKIP {counts[SKIP]}"
          f" · FAIL {counts[FAIL]} ==")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
