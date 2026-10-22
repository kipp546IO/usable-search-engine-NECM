"""B站搜索回归测试（可回归的 M2/M3 验收）。

判定口径：每条用例独立断言，全过才算过。
覆盖：
  - 治理不变式：标准档返回的每条都必须「全词命中」且平台有命中标注（纯推位不会漏进来）
  - 多通道召回：错字「春廷雪」必须触发补通道并捞回真实视频
  - 维度过滤：名称/简介/标签 三个字段过滤的纯净性
  - 时间/时长过滤：2小时以上、一周内
  - 排序：按播放量单调不增
  - UP主 / UID 维度
  - 翻页：第1页与第2页不重复

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_bilibili.py
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_bilibili.py --case channels
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_bilibili.py --canary

退出码：全过 0；真实 FAIL 才返回 1；风控 SKIP 不算失败。原始结果写入 tests/last_run_bili.json。
注意：B站匿名连搜会被风控降权；用例之间默认留 1.5 秒，遇空结果会自动退避重试一次。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import bilibili
from usable_search.server import run_bilibili

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_RUN = os.path.join(HERE, "last_run_bili.json")


def _contains(item: dict, q: str) -> bool:
    ql = q.lower()
    return (ql in (item.get("title") or "").lower()
            or ql in (item.get("desc") or "").lower()
            or ql in (item.get("tag") or "").lower())


# ---------- 用例 ----------

def case_phrase() -> tuple[bool, dict]:
    """标准档不变式：每条都全词命中，且平台有命中标注（零命中推位不得漏入）。"""
    q = "キラキラだとか夢だとか"
    d = run_bilibili(q, {"mode": "all", "pagesize": 30, "match": "std"})
    items = d["items"]
    ok = (len(items) > 0
          and all(_contains(it, q) for it in items)
          and all(it.get("hit_columns") for it in items))
    return ok, {"n": len(items), "scanned": d["scanned"], "gov": d["governance"]["dropped"]}


def case_channels() -> tuple[bool, dict]:
    """多通道召回：错字「春廷雪」必须补通道并捞到真实视频。"""
    q = "春廷雪"
    d = run_bilibili(q, {"mode": "all", "pagesize": 30, "match": "std"})
    items = d["items"]
    typo_hits = [it for it in items if q in (it.get("title") or "") + (it.get("desc") or "") + (it.get("tag") or "")]
    ok = len(d.get("channels") or []) >= 2 and len(typo_hits) >= 1
    return ok, {"n": len(items), "channels": d.get("channels"),
                "typo_hits": [it["title"][:32] for it in typo_hits[:3]],
                "scanned": d["scanned"]}


def case_title() -> tuple[bool, dict]:
    """名称维度：返回条目标题必须都含关键词。"""
    q = "MyGO"
    d = run_bilibili(q, {"mode": "title", "pagesize": 30})
    items = d["items"]
    ok = len(items) >= 10 and all(q.lower() in it["title"].lower() for it in items)
    return ok, {"n": len(items), "pool": d["pool_size"]}


def case_desc() -> tuple[bool, dict]:
    """简介维度：返回条目简介必须都含关键词。"""
    q = "MyGO"
    d = run_bilibili(q, {"mode": "desc", "pagesize": 30})
    items = d["items"]
    ok = len(items) >= 5 and all(q.lower() in (it.get("desc") or "").lower() for it in items)
    return ok, {"n": len(items), "pool": d["pool_size"], "scanned": d["scanned"]}


def case_tag() -> tuple[bool, dict]:
    """标签维度：返回条目标签必须都含关键词。"""
    q = "MyGO"
    d = run_bilibili(q, {"mode": "tag", "pagesize": 30})
    items = d["items"]
    ok = len(items) >= 5 and all(q.lower() in (it.get("tag") or "").lower() for it in items)
    return ok, {"n": len(items), "pool": d["pool_size"], "scanned": d["scanned"]}


def case_dur() -> tuple[bool, dict]:
    """时长过滤：2小时以上 → 每条时长 ≥ 7200 秒。"""
    d = run_bilibili("MyGO", {"mode": "all", "pagesize": 30, "match": "std", "dur": "120-"})
    items = d["items"]
    ok = len(items) >= 3 and all(it["duration_sec"] >= 7200 for it in items)
    return ok, {"n": len(items), "durs": [it["duration_sec"] for it in items[:5]]}


def case_pub() -> tuple[bool, dict]:
    """发布时间过滤：一周内 → 每条发布在 7 天内。"""
    now = time.time()
    d = run_bilibili("MyGO", {"mode": "all", "pagesize": 30, "match": "std", "pub": "7d"})
    items = d["items"]
    ok = len(items) >= 5 and all(it["pubdate"] >= now - 7 * 86400 - 3600 for it in items)
    return ok, {"n": len(items)}


def case_order() -> tuple[bool, dict]:
    """排序：按播放量 → 单调不增。"""
    d = run_bilibili("MyGO", {"mode": "all", "pagesize": 30, "match": "std", "order": "click"})
    plays = [it["play"] for it in d["items"]]
    ok = len(plays) >= 10 and all(plays[i] >= plays[i + 1] for i in range(len(plays) - 1))
    return ok, {"n": len(plays), "top": plays[:5]}


def case_user() -> tuple[bool, dict]:
    """UP主维度：返回用户条目带 mid 与昵称。"""
    d = run_bilibili("MyGO", {"mode": "user", "pagesize": 30})
    items = d["items"]
    ok = len(items) >= 5 and all(it.get("mid") and it.get("name") for it in items)
    return ok, {"n": len(items), "first": items[0]["name"] if items else None}


def case_uid() -> tuple[bool | None, dict]:
    """UID 维度：按 UID 直取用户；风控时 SKIP，不把平台限制记成产品失败。"""
    try:
        d = run_bilibili("1459104794", {"mode": "uid", "pagesize": 30})
    except bilibili.RiskControlError as exc:
        return None, {"skip": "B站风控", "detail": str(exc)[:90]}
    items = d["items"]
    ok = len(items) == 1 and int(items[0]["mid"]) == 1459104794
    return ok, {"first": items[0]["name"] if items else None,
                "fans": items[0].get("fans") if items else None}


def case_paging() -> tuple[bool, dict]:
    """翻页：第 1 页与第 2 页的 bvid 不重复。"""
    opts = {"mode": "all", "pagesize": 50, "match": "std"}
    p1 = run_bilibili("MyGO", dict(opts, page=1))
    time.sleep(1.0)
    p2 = run_bilibili("MyGO", dict(opts, page=2))
    s1 = {it["bvid"] for it in p1["items"]}
    s2 = {it["bvid"] for it in p2["items"]}
    ok = len(s1) == 50 and len(s2) > 0 and not (s1 & s2)
    return ok, {"page1": len(s1), "page2": len(s2), "overlap": len(s1 & s2)}


def case_oldest() -> tuple[bool, dict]:
    """最旧排序：日期必须严格递增，且真能挖到 2024 年以前的老视频（防退化成「最新1000条倒排」）。"""
    cutoff = int(time.mktime((2024, 1, 1, 0, 0, 0, 0, 0, -1)))
    d = run_bilibili("春日影", {"mode": "title", "pagesize": 30, "order": "oldest"})
    items = d["items"]
    dates = [it["pubdate"] for it in items]
    ok = (len(items) >= 10 and dates == sorted(dates) and dates[0] < cutoff
          and (d.get("channels") or []) == ["pubdate-window"])
    return ok, {"n": len(items), "first": dates[0] if dates else None,
                "last": dates[-1] if dates else None, "channels": d.get("channels")}


CASES = [
    ("phrase", "标准档全词不变式", case_phrase),
    ("channels", "多通道召回（错字）", case_channels),
    ("title", "名称维度纯净性", case_title),
    ("desc", "简介维度纯净性", case_desc),
    ("tag", "标签维度纯净性", case_tag),
    ("dur", "时长过滤 2h+", case_dur),
    ("pub", "发布时间 7 天内", case_pub),
    ("order", "播放量排序单调", case_order),
    ("oldest", "最旧排序（真最旧）", case_oldest),
    ("user", "UP主维度", case_user),
    ("uid", "UID 维度", case_uid),
    ("paging", "翻页不重复", case_paging),
]


def canary() -> bool:
    """B站搜索连通性探针（风控时返回空）。"""
    try:
        return len(bilibili.search_videos("MyGO", page=1)) > 0
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="B站搜索回归测试")
    ap.add_argument("--case", help="只跑某条用例 id")
    ap.add_argument("--canary", action="store_true", help="只做连通性探针")
    ap.add_argument("--gap", type=float, default=1.5, help="用例之间的等待秒数")
    args = ap.parse_args()

    if args.canary:
        ok = canary()
        print("[canary]", "OK——B站搜索接口有响应" if ok else "空结果——疑似风控，稍后再试")
        return 0 if ok else 2

    cases = CASES
    if args.case:
        cases = [c for c in CASES if c[0] == args.case]
        if not cases:
            print("没有这条用例:", args.case)
            return 1

    print(f"== B站回归测试（{len(cases)} 条用例）==")
    results, all_ok = [], True
    skipped = 0
    for i, (cid, label, fn) in enumerate(cases):
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, {"error": repr(exc)}
        secs = round(time.time() - t0, 1)
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        results.append({"id": cid, "label": label, "ok": ok, "status": status,
                        "seconds": secs, "detail": detail})
        all_ok = all_ok and (ok is not False)
        skipped += 1 if ok is None else 0
        print(f"[{cid}] {label} —— {status}（{secs}s）  {json.dumps(detail, ensure_ascii=False)}")
        if i != len(cases) - 1:
            time.sleep(args.gap)

    total, passed = len(results), sum(1 for r in results if r["ok"] is True)
    print(f"\n== 汇总: {passed}/{total} PASS · SKIP {skipped} · FAIL {total - passed - skipped} ==")
    try:
        with open(LAST_RUN, "w", encoding="utf-8") as f:
            json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results},
                      f, ensure_ascii=False, indent=2)
        print("原始结果:", LAST_RUN)
    except Exception as e:  # noqa: BLE001
        print("写 last_run_bili.json 失败:", e)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
