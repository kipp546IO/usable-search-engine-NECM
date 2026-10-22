"""档案复查脚本：按「最久未复查」抽样验活，默认 dry-run（只读，不写库）。

用法（仓库根目录）：
    python tools/recheck_archive.py --limit 50 --interval 1.0
    python tools/recheck_archive.py --limit 50 --interval 1.0 --apply   # 写回 status/checked_at/code
    python tools/recheck_archive.py --order last_seen --limit 50        # 换取样键：找长期无人再见的旧档

口径：
- 默认 dry-run：用 `mode=ro` 只读打开 archive 库，不建表、不写回；只有显式 `--apply` 才 UPDATE。
- 逐条调用 `bilibili.video_status`（`x/web-interface/view`），条间 `--interval` 秒节流（默认 1.0）。
- 取样键 `--order checked`（默认，最久未复查）/ `last_seen`（最久没被再搜到）/ `first_seen`（最早建档）。
- 输出：状态分布（ok/gone/hidden/error）、错误码直方图、耗时 min/p50/p95/max、状态变化清单。
- 不改主流程：不经 server，也不调 `archive.recheck()`（那个函数固定 0.8s 且无条件写回）。

退出码：0 = 跑完（含个别 error）；1 = 部分失败但已跑完且 error 过半；2 = 参数/库不可读。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import bilibili  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "data", "archive.sqlite3")
ORDERS = {
    "checked": "checked_at ASC, last_seen DESC",
    "last_seen": "last_seen ASC, checked_at ASC",
    "first_seen": "first_seen ASC, checked_at ASC",
}


def _ro_uri(path: str) -> str:
    """sqlite 只读 URI：不依赖 cwd，路径里的空格/中文交给 as_uri 转义。"""
    return "file:" + urllib.parse.quote(os.path.abspath(path).replace(os.sep, "/")) + "?mode=ro"


def _connect(path: str, write: bool) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if write:
        return sqlite3.connect(path, timeout=10)
    return sqlite3.connect(_ro_uri(path), uri=True, timeout=10)


def pick(conn: sqlite3.Connection, limit: int, order: str, status: str, q: str) -> list[tuple]:
    sql = "SELECT bvid, title, status, code, checked_at, last_seen FROM seen"
    wheres, args = [], []
    if status:
        wheres.append("status = ?")
        args.append(status)
    if q:
        wheres.append("(title LIKE ? OR author LIKE ? OR bvid = ?)")
        args += [f"%{q}%", f"%{q}%", q]
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY " + ORDERS[order] + " LIMIT ?"
    args.append(limit)
    return list(conn.execute(sql, args))


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def main(argv: list[str] | None = None) -> int:
    try:
        _reconf = getattr(sys.stdout, "reconfigure", None)
        if _reconf:
            _reconf(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="档案复查（默认 dry-run，只读）")
    ap.add_argument("--db", default=DEFAULT_DB, help="archive 库路径（默认 data/archive.sqlite3）")
    ap.add_argument("--limit", type=int, default=50, help="抽样条数（默认 50，上限 500）")
    ap.add_argument("--interval", type=float, default=1.0, help="条间节流秒数（默认 1.0）")
    ap.add_argument("--order", choices=sorted(ORDERS), default="checked", help="取样键")
    ap.add_argument("--status", default="", help="只看某状态（ok/unknown/gone/hidden/error）")
    ap.add_argument("--q", default="", help="按标题/UP/bvid 过滤")
    ap.add_argument("--apply", action="store_true", help="写回 status/checked_at/code（默认不写）")
    ap.add_argument("--apply-db", default="", help="配合 --apply：写到另一个库（先副本后切，不动原库）")
    ap.add_argument("--out", default="", help="把汇总写成 JSON（可选；含标题，别落进仓库）")
    args = ap.parse_args(argv)

    limit = max(1, min(500, int(args.limit)))
    interval = max(0.0, float(args.interval))
    target = args.apply_db or args.db
    try:
        conn = _connect(target, write=args.apply)
        rows = pick(conn, limit, args.order, args.status, args.q)
    except Exception as exc:  # noqa: BLE001
        print(f"[fatal] 库打不开或抽样失败：{exc!r}")
        return 2
    if not rows:
        print(f"抽样 0 条（db={target} order={args.order} status={args.status!r} q={args.q!r}），无事可做")
        return 0

    print(f"db={target} mode={'rw' if args.apply else 'ro(dry-run)'} order={args.order} "
          f"limit={limit} interval={interval:.2f}s picked={len(rows)}")
    started = time.time()
    dist = collections.Counter()
    codes = collections.Counter()
    durations: list[float] = []
    changes: list[dict] = []
    first_ok = 0
    ok_title_drift = 0
    failures = 0
    error_items: list[dict] = []

    for i, (bvid, title, old_status, old_code, _checked_at, _last_seen) in enumerate(rows, 1):
        t0 = time.perf_counter()
        try:
            st = bilibili.video_status(bvid)
        except Exception as exc:  # noqa: BLE001
            st = {"bvid": bvid, "status": "error", "code": -1, "message": repr(exc)[:120]}
            failures += 1
        durations.append((time.perf_counter() - t0) * 1000.0)
        status = st.get("status") or "error"
        code = int(st.get("code") or 0)
        dist[status] += 1
        codes[code] += 1
        if status == "ok":
            if st.get("title") and st["title"] != title:
                ok_title_drift += 1
            if old_status == "unknown":
                first_ok += 1
        # 「变化」只记值得看的：新发现下架/不可见，或已判过的状态翻转；unknown→ok 是首次建档，另计
        if status in ("gone", "hidden") or (old_status != "unknown" and status != old_status):
            changes.append({"bvid": bvid, "title": title, "was": old_status, "now": status,
                            "code": code, "message": st.get("message") or ""})
        if status == "error":
            error_items.append({"bvid": bvid, "title": title, "code": code,
                                "message": st.get("message") or ""})
        if args.apply:
            conn.execute("UPDATE seen SET status=?, checked_at=?, code=? WHERE bvid=?",
                         (status, int(time.time()), code, bvid))
        if i < len(rows):
            time.sleep(interval)
    if args.apply:
        conn.commit()
    conn.close()
    elapsed = time.time() - started

    print(f"checked={len(rows)} elapsed={elapsed:.1f}s")
    print("分布: " + " ".join(f"{k}={v}" for k, v in sorted(dist.items())))
    print("错误码: " + " ".join(f"{k}={v}" for k, v in sorted(codes.items())))
    print(f"耗时ms: min={min(durations):.1f} p50={pct(durations, 0.50):.1f} "
          f"p95={pct(durations, 0.95):.1f} max={max(durations):.1f} "
          f"mean={statistics.fmean(durations):.1f}")
    print(f"状态变化: {len(changes)} 条" + ("（dry-run 未写回）" if not args.apply else ""))
    for c in changes[:20]:
        msg = f" {c['message'][:40]!r}" if c.get("message") else ""
        print(f"  {c['bvid']} {c['was']} -> {c['now']} code={c['code']}{msg} | {c['title'][:60]}")
    print(f"首次建档(unknown->ok): {first_ok}")
    print(f"错误明细: {len(error_items)} 条")
    for e in error_items[:10]:
        msg = f" {e['message'][:40]!r}" if e.get("message") else ""
        print(f"  {e['bvid']} code={e['code']}{msg} | {e['title'][:60]}")
    print(f"标题漂移(ok 且与库内标题不同): {ok_title_drift}")
    print(f"请求异常: {failures}")

    summary = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": os.path.abspath(target), "apply": bool(args.apply), "order": args.order,
        "limit": len(rows), "interval": interval, "elapsed_s": round(elapsed, 2),
        "distribution": dict(sorted(dist.items())), "codes": {str(k): v for k, v in sorted(codes.items())},
        "ms": {"min": round(min(durations), 1), "p50": round(pct(durations, 0.5), 1),
               "p95": round(pct(durations, 0.95), 1), "max": round(max(durations), 1)},
        "changes": changes, "first_ok": first_ok, "errors": error_items,
        "title_drift": ok_title_drift, "request_errors": failures,
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        print(f"summary -> {args.out}")

    errors = dist.get("error", 0)
    if len(rows) >= 5 and errors * 2 > len(rows):
        print(f"结论: error {errors}/{len(rows)} 过半，接口可能被风控，先别 --apply")
        return 1
    print("结论: 跑完" + ("，已写回" if args.apply else "，dry-run 未写库"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
