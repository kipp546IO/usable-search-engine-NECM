"""模糊搜索兜底回归（X3-3，离线零网络）。

覆盖：
  - 开关 USABLE_SEARCH_FUZZY 默认关、FUZZY_RATIO 非法值回落
  - 滑窗相似度：同音（春廷雪→春庭雪）、错字（春曰影）、颠倒（日春影）
  - OSA 距离：相邻交换算一步（区分「春日影」和「初日影」）
  - 治理层：fuzzy 只加保留、不新增剔除（开/关 dropped 对比）
  - 本地库兜底：精确 0 条时返回 fuzzy=1，top1 是目标词
  - 服务层接线：run_netease_songs 在精确为空时走兜底，items 带 fuzzy=1

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_fuzzy.py
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import fuzzy, governance, server, store  # noqa: E402

RUN_DIR = os.path.join(ROOT, "tests", ".run")
_FAILS = []
_COUNT = [0]


def _check(name: str, cond, detail: str = "") -> None:
    _COUNT[0] += 1
    if cond:
        print(f"  [{name}] PASS {detail}")
    else:
        _FAILS.append(name)
        print(f"  [{name}] FAIL {detail}")


def _db(tag: str) -> str:
    os.makedirs(RUN_DIR, exist_ok=True)
    path = os.path.join(RUN_DIR, f"fuzzy_{os.getpid()}_{tag}.sqlite3")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    return path


def _seed(tag: str) -> str:
    """临时库：两条「春日影」+ 若干无关项（覆盖 ≥3 字模糊兜底）。"""
    path = _db(tag)
    idx = store.Index(path)
    idx.upsert_songs([
        {"source": "netease", "id": 1, "title": "春日影", "artists": "MyGO!!!!!", "album": "迷跡波",
         "cover": "x"},
        {"source": "netease", "id": 2, "title": "春日影 (MyGO!!!!! ver.)", "artists": "MyGO!!!!!",
         "album": "迷跡波", "cover": "x"},
        {"source": "netease", "id": 3, "title": "完全无关的歌", "artists": "某人", "album": "某专辑",
         "cover": "x"},
        {"source": "netease", "id": 4, "title": "夜に駆ける", "artists": "YOASOBI", "album": "THE BOOK",
         "cover": "x"},
    ])
    idx.conn.close()
    return path


def main() -> int:
    print("== 模糊搜索兜底（X3-3）==")

    # 1. 开关与阈值
    os.environ.pop("USABLE_SEARCH_FUZZY", None)
    _check("flag-default-off", fuzzy.enabled() is False)
    os.environ["USABLE_SEARCH_FUZZY"] = "1"
    _check("flag-on", fuzzy.enabled() is True)
    os.environ["USABLE_SEARCH_FUZZY"] = "0"
    _check("flag-zero-off", fuzzy.enabled() is False)
    os.environ["USABLE_SEARCH_FUZZY"] = "1"
    os.environ.pop("FUZZY_RATIO", None)
    _check("thr-default", fuzzy.threshold() == 0.66)
    os.environ["FUZZY_RATIO"] = "0.8"
    _check("thr-override", fuzzy.threshold() == 0.8)
    os.environ["FUZZY_RATIO"] = "abc"
    _check("thr-bad-fallback", fuzzy.threshold() == 0.66)
    os.environ.pop("FUZZY_RATIO", None)

    # 2. 滑窗相似度三例
    _check("same-sound", fuzzy.score("春廷雪", "【春庭雪】") >= 0.66,
           f"score={fuzzy.score('春廷雪', '【春庭雪】'):.4f}")
    _check("typo", fuzzy.score("春曰影", "春日影") >= 0.66,
           f"score={fuzzy.score('春曰影', '春日影'):.4f}")
    _check("neighbor", fuzzy.score("日春影", "春日影") >= 0.66,
           f"score={fuzzy.score('日春影', '春日影'):.4f}")
    _check("no-hit", fuzzy.score("春日影", "完全无关") < 0.66,
           f"score={fuzzy.score('春日影', '完全无关'):.4f}")

    # 3. OSA：交换一步 vs 两处替换
    _check("osa-swap", fuzzy.osa("日春影", "春日影") == 1, f"osa={fuzzy.osa('日春影', '春日影')}")
    _check("osa-sub", fuzzy.osa("日春影", "初日影") == 2, f"osa={fuzzy.osa('日春影', '初日影')}")

    # 4. 治理层：模糊只加保留，不新增剔除
    v_open = {"title": "春庭雪", "desc": "", "tag": "", "hit_columns": ["title"]}
    g_open = governance.Governor("春廷雪", match="std", fuzzy=True)
    kept_open = g_open.check(v_open) is None and v_open.get("fuzzy") == 1
    _check("gov-fuzzy-keep", kept_open, f"summary={g_open.summary()['fuzzy_kept']}")
    v_closed = {"title": "春庭雪", "desc": "", "tag": "", "hit_columns": ["title"]}
    g_closed = governance.Governor("春廷雪", match="std", fuzzy=False)
    dropped_closed = g_closed.check(v_closed) == "phrase"
    _check("gov-fuzzy-off-drops", dropped_closed, f"dropped={g_closed.summary()['dropped']}")
    _check("gov-no-new-drops",
           g_open.summary()["dropped_total"] <= g_closed.summary()["dropped_total"],
           f"open={g_open.summary()['dropped_total']} closed={g_closed.summary()['dropped_total']}")
    v_exact = {"title": "春廷雪 live", "desc": "", "tag": "", "hit_columns": ["title"]}
    g_exact = governance.Governor("春廷雪", match="std", fuzzy=True)
    _check("gov-exact-unchanged", g_exact.check(v_exact) is None and not v_exact.get("fuzzy"),
           "精确命中不打 fuzzy")

    # 5. 本地库兜底
    db = _seed("fallback")
    idx = store.Index(db)
    for q in ("春曰影", "日春影"):
        _check(f"exact-empty-{q}", idx.search(q, limit=5) == [], "精确路径应 0 条")
        rows = fuzzy.fallback(idx.conn, q, limit=10)
        _check(f"fallback-{q}", bool(rows) and all(r["fuzzy"] == 1 for r in rows),
               f"hits={len(rows)} top1={rows[0]['title'] if rows else None}")
        _check(f"fallback-top1-{q}", bool(rows) and "春日影" in rows[0]["title"],
               f"top1={rows[0]['title'] if rows else None} score={rows[0]['fuzzy_score'] if rows else 0}")
    idx.conn.close()

    # 6. 服务层接线：精确为空 → 兜底，items 带 fuzzy=1
    calls = {"n": 0}

    def _fake_collect(q, pages=2):
        calls["n"] += 1
        return [], {"search": {"total": 0}, "prefer_artists": None}

    orig_collect, orig_cover, orig_lyric = server.pipeline.collect_records, server._fill_covers, server._attach_lyric_lines
    server.pipeline.collect_records = _fake_collect
    server._fill_covers = lambda *a, **k: None
    server._attach_lyric_lines = lambda *a, **k: None
    try:
        os.environ["USABLE_SEARCH_FUZZY"] = "1"
        out = server.run_netease_songs("春曰影", db, page=1, pagesize=30)
        _check("server-fuzzy-on",
               bool(out["items"]) and out["items"][0]["fuzzy"] == 1 and out["stats"]["fuzzy"] is True,
               f"items={len(out['items'])} top1={out['items'][0]['title'] if out['items'] else None}")
        os.environ["USABLE_SEARCH_FUZZY"] = "0"
        out2 = server.run_netease_songs("日春影", db, page=1, pagesize=30)
        _check("server-fuzzy-off", out2["items"] == [] and out2["stats"]["fuzzy"] is False,
               f"items={len(out2['items'])}")
    finally:
        server.pipeline.collect_records = orig_collect
        server._fill_covers = orig_cover
        server._attach_lyric_lines = orig_lyric
        os.environ.pop("USABLE_SEARCH_FUZZY", None)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db + suffix)
            except OSError:
                pass

    total = _COUNT[0]
    passed = total - len(_FAILS)
    print(f"== 汇总: {passed}/{total} PASS" + ("" if not _FAILS else f" · FAIL {len(_FAILS)}: {_FAILS}"))
    return 0 if not _FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
