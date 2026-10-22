"""召回回归测试（可回归的 M1 验收）。

判定口径（每条用例两个层次）：
  1) 召回层：期望条目必须出现在【采集结果】里；
  2) 排序层：期望条目在【自定义排序】后的名次 ≤ top_n。

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_recall.py
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_recall.py --case kirakira
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_recall.py --canary

退出码：全过 0，否则 1；canary 不通 2。本次原始结果写入 tests/last_run.json。
注意：网易云接口有 IP 限流，连续猛跑会返回空结果——用例之间默认留 3 秒。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import pipeline
from usable_search.rank import dedupe, rank
from usable_search.store import Index

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_PATH = os.path.join(HERE, "recall_cases.json")
RUN_DIR = os.path.join(HERE, ".run")
LAST_RUN = os.path.join(HERE, "last_run.json")


def canary() -> bool:
    """搜索接口连通性探针（限流时会返回空结果）。"""
    from usable_search import netease
    try:
        return len(netease.search_songs("キラキラだとか夢だとか", pages=1)) > 0
    except Exception:  # noqa: BLE001
        return False


def run_case(case: dict) -> dict:
    q = case["query"]
    pages = int(case.get("pages", 3))
    top_n = int(case.get("top_n", 3))
    t0 = time.time()

    os.makedirs(RUN_DIR, exist_ok=True)
    db = os.path.join(RUN_DIR, f"{case['id']}.sqlite3")
    if os.path.exists(db):
        os.remove(db)

    idx = Index(db)
    records, stats = pipeline.collect_records(q, pages=pages)
    if stats["search"] == 0:  # 空结果=被限流的典型信号，退一步再试一次
        time.sleep(30)
        records, stats = pipeline.collect_records(q, pages=pages)
    collected = {r.get("id") for r in records}
    idx.upsert_songs(records)

    # 还原「我的歌单/收藏」标记（数据在 data/account/mine_ids.json，机器本地、不入库）
    mine_file = os.path.join(ROOT, "data", "account", "mine_ids.json")
    if os.path.exists(mine_file):
        try:
            with open(mine_file, encoding="utf-8") as f:
                idx.mark_mine(json.load(f))
        except Exception as exc:  # noqa: BLE001
            print("[warn] mine 标记失败（继续跑）:", exc, file=sys.stderr)

    rows = idx.search(q)
    ranked = rank(dedupe(rows), q, prefer_artists=stats.get("prefer_artists"))
    rank_pos = {rec["id"]: i + 1 for i, (_, _, rec) in enumerate(ranked)}

    items = []
    for e in case["expect"]:
        eid = e["id"]
        items.append({
            "id": eid,
            "label": e.get("name", ""),
            "collected": eid in collected,
            "rank": rank_pos.get(eid),
        })
    ok = all(it["collected"] and it["rank"] is not None and it["rank"] <= top_n for it in items)
    return {
        "id": case["id"], "query": q, "ok": ok,
        "seconds": round(time.time() - t0, 1),
        "collected_total": len(collected),
        "index_total": idx.count(),
        "stats": stats,
        "expect": items,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="召回回归测试（M1 验收）")
    ap.add_argument("--case", help="只跑某条用例 id")
    ap.add_argument("--canary", action="store_true", help="只做连通性探针")
    ap.add_argument("--gap", type=float, default=3.0, help="用例之间的等待秒数")
    args = ap.parse_args()

    if args.canary:
        ok = canary()
        print("[canary]", "OK——搜索接口有响应" if ok else "空结果——疑似限流，稍后再试")
        return 0 if ok else 2

    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print("没有这条用例:", args.case)
            return 1

    print(f"== 召回回归（{len(cases)} 条用例）==")
    results, all_ok = [], True
    for i, c in enumerate(cases):
        r = run_case(c)
        results.append(r)
        all_ok = all_ok and r["ok"]
        flag = "PASS" if r["ok"] else "FAIL"
        print(f"\n[{r['id']}] {r['query']} —— {flag}（{r['seconds']}s，采集 {r['collected_total']} 条）")
        for it in r["expect"]:
            rk = f"#{it['rank']}" if it["rank"] else "未入榜"
            cc = "采集✓" if it["collected"] else "采集✗"
            print(f"    · {it['id']} {cc} 排序{rk} | {it['label']}")
        if i != len(cases) - 1:
            time.sleep(args.gap)

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    print(f"\n== 汇总: {passed}/{total} PASS ==")
    try:
        with open(LAST_RUN, "w", encoding="utf-8") as f:
            json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results},
                      f, ensure_ascii=False, indent=2)
        print("原始结果:", LAST_RUN)
    except Exception as e:  # noqa: BLE001
        print("写 last_run.json 失败:", e)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
