"""采集管线（多入口聚合）：搜索扫穿 + 专辑展开 + 歌手热歌。

供 CLI 与回归测试共用；后续入口（歌词/歌单）在此扩展。
设计原则：宁可多收（召回优先），排序在 rank 层再做过滤。
"""
from __future__ import annotations

import logging

from . import netease

_log = logging.getLogger(__name__)


def collect_records(q: str, pages: int = 3, album_cap: int = 3, artist_cap: int = 4) -> tuple[list, dict]:
    """按关键词多入口采集，返回 (records, stats)。

    入口：
      ① 单曲搜索分页扫穿（官方把很多条目埋在很后面，必须扫到底）；
      ② 标题含关键词的专辑 → 全曲目；
      ③ 前缀命中条目里出现的艺人（按出现顺序去重）→ 歌手页热歌。
    """
    stats = {"search": 0, "albums": [], "artists": []}

    fresh = netease.search_songs(q, pages=pages)
    stats["search"] = len(fresh)

    album_extra: list = []
    albums = netease.search_albums(q)
    picked = [a for a in albums if q.lower() in (a.get("name") or "").lower()][:album_cap]
    for a in picked:
        try:
            songs = netease.album_songs(a.get("id"))
        except Exception as exc:  # noqa: BLE001 —— 单张专辑失败不该拖垮整次采集
            _log.warning("专辑展开失败 id=%s: %r", a.get("id"), exc)
            continue
        album_extra.extend(songs)
        stats["albums"].append({"name": a.get("name") or "", "count": len(songs)})

    # 原唱线索：同名精确命中里出现最多的艺人（比"取第一张专辑的艺人"稳——专辑排序会把同人翻唱顶到最前）
    prefer_artists: list = []
    exact_same = [s for s in fresh if (s.get("name") or "").strip().lower() == q.strip().lower()]
    if exact_same:
        freq: dict[str, int] = {}
        first_seen: dict[str, int] = {}
        for i, s in enumerate(exact_same):
            for a in (s.get("artists") or s.get("ar") or []):
                nm = a.get("name") or ""
                if not nm:
                    continue
                freq[nm] = freq.get(nm, 0) + 1
                first_seen.setdefault(nm, i)
        prefer_artists = sorted(freq, key=lambda n: (-freq[n], first_seen[n]))[:2]
    if not prefer_artists:  # 没有同名精确命中（如前缀搜索）→ 退回同名专辑的艺人
        for a in picked[:1]:
            art = a.get("artist")
            nm = art.get("name") if isinstance(art, dict) else (art if isinstance(art, str) else "")
            if nm:
                prefer_artists.append(nm)
    stats["prefer_artists"] = prefer_artists

    artist_extra: list = []
    order: list = []
    for s in fresh:
        nm = s.get("name") or ""
        if not nm.startswith(q):
            continue
        for a in (s.get("artists") or s.get("ar") or []):
            name = a.get("name") or ""
            if name and name not in order:
                order.append(name)
        if len(order) >= artist_cap * 2:
            break
    for name in order[:artist_cap]:
        hits = netease.search_artists(name)
        exact = [h for h in hits if (h.get("name") or "") == name]
        if not exact:
            continue
        try:
            songs = netease.artist_all_songs(exact[0].get("id"))
        except Exception as exc:  # noqa: BLE001 —— 单个歌手失败不拖垮整次采集
            _log.warning("歌手全曲失败 %s: %r", name, exc)
            continue
        artist_extra.extend(songs)
        stats["artists"].append({"name": name, "count": len(songs)})

    records = ([netease.norm_song(s, "search") for s in fresh]
               + [netease.norm_song(s, "album") for s in album_extra]
               + [netease.norm_song(s, "artist") for s in artist_extra])
    return records, stats
