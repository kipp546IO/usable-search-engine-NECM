"""网易云时长过滤回归（N5 第一步）。

覆盖：
  - `_parse_dur` 的歌曲档（分钟）与自定义口径
  - 实测：`dur=4-6` 返回的每一首时长都在 240~360 秒
用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_netease_dur.py
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search.server import _parse_dur, run_netease_songs

RUN_DIR = os.path.join(ROOT, "tests", ".run")
DB = os.path.join(RUN_DIR, "netease_dur.sqlite3")


def case_parse() -> tuple[bool, dict]:
    """解析口径：分档是分钟、返回秒；自定义支持只填一侧；空串=不过滤。"""
    ok = (_parse_dur("4-6") == (240, 360)
          and _parse_dur("0-2") == (0, 120)
          and _parse_dur("6-") == (360, None)
          and _parse_dur("custom:2,4") == (120, 240)
          and _parse_dur("custom:,4") == (None, 240)
          and _parse_dur("") is None)
    return ok, {"4-6": _parse_dur("4-6"), "custom:2,4": _parse_dur("custom:2,4")}


def case_live() -> tuple[bool, dict]:
    """实测 4~6 分钟档：每一首时长必须落在 [240, 360] 秒。"""
    os.makedirs(RUN_DIR, exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    d = run_netease_songs("春日影", DB, dur="4-6")
    items = d.get("items") or []
    secs = [round((it.get("duration") or 0) / 1000) for it in items]
    ok = bool(items) and all(240 <= s <= 360 for s in secs) and len(items) <= 30
    return ok, {"n": len(items), "secs": secs[:8], "hit": items[0]["title"] if items else None}


CASES = [
    ("parse", "时长解析口径", case_parse),
    ("live", "4~6 分钟档实测", case_live),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="网易云时长过滤回归")
    ap.add_argument("--case", help="只跑某条用例 id")
    args = ap.parse_args()
    cases = [c for c in CASES if (not args.case or c[0] == args.case)]
    if not cases:
        print("没有这条用例:", args.case)
        return 1

    print(f"== 网易云时长过滤回归（{len(cases)} 条）==")
    passed, all_ok = 0, True
    for cid, label, fn in cases:
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, {"error": repr(exc)}
        all_ok = all_ok and ok
        passed += 1 if ok else 0
        print(f"[{cid}] {label} —— {'PASS' if ok else 'FAIL'}（{round(time.time()-t0,1)}s）"
              f"  {json.dumps(detail, ensure_ascii=False)}")
    print(f"\n== 汇总: {passed}/{len(cases)} PASS ==")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
