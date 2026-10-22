"""FTS5 查询切换回归（E4-FTS5 P2b，离线零网络）。

P2b 的边界：只改 search() 的**取数路径**，不改对外契约。
  - 开关（构造参数 use_fts / 环境变量 USABLE_SEARCH_FTS）**默认关** → 走 LIKE
  - 开关开 + 查询 >= 3 字符 + fts_ok → 走 MATCH（短语查询，内部双引号按 FTS5 写成 ""）
  - 1~2 字查询必须回退 LIKE：trigram 对 < 3 字符没有 token，命中恒为 0
  - MATCH 路径任何 sqlite3.OperationalError 都回退 LIKE，不抛给调用方
  - 返回仍是 11 个键、按 fetched_at 降序（与 LIKE 路径逐字节同契约）

所有数据写死在文件里；全程只碰 tests/.run 下的临时库，不读也不写 data/。

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_store_fts_query.py
退出码：有 FAIL 返回 1；只有 PASS/SKIP 返回 0（整条 SKIP 不算失败）。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import store  # noqa: E402

RUN_DIR = os.path.join(ROOT, "tests", ".run")

PASS, SKIP, FAIL = "PASS", "SKIP", "FAIL"

TAG = "store_fts_query"

ENV_KEY = "USABLE_SEARCH_FTS"

# 6 首固定夹具：至少 5 条，且「春日影」命中 2 条、「春日」命中 3 条
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
    {"source": "netease", "id": 104, "title": "春日的信", "artists": "灯",
     "album": "春日集", "album_id": 9003, "cover": "", "duration": 200000,
     "fee": 0, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 105, "title": "影色舞", "artists": "MyGO!!!!!",
     "album": "影", "album_id": 9004, "cover": "", "duration": 190000,
     "fee": 0, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 106, "title": "雨", "artists": "立希",
     "album": "", "album_id": None, "cover": "", "duration": 180000,
     "fee": 0, "origin": "fixture", "mine": 0},
]

# 固定基准时间戳 + 偏移：顺序不依赖运行时时钟（fetched_at 降序 → 106,105,104,103,102,101）
BASE_TS = 1700000000
FETCH_OFFSET = {101: 10, 102: 20, 103: 30, 104: 40, 105: 50, 106: 60}

EXPECT_KEYS = [
    "source", "id", "title", "artists", "album", "album_id",
    "cover", "duration", "fee", "origin", "mine",
]

_SENTINEL = object()


# ---------------------------------------------------------------- 临时库
def _fresh_db(tag: str) -> str:
    """每个用例一个临时库（带 pid，避免并发互踩）。"""
    os.makedirs(RUN_DIR, exist_ok=True)
    db = os.path.join(RUN_DIR, f"{TAG}_{os.getpid()}_{tag}.sqlite3")
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


def _seed(idx: "store.Index") -> None:
    """写库 + 手工钉死 fetched_at（顺序不依赖时钟）。"""
    idx.upsert_songs(SONGS)
    idx.conn.executemany(
        "UPDATE songs SET fetched_at=? WHERE source=? AND id=?",
        [(BASE_TS + FETCH_OFFSET[s["id"]], s["source"], s["id"]) for s in SONGS])
    idx.conn.commit()


class _EnvOff:
    """临时摘掉 USABLE_SEARCH_FTS，退出时原样还原（替身环境不干扰结论）。"""

    def __enter__(self):
        self._saved = os.environ.pop(ENV_KEY, _SENTINEL)
        return self

    def __exit__(self, *_exc):
        os.environ.pop(ENV_KEY, None)
        if self._saved is not _SENTINEL:
            os.environ[ENV_KEY] = self._saved
        return False


# ---------------------------------------------------------------- 探针
def _like_ids(idx: "store.Index", q: str, limit: int = 1000) -> list:
    """显式 LIKE 基准（与 P2a 之前的 search() 同一段 SQL，独立算一遍）。"""
    like = f"%{store._escape_like(q)}%"
    return [r["id"] for r in idx.conn.execute(
        "SELECT id FROM songs WHERE title LIKE ? ESCAPE '\\' OR artists LIKE ? ESCAPE '\\' "
        "OR album LIKE ? ESCAPE '\\' ORDER BY fetched_at DESC LIMIT ?",
        (like, like, like, limit))]


def _ids(rows: list) -> list:
    return [r["id"] for r in rows]


def _trace(idx: "store.Index", fn) -> tuple:
    """跑一次 fn，顺便留下这条连接上执行过的 SQL（证明走了哪条分支）。"""
    seen: list = []
    idx.conn.set_trace_callback(seen.append)
    try:
        out = fn()
    finally:
        idx.conn.set_trace_callback(None)
    return out, seen


def _used_match(seen: list) -> bool:
    return any("MATCH" in s.upper() for s in seen)


def _fetched_at(idx: "store.Index") -> dict:
    return {r[0]: r[1] for r in idx.conn.execute(
        "SELECT id, fetched_at FROM songs WHERE source='netease'")}


def _skip_if_no_fts(idx: "store.Index", why: str):
    if getattr(idx, "fts_ok", False):
        return None
    return SKIP, {"SKIP 原因": "fts5/trigram 不可用，无法验证 MATCH 路径", "备注": why}


# ---------------------------------------------------------------- 用例 1
def case_flag_off_uses_like() -> tuple[str, dict]:
    """默认构造（不传 use_fts、不设环境变量）：use_fts 为 False，且结果等于显式 LIKE。"""
    db = _fresh_db("off")
    try:
        with _EnvOff():
            idx = store.Index(db)              # 默认构造：绝不能默认走 FTS
            flag = idx.use_fts
            _seed(idx)
            rows_long, seen_long = _trace(idx, lambda: idx.search("春日影", limit=1000))
            rows_short = idx.search("春", limit=1000)
            rows_mid = idx.search("春日", limit=1000)
            base_long = _like_ids(idx, "春日影")
            base_short = _like_ids(idx, "春")
            base_mid = _like_ids(idx, "春日")
            idx.conn.close()
    finally:
        _drop_db(db)

    ok = (flag is False and _used_match(seen_long) is False
          and _ids(rows_long) == base_long
          and _ids(rows_short) == base_short
          and _ids(rows_mid) == base_mid
          and bool(base_long) and bool(base_short))
    return (PASS if ok else FAIL), {
        "默认 use_fts": flag, "默认路径走 MATCH": _used_match(seen_long),
        "春日影 search/LIKE": f"{_ids(rows_long)}/{base_long}",
        "春 search/LIKE": f"{_ids(rows_short)}/{base_short}",
        "春日 search/LIKE": f"{_ids(rows_mid)}/{base_mid}",
    }


# ---------------------------------------------------------------- 用例 2
def case_short_query_uses_like() -> tuple[str, dict]:
    """开关打开后，1 字 / 2 字查询仍能命中，且命中集与关掉开关时逐条一致。"""
    db = _fresh_db("short")
    try:
        with _EnvOff():
            off = store.Index(db, use_fts=False)
            _seed(off)
            off_1 = _ids(off.search("春", limit=1000))
            off_2 = _ids(off.search("春日", limit=1000))
            off_kira = _ids(off.search("キラ", limit=1000))
            off.conn.close()

            on = store.Index(db, use_fts=True)
            fts_ok = on.fts_ok
            on_1, seen_1 = _trace(on, lambda: on.search("春", limit=1000))
            on_2 = on.search("春日", limit=1000)
            on_kira = on.search("キラ", limit=1000)
            on.conn.close()
    finally:
        _drop_db(db)

    ok = _used_match(seen_1) is False
    ok = ok and _ids(on_1) == off_1 == [104, 102, 101]      # fetched_at 降序
    ok = ok and _ids(on_2) == off_2 == [104, 102, 101]
    ok = ok and _ids(on_kira) == off_kira == [103]
    return (PASS if ok else FAIL), {
        "fts_ok": fts_ok, "1 字走 MATCH": _used_match(seen_1),
        "春 on/off": f"{_ids(on_1)}/{off_1}",
        "春日 on/off": f"{_ids(on_2)}/{off_2}",
        "キラ on/off": f"{_ids(on_kira)}/{off_kira}",
    }


# ---------------------------------------------------------------- 用例 3
def case_long_query_equivalent() -> tuple[str, dict]:
    """>= 3 字：MATCH 路径的 id 集合与 LIKE 完全相等（抽 3 个查询交叉验）。"""
    db = _fresh_db("long")
    try:
        with _EnvOff():
            off = store.Index(db, use_fts=False)
            _seed(off)
            base = {q: _ids(off.search(q, limit=1000)) for q in ("春日影", "春日集", "影色舞")}
            off.conn.close()

            on = store.Index(db, use_fts=True)
            _skip = _skip_if_no_fts(on, "long_equivalent")
            if _skip:
                on.conn.close()
                return _skip
            got = {}
            used = {}
            for q in base:
                rows, seen = _trace(on, lambda q=q: on.search(q, limit=1000))
                got[q] = _ids(rows)
                used[q] = _used_match(seen)
            on.conn.close()
    finally:
        _drop_db(db)

    eq = {q: sorted(got[q]) == sorted(base[q]) for q in base}
    # 夹具保证「春日影」至少 2 条命中，否则相等没意义
    ok = (all(eq.values()) and all(used.values())
          and len(base["春日影"]) >= 2 and got["春日影"] == base["春日影"])
    return (PASS if ok else FAIL), {
        "MATCH 路径": used,
        "春日影 MATCH/LIKE": f"{got.get('春日影')}/{base['春日影']}",
        "春日集 MATCH/LIKE": f"{got.get('春日集')}/{base['春日集']}",
        "影色舞 MATCH/LIKE": f"{got.get('影色舞')}/{base['影色舞']}",
        "集合相等": eq,
    }


# ---------------------------------------------------------------- 用例 4
def case_malformed_query_falls_back() -> tuple[str, dict]:
    """带引号/星号/冒号的脏串：不抛异常、不 500，结果与 LIKE 一致（或为空）。"""
    db = _fresh_db("bad")
    bad = ['"a" OR "b"', '春*', 'a:b', 'NEAR(', '春"日', '**', '-春日', 'title:春日影']
    try:
        with _EnvOff():
            off = store.Index(db, use_fts=False)
            _seed(off)
            base = {q: _ids(off.search(q, limit=1000)) for q in bad}
            off.conn.close()

            on = store.Index(db, use_fts=True)
            got, errs = {}, {}
            for q in bad:
                try:
                    got[q] = _ids(on.search(q, limit=1000))
                except Exception as exc:  # noqa: BLE001 —— 抛出来就是 FAIL
                    errs[q] = repr(exc)
            on.conn.close()
    finally:
        _drop_db(db)

    mismatched = {q: f"{got[q]}!={base[q]}" for q in bad
                  if q not in errs and not (got[q] == base[q] or got[q] == [])}
    ok = not errs and not mismatched
    return (PASS if ok else FAIL), {
        "异常": errs or "无",
        "不一致": mismatched or "无",
        "样本": {q: f"MATCH路径={got.get(q)} LIKE={base[q]}" for q in bad[:3]},
    }


# ---------------------------------------------------------------- 用例 5
def case_contract_unchanged() -> tuple[str, dict]:
    """MATCH 路径的返回契约不变：11 个键 + 键序 + fetched_at 降序。"""
    db = _fresh_db("contract")
    try:
        with _EnvOff():
            on = store.Index(db, use_fts=True)
            _skip = _skip_if_no_fts(on, "contract")
            if _skip:
                on.conn.close()
                return _skip
            _seed(on)
            rows, seen = _trace(on, lambda: on.search("春日影", limit=1000))
            ts = _fetched_at(on)
            on.conn.close()
    finally:
        _drop_db(db)

    keys_ok = all(list(r) == EXPECT_KEYS for r in rows)
    ids = _ids(rows)
    ordered = [ts[i] for i in ids] == sorted((ts[i] for i in ids), reverse=True)
    ok = (bool(rows) and _used_match(seen) and keys_ok and ordered
          and ids == [102, 101])
    return (PASS if ok else FAIL), {
        "走 MATCH": _used_match(seen), "命中 id": ids,
        f"fetched_at 降序": ordered,
        "键数/键序": f"{len(rows[0]) if rows else 0}/{EXPECT_KEYS == list(rows[0]) if rows else False}",
        "键集相同": keys_ok,
    }


CASES = [
    ("flag_off_uses_like", "默认关：不传 use_fts 时走 LIKE", case_flag_off_uses_like),
    ("short_query_uses_like", "1~2 字查询回退 LIKE 且命中集不变", case_short_query_uses_like),
    ("long_query_equivalent", ">= 3 字 MATCH 与 LIKE 结果集相等", case_long_query_equivalent),
    ("malformed_query_falls_back", "脏查询不抛异常、自动回退", case_malformed_query_falls_back),
    ("contract_unchanged", "MATCH 路径 11 列契约 + fetched_at 降序", case_contract_unchanged),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== FTS5 查询切换 P2b（{len(CASES)} 条，离线）==")
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
    # 汇总行必须带 "SKIP M" 与 "FAIL 0"：healthcheck 靠这两个片段判 SKIP/PASS
    print(f"\n== 汇总: {counts[PASS]}/{total} PASS · SKIP {counts[SKIP]}"
          f" · FAIL {counts[FAIL]} ==")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
