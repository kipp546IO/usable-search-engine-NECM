"""相关性可见化回归（离线，零网络）。

覆盖：
  - match_field_of 优先级：title > desc > tag > 空
  - match_field_of 大小写不敏感
  - Governor(match='std2')：仅标签命中被剔除，标题命中保留
  - Governor(match='std')：仅标签命中仍保留（旧行为不变）
  - order_by_match_field：标题 → 简介 → 标签 → 其它，同档相对顺序不变

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_relevance.py
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import governance, server


def _v(**kw) -> dict:
    """构造一条最小视频记录（默认三处都不含关键词）。"""
    v = {"bvid": "BV1", "title": "", "desc": "", "tag": "", "hit_columns": []}
    v.update(kw)
    return v


def case_match_field_priority() -> tuple[bool, dict]:
    """三处都命中→title；只简介→desc；只标签→tag；都不含→空。"""
    all_hit = _v(title="春日影 live", desc="春日影 cover", tag="春日影")
    only_desc = _v(desc="翻唱 春日影")
    only_tag = _v(tag="春日影,邦邦")
    none = _v(title="别的歌", desc="无关", tag="其他")
    got = {
        "all": server.match_field_of(all_hit, "春日影"),
        "desc": server.match_field_of(only_desc, "春日影"),
        "tag": server.match_field_of(only_tag, "春日影"),
        "none": server.match_field_of(none, "春日影"),
    }
    ok = got == {"all": "title", "desc": "desc", "tag": "tag", "none": ""}
    return ok, got


def case_match_field_case() -> tuple[bool, dict]:
    """查询词大写、内容小写（及反过来）都能命中。"""
    v = _v(title="Spring Song", desc="", tag="")
    got_title = server.match_field_of(v, "SPRING")
    got_tag = server.match_field_of(_v(tag="春日影"), "春日影")
    got_miss = server.match_field_of(_v(title="spring"), "AUTUMN")
    ok = got_title == "title" and got_tag == "tag" and got_miss == ""
    return ok, {"upper_q": got_title, "tag": got_tag, "miss": got_miss}


def case_governor_std2() -> tuple[bool, dict]:
    """std2：仅标签命中 → phrase 剔除；标题命中 → 保留。"""
    gov = governance.Governor("春日影", match="std2")
    tag_only = _v(tag="春日影", hit_columns=["tag"])
    r1 = gov.check(tag_only)
    title_hit = _v(title="春日影", hit_columns=["title"])
    r2 = gov.check(title_hit)
    ok = (r1 == "phrase" and gov.dropped["phrase"] == 1
          and r2 is None and gov.enabled is True)
    return ok, {"tag_only": r1, "dropped": gov.dropped["phrase"],
                "title_hit": r2, "enabled": gov.enabled}


def case_governor_std_still_allows_tag() -> tuple[bool, dict]:
    """std：仅标签命中仍保留（旧行为不变）。"""
    gov = governance.Governor("春日影", match="std")
    r = gov.check(_v(tag="春日影", hit_columns=["tag"]))
    ok = r is None and gov.dropped["phrase"] == 0 and gov.kept == 1
    return ok, {"result": r, "dropped": gov.dropped["phrase"], "kept": gov.kept}


def case_sort_tag_last() -> tuple[bool, dict]:
    """稳定重排：title, desc, tag, 空；同档内相对顺序不变。"""
    items = [
        _v(bvid="T1", match_field="tag"),
        _v(bvid="D1", match_field="desc"),
        _v(bvid="H1", match_field="title"),
        _v(bvid="X1", match_field=""),
        _v(bvid="H2", match_field="title"),
        _v(bvid="T2", match_field="tag"),
    ]
    order = [v["bvid"] for v in server.order_by_match_field(items)]
    expect = ["H1", "H2", "D1", "T1", "T2", "X1"]
    ok = order == expect and len(order) == len(items)
    return ok, {"order": order}


def case_platform_dur() -> tuple[bool, dict]:
    """本地时长区间要正确映射到平台档，映射不上时返回 None 走本地过滤。"""
    from usable_search.server import _platform_dur
    cases = {
        (None, 600): "1",      # 10 分钟以内
        (600, 900): "2",       # 10~15 分钟
        (900, 1800): "2",      # 15~30 分钟
        (1800, 3600): "3",     # 30~60 分钟
        (3600, 7200): "4",     # 60~120 分钟
        (7200, None): "4",     # 120 分钟以上
        (300, 1200): None,     # 跨档：平台筛不了，本地兜底
        (None, None): None,    # 不限
    }
    got = {k: _platform_dur(k) for k in cases}
    ok = got == cases
    return ok, {"got": {str(k): v for k, v in got.items()}}


CASES = [
    ("match_field_priority", "命中字段优先级", case_match_field_priority),
    ("match_field_case", "命中字段大小写不敏感", case_match_field_case),
    ("governor_std2", "std2 剔除仅标签命中", case_governor_std2),
    ("governor_std_still_allows_tag", "std 仍允许标签命中", case_governor_std_still_allows_tag),
    ("sort_tag_last", "标签命中稳定降到最后", case_sort_tag_last),
    ("platform_dur", "时长区间→平台时长档映射", case_platform_dur),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== 相关性可见化回归（{len(CASES)} 条，离线）==")
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
