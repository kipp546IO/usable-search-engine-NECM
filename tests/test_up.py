"""B8 UP主 / 合集维度回归。

覆盖：
  - UP主投稿列表（WBI + buvid Cookie）：条数、字段完整性
  - 合集/系列列表：指定 UP 的合集能列出，含「刺团在伊拉克的生活」
  - 合集选集：9 集全出、含目标视频、时长/日期有效

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_up.py
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_up.py --case seasons
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT + "/src")

from usable_search import bilibili
from usable_search.bilibili import RiskControlError

MID = 3493140828785159      # 川崎抹香鲸
SEASON = 7266495            # 刺团在伊拉克的生活
TARGET = "BV1ZJPQzNEnr"     # 伊拉克486（但是劳乡版）


def case_videos() -> tuple[bool | None, dict]:
    """UP主投稿；风控时返回 None（SKIP），不算失败。"""
    try:
        d = bilibili.user_videos(MID, page=1, page_size=30)
    except RiskControlError as exc:
        return None, {"skip": "B站风控", "detail": str(exc)[:90]}
    items = d.get("items") or []
    ok = (len(items) >= 10
          and all(it.get("bvid") and it.get("title") for it in items)
          and all(it.get("pubdate", 0) > 0 for it in items[:5]))
    return ok, {"n": len(items), "total": d.get("total"),
                "first": items[0]["title"][:30] if items else None}


def case_seasons() -> tuple[bool, dict]:
    d = bilibili.user_seasons(MID)
    seasons = d.get("seasons") or []
    hit = next((s for s in seasons if int(s.get("season_id") or 0) == SEASON), None)
    ok = bool(seasons) and hit is not None and int(hit.get("total") or 0) == 9
    return ok, {"n": len(seasons), "hit": (hit or {}).get("title"),
                "total": (hit or {}).get("total")}


def case_archives() -> tuple[bool, dict]:
    d = bilibili.season_archives(MID, SEASON, page_size=50)
    items = d.get("items") or []
    bvids = {it.get("bvid") for it in items}
    ok = (len(items) == 9 and TARGET in bvids
          and all(it.get("duration_sec", 0) > 0 for it in items)
          and all(it.get("pubdate", 0) > 0 for it in items))
    return ok, {"n": len(items), "has_target": TARGET in bvids,
                "title": d.get("title")}


CASES = [
    ("videos", "UP主投稿列表", case_videos),
    ("seasons", "合集/系列列表", case_seasons),
    ("archives", "合集选集", case_archives),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="B8 UP主/合集回归")
    ap.add_argument("--case", help="只跑某条用例 id")
    ap.add_argument("--gap", type=float, default=1.5, help="用例之间的等待秒数（防风控）")
    args = ap.parse_args()

    cases = [c for c in CASES if (not args.case or c[0] == args.case)]
    if not cases:
        print("没有这条用例:", args.case)
        return 1

    print(f"== UP主/合集回归（{len(cases)} 条）==")
    passed, skipped, failed = 0, 0, 0
    for cid, label, fn in cases:
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, {"error": repr(exc)}
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        passed += 1 if ok is True else 0
        skipped += 1 if ok is None else 0
        failed += 1 if ok is False else 0
        print(f"[{cid}] {label} —— {status}（{round(time.time()-t0,1)}s）"
              f"  {json.dumps(detail, ensure_ascii=False)}")
        if cid != cases[-1][0]:
            time.sleep(args.gap)
    print(f"\n== 汇总: {passed}/{len(cases)} PASS · SKIP {skipped} · FAIL {failed} ==")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
