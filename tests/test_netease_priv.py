"""网易云「音质 / VIP / 原唱」筛选与排序回归（全程不联网）。

覆盖：
  - 每张卡片带 quality / quality_name / vip / original 字段，且取值正确
  - vip=only / vip=not 三态过滤
  - orig=only / orig=not（原唱候选按 rank 的 kind 判定）
  - sort=quality：超清母带 > 高清臻音 > 无损 > 极高 > 较高 > 标准，同档保持原顺序
  - 权限查询带缓存：同一批 id 连续搜两次只打一次接口
用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_netease_priv.py
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import netease, pipeline, server

RUN_DIR = os.path.join(ROOT, "tests", ".run")
Q = "音质测试"

# id -> (音质档, fee)。fee 非 0 视为 VIP/付费
# id -> (音质档, fee, originCoverType, 原曲作者)。fee 非 0 视为 VIP/付费；
# cover_type==2 或 orig_artists 有值代表接口标明是改编/翻唱，不算原唱
PRIV = {
    1: ("lossless", 1, 1, ""),
    2: ("hires", 0, 2, "MyGO!!!!!"),
    3: ("jymaster", 1, 1, ""),
    4: ("standard", 0, 0, ""),
    5: ("lossless", 1, 0, ""),
    6: ("exhigh", 0, 0, ""),
    # 同批推断用：101 是明确原唱，102 同歌手但接口没标，103 同曲别的歌手
    101: ("lossless", 0, 1, ""),
    102: ("lossless", 0, 0, ""),
    103: ("lossless", 0, 0, ""),
}
# id -> 标题（后三条是翻唱/伴奏，用来验原唱过滤）
TITLES = {
    1: f"{Q}",
    2: f"{Q}",
    3: f"{Q}",
    4: f"{Q} 翻唱",
    5: f"{Q} 伴奏",
    6: f"{Q} 改编",
}


def _db(tag: str) -> str:
    os.makedirs(RUN_DIR, exist_ok=True)
    path = os.path.join(RUN_DIR, f"netease_priv_{tag}_{os.getpid()}.sqlite3")
    if os.path.exists(path):
        os.remove(path)
    return path


def _records() -> list:
    out = []
    for sid, title in TITLES.items():
        out.append({
            "source": "netease", "id": sid, "title": title,
            "artists": "测试艺人", "album": "测试专辑", "album_id": 1,
            "cover": "", "duration": 180000, "fee": 0, "origin": "search",
            "publish_time": int(time.time() * 1000) - 86400_000,
        })
    return out


def _install(records: list) -> dict:
    """采集/歌词/封面全假；song_privileges 记调用次数。"""
    calls = {"collect": 0, "priv": 0}

    def fake_collect(q, pages=3):
        calls["collect"] += 1
        return [dict(r) for r in records], {"search": len(records), "albums": [], "artists": [],
                                            "prefer_artists": []}

    def fake_priv(ids, chunk=100, timeout=12):
        calls["priv"] += 1
        return {i: {"level": PRIV[i][0], "fee": PRIV[i][1], "st": 0,
                    "cover_type": PRIV[i][2], "orig_artists": PRIV[i][3]}
                for i in ids if i in PRIV}

    pipeline.collect_records = fake_collect
    netease.song_privileges = fake_priv
    server._attach_lyric_lines = lambda *a, **k: None
    netease.songs_detail = lambda ids: []
    netease.song_lyric = lambda sid: ("", 0)
    server._NETEASE_CACHE.clear()
    server._PRIV_CACHE.clear()
    return calls


def _run(tag: str, **kw) -> dict:
    return server.run_netease_songs(Q, _db(tag), pagesize=30, **kw)


# ------------------------------------------------------------------ 用例
def test_fields() -> dict:
    _install(_records())
    d = _run("fields")
    got = {it["id"]: (it["quality_name"], it["vip"], it["original"]) for it in d["items"]}
    assert got[1] == ("无损", 1, 1), got
    assert got[2] == ("高清臻音", 0, 0), got      # 接口标了改编 → 不是原唱
    assert got[3] == ("超清母带", 1, 1), got
    assert got[5] == ("无损", 1, 0), got          # 伴奏 → 非原唱
    assert got[6] == ("极高", 0, 0), got          # 改编 → 非原唱
    return {"样本": got}


def test_vip_filter() -> dict:
    _install(_records())
    only = _run("vip_only", vip="only")
    notv = _run("vip_not", vip="not")
    assert {it["id"] for it in only["items"]} == {1, 3, 5}, only["items"]
    assert {it["id"] for it in notv["items"]} == {2, 4, 6}, notv["items"]
    assert all(it["vip"] for it in only["items"])
    assert not any(it["vip"] for it in notv["items"])
    return {"only": only["total"], "not": notv["total"]}


def test_orig_filter() -> dict:
    _install(_records())
    only = _run("orig_only", orig="only")
    noto = _run("orig_not", orig="not")
    assert {it["id"] for it in only["items"]} == {1, 3}, only["items"]   # 2 是翻唱，被排除
    assert {it["id"] for it in noto["items"]} == {2, 4, 5, 6}, noto["items"]
    return {"only": only["total"], "not": noto["total"]}


def test_quality_sort() -> dict:
    _install(_records())
    d = _run("qsort", sort="quality")
    seq = [it["quality"] for it in d["items"]]
    rank = {"jymaster": 6, "hires": 5, "lossless": 4, "exhigh": 3, "higher": 2, "standard": 1, "none": 0}
    assert seq == sorted(seq, key=lambda x: -rank[x]), seq
    assert seq[0] == "jymaster" and seq[-1] == "standard", seq
    return {"顺序": seq}


def test_priv_cache() -> dict:
    calls = _install(_records())
    _run("cache1")
    _run("cache2")            # 同 q、不同 db 文件 → 排序池重算，但权限应命中缓存
    assert calls["priv"] == 1, calls
    return {"priv_calls": calls["priv"], "collect_calls": calls["collect"]}


def test_same_batch_inference() -> dict:
    """同批同名曲：同歌手 → 推断原唱；不同歌手 → 疑似翻唱。"""
    base = dict(source="netease", album="同名专辑", album_id=1, cover="", duration=180000,
                fee=0, origin="search", publish_time=int(time.time() * 1000))
    recs = [
        dict(base, id=101, title=Q, artists="原唱A"),
        dict(base, id=102, title=Q, artists="原唱A"),
        dict(base, id=103, title=Q, artists="翻唱B"),
        dict(base, id=104, title=f"{Q} 另一个版本", artists="路人C"),   # 标题不同，不该被推断
    ]
    _install(recs)
    d = _run("guess")
    got = {it["id"]: it for it in d["items"]}
    assert got[101]["original"] == 1, got[101]
    assert got[102]["original"] == 0 and got[102]["guess_orig"] == 1, got[102]
    assert got[103]["guess_cover"] == 1 and got[103]["guess_artist"] == "原唱A", got[103]
    assert got[104]["guess_cover"] == 0 and got[104]["guess_orig"] == 0, got[104]
    return {"101": "原唱", "102": "原唱(推断)", "103": "疑似翻唱", "104": "未标注"}


def test_pass_mode() -> dict:
    f = server._pass_mode
    assert f("", True) and f("", False)
    assert f("only", True) and not f("only", False)
    assert f("not", False) and not f("not", True)
    return {"三态": "ok"}


CASES = [
    ("fields", "字段与取值", test_fields),
    ("vip", "VIP 三态", test_vip_filter),
    ("orig", "原唱三态", test_orig_filter),
    ("qsort", "音质排序", test_quality_sort),
    ("cache", "权限缓存只打一次", test_priv_cache),
    ("guess", "同批同名推断", test_same_batch_inference),
    ("mode", "_pass_mode 三态", test_pass_mode),
]

_ORIG = (pipeline.collect_records, server._attach_lyric_lines,
         netease.songs_detail, netease.song_lyric, netease.song_privileges)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== 网易云音质/VIP/原唱回归（{len(CASES)} 条）==")
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
         netease.songs_detail, netease.song_lyric, netease.song_privileges) = _ORIG
        server._NETEASE_CACHE.clear()
        server._PRIV_CACHE.clear()
    print(f"\n== 汇总: {passed}/{len(CASES)} PASS ==")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
