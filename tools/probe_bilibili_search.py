"""B站搜索探针：验证 WBI 签名搜索可用 + 看看返回长什么样。

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tools/probe_bilibili_search.py "关键词"
"""
from __future__ import annotations

import sys

from usable_search import bilibili


def main() -> None:
    q = sys.argv[1] if len(sys.argv) > 1 else "キラキラだとか夢だとか"
    items = bilibili.search_videos(q, page=1)
    videos = [it for it in items if it.get("bvid")]
    print(f"[bilibili] 搜「{q}」→ 原始 {len(items)} 条 / 视频 {len(videos)} 条")
    if items:
        print("首条字段:", sorted(items[0].keys())[:24])
    print()
    for i, it in enumerate(videos[:12], 1):
        v = bilibili.norm_video(it)
        play = v["play"] if isinstance(v["play"], str) else f"{v['play']:,}"
        print(f"{i:>2}. [play={play}|{v['duration']}] {v['title'][:58]}")
        print(f"     UP: {v['author'][:26]} | {v['url']}")


if __name__ == "__main__":
    main()
