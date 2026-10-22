#!/usr/bin/env python
"""网易云搜索探针（usable-search-engine 数据通道侦察工具）.

复现「搜索埋原唱」现象：对关键词做分页扫穿，输出全部命中位置，
用来证明"多入口聚合 + 分页到底"的必要性。

用法:
    python tools/probe_netease_search.py "キラキラだとか夢だとか" [艺人过滤（默认 Poppin'Party）]

注意: 页面间有 3 秒间隔，礼貌访问，不要加速跑。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://music.163.com/",
}


def search_page(query: str, offset: int = 0, limit: int = 100) -> dict:
    """调一次网易云单曲搜索接口，返回解析后的 JSON。"""
    url = ("https://music.163.com/api/search/get/web?csrf_token=&s="
           + urllib.parse.quote(query) + f"&type=1&offset={offset}&total=true&limit={limit}")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "キラキラだとか夢だとか"
    artist_filter = sys.argv[2] if len(sys.argv) > 2 else "Poppin'Party"

    first = search_page(query)
    result = first.get("result") or {}
    songs = result.get("songs") or []
    print(f"# 关键词: {query}")
    print(f"# songCount: {result.get('songCount')}  首页返回: {len(songs)}")
    print("# 前 10 条（用户实际看到的第一屏）:")
    for i, s in enumerate(songs[:10], 1):
        artists = "、".join(a.get("name", "") for a in s.get("artists", []))
        print(f"  {i:>3}. {s.get('name', '')[:60]} | {artists}")

    print(f"\n# 分页扫穿（找含「{artist_filter}」的条目）:")
    for offset in range(0, 400, 100):
        if offset:
            time.sleep(3)
        page = search_page(query, offset=offset)
        batch = (page.get("result") or {}).get("songs") or []
        for i, s in enumerate(batch):
            artists = " ".join(a.get("name", "") for a in s.get("artists", []))
            if artist_filter in artists:
                print(f"  #{offset + i + 1:>4} | {s.get('name', '')[:60]} | {artists[:44]}")
        if len(batch) < 100:
            break


if __name__ == "__main__":
    main()
