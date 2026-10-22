"""FTS5 影子索引回归（E4-FTS5 P2a，离线零网络）。

P2a 的边界：只建「影子索引」，不改搜索行为 —— search() 仍是纯 LIKE。
本文件钉死：
  - user_version 版本化迁移：连开两次幂等、触发器不重复建、老库（缺
    mine/cover、无 FTS）能平滑升级
  - external content 影子表由三个触发器同步（AI / AU / AD），
    特别是 upsert_songs 的 ON CONFLICT DO UPDATE 走 UPDATE 路径也不能漂移
  - rebuild_fts() 能从 songs 整表重建、修漂移
  - 迁移前后 search() 的结果（id 列表 + 键序）逐条一致

一个 external content 的坑：`SELECT COUNT(*) FROM songs_fts` 走的是 content
表（songs），所以它永远等于 songs 行数，看不出索引漂移。真实索引条目数要从
`<fts>_docsize` 读，本文件的 _index_rows() 走的就是这条。

所有数据写死在文件里；全程只碰 tests/.run 下的临时库，不读也不写 data/。

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_store_fts_shadow.py
退出码：有 FAIL 返回 1；只有 PASS/SKIP 返回 0（整条 SKIP 不算失败）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import store  # noqa: E402

RUN_DIR = os.path.join(ROOT, "tests", ".run")

PASS, SKIP, FAIL = "PASS", "SKIP", "FAIL"

TAG = "store_fts_shadow"

# 固定 3 首（写死，不碰真实库）
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
]

# 固定基准时间戳 + 偏移：顺序不依赖运行时时钟（fetched_at 降序 → 103,101,102）
BASE_TS = 1700000000
FETCH_OFFSET = {101: 20, 102: 10, 103: 30}

EXPECT_KEYS = [
    "source", "id", "title", "artists", "album", "album_id",
    "cover", "duration", "fee", "origin", "mine",
]

# 老库（v0）结构：没有 mine / cover，也没有任何 FTS 对象 —— 模拟线上最老的一份库
LEGACY_SCHEMA = """
CREATE TABLE songs (
    source   TEXT NOT NULL DEFAULT 'netease',
    id       INTEGER NOT NULL,
    title    TEXT NOT NULL,
    artists  TEXT DEFAULT '',
    album    TEXT DEFAULT '',
    album_id INTEGER,
    duration INTEGER DEFAULT 0,
    fee      INTEGER,
    origin   TEXT DEFAULT '',
    fetched_at INTEGER,
    PRIMARY KEY (source, id)
);
CREATE TABLE lyrics (
    song_id    INTEGER PRIMARY KEY,
    lrc        TEXT DEFAULT '',
    fetched_at INTEGER
);
"""


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


def _seed(idx: "store.Index", songs=None) -> None:
    """写库 + 手工钉死 fetched_at（顺序不依赖时钟）。"""
    songs = SONGS if songs is None else songs
    idx.upsert_songs(songs)
    idx.conn.executemany(
        "UPDATE songs SET fetched_at=? WHERE source=? AND id=?",
        [(BASE_TS + FETCH_OFFSET[s["id"]], s["source"], s["id"]) for s in songs])
    idx.conn.commit()


def _make_legacy_db(tag: str, rows=None) -> str:
    """手工建一个只有旧列、没有 FTS 的老库（绕开 store.Index，不然会顺手迁移）。"""
    rows = SONGS if rows is None else rows
    db = _fresh_db(tag)
    conn = sqlite3.connect(db)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO songs(source,id,title,artists,album,album_id,duration,fee,origin,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(r["source"], r["id"], r["title"], r.get("artists", ""), r.get("album", ""),
              r.get("album_id"), r.get("duration") or 0, r.get("fee"), r.get("origin", ""),
              BASE_TS + FETCH_OFFSET[r["id"]]) for r in rows])
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()
    return db


# ---------------------------------------------------------------- 探针
def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _trigger_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'songs_a%'"
    ).fetchone()[0]


def _index_rows(conn: sqlite3.Connection) -> int:
    """影子索引里真实的条目数。

    external content 表上 `SELECT COUNT(*) FROM songs_fts` 读的是 songs，
    漂移看不出来；<fts>_docsize 才是索引自己记的每篇文档大小。
    """
    return conn.execute("SELECT COUNT(*) FROM songs_fts_docsize").fetchone()[0]


def _visible_rows(conn: sqlite3.Connection) -> int:
    """external content 表的 COUNT(*)：等于 content 表行数（对照用）。"""
    return conn.execute("SELECT COUNT(*) FROM songs_fts").fetchone()[0]


def _match_ids(conn: sqlite3.Connection, q: str) -> list:
    """MATCH 命中的歌曲 id（升序）。

    带双引号：裸写 !!!! 这类查询会触发 fts5 语法错误；trigram 下「词」就是子串。
    """
    return sorted(r[0] for r in conn.execute(
        "SELECT s.id FROM songs_fts JOIN songs s ON s.rowid = songs_fts.rowid "
        "WHERE songs_fts MATCH ?", (f'"{q}"',)).fetchall())


def _columns(conn: sqlite3.Connection) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(songs)").fetchall()}


def _skip_if_no_fts(idx: "store.Index", why: str = "") -> tuple[str, dict] | None:
    """fts5/trigram 不可用：记 SKIP（带原因），不算失败。"""
    if getattr(idx, "fts_ok", False):
        return None
    return SKIP, {"SKIP 原因": "fts5/trigram 不可用，影子索引降级为纯 LIKE", "备注": why}


# ---------------------------------------------------------------- 用例 1
def case_migration_idempotent() -> tuple[str, dict]:
    """新库连开两次：第二次不抛异常；user_version=1；触发器恰好 3 个。"""
    db = _fresh_db("idem")
    try:
        idx1 = store.Index(db)
        ver1 = _user_version(idx1.conn)
        trig1 = _trigger_count(idx1.conn)
        idx1.conn.close()

        idx2 = store.Index(db)          # 第二次打开：必须不报错
        ver2 = _user_version(idx2.conn)
        trig2 = _trigger_count(idx2.conn)
        fts2 = idx2.fts_ok
        idx2.conn.close()

        idx3 = store.Index(db)          # 第三次，确认稳定
        ver3, trig3 = _user_version(idx3.conn), _trigger_count(idx3.conn)
        idx3.conn.close()
    finally:
        _drop_db(db)

    ok = (ver1 == ver2 == ver3 == store.SCHEMA_VERSION == 1
          and trig1 == trig2 == trig3 == 3 and fts2 is True)
    return (PASS if ok else FAIL), {
        "user_version": [ver1, ver2, ver3], "触发器数": [trig1, trig2, trig3],
        "第二次 fts_ok": fts2,
    }


# ---------------------------------------------------------------- 用例 2
def case_legacy_db_compat() -> tuple[str, dict]:
    """老库（缺 mine/cover、无 FTS）用 Index 打开：不报错、列补齐、版本推到 1。"""
    db = _make_legacy_db("legacy")
    try:
        before = {"mine/cover 存在": False}
        idx = store.Index(db)           # 关键：不得抛异常
        cols = _columns(idx.conn)
        ver = _user_version(idx.conn)
        trig = _trigger_count(idx.conn)
        fts = idx.fts_ok
        # 老库已有数据 → 迁移时应顺带灌进索引，而不是留一张空影子表
        rows = _index_rows(idx.conn) if fts else 0
        matched = _match_ids(idx.conn, "春日影") if fts else []
        searchable = len(idx.search("春日影", limit=100)) if fts else 0
        idx.conn.close()
    finally:
        _drop_db(db)

    legacy_ok = "mine" not in before and {"mine", "cover"} <= cols
    ok = (legacy_ok and ver == 1 and fts is True and rows == len(SONGS)
          and matched == [101, 102] and searchable == 2)
    if fts is not True:
        return SKIP, {"SKIP 原因": "fts5/trigram 不可用，只验证了列补齐",
                      "列": sorted(cols), "user_version": ver}
    return (PASS if ok else FAIL), {
        "列补齐": sorted(cols), "user_version": ver, "触发器数": trig,
        "fts_ok": fts, f"索引条目/源行数": f"{rows}/{len(SONGS)}",
        "MATCH 春日影": matched, "search 命中": searchable,
    }


# ---------------------------------------------------------------- 用例 3
def case_shadow_sync_insert() -> tuple[str, dict]:
    """插 3 条 → 影子表 3 行，且 MATCH 能命中中文标题。"""
    db = _fresh_db("ins")
    try:
        idx = store.Index(db)
        _skip = _skip_if_no_fts(idx, "insert")
        if _skip:
            idx.conn.close()
            return _skip
        _seed(idx)
        visible = _visible_rows(idx.conn)
        rows = _index_rows(idx.conn)
        hit = _match_ids(idx.conn, "春日影")
        hit_kira = _match_ids(idx.conn, "キラキラ")
        idx.conn.close()
    finally:
        _drop_db(db)

    ok = visible == 3 and rows == 3 and hit == [101, 102] and hit_kira == [103]
    return (PASS if ok else FAIL), {
        "songs_fts COUNT": visible, "索引条目": rows,
        "MATCH 春日影": hit, "MATCH キラキラ": hit_kira,
    }


# ---------------------------------------------------------------- 用例 4
def case_shadow_sync_update() -> tuple[str, dict]:
    """改标题走 ON CONFLICT DO UPDATE（UPDATE 路径）：旧标题失配、新标题命中。"""
    db = _fresh_db("upd")
    try:
        idx = store.Index(db)
        _skip = _skip_if_no_fts(idx, "update")
        if _skip:
            idx.conn.close()
            return _skip
        _seed(idx)
        before = _match_ids(idx.conn, "春日影")
        # 同一条 (source,id) 再 upsert：命中 ON CONFLICT DO UPDATE，rowid 不变
        idx.upsert_songs([{"source": "netease", "id": 101, "title": "秋の影",
                           "artists": "MyGO!!!!!", "album": "秋", "cover": "",
                           "duration": 240000, "fee": 0, "origin": "fixture"}])
        after_old = _match_ids(idx.conn, "春日影")
        after_new = _match_ids(idx.conn, "秋の影")
        rows = _index_rows(idx.conn)
        cover_kept = idx.conn.execute(
            "SELECT cover FROM songs WHERE source='netease' AND id=101").fetchone()[0]
        idx.conn.close()
    finally:
        _drop_db(db)

    ok = (before == [101, 102] and after_old == [102] and after_new == [101]
          and rows == 3 and cover_kept == "https://example.invalid/101.jpg")
    return (PASS if ok else FAIL), {
        "改前 MATCH 春日影": before, "改后 MATCH 春日影": after_old,
        "MATCH 秋の影": after_new, "索引条目(应为3,漂移会>3)": rows,
        "cover 仍保留": cover_kept,
    }


# ---------------------------------------------------------------- 用例 5
def case_shadow_sync_delete() -> tuple[str, dict]:
    """删一条 → 影子表行数同步减 1，被删的那首不再命中。"""
    db = _fresh_db("del")
    try:
        idx = store.Index(db)
        _skip = _skip_if_no_fts(idx, "delete")
        if _skip:
            idx.conn.close()
            return _skip
        _seed(idx)
        before = _index_rows(idx.conn)
        idx.conn.execute("DELETE FROM songs WHERE source='netease' AND id=103")
        idx.conn.commit()
        after = _index_rows(idx.conn)
        visible = _visible_rows(idx.conn)
        hit = _match_ids(idx.conn, "キラキラ")
        idx.conn.close()
    finally:
        _drop_db(db)

    ok = before == 3 and after == 2 and visible == 2 and hit == []
    return (PASS if ok else FAIL), {
        "删前索引条目": before, "删后索引条目": after,
        "songs_fts COUNT": visible, "MATCH キラキラ": hit,
    }


# ---------------------------------------------------------------- 用例 6
def case_rebuild_recovers() -> tuple[str, dict]:
    """手工清空影子索引制造漂移 → rebuild_fts() 返回 True 且索引恢复。"""
    db = _fresh_db("rebuild")
    try:
        idx = store.Index(db)
        _skip = _skip_if_no_fts(idx, "rebuild")
        if _skip:
            idx.conn.close()
            return _skip
        _seed(idx)
        good = _index_rows(idx.conn)
        # external content 表允许直接 DELETE：清掉索引、保留 songs（真实漂移）
        idx.conn.execute("DELETE FROM songs_fts")
        idx.conn.commit()
        drifted = _index_rows(idx.conn)
        drifted_hit = _match_ids(idx.conn, "春日影")
        ok_rebuild = idx.rebuild_fts()
        fixed = _index_rows(idx.conn)
        fixed_hit = _match_ids(idx.conn, "春日影")
        visible = _visible_rows(idx.conn)
        idx.conn.close()
    finally:
        _drop_db(db)

    ok = (good == 3 and drifted == 0 and drifted_hit == []
          and ok_rebuild is True and fixed == 3 and fixed_hit == [101, 102]
          and visible == 3)
    return (PASS if ok else FAIL), {
        "初始条目": good, "清空后条目": drifted, "漂移时 MATCH": drifted_hit,
        "rebuild 返回": ok_rebuild, "rebuild 后条目": fixed,
        "rebuild 后 MATCH": fixed_hit, "songs_fts COUNT": visible,
    }


# ---------------------------------------------------------------- 用例 7
def case_search_unchanged() -> tuple[str, dict]:
    """迁移前后 search('春日影') 完全一致：id 列表 + 键序（P2a 不碰搜索行为）。

    「迁移前」用真实 search() 取：把 _FTS_TABLE 临时换成坏 DDL，
    让 Index 在同一个老库上只完成 v0 列补齐（fts_ok=False，user_version 仍为 0），
    这样两侧走的都是同一段 LIKE 代码，比对才有意义。顺带把降级路径也验了。
    """
    db = _make_legacy_db("same")
    real_ddl = store._FTS_TABLE
    real_level = store._log.level
    try:
        store._FTS_TABLE = ("CREATE VIRTUAL TABLE IF NOT EXISTS songs_fts "
                            "USING fts5(x, tokenize='no_such_tokenizer_xyz')")
        store._log.setLevel(logging.CRITICAL)  # 故意的降级试一试，别把 warning 打花屏
        try:
            idx0 = store.Index(db)
            before_fts_ok = idx0.fts_ok
            before_ver = _user_version(idx0.conn)
            before_rows = idx0.search("春日影", limit=1000)
            before_trigger = _trigger_count(idx0.conn)
            idx0.conn.close()
        finally:
            store._FTS_TABLE = real_ddl
            store._log.setLevel(real_level)

        idx1 = store.Index(db)
        after_fts_ok = idx1.fts_ok
        after_ver = _user_version(idx1.conn)
        after_rows = idx1.search("春日影", limit=1000)
        after_trigger = _trigger_count(idx1.conn)
        idx1.conn.close()
    finally:
        store._FTS_TABLE = real_ddl
        _drop_db(db)

    ok = (after_fts_ok is True and after_ver == 1 and after_trigger == 3
          and [r["id"] for r in before_rows] == [r["id"] for r in after_rows] == [101, 102]
          and [list(r) for r in before_rows] == [list(r) for r in after_rows]
          and bool(before_rows) and list(before_rows[0]) == EXPECT_KEYS)
    if after_fts_ok is not True:
        return SKIP, {"SKIP 原因": "fts5/trigram 不可用，无法完成迁移前后比对",
                      "迁移前 id": [r["id"] for r in before_rows]}
    detail = {
        "迁移前 fts_ok/user_version": f"{before_fts_ok}/{before_ver}",
        "迁移后 fts_ok/user_version": f"{after_fts_ok}/{after_ver}",
        "迁移前 id": [r["id"] for r in before_rows],
        "迁移后 id": [r["id"] for r in after_rows],
        "键序": list(before_rows[0]) if before_rows else [],
        "触发器数": [before_trigger, after_trigger],
    }
    return (PASS if ok else FAIL), detail


CASES = [
    ("migration_idempotent", "user_version 迁移幂等 + 触发器恰好 3 个", case_migration_idempotent),
    ("legacy_db_compat", "老库（缺列/无 FTS）平滑升级并回灌索引", case_legacy_db_compat),
    ("shadow_sync_insert", "插入 → 影子索引同步", case_shadow_sync_insert),
    ("shadow_sync_update", "ON CONFLICT DO UPDATE → 旧值清除、新值命中", case_shadow_sync_update),
    ("shadow_sync_delete", "删除 → 影子索引同步", case_shadow_sync_delete),
    ("rebuild_recovers", "漂移后 rebuild 恢复", case_rebuild_recovers),
    ("search_unchanged", "迁移前后 search() 结果逐条一致", case_search_unchanged),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== FTS5 影子索引 P2a（{len(CASES)} 条，离线）==")
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
