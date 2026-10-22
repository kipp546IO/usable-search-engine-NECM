"""E4-P2c 阈值门回归（离线，零网络，零新依赖）。

覆盖三条阈值 + 开关语义 + 接线：
  - songs / 库容 / p95 三条边界各测「差一点不过」「正好过」
  - 默认 off：未设 / 空 / 0 / 无法识别的值一律关（失败关闭）
  - on：1 / true / on（大小写不敏感）
  - auto：按注入证据判断；三项任一命中即开，reason 能说清
  - auto 真实小库：open_index 不会把 FTS 打开（规模远不够），显式 use_fts=True 仍压得过 auto
  - 库不存在/路径坏：证据全 0、auto 判关、不抛异常
  - 门用的 LIKE SQL 必须与 store._LIKE_SQL 逐字一致（防两边漂移）

所有数据写死在文件里；只碰 tests/.run 下的临时库，不读也不写 data/。

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_fts_gate.py
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import fts_gate, store  # noqa: E402

RUN_DIR = os.path.join(ROOT, "tests", ".run")
ENV_KEY = fts_gate.ENV_KEY
MB = 1024 * 1024

# 6 首固定夹具（标题都 >= 3 字，p95 采样有词可查）
SONGS = [
    {"source": "netease", "id": 201, "title": "春日影", "artists": "MyGO!!!!!",
     "album": "春日影", "album_id": 9101, "cover": "", "duration": 240000,
     "fee": 0, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 202, "title": "今天唱了春日影", "artists": "MyGO!!!!!",
     "album": "诗超绊", "album_id": 9102, "cover": "", "duration": 260000,
     "fee": 1, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 203, "title": "キラキラだとか夢だとか", "artists": "MyGO!!!!!",
     "album": "", "album_id": None, "cover": "", "duration": 210000,
     "fee": 0, "origin": "fixture", "mine": 1},
    {"source": "netease", "id": 204, "title": "春日的信", "artists": "灯",
     "album": "春日集", "album_id": 9103, "cover": "", "duration": 200000,
     "fee": 0, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 205, "title": "影色舞", "artists": "MyGO!!!!!",
     "album": "影", "album_id": 9104, "cover": "", "duration": 190000,
     "fee": 0, "origin": "fixture", "mine": 0},
    {"source": "netease", "id": 206, "title": "雨", "artists": "立希",
     "album": "", "album_id": None, "cover": "", "duration": 180000,
     "fee": 0, "origin": "fixture", "mine": 0},
]


def _fresh_db(tag: str) -> str:
    os.makedirs(RUN_DIR, exist_ok=True)
    db = os.path.join(RUN_DIR, f"fts_gate_{os.getpid()}_{tag}.sqlite3")
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


def _seed(db: str) -> None:
    idx = store.Index(db, use_fts=False)
    try:
        idx.upsert_songs([dict(s) for s in SONGS])
    finally:
        idx.conn.close()


class _Env:
    """临时改环境变量，退出时原样还原。"""

    def __init__(self, value):
        self.value = value
        self.saved = None

    def __enter__(self):
        self.saved = os.environ.get(ENV_KEY)
        if self.value is None:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = self.value
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = self.saved
        return False


# ---------------------------------------------------------------- 用例
def case_sql_alignment():
    """门量的必须是 store 真正跑的 LIKE。"""
    same = fts_gate._LIKE_SQL == store._LIKE_SQL
    return same, {"gate": fts_gate._LIKE_SQL[:40], "store": store._LIKE_SQL[:40]}


def case_songs_threshold():
    """10 万首：99999 关，100000 开。"""
    low = fts_gate.evaluate(99_999, 0, 0.0)
    high = fts_gate.evaluate(100_000, 0, 0.0)
    ok = (not low["enable"]) and high["enable"] and high["hit"] == ["songs"]
    return ok, {"99999": low["enable"], "100000": high["enable"], "hit": high["hit"]}


def case_db_threshold():
    """100MB：差 1 字节关，正好 100MB 开。"""
    low = fts_gate.evaluate(0, 100 * MB - 1, 0.0)
    high = fts_gate.evaluate(0, 100 * MB, 0.0)
    ok = (not low["enable"]) and high["enable"] and high["hit"] == ["db_bytes"]
    return ok, {"99MB+": low["enable"], "100MB": high["enable"], "hit": high["hit"]}


def case_p95_threshold():
    """p95 50ms：49.999 关，50.0 开。"""
    low = fts_gate.evaluate(0, 0, 49.999)
    high = fts_gate.evaluate(0, 0, 50.0)
    ok = (not low["enable"]) and high["enable"] and high["hit"] == ["p95_ms"]
    return ok, {"49.999": low["enable"], "50.0": high["enable"], "hit": high["hit"]}


def case_all_below():
    """三项都差一点 → 一条都不许命中。"""
    v = fts_gate.evaluate(99_999, 100 * MB - 1, 49.999)
    return (v["enable"] is False and v["hit"] == []), v["values"]


def case_env_default_off():
    """未设/空/0/乱七八糟 → 一律 off（失败关闭），且不许去量库。"""
    out = {}
    ok = True
    for raw in (None, "", " ", "0", "off", "false", "auto1"):
        env = {} if raw is None else {ENV_KEY: raw}
        d = fts_gate.resolve("Z:/nope/missing.sqlite3", env=env, cache=False)
        out[repr(raw)] = (d["mode"], d["enable"])
        ok = ok and d["mode"] == "off" and d["enable"] is False
    # "AUTO " 带尾空格 → 归一成 auto，会被当 auto 走；单独看它不炸即可
    out["AUTO "] = fts_gate.resolve("Z:/nope/missing.sqlite3",
                                    env={ENV_KEY: "AUTO "}, cache=False)["mode"]
    return ok and out["AUTO "] == "auto", out


def case_env_on():
    """1/true/on（含大小写）→ 无条件开。"""
    out = {}
    ok = True
    for raw in ("1", "true", "TRUE", "on", "On"):
        d = fts_gate.resolve("Z:/nope/missing.sqlite3", env={ENV_KEY: raw}, cache=False)
        out[raw] = (d["mode"], d["enable"], d["reason"])
        ok = ok and d["mode"] == "on" and d["enable"] is True
    return ok, out


def case_auto_evidence():
    """auto + 注入证据：任一阈值过就开，reason 说清。"""
    below = fts_gate.resolve("Z:/nope/x.sqlite3", env={ENV_KEY: "auto"},
                             evidence={"songs": 1, "db_bytes": 1, "p95_ms": 0.5}, cache=False)
    by_songs = fts_gate.resolve("Z:/nope/x.sqlite3", env={ENV_KEY: "auto"},
                                evidence={"songs": 100_000, "db_bytes": 1, "p95_ms": 0.5},
                                cache=False)
    by_db = fts_gate.resolve("Z:/nope/x.sqlite3", env={ENV_KEY: "auto"},
                             evidence={"songs": 1, "db_bytes": 100 * MB, "p95_ms": 0.5},
                             cache=False)
    by_p95 = fts_gate.resolve("Z:/nope/x.sqlite3", env={ENV_KEY: "auto"},
                              evidence={"songs": 1, "db_bytes": 1, "p95_ms": 50.0},
                              cache=False)
    ok = (not below["enable"] and below["reason"] == "threshold_not_met"
          and by_songs["enable"] and by_songs["hit"] == ["songs"]
          and by_db["enable"] and by_db["hit"] == ["db_bytes"]
          and by_p95["enable"] and by_p95["hit"] == ["p95_ms"])
    return ok, {"below": below["enable"], "songs": by_songs["hit"],
                "db": by_db["hit"], "p95": by_p95["hit"]}


def case_auto_real_db():
    """真实小库 + auto：证据能取到、判关、open_index 不开 FTS。"""
    db = _fresh_db("real")
    try:
        _seed(db)
        fts_gate.clear_cache()
        d = fts_gate.resolve(db, env={ENV_KEY: "auto"}, cache=False)
        ev = fts_gate.collect_evidence(db, cache=False)
        with _Env("auto"):
            opened = fts_gate.open_index(db)
            via_store = store.Index(db)  # store 自己只认 1/true/on → auto 必须当关
            try:
                ok = (d["mode"] == "auto" and d["enable"] is False
                      and ev["songs"] == len(SONGS) and ev["db_bytes"] > 0
                      and ev["p95_ms"] >= 0.0
                      and opened.use_fts is False and via_store.use_fts is False)
                detail = {"evidence": ev, "enable": d["enable"],
                          "open_index.use_fts": opened.use_fts,
                          "store.Index.use_fts": via_store.use_fts}
            finally:
                opened.conn.close()
                via_store.conn.close()
        return ok, detail
    finally:
        _drop_db(db)


def case_explicit_override():
    """显式表态压过 auto：use_fts=True 在 auto 下仍开；env=1 时 open_index 也开。"""
    db = _fresh_db("override")
    try:
        _seed(db)
        with _Env("auto"):
            forced = fts_gate.open_index(db, use_fts=True)
            try:
                forced_on = forced.use_fts
            finally:
                forced.conn.close()
        with _Env("1"):
            by_env = fts_gate.open_index(db)
            try:
                env_on = by_env.use_fts
            finally:
                by_env.conn.close()
        return (forced_on is True and env_on is True), {"use_fts=True": forced_on, "env=1": env_on}
    finally:
        _drop_db(db)


def case_fail_closed():
    """坏路径：证据全 0、auto 判关、不抛异常。"""
    try:
        ev = fts_gate.collect_evidence("Z:/nope/does-not-exist.sqlite3", cache=False)
        d = fts_gate.resolve("Z:/nope/does-not-exist.sqlite3", env={ENV_KEY: "auto"}, cache=False)
        ok = (ev == {"songs": 0, "db_bytes": 0, "p95_ms": 0.0}
              and d["enable"] is False and d["reason"] == "threshold_not_met")
        return ok, {"evidence": ev, "enable": d["enable"]}
    except Exception as exc:  # noqa: BLE001
        return False, {"error": repr(exc)}


def case_p95_measure():
    """measure_p95 在真实小库上给出非负毫秒数（不要求绝对值）。"""
    db = _fresh_db("p95")
    try:
        _seed(db)
        ms = fts_gate.measure_p95(db, samples=5, reps=2)
        return (ms >= 0.0), {"p95_ms": round(ms, 4)}
    finally:
        _drop_db(db)


CASES = [
    ("sql_alignment", "门 SQL 与 store 一致", case_sql_alignment),
    ("songs_threshold", "songs 10 万边界", case_songs_threshold),
    ("db_threshold", "库容 100MB 边界", case_db_threshold),
    ("p95_threshold", "p95 50ms 边界", case_p95_threshold),
    ("all_below", "三项全差一点", case_all_below),
    ("env_default_off", "默认 off / 无法识别", case_env_default_off),
    ("env_on", "1/true/on 直开", case_env_on),
    ("auto_evidence", "auto 三阈值（注入证据）", case_auto_evidence),
    ("auto_real_db", "auto 真实小库判关", case_auto_real_db),
    ("explicit_override", "显式表态优先", case_explicit_override),
    ("fail_closed", "坏路径失败关闭", case_fail_closed),
    ("p95_measure", "p95 现场采样可用", case_p95_measure),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== FTS 阈值门回归（{len(CASES)} 条，离线）==")
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
