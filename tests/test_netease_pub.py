"""网易云「发行时间」筛选回归（N5 收尾项，全程不联网）。

覆盖：
  - `_pub_band` / `_pub_date` / `_pub_hit` 口径：近 N 年边界（正好一年前那天算命中）、
    缺失/为 0 的 publishTime 在筛选开启时排除、自定义年份区间、非法参数当不限
  - `norm_song` 把 album.publishTime 带进记录（毫秒，不额外请求）
  - 服务层：筛选发生在排序后、分页截断前（第 2 页仍有货，不是「先截断再过滤」）
  - cache_key 含 pub：不同筛选共用一条缓存会串结果，这里用「缓存命中次数」验伪
用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_netease_pub.py
    PYTHONUTF8=1 PYTHONPATH=src python -m pytest tests/test_netease_pub.py -q
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import calendar
import json
import os
import sys
import time
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import netease, pipeline, server

RUN_DIR = os.path.join(ROOT, "tests", ".run")
Q = "发行时间测试"
TODAY = date.today()


# ------------------------------------------------------------------ 假数据工具
def _ms(y: int, m: int, d: int) -> int:
    """本地某天正午 → 毫秒时间戳（取正午，避开时区把日期挪一天）。"""
    return int(time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))) * 1000


def _shift(years: int = 0, days: int = 0) -> date:
    """按日历年平移今天（2/29 落到平年退到 2/28）。"""
    y = TODAY.year - years
    d = TODAY.day
    if TODAY.month == 2 and d == 29 and not calendar.isleap(y):
        d = 28
    return date(y, TODAY.month, d) + timedelta(days=days)


def _db(tag: str) -> str:
    os.makedirs(RUN_DIR, exist_ok=True)
    path = os.path.join(RUN_DIR, f"netease_pub_{tag}_{os.getpid()}.sqlite3")
    if os.path.exists(path):
        os.remove(path)
    return path


def _rec(i: int, day: date | None) -> dict:
    """一条候选；day=None 表示 publishTime 缺失（0）。"""
    return {
        "source": "netease", "id": i, "title": f"{Q} {i:03d}",
        "artists": "测试艺人", "album": "测试专辑", "album_id": 1,
        "cover": "https://example.invalid/cover.jpg", "duration": 180000,
        "fee": 0, "origin": "search",
        "publish_time": _ms(day.year, day.month, day.day) if day else 0,
    }


def _install(records: list) -> list:
    """把采集/封面/歌词全换成假的，并记下 collect 被调用了几次（验缓存）。"""
    calls: list = []
    server._NETEASE_CACHE.clear()
    pipeline.collect_records = lambda q, pages=3: (
        calls.append(q) or [dict(r) for r in records],
        {"search": len(records), "albums": [], "artists": [], "prefer_artists": []},
    )
    server._attach_lyric_lines = lambda *a, **k: None      # 歌词要打接口，关掉
    netease.songs_detail = lambda ids: []                  # 封面兜底也关掉
    netease.song_lyric = lambda sid: ("", 0)
    return calls


# ------------------------------------------------------------------ 用例
def test_parse_and_boundary() -> dict:
    """固定 now=2026-10-22：近 1 年从 2025-10-22 起（含当天）。"""
    now = time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1))
    band = server._pub_band("1", now=now)
    assert band == ((2025, 10, 22), None), band
    assert server._pub_hit(_ms(2025, 10, 22), band), "正好一年前那天必须算命中"
    assert server._pub_hit(_ms(2026, 10, 22), band)
    assert not server._pub_hit(_ms(2025, 10, 21), band), "一年前一天必须排除"
    # 未知发行时间：筛选开启就排除（宁缺勿滥）
    assert not server._pub_hit(0, band)
    assert not server._pub_hit(None, band)
    assert not server._pub_hit("", band)
    assert not server._pub_hit(123, band), "离谱时间戳当未知"
    # 不限
    assert server._pub_band("") is None
    assert server._pub_band("abc") is None
    assert server._pub_hit(0, server._pub_band("")) is True
    # 自定义区间（含两端、写反自动交换、可只写一侧）
    assert server._pub_band("2015-2020") == ((2015, 1, 1), (2020, 12, 31))
    assert server._pub_band("2020-2015") == ((2015, 1, 1), (2020, 12, 31))
    assert server._pub_band("2015-") == ((2015, 1, 1), None)
    assert server._pub_band("-2020") == (None, (2020, 12, 31))
    assert server._pub_hit(_ms(2020, 12, 31), ((2015, 1, 1), (2020, 12, 31)))
    assert not server._pub_hit(_ms(2021, 1, 1), ((2015, 1, 1), (2020, 12, 31)))
    # 2/29 落到平年：退到 2/28，不能把这天判成不存在
    leap_now = time.mktime((2024, 2, 29, 12, 0, 0, 0, 0, -1))
    assert server._pub_band("1", now=leap_now) == ((2023, 2, 28), None)
    # 秒/毫秒兼容：别把 2023 判成 1970
    assert server._pub_date(_ms(2023, 11, 1)) == (2023, 11, 1)
    assert server._pub_date(_ms(2023, 11, 1) // 1000) == (2023, 11, 1)
    return {"band": band, "leap": server._pub_band("1", now=leap_now)}


def test_norm_song_keeps_publish_time() -> dict:
    """原始结果自带 album.publishTime，要一路带到记录里（不额外请求）。"""
    rec = netease.norm_song(
        {"id": 1, "name": "某歌", "ar": [{"name": "某人"}],
         "al": {"id": 2, "name": "某专辑", "publishTime": 1698768000000}}, "search")
    assert rec["publish_time"] == 1698768000000, rec
    assert server._pub_date(rec["publish_time"]) == (2023, 11, 1)
    missing = netease.norm_song({"id": 3, "name": "无发行时间", "al": {"id": 4}}, "search")
    assert missing["publish_time"] == 0
    return {"publish_time": rec["publish_time"]}


def test_recent_one_year_runtime() -> dict:
    """服务层实测（今天为基准）：正好一年前命中，差一天与未知排除。"""
    db = _db("recent")
    exact = _shift(years=1)
    older = _shift(years=1, days=-1)
    records = [
        _rec(1, _shift(days=-30)),        # 30 天前
        _rec(2, exact),                   # 正好一年前那一天（命中）
        _rec(3, older),                   # 一年前一天（排除）
        _rec(4, None),                    # 缺 publishTime（排除）
        _rec(5, _shift(years=2)),         # 两年前（排除）
    ]
    _install(records)
    out = server.run_netease_songs(Q, db, pub="1")
    ids = [it["id"] for it in out["items"]]
    assert ids == [1, 2], (ids, exact, older)
    assert out["total"] == 2 and out["stats"]["pub"] == "1"
    base = server.run_netease_songs(Q, db, pub="")
    assert base["total"] == 5, "不限时 5 条都要在（含未知发行时间）"
    return {"pub=1": ids, "total_all": base["total"]}


def test_custom_year_range() -> dict:
    """自定义年份区间：两端都含；越界年份排除。"""
    db = _db("custom")
    y = TODAY.year
    records = [
        _rec(1, date(y - 6, 6, 1)), _rec(2, date(y - 5, 1, 1)), _rec(3, date(y - 3, 12, 31)),
        _rec(4, date(y - 2, 1, 1)), _rec(5, None),
    ]
    _install(records)
    out = server.run_netease_songs(Q, db, pub=f"{y - 5}-{y - 3}")
    ids = sorted(it["id"] for it in out["items"])
    assert ids == [2, 3], ids
    # 写反了也要认
    rev = server.run_netease_songs(Q, db, pub=f"{y - 3}-{y - 5}")
    assert sorted(it["id"] for it in rev["items"]) == [2, 3]
    # 只写结束年：往前不限
    tail = server.run_netease_songs(Q, db, pub=f"-{y - 5}")
    assert sorted(it["id"] for it in tail["items"]) == [1, 2], tail["total"]
    return {"range": f"{y - 5}-{y - 3}", "ids": ids}


def test_paging_with_filter() -> dict:
    """筛选在排序后、分页截断前：60 条里 45 条近一年，第 2 页仍要有 15 条。"""
    db = _db("paging")
    records = [_rec(i, _shift(days=-30)) for i in range(1, 46)]
    records += [_rec(i, _shift(years=8)) for i in range(46, 61)]
    calls = _install(records)
    p1 = server.run_netease_songs(Q, db, pub="1", page=1, pagesize=30)
    p2 = server.run_netease_songs(Q, db, pub="1", page=2, pagesize=30)
    p3 = server.run_netease_songs(Q, db, pub="1", page=3, pagesize=30)
    ids1 = [it["id"] for it in p1["items"]]
    ids2 = [it["id"] for it in p2["items"]]
    assert p1["total"] == 45, p1["total"]
    assert len(ids1) == 30 and len(ids2) == 15, (len(ids1), len(ids2))
    assert len(ids1) + len(ids2) == 45
    assert not set(ids1) & set(ids2), "页间不许重叠"
    assert p2["has_more"] is False and p1["has_more"] is True
    assert all(i <= 45 for i in ids1 + ids2), "老歌混进来了"
    assert p3["page"] == 2 and len(p3["items"]) == 15, "超页要夹到最后一页"
    assert len(calls) == 1, f"同一 cache_key 只该采集一次，实际 {len(calls)}"
    allbase = server.run_netease_songs(Q, db, pub="", page=1, pagesize=30)
    assert allbase["total"] == 60
    return {"total_pub1": p1["total"], "p1": len(ids1), "p2": len(ids2), "total_all": 60}


def test_cache_key_isolation() -> dict:
    """cache_key 必须含 pub：否则先查「近1年」再查「不限」会拿到过滤后的残池。

    注意 `_db_stamp` 的口径：任何一次入库都会让别的 key 的缓存失效（历史上就有的设计），
    所以这里不苛求「跨 pub 交替也零采集」，只验两件事：
      ① 交错查询的结果绝不串（这才是串缓存的真实症状）；
      ② 同一个 key 连续查是缓存命中，且两个 pub 各占一条缓存条目。
    """
    db = _db("cache")
    records = [_rec(1, _shift(days=-30)), _rec(2, _shift(years=9)), _rec(3, None)]
    calls = _install(records)
    a1 = server.run_netease_songs(Q, db, pub="")
    a2 = server.run_netease_songs(Q, db, pub="")       # 同 key 连查：必须命中缓存
    n_after_a = len(calls)
    b1 = server.run_netease_songs(Q, db, pub="1")
    b2 = server.run_netease_songs(Q, db, pub="1")      # 同 key 连查：命中
    c = server.run_netease_songs(Q, db, pub="")        # 回到不限：绝不能被 b 污染
    d = server.run_netease_songs(Q, db, pub="1")
    assert a1["total"] == 3 and a2["total"] == 3
    assert n_after_a == 1, f"同 key 连查应只采集一次，实际 {n_after_a}"
    assert b1["total"] == 1 and b2["total"] == 1, (b1["total"], b2["total"])
    assert c["total"] == 3, f"不限被串成了 {c['total']} 条"
    assert d["total"] == 1, f"近1年被串成了 {d['total']} 条"
    assert [it["id"] for it in c["items"]] == [it["id"] for it in a1["items"]]
    keys = [k for k in server._NETEASE_CACHE if k[0] == os.path.abspath(db)]
    pubs = sorted(k[3] for k in keys)
    assert pubs == ["", "1"], f"两个 pub 应是两条缓存，实际 {pubs}"
    sizes = {k[3]: len(server._NETEASE_CACHE[k]["items"]) for k in keys}
    assert sizes == {"": 3, "1": 1}, f"缓存内容也各归各，实际 {sizes}"
    return {"cache_pubs": pubs, "cached_items": sizes, "collect_calls": len(calls),
            "totals": [a1["total"], b1["total"], c["total"], d["total"]]}


def test_http_passes_pub() -> dict:
    """HTTP 层：/api/search 要把 pub 原样透传给 run_netease_songs（只起本机端口）。"""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    db = _db("http")
    _install([])
    seen: dict = {}
    orig_run, orig_db = server.run_netease_songs, server.Handler.db_path
    orig_log = server.Handler.log_message

    def fake_run(q, db_path, dur="", pub="", sort="", vip="", orig="", page=1, pagesize=30):
        seen.update({"q": q, "dur": dur, "pub": pub, "sort": sort, "vip": vip, "orig": orig,
                     "page": page, "pagesize": pagesize})
        return {"items": [], "stats": {}, "page": 1, "pagesize": 30, "total": 0, "has_more": False}

    srv = None
    try:
        server.run_netease_songs = fake_run
        server.Handler.db_path = db
        server.Handler.log_message = lambda *a, **k: None
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = (f"http://127.0.0.1:{port}/api/search?src=netease&type=song&q=%E6%B5%8B"
               "&pub=2015-2020&dur=2-4&sort=quality&vip=only&orig=not&page=2&size=50")
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    finally:
        server.run_netease_songs = orig_run
        server.Handler.db_path = orig_db
        server.Handler.log_message = orig_log
        if srv is not None:
            srv.shutdown()
            srv.server_close()
    assert seen.get("pub") == "2015-2020", seen
    assert (seen.get("sort"), seen.get("vip"), seen.get("orig")) == ("quality", "only", "not"), seen
    assert seen.get("dur") == "2-4" and str(seen.get("page")) == "2", seen
    assert payload.get("q") == "测", payload
    return {"seen": seen}


CASES = [
    ("parse", "口径与近1年边界", test_parse_and_boundary),
    ("norm", "publishTime 透传", test_norm_song_keeps_publish_time),
    ("recent", "近1年实测（含边界/缺失）", test_recent_one_year_runtime),
    ("custom", "自定义年份区间", test_custom_year_range),
    ("paging", "分页与筛选叠加", test_paging_with_filter),
    ("cache", "cache_key 隔离", test_cache_key_isolation),
    ("http", "/api/search 透传 pub", test_http_passes_pub),
]

_ORIG = (pipeline.collect_records, server._attach_lyric_lines,
         netease.songs_detail, netease.song_lyric)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== 网易云发行时间筛选回归（{len(CASES)} 条）==")
    passed, all_ok = 0, True
    try:
        for cid, label, fn in CASES:
            t0 = time.time()
            try:
                detail = fn()
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, {"error": repr(exc)}
            all_ok = all_ok and ok
            passed += 1 if ok else 0
            print(f"[{cid}] {label} —— {'PASS' if ok else 'FAIL'}（{round(time.time()-t0, 2)}s）"
                  f"  {json.dumps(detail, ensure_ascii=False)}")
    finally:
        (pipeline.collect_records, server._attach_lyric_lines,
         netease.songs_detail, netease.song_lyric) = _ORIG
        server._NETEASE_CACHE.clear()
    print(f"\n== 汇总: {passed}/{len(CASES)} PASS ==")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
