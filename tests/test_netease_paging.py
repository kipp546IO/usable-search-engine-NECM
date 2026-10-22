"""网易云歌名结果分页回归。

不访问外网：用固定候选验证后端分页口径、页间不重叠和页大小。
用法（仓库根目录）：
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_netease_paging.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import pipeline
from usable_search import server

RUN_DIR = os.path.join(ROOT, "tests", ".run")
DB = os.path.join(RUN_DIR, f"netease_paging_{os.getpid()}.sqlite3")


def _records(n: int = 120) -> list[dict]:
    return [
        {
            "source": "netease",
            "id": i,
            "title": f"分页目标 {i:03d}",
            "artists": "测试艺人",
            "album": "测试专辑",
            "album_id": 1,
            "cover": "https://example.invalid/cover.jpg",
            "duration": 180000,
            "fee": 0,
            "origin": "search",
        }
        for i in range(1, n + 1)
    ]


def main() -> int:
    os.makedirs(RUN_DIR, exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)

    old_collect = pipeline.collect_records
    old_attach = server._attach_lyric_lines
    calls = []

    def fake_collect(q: str, pages: int = 3):
        calls.append(pages)
        return _records(), {"search": 120, "albums": [], "artists": [], "prefer_artists": []}

    pipeline.collect_records = fake_collect
    server._attach_lyric_lines = lambda *args, **kwargs: None
    try:
        first = server.run_netease_songs("分页目标", DB, page=1, pagesize=30)
        second = server.run_netease_songs("分页目标", DB, page=2, pagesize=30)
        wide = server.run_netease_songs("分页目标", DB, page=2, pagesize=50)
        malformed = server.run_netease_songs("分页目标", DB, page="abc", pagesize="40")
        external = server.Index(DB)
        try:
            external.upsert_songs([_records(121)[-1]])
        finally:
            external.conn.close()
        refreshed = server.run_netease_songs("分页目标", DB, page=5, pagesize=30)
    finally:
        pipeline.collect_records = old_collect
        server._attach_lyric_lines = old_attach
        server._NETEASE_CACHE.clear()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(DB + suffix)
            except FileNotFoundError:
                pass

    first_ids = [it["id"] for it in first["items"]]
    second_ids = [it["id"] for it in second["items"]]
    wide_ids = [it["id"] for it in wide["items"]]
    ok = (
        first.get("page") == 1
        and second.get("page") == 2
        and first.get("pagesize") == 30
        and second.get("pagesize") == 30
        and len(first_ids) == 30
        and len(second_ids) == 30
        and not set(first_ids) & set(second_ids)
        and first_ids == list(range(1, 31))
        and second_ids == list(range(31, 61))
        and wide.get("pagesize") == 50
        and wide_ids == list(range(51, 101))
        and malformed.get("page") == 1
        and malformed.get("pagesize") == 30
        and malformed.get("total") == 120
        and refreshed.get("total") == 121
        and refreshed.get("page") == 5
        and second.get("has_more") is True
        and calls == [2, 2]
    )
    print("[paging]", "PASS" if ok else "FAIL", {
        "first": first_ids[:3] + first_ids[-3:],
        "second": second_ids[:3] + second_ids[-3:],
        "wide": wide_ids[:3] + wide_ids[-3:],
        "calls": calls,
    })
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
