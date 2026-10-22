"""B15 档案 / 下架侦测回归（可回归的验收）。

覆盖：
  - 档案写入/统计/查询（本地库，零网络）
  - 稿件状态探针：真实 bvid → ok；假 bvid → 非 ok（error/gone）
  - 复查流程：写入 → recheck → 状态落库、checked 计数正确
  - 状态优先级：gone/hidden 排在最前

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_archive.py
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_archive.py --case live
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

from usable_search import archive, bilibili

RUN_DIR = os.path.join(ROOT, "tests", ".run")
DB = os.path.join(RUN_DIR, "archive_test.sqlite3")


def _fresh_db() -> str:
    os.makedirs(RUN_DIR, exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    return DB


def case_roundtrip() -> tuple[bool, dict]:
    """写入 → 统计 → 查询，全链无网络。"""
    db = _fresh_db()
    vids = [
        {"bvid": "BVtest0000001", "title": "测试视频·春日影", "author": "测试UP",
         "mid": 1, "cover": "", "pubdate": 1600000000, "play": 123, "danmaku": 4},
        {"bvid": "BVtest0000002", "title": "另一个视频", "author": "测试UP2",
         "mid": 2, "cover": "", "pubdate": 1600000001, "play": 9, "danmaku": 0},
    ]
    n = archive.remember(vids, db)
    st = archive.stats(db)
    rows = archive.query(q="春日影", db_path=db, limit=10)
    n2 = archive.remember(vids, db)  # 重复写入应当幂等
    st2 = archive.stats(db)
    ok = (n == 2 and st["total"] == 2 and st["unknown"] == 2 and len(rows) == 1
          and rows[0]["bvid"] == "BVtest0000001" and n2 == 2 and st2["total"] == 2)
    return ok, {"written": n, "stats": st, "hit": rows[0]["title"] if rows else None}


def _first_bvid() -> str | None:
    d = bilibili.search_videos_meta("MyGO", page=1, page_size=50)
    items = d.get("result") or []
    return items[0].get("bvid") if items else None


def _gone_bvid(bvid: str) -> str:
    """拿真实 bvid 改末位字符，构造一个「格式合法但不存在」的 bvid（-404）。"""
    return bvid[:-1] + ("X" if bvid[-1] != "X" else "Y")


def case_status_live() -> tuple[bool, dict]:
    """真实 bvid 必须是 ok；改末位字符得到的不存在 bvid 必须是 gone(-404)。"""
    bvid = _first_bvid()
    if not bvid:
        return False, {"error": "B站搜索无结果（疑似风控）"}
    st = bilibili.video_status(bvid)
    fk = _gone_bvid(bvid)
    gone = bilibili.video_status(fk)
    ok = st.get("status") == "ok" and gone.get("status") == "gone"
    return ok, {"bvid": bvid, "status": st.get("status"),
                "fake_bvid": fk, "fake_status": gone.get("status"),
                "fake_code": gone.get("code")}


def case_recheck() -> tuple[bool, dict]:
    """复查：真实 bvid → ok；伪造的不存在 bvid → gone，状态要落库。"""
    db = _fresh_db()
    d = bilibili.search_videos_meta("MyGO", page=1, page_size=50)
    items = d.get("result") or []
    if not items:
        return False, {"error": "搜索无结果"}
    real = items[0].get("bvid")
    fk = _gone_bvid(real)
    archive.remember([{"bvid": real, "title": items[0].get("title") or "", "author": "x"}], db)
    archive.remember([{"bvid": fk, "title": "下架测试", "author": "x"}], db)
    out = archive.recheck(limit=2, db_path=db)
    rows = {r["bvid"]: r for r in archive.query(db_path=db, limit=10)}
    if rows.get(fk, {}).get("status") != "gone":   # 风控（-412）会把状态打成 error，隔一会儿重试一次
        time.sleep(2.0)
        out = archive.recheck(limit=2, db_path=db)
        rows = {r["bvid"]: r for r in archive.query(db_path=db, limit=10)}
    ok = (out.get("checked") == 2
          and rows.get(real, {}).get("status") == "ok"
          and rows.get(fk, {}).get("status") == "gone"
          and out["stats"]["total"] == 2
          and any(x["bvid"] == fk for x in out["items"]))
    return ok, {"checked": out.get("checked"),
                "real": rows.get(real, {}).get("status"),
                "fake": rows.get(fk, {}).get("status"),
                "flagged": [x["bvid"] for x in out["items"]]}


CASES = [
    ("roundtrip", "档案写入/统计/查询", case_roundtrip),
    ("status", "稿件状态探针（真实+造假）", case_status_live),
    ("recheck", "复查落库", case_recheck),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="B15 档案/下架侦测回归")
    ap.add_argument("--case", help="只跑某条用例 id")
    ap.add_argument("--gap", type=float, default=1.5, help="用例之间的等待秒数（防风控）")
    args = ap.parse_args()

    cases = [c for c in CASES if (not args.case or c[0] == args.case)]
    if not cases:
        print("没有这条用例:", args.case)
        return 1

    print(f"== 档案/下架侦测回归（{len(cases)} 条）==")
    all_ok = True
    passed = 0
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
        if cid != cases[-1][0]:
            time.sleep(args.gap)
    print(f"\n== 汇总: {passed}/{len(cases)} PASS ==")
    try:
        os.remove(DB)
    except OSError:
        pass
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
