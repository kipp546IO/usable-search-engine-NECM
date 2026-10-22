"""统一搜索框（M3 原型 v0.3）：分平台精确搜索（在前）＋ 聚合（殿后）。

- 网易云：歌名（智能）／歌词／专辑／歌手／歌单／播客
- B站：综合／名称／简介／标签／UP主／UID × 时长/发布时间过滤 × 排序 × 翻页
- 聚合：网易云歌名 ＋ B站综合（并列两栏）

零依赖（标准库 http.server）。用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python -m usable_search serve
然后浏览器打开 http://127.0.0.1:18788/
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import archive, backup, bilibili, fts_gate, fuzzy, governance, netease, pipeline, probe
from .rank import dedupe, rank
from .store import Index

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB_DIR = os.path.join(ROOT, "web")
_NETEASE_LOCK = threading.Lock()  # 串行化网易云采集段，避免并发写库
_NETEASE_CACHE: dict = {}
_NETEASE_CACHE_TTL = 600  # 分页在短时间内复用同一份排序池，避免每页重复抓取
# 音质档位：jymaster=超清母带 > hires=高清臻音 > lossless=无损 > exhigh=极高 > higher=较高 > standard=标准
_QUALITY_NAME = {"jymaster": "超清母带", "hires": "高清臻音", "lossless": "无损",
                 "exhigh": "极高", "higher": "较高", "standard": "标准", "none": "未知"}
_QUALITY_RANK = {"jymaster": 6, "hires": 5, "lossless": 4, "exhigh": 3, "higher": 2, "standard": 1, "none": 0}
_PRIV_CACHE: dict = {}          # song_id -> (level, fee, st, ts)
_PRIV_TTL = 24 * 3600           # 版权/音质一天内基本不变
_log = logging.getLogger(__name__)
_ARCHIVE_DB = os.path.join(ROOT, "data", "archive.sqlite3")  # B15 下架侦测档案
_PROBE_DB = probe.PROBE_DB  # 接口探针库：与埋点共用同一个常量，避免读写两处
_BACKUP_DB = os.path.join(ROOT, "data", "backup.sqlite3")  # 失效视频备份站查询缓存
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")  # BV 号形如 BV17x411w7KC


def _db_stamp(path: str):
    """本地索引指纹；外部入库后让分页缓存自动失效。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# ---------- 歌词小工具 ----------

_LRC_TAG = re.compile(r"\[[^\]]*\]")


def lrc_lines(lrc: str) -> list:
    out = []
    for raw in (lrc or "").splitlines():
        s = _LRC_TAG.sub("", raw).strip()
        if s:
            out.append(s)
    return out


def match_lyric(lrc: str, q: str):
    """在歌词里找包含关键词的一行；返回原行文本或 None。"""
    ql = q.lower()
    ql2 = ql.replace(" ", "")
    for line in lrc_lines(lrc):
        ll = line.lower()
        if ql in ll or (ql2 and ql2 in ll.replace(" ", "")):
            return line
    return None


# ---------- 网易云 ----------

def _fill_covers(idx: Index, items: list) -> None:
    """对缺封面的条目批量补封面（一次 song/detail），并回写库。"""
    missing = [it["id"] for it in items if not it.get("cover")][:60]
    if not missing:
        return
    try:
        details = netease.songs_detail(missing)
    except Exception:  # noqa: BLE001
        return
    cmap = {}
    for s in details:
        al = s.get("al") or s.get("album") or {}
        pic = al.get("picUrl") or ""
        if s.get("id") and pic:
            cmap[s["id"]] = pic
    for it in items:
        if not it.get("cover") and cmap.get(it["id"]):
            it["cover"] = cmap[it["id"]]
            idx.set_cover(it["id"], it["cover"])


def _attach_lyric_lines(idx: Index, items: list, q: str, cap: int = 6, window: int = 15) -> None:
    """给歌曲卡补第三行歌词（命中关键词才给）。cap 限制每次新增抓取量；命中过的入缓存。"""
    fetched = 0
    for it in items[:window]:
        lrc = idx.get_lyric(it["id"])
        if lrc is None:
            if fetched >= cap:
                continue
            try:
                lrc, _t = netease.song_lyric(it["id"])
            except Exception:  # noqa: BLE001
                lrc = ""
            idx.set_lyric(it["id"], lrc)
            fetched += 1
        if lrc:
            hit = match_lyric(lrc, q)
            if hit:
                it["lyric_line"] = hit


def run_netease_songs(q: str, db_path: str, dur: str = "", pub: str = "",
                       sort: str = "", vip: str = "", orig: str = "",
                       page: int | str = 1, pagesize: int | str = 30) -> dict:
    """歌名（智能）：多入口采集 → 索引 → 自定义排序 → 封面/歌词补全。

    dur：时长筛选（分钟档，如 "2-4"；custom:2,4），在排序后、截断前过滤。
    pub：发行时间筛选（""=不限；"1"/"3"/"5"/"10"=近 N 年；"YYYY-YYYY"=年份区间），
         同样只在排序后、截断前过滤；publishTime 缺失/为 0 的条目在筛选开启时一律丢弃。
    sort：""=默认相关度排序；"quality"=按最高可用音质从高到低（同档保持原相关度顺序）。
    vip：""=不限；"only"=只看 VIP/付费；"not"=只看免费。
    orig：""=不限；"only"=只看原唱候选；"not"=排除原唱候选。
    page/pagesize：结果页码与每页数量；页大小沿用 B 站的 30/50/100/300。
    """
    sort = (sort or "").strip().lower()
    vip = (vip or "").strip().lower()
    orig = (orig or "").strip().lower()
    page = _int_arg(page, 1, 1, 200)
    pagesize = _int_arg(pagesize, 30, 30, 300)
    if pagesize not in (30, 50, 100, 300):
        pagesize = 30
    cache_key = (os.path.abspath(db_path), q, dur or "", pub or "", sort, vip, orig)
    with _NETEASE_LOCK:
        idx = fts_gate.open_index(db_path)
        try:
            cached = _NETEASE_CACHE.get(cache_key)
            if (cached and time.time() - cached["ts"] < _NETEASE_CACHE_TTL
                    and cached.get("db_stamp") == _db_stamp(db_path)):
                ranked_items = [dict(item) for item in cached["items"]]
                response_stats = dict(cached["stats"])
            else:
                records, stats = pipeline.collect_records(q, pages=2)
                # 发行时间靠原始结果里的 album.publishTime（毫秒）本地过滤，不再打接口
                pub_ms = {r["id"]: r.get("publish_time") or 0
                          for r in records if r.get("id")}
                idx.upsert_songs(records)
                rows = idx.search(q)
                fuzzy_used = False
                if not rows and fuzzy.enabled():
                    # 精确结果为空 → difflib 兜底扫本地候选（只读 SELECT，不碰 store.py）
                    rows = fuzzy.fallback(idx.conn, q)
                    fuzzy_used = bool(rows)
                if fuzzy_used:
                    # 兜底行已按相似度降序，直接用相似度当分数（不走标题匹配打分）
                    ranked = [(int(round((r.get("fuzzy_score") or 0) * 1000)), "模糊命中", r)
                              for r in rows]
                else:
                    ranked = rank(dedupe(rows), q, prefer_artists=stats.get("prefer_artists"))
                band = _parse_dur(dur) if dur else None
                pub_band = _pub_band(pub)
                priv = _privileges([r["id"] for _, _, r in ranked])
                ranked_items = []
                for score, kind, r in ranked:
                    ms = int(r.get("duration") or 0)      # 网易云 duration 单位是毫秒
                    secs = ms / 1000.0
                    if band:
                        lo, hi = band
                        if (lo is not None and secs < lo) or (hi is not None and secs > hi):
                            continue
                    # 排序已完成，这里过滤后再交给下面的分页截断
                    if not _pub_hit(pub_ms.get(r["id"]), pub_band):
                        continue
                    pv = priv.get(r["id"]) or {}
                    lvl = (pv.get("level") or "none").lower()
                    is_vip = int(pv.get("fee") or 0) != 0
                    if not _pass_mode(vip, is_vip):
                        continue
                    # 原唱要「标题没有版本标记」＋「接口没说这是改编/翻唱」两条同时成立，
                    # 单靠标题会把一堆同名翻唱也算成原唱（2026-10-22 实测踩过）
                    # originCoverType: 1=原创(rights clear)、2/3=改编/翻唱/其他版本、0=未标注。
                    # 只认 1：宁可漏标，也不能把别人的翻唱说成原唱（2026-10-22 实测踩过）
                    is_orig = (kind == "原版候选"
                               and int(pv.get("cover_type") or 0) == 1
                               and not (pv.get("orig_artists") or "").strip())
                    if not _pass_mode(orig, is_orig):
                        continue
                    ranked_items.append({
                        "id": r["id"], "title": r["title"], "artists": r["artists"],
                        "album": r["album"], "cover": r.get("cover") or "",
                        "duration": ms, "kind": kind, "score": score, "mine": bool(r.get("mine")),
                        "fuzzy": 1 if r.get("fuzzy") else 0,
                        "publish_time": pub_ms.get(r["id"]) or 0,   # 毫秒；未知为 0，前端据此决定显不显示
                        "quality": lvl, "quality_name": _QUALITY_NAME.get(lvl, "未知"),
                        "vip": 1 if is_vip else 0, "original": 1 if is_orig else 0,
                        "cover_type": int(pv.get("cover_type") or 0),
                        "orig_artists": pv.get("orig_artists") or "",
                        "guess_cover": 0, "guess_artist": "", "guess_orig": 0,
                        "url": f"https://music.163.com/song?id={r['id']}",
                    })
                # 同批推断：这一批里若有 coverType==1 的明确原唱，其余同名不同歌手的多半是翻唱。
                # 只做提示，不写死成"原唱"。
                canon = {}
                for it in ranked_items:
                    if int(it.get("cover_type") or 0) == 1 and not it.get("orig_artists"):
                        canon.setdefault(_norm_title(it["title"]), it["artists"])
                for it in ranked_items:
                    if it.get("original") or it.get("orig_artists"):
                        continue
                    if int(it.get("cover_type") or 0) in (2, 3):
                        continue
                    ref = canon.get(_norm_title(it["title"]))
                    if ref and ref != it["artists"]:
                        it["guess_cover"] = 1
                        it["guess_artist"] = ref
                    elif ref and ref == it["artists"]:
                        # 同批同名、同歌手，只是接口这条没标 → 基本就是同一首原唱
                        it["guess_orig"] = 1
                if sort == "quality":
                    # 稳定排序：同档音质保持原来的相关度顺序
                    ranked_items.sort(key=lambda it: -_QUALITY_RANK.get(it.get("quality") or "none", 0))
                response_stats = {
                    "search": stats.get("search"),
                    "albums": len(stats.get("albums") or []),
                    "artists": len(stats.get("artists") or []),
                    "prefer_artists": stats.get("prefer_artists"),
                    "fuzzy": fuzzy_used,
                    "pub": pub or "", "sort": sort, "vip": vip, "orig": orig,
                }
                _NETEASE_CACHE[cache_key] = {
                    "items": [dict(item) for item in ranked_items],
                    "stats": dict(response_stats),
                    "ts": time.time(),
                    "db_stamp": _db_stamp(db_path),
                }
                if len(_NETEASE_CACHE) > 20:
                    oldest_key = min(_NETEASE_CACHE, key=lambda k: _NETEASE_CACHE[k]["ts"])
                    _NETEASE_CACHE.pop(oldest_key, None)

            total = len(ranked_items)
            max_page = max(1, (total + pagesize - 1) // pagesize)
            page = min(page, max_page)
            start = (page - 1) * pagesize
            items = [dict(item) for item in ranked_items[start:start + pagesize]]
            _fill_covers(idx, items)
            _attach_lyric_lines(idx, items, q)
            cached_entry = _NETEASE_CACHE.get(cache_key)
            if cached_entry:
                cached_entry["db_stamp"] = _db_stamp(db_path)
        finally:
            idx.conn.close()
    return {"items": items, "stats": response_stats, "page": page, "pagesize": pagesize,
            "total": total, "has_more": start + pagesize < total}


def run_netease_lyric(q: str, db_path: str) -> dict:
    """歌词：返回歌词里包含关键词的歌 ＋ 命中行（前端高亮）。"""
    raw = netease.search_lyric(q, pages=1)
    with _NETEASE_LOCK:
        idx = fts_gate.open_index(db_path)
        try:
            recs, seen = [], set()
            for s in raw[:40]:
                rec = netease.norm_song(s, "search")
                if rec.get("id") and rec["id"] not in seen:
                    seen.add(rec["id"])
                    recs.append(rec)
            idx.upsert_songs(recs)
            mine_set = set()
            if recs:
                ids = [r["id"] for r in recs]
                qm = ",".join("?" * len(ids))
                mine_set = {row[0] for row in idx.conn.execute(
                    f"SELECT id FROM songs WHERE source='netease' AND mine=1 AND id IN ({qm})", ids)}  # nosec B608 —— 仅 "?" 占位符拼接，值全部参数化
            items = [{
                "id": rec["id"], "title": rec["title"], "artists": rec["artists"],
                "album": rec["album"], "cover": rec.get("cover") or "",
                "kind": "", "score": 0, "mine": rec["id"] in mine_set,
                "url": f"https://music.163.com/song?id={rec['id']}",
            } for rec in recs[:30]]
            _fill_covers(idx, items)
            _attach_lyric_lines(idx, items, q, cap=10, window=15)
            # 「歌词里亲眼验证过命中」的排前（更贴用户预期），其余保持网易云原始顺序
            items.sort(key=lambda it: (not it.get("lyric_line"),))
        finally:
            idx.conn.close()
    return {"items": items, "stats": {
        "lyric_hit": sum(1 for it in items if it.get("lyric_line")),
    }}


def run_netease_typed(q: str, kind: str) -> dict:
    """专辑 / 歌手 / 歌单 / 播客（直搜返回，不做索引）。"""
    if kind == "album":
        raw = netease.search_albums(q, pages=1)
        items = [{
            "id": a.get("id"), "name": a.get("name") or "",
            "cover": a.get("picUrl") or "",
            "sub": ((a.get("artist") or {}).get("name") or ""),
            "extra": f"{a.get('size') or a.get('songSize') or '?'} 首",
            "url": f"https://music.163.com/album?id={a.get('id')}",
        } for a in raw[:40]]
    elif kind == "artist":
        raw = netease.search_artists(q, pages=1)
        items = [{
            "id": a.get("id"), "name": a.get("name") or "",
            "cover": a.get("img1v1Url") or a.get("picUrl") or "",
            "sub": "",
            "extra": f"歌曲 {a.get('musicSize') or '?'} · 专辑 {a.get('albumSize') or '?'}",
            "url": f"https://music.163.com/artist?id={a.get('id')}",
        } for a in raw[:40]]
    elif kind == "playlist":
        raw = netease.search_playlists(q, pages=1)
        items = [{
            "id": p.get("id"), "name": p.get("name") or "",
            "cover": p.get("coverImgUrl") or "",
            "sub": (p.get("creator") or {}).get("nickname") or "",
            "extra": f"{p.get('trackCount') or '?'} 首",
            "url": f"https://music.163.com/playlist?id={p.get('id')}",
        } for p in raw[:40]]
    elif kind == "podcast":
        raw = netease.search_radios(q, pages=1)
        items = [{
            "id": d.get("id"), "name": d.get("name") or "",
            "cover": d.get("picUrl") or "",
            "sub": (d.get("dj") or {}).get("nickname") or "",
            "extra": f"{d.get('programCount') or '?'} 期",
            "url": f"https://music.163.com/djradio?id={d.get('id')}",
        } for d in raw[:40]]
    else:
        raise ValueError(f"未知类型: {kind}")
    return {"items": items, "stats": {"count": len(items)}}


# ---------- B站 ----------

_BILI_CACHE: dict = {}
_BILI_CACHE_TTL = 600  # 秒；同名搜索的抓取池缓存（翻页/切排序不重复抓）

_DUR_BUCKETS = {
    "10-15": (10 * 60, 15 * 60),
    "15-30": (15 * 60, 30 * 60),
    "30-60": (30 * 60, 60 * 60),
    "60-120": (60 * 60, 120 * 60),
    "120-": (120 * 60, None),
    # 歌曲档（网易云时长过滤，单位分钟）
    "0-2": (0, 2 * 60),
    "2-4": (2 * 60, 4 * 60),
    "4-6": (4 * 60, 6 * 60),
    "6-": (6 * 60, None),
}


def _parse_dur(dur: str):
    """时长筛选：返回 (最小秒, 最大秒)（None=不限）；空字符串返回 None。"""
    if not dur:
        return None
    if dur in _DUR_BUCKETS:
        return _DUR_BUCKETS[dur]
    if dur.startswith("custom"):
        try:
            spec = dur.split(":", 1)[1]
            a, b = (spec.split(",") + ["", ""])[:2]
            lo = float(a) * 60 if a.strip() else None
            hi = float(b) * 60 if b.strip() else None
            return (lo, hi)
        except Exception:  # noqa: BLE001
            return None
    return None


def _day_start(y: int, m: int, d: int, end: bool = False) -> int:
    return int(time.mktime((y, m, d, 23, 59, 59, 0, 0, -1) if end else (y, m, d, 0, 0, 0, 0, 0, -1)))


def _parse_pub(pub: str):
    """发布时间筛选：返回 (begin_ts, end_ts)；None=不限。

    自定义口径（既定）：前者＝最近（默认现在），后者＝以前（默认无限前推）。
    """
    if not pub:
        return None
    now = int(time.time())
    if pub == "today":
        lt = time.localtime(now)
        return (_day_start(lt.tm_year, lt.tm_mon, lt.tm_mday), None)
    if pub == "7d":
        return (now - 7 * 86400, None)
    if pub == "30d":
        return (now - 30 * 86400, None)
    if pub == "365d":
        return (now - 365 * 86400, None)
    if pub == "older1y":
        return (None, now - 365 * 86400)
    if pub.startswith("custom"):
        try:
            spec = pub.split(":", 1)[1]
            a, b = (spec.split(",") + ["", ""])[:2]

            def ts(s, end=False):
                s = s.strip()
                if not s:
                    return None
                t = time.strptime(s, "%Y-%m-%d")
                return _day_start(t.tm_year, t.tm_mon, t.tm_mday, end=end)

            recent = ts(a) or now          # 前者=最近（默认现在）
            older = ts(b) or None          # 后者=以前（默认不限）
            if older and recent < older:   # 写反了自动交换
                older, recent = recent, older
            return (older, recent)
        except Exception:  # noqa: BLE001
            return None
    return None


_PUB_YEARS = {"1": 1, "3": 3, "5": 5, "10": 10}


def _pub_band(pub: str, now: float | None = None):
    """网易云发行时间口径：返回 ((y,m,d) 起, (y,m,d) 止)；None=不限。

    ""=不限；"1"/"3"/"5"/"10"=近 N 年（今天往前推 N 个日历年，边界那天算命中）；
    "YYYY-YYYY"=自定义年份区间（含两端，写反自动交换，也接受 "YYYY-"/"-YYYY"）。
    解析不了一律当不限——参数写坏不该把结果清空。
    """
    s = (pub or "").strip().lower()
    if not s or s in ("none", "all", "0"):
        return None
    if s in _PUB_YEARS:
        n = _PUB_YEARS[s]
        lt = time.localtime(time.time() if now is None else now)
        y, m, d = lt.tm_year - n, lt.tm_mon, lt.tm_mday
        if m == 2 and d == 29 and not calendar.isleap(y):
            d = 28                     # 2/29 落到平年：退到 2/28，否则这天永远比不到
        return ((y, m, d), None)
    if "-" in s:
        a, b = (s.split("-", 1) + [""])[:2]

        def _yr(x: str):
            x = x.strip()
            if not x.isdigit():
                return None
            v = int(x)
            return v if 1900 <= v <= 2100 else None

        lo, hi = _yr(a), _yr(b)
        if lo is None and hi is None:
            return None
        if lo and hi and lo > hi:
            lo, hi = hi, lo
        return ((lo, 1, 1) if lo else None, (hi, 12, 31) if hi else None)
    return None


def _pub_date(publish_ms):
    """publishTime → 本地 (y, m, d)；缺失/0/离谱一律 None（当作未知）。"""
    try:
        v = int(publish_ms or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # 网易云给毫秒（1698768000000）；兼容误给秒的抓法，别把 2023 判成 1970
    secs = v / 1000.0 if v >= 1e11 else float(v)
    try:
        lt = time.localtime(secs)
    except (OSError, OverflowError, ValueError):
        return None
    if lt.tm_year < 1900 or lt.tm_year > 2100:
        return None
    return (lt.tm_year, lt.tm_mon, lt.tm_mday)


def _norm_title(s: str) -> str:
    """标题归一：去掉括号里的版本说明、空格与标点，用于同批同名曲比对。"""
    s = (s or "").lower()
    s = re.sub(r"[（(\[【].*?[）)\]】]", "", s)
    s = re.sub(r"[\s·・\-—_/、,，.。!！?？'\"“”‘’]+", "", s)
    return s.strip()


def _pass_mode(mode: str, flag: bool) -> bool:
    """三态筛选：""/"all"=不限；"only"=只要 True；"not"=只要 False。"""
    if mode == "only":
        return flag
    if mode == "not":
        return not flag
    return True


def _privileges(ids: list) -> dict:
    """批量取音质/付费标记，带 24 小时内存缓存；失败时按未知处理。"""
    now = time.time()
    out: dict = {}
    need = []
    for sid in ids:
        if not sid:
            continue
        hit = _PRIV_CACHE.get(sid)
        if hit and now - hit[3] < _PRIV_TTL:
            out[sid] = {"level": hit[0], "fee": hit[1], "st": hit[2],
                        "cover_type": hit[4] if len(hit) > 4 else 0,
                        "orig_artists": hit[5] if len(hit) > 5 else ""}
        else:
            need.append(sid)
    need = list(dict.fromkeys(need))
    if need:
        try:
            fresh = netease.song_privileges(need)
        except Exception as exc:
            _log.warning("privileges failed: %s", exc)
            fresh = {}
        for sid, v in fresh.items():
            _PRIV_CACHE[sid] = (v["level"], v["fee"], v["st"], now,
                                v.get("cover_type", 0), v.get("orig_artists", ""))
            out[sid] = v
    if len(_PRIV_CACHE) > 50000:
        for k in sorted(_PRIV_CACHE, key=lambda k: _PRIV_CACHE[k][3])[:10000]:
            _PRIV_CACHE.pop(k, None)
    return out


def _pub_hit(publish_ms, band) -> bool:
    """band 内才算命中；band=None（不限）恒真。未知发行时间在筛选开启时排除（宁缺勿滥）。"""
    if not band:
        return True
    d = _pub_date(publish_ms)
    if d is None:
        return False
    lo, hi = band
    return (lo is None or d >= lo) and (hi is None or d <= hi)


def _hot_score(v: dict) -> float:
    """综合热度（默认公式，可后续做成可配置）：播放 + 2×弹幕 + 3×收藏 + 2×点赞。"""
    return ((v.get("play") or 0) + 2 * (v.get("danmaku") or 0)
            + 3 * (v.get("favorites") or 0) + 2 * (v.get("like") or 0))


def _bili_field_hit(v: dict, mode: str, q: str) -> bool:
    ql = q.lower()
    if mode == "title":
        return ql in (v.get("title") or "").lower()
    if mode == "desc":
        return ql in (v.get("desc") or "").lower()
    if mode == "tag":
        return ql in (v.get("tag") or "").lower()
    return True


# 命中字段的展示/排序优先级：标题 > 简介 > 标签
_MATCH_FIELD_RANK = {"title": 0, "desc": 1, "tag": 2, "fuzzy": 3}


def _platform_dur(dur) -> str | None:
    """把本地时长区间映射到平台时长档，让平台先筛掉大半候选。

    平台档：1=<10分钟，2=10~30分钟，3=30~60分钟，4=>60分钟。
    只有区间能完整落进单一平台档时才映射；跨档或自定义怪区间返回 None（退回本地过滤）。
    """
    if not dur:
        return None
    lo, hi = dur
    if hi is not None and hi <= 600:
        return "1"
    if lo is not None and lo >= 600 and hi is not None and hi <= 1800:
        return "2"
    if lo is not None and lo >= 1800 and hi is not None and hi <= 3600:
        return "3"
    if lo is not None and lo >= 3600:
        return "4"
    return None


def match_field_of(v: dict, q: str) -> str:
    """返回命中字段：'title' / 'desc' / 'tag' / ''（都不命中）。大小写不敏感。优先级 title > desc > tag。"""
    ql = (q or "").lower()
    if not ql:
        return ""
    if ql in (v.get("title") or "").lower():
        return "title"
    if ql in (v.get("desc") or "").lower():
        return "desc"
    if ql in (v.get("tag") or "").lower():
        return "tag"
    return ""


def order_by_match_field(items: list) -> list:
    """稳定重排：标题命中 → 简介 → 标签 → 其它（含空）；同一档内保持原顺序。"""
    return sorted(items, key=lambda v: _MATCH_FIELD_RANK.get(v.get("match_field") or "", 9))



def _int_arg(value, default: int, lo: int, hi: int) -> int:
    """把查询参数安全转成整数并夹到 [lo, hi]——绝不因脏参数抛 500。"""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _req_int(qs: dict, key: str):
    """必填整数参数：非法/缺失返回 None（由调用方回 400）。"""
    raw = (qs.get(key) or [""])[0]
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def run_bilibili(q: str, opts: dict | None = None) -> dict:
    """B站搜索：六维度 × 时长/时间过滤 × 排序 × 翻页。"""
    o = opts or {}
    mode = o.get("mode") or "all"
    if mode not in ("all", "title", "desc", "tag", "user", "uid"):
        mode = "all"
    page = _int_arg(o.get("page"), 1, 1, 200)
    size = _int_arg(o.get("pagesize"), 30, 30, 300)
    if size not in (30, 50, 100, 300):
        size = 30
    page = min(page, max(1, 1000 // size))  # 平台单关键词上限 1000 条，翻页别越界空扫
    order = o.get("order") or "totalrank"
    match = (o.get("match") or "").strip()   # 命中严格度：std 任一处含全词 / strict 仅标题含全词 / 空=不过滤
    dur = _parse_dur(o.get("dur") or "")
    pub = _parse_pub(o.get("pub") or "")

    base = {"mode": mode, "page": page, "pagesize": size, "order": order,
            "total": 0, "has_more": False}

    # ---- UP主 / UID ----
    if mode == "user":
        page_size, want = 50, page * size
        users, total, pg, seen_mid = [], 0, 1, set()
        while len(users) < want and pg <= 20:
            d = bilibili.search_users(q, page=pg, page_size=page_size)
            total = d.get("numResults") or total
            batch = [bilibili.norm_user(u) for u in d["result"]]
            for u in batch:
                if u["mid"] not in seen_mid:
                    seen_mid.add(u["mid"])
                    users.append(u)
            if len(batch) < page_size:
                break
            pg += 1
            if len(users) < want:
                time.sleep(0.8)
        start = (page - 1) * size
        return {**base, "kind": "user", "items": users[start:start + size], "total": total,
                "scanned": len(users), "pool_size": len(users),
                "has_more": start + size < total}

    if mode == "uid":
        if not q.isdigit():
            return {**base, "kind": "user", "items": [], "error": "UID 搜索需要纯数字"}
        u = bilibili.user_info(int(q))
        return {**base, "kind": "user", "items": [u] if u else [],
                "total": 1 if u else 0, "scanned": 1 if u else 0,
                "pool_size": 1 if u else 0}

    # ---- 视频系（综合/名称/简介/标签）----
    api_order = "pubdate" if order == "oldest" else (
        order if order in ("totalrank", "pubdate", "click", "dm", "stow") else "totalrank")
    time_scan = order == "oldest"      # 真·最旧：走时间窗扫描，不用「最新 1000 条」倒排
    local_sort = order in ("hot", "like")
    need_full = local_sort and 1000  # 本地排序：尽量抓满（B站上限 1000）
    key = (q, mode, order, o.get("dur") or "", o.get("pub") or "", match, fuzzy.enabled())
    cached = _BILI_CACHE.get(key)
    pool = total = scanned = None
    gov = governance.Governor(q, match=(match if mode == "all" else ""), source="bilibili")
    gov_summary = None
    if cached and time.time() - cached["ts"] < _BILI_CACHE_TTL:
        pool, total, scanned = cached["pool"], cached["total"], cached["scanned"]
        gov_summary = cached.get("gov")
    need = 1000 if need_full else page * size
    channels: list = []
    oldest_state: dict | None = None       # 最旧模式：{w0: 窗口起点, pg: 窗口内下一待抓页}
    if cached and cached.get("channels"):
        channels = cached["channels"]
    if cached and cached.get("oldest_state"):
        oldest_state = cached["oldest_state"]
    if pool is None or len(pool) < need:
        if pool:                                  # 扩展模式：保住旧池、只补新条目（翻页稳定，避免重建漂移）
            seen = {it["bvid"] for it in pool}
            scanned = scanned or 0
            total = total or 0
            if gov_summary:                       # 治理计数接续旧账
                try:
                    for k, v in (gov_summary.get("dropped") or {}).items():
                        gov.dropped[k] = int(v or 0)
                    gov.kept = int(gov_summary.get("kept") or 0)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("治理计数接续失败: %r", exc)
        else:                                     # 全新抓取
            pool, total, scanned, seen = [], 0, 0, set()
        channels = [api_order]

        def _ingest(batch: list) -> None:
            """逐条过（字段命中 → 治理 → 时长/时间过滤）后入池。"""
            for it in batch:
                v = bilibili.norm_video(it)
                if not v["bvid"] or v["bvid"] in seen:
                    continue
                v["match_field"] = match_field_of(v, q)   # 命中字段只算一次，供排序/前端展示
                if not _bili_field_hit(v, mode, q):
                    continue
                if gov.check(v):          # 治理层：纯推位 / 直播卡 / 非全词
                    continue
                if v.get("fuzzy"):        # 模糊命中（X3-3）：只标注；排序档在标签命中之后
                    v["match_field"] = "fuzzy"
                if dur:
                    ds = v.get("duration_sec") or 0
                    lo, hi = dur
                    if (lo is not None and ds < lo) or (hi is not None and ds > hi):
                        continue
                if pub:
                    pd = v.get("pubdate") or 0
                    if (pub[0] and pd < pub[0]) or (pub[1] and pd > pub[1]):
                        continue
                seen.add(v["bvid"])
                pool.append(v)

        def _scan(order_name: str, max_pages: int, win: tuple[int, int] | None = None) -> None:
            """扫一条排序通道（或一个时间窗），追加进 pool。"""
            nonlocal total, scanned
            pg = 1
            empty_streak = 0     # 连续零收获的页数：窄过滤下翻页多半是空转，连空 3 页就收手
            while pg <= max_pages and len(pool) < need:
                d = bilibili.search_videos_meta(
                    q, page=pg, page_size=50, order=order_name,
                    pub_begin=(win[0] if win else (pub[0] if pub else None)),
                    pub_end=(win[1] if win else (pub[1] if pub else None)),
                    duration=_platform_dur(dur))
                batch = d.get("result") or []
                total = d.get("numResults") or total
                if not batch:
                    break
                scanned += len(batch)
                before = len(pool)
                _ingest(batch)
                empty_streak = empty_streak + 1 if len(pool) == before else 0
                if empty_streak >= 3:
                    break        # 连 3 页一条都没捞到，换通道更有意义
                if len(batch) < 50:
                    break
                pg += 1
                if len(pool) < need:
                    time.sleep(0.55)

        def _scan_oldest_window(w0: int, w1: int, start_pg: int | None = None) -> int | None:
            """窗口内从「最老」一端抓起：先探总数，再从末页倒着抓到第 1 页。

            返回窗口内下一待抓页（整窗抓完返回 None）。续页必须接着同一窗口往下抓，
            否则会跳过这个窗口剩下的条目（曾因此出现 2022-03 → 2023-08 的断层）。
            """
            nonlocal total, scanned
            probe = bilibili.search_videos_meta(q, page=1, page_size=50, order="pubdate",
                                                pub_begin=w0, pub_end=w1)
            n = probe.get("numResults") or 0
            scanned += len(probe.get("result") or [])
            if n <= 0:
                return None
            total = total or n
            pages = max(1, min(20, (min(n, 1000) + 49) // 50))
            pg = start_pg if (start_pg and 1 <= start_pg <= pages) else pages
            while pg >= 1:
                if len(pool) >= need or scanned >= 20000:
                    return pg
                if pg == 1:
                    batch = probe.get("result") or []
                else:
                    d = bilibili.search_videos_meta(q, page=pg, page_size=50, order="pubdate",
                                                    pub_begin=w0, pub_end=w1)
                    batch = d.get("result") or []
                    scanned += len(batch)
                if batch:
                    _ingest(batch)
                pg -= 1
                if pg >= 1 and len(pool) < need:
                    time.sleep(0.5)
            return None

        def _scan_oldest(state: dict | None = None) -> None:
            """真·最旧：从 2009 年起按一年一个时间窗往前推进，直到凑够所需条目。

            state 记「窗口起点 + 窗口内下一页」——翻页时接着同一窗口继续，时间轴才连续。
            """
            nonlocal oldest_state
            t_begin = int(time.mktime((2009, 1, 1, 0, 0, 0, 0, 0, -1)))
            t_end = int(time.time())
            if pub:
                t_begin = pub[0] or t_begin
                t_end = pub[1] or t_end
            win = 365 * 86400
            cur, pg = t_begin, None
            if state and state.get("w0", 0) > t_begin:
                cur, pg = state["w0"], state.get("pg")
            scanned_before = scanned
            while cur < t_end and len(pool) < need and scanned - scanned_before < 20000:
                nxt = _scan_oldest_window(cur, min(cur + win, t_end), pg)
                if nxt is not None:
                    oldest_state = {"w0": cur, "pg": nxt}
                    return
                cur += win
                pg = None
                if len(pool) < need:
                    time.sleep(0.35)
            oldest_state = None

        if time_scan:
            _scan_oldest(oldest_state)
            channels = ["pubdate-window"]
            total = len(pool)          # 最旧模式没有全局总数可言，按已采到的匹配数计
        else:
            # 带时长/时间过滤时命中率天然低，翻 20 页全是空转——砍到 10 页（实测 10~15 分钟档：
            # 20 页要 20 秒、1000 条；10 页约 10 秒，命中数够一页）。
            _scan(api_order, 10 if (dur or pub) else 20)
            # 多通道召回（机制）：主排序凑不满＝关键词被推送淹没/长尾错字——
            # 实测 totalrank 充满纯推位，而 pubdate/click/dm 通道几乎无推位，换通道补捞
            if not need_full and not (mode == "all" and match == ""):
                for ch in ("pubdate", "click", "dm", "stow"):
                    # 带过滤时命中率低，补通道也多半是空转：最多补 1 条通道（共 2 条）就收手。
                    if len(pool) >= need or len(channels) >= (2 if (dur or pub) else 3):
                        break
                    if ch == api_order or ch in channels:
                        continue
                    _scan(ch, 10)
                    channels.append(ch)
        gov_summary = gov.summary()
        archive.remember(pool, _ARCHIVE_DB)   # 见过的视频入档（B15 下架侦测的数据源）
        _BILI_CACHE[key] = {"pool": pool, "total": total, "scanned": scanned,
                            "gov": gov_summary, "channels": channels,
                            "oldest_state": oldest_state, "ts": time.time()}
        governance.log_run(governance.DB_DEFAULT, "bilibili", q, mode,
                           {"order": order, "dur": o.get("dur") or "",
                            "pub": o.get("pub") or "", "match": match,
                            "channels": channels},
                           scanned, gov_summary)
        if len(_BILI_CACHE) > 10:  # 简易容量控制
            oldest_key = min(_BILI_CACHE, key=lambda k: _BILI_CACHE[k]["ts"])
            _BILI_CACHE.pop(oldest_key, None)
    if gov_summary is None:
        gov_summary = gov.summary()

    # 注意：**不能**在这里对全池重排——pool 会随翻页向平台补扫而增长，
    # 全池重排会让页边界漂移，实测导致第 1/2 页出现重叠（B站套件 paging 用例）。
    # 命中字段排序只作用于「当页窗口」，见下方 window 处理。
    if order == "oldest":
        pool = sorted(pool, key=lambda v: v.get("pubdate") or 2**31)  # 无发布时间的排最后，别装最老
    elif order == "hot":
        pool = sorted(pool, key=lambda v: _hot_score(v), reverse=True)
    elif order == "like":
        pool = sorted(pool, key=lambda v: v.get("like") or 0, reverse=True)

    start = (page - 1) * size
    window = pool[start:start + size]
    if order == "totalrank":
        # 只在当页窗口内按命中字段排（标题 > 简介 > 标签 > 其它）：稳定，不改同档原顺序，
        # 且不改变全池顺序——页间不会因为窗口内重排而重复。
        window = order_by_match_field(window)
    for v in window:      # 前端要用的字段兜底（正常路径已由 norm_video / _ingest 填好）
        if "match_field" not in v:
            v["match_field"] = match_field_of(v, q)
        v.setdefault("hit_columns", [])
        v.setdefault("tag", "")
    return {**base, "kind": "video", "items": window,
            "total": total or len(pool), "scanned": scanned or 0,
            "pool_size": len(pool), "channels": channels,
            "push_dropped": (gov_summary.get("dropped") or {}).get("push", 0),
            "governance": gov_summary,
            "has_more": (len(pool) > start + size) or (scanned or 0) < (total or 0)}


# ---------- 聚合（殿后） ----------

def run_all(q: str, db_path: str) -> dict:
    t0 = time.time()
    out = {"q": q,
           "netease": {"items": [], "stats": {}, "error": None},
           "bilibili": {"items": [], "stats": {}, "error": None}}
    try:
        out["netease"].update(run_netease_songs(q, db_path))
    except Exception as exc:  # noqa: BLE001
        out["netease"]["error"] = repr(exc)
    try:
        b = run_bilibili(q, {"mode": "all", "pagesize": 30, "match": "std"})
        out["bilibili"].update({"items": b.get("items", []), "stats": {
            "total": b.get("total"), "kind": b.get("kind")}})
    except Exception as exc:  # noqa: BLE001
        out["bilibili"]["error"] = repr(exc)
    out["elapsed"] = round(time.time() - t0, 1)
    return out


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "usable-search/0.3"
    db_path = "data/index.sqlite3"

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._json(500, {"error": str(exc)})
            return
        if u.path == "/api/ping":
            self._send(200, b'{"ok": true}')
            return
        if u.path == "/api/probe":
            # 接口长期可用性看板：days 夹在 1..30，脏参数绝不 500
            qs = urllib.parse.parse_qs(u.query)
            days = _int_arg((qs.get("days") or ["7"])[0], 7, 1, 30)
            try:
                self._json(200, probe.stats(days=days, db_path=_PROBE_DB))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": repr(exc)})
            return
        if u.path == "/api/governance":
            qs = urllib.parse.parse_qs(u.query)
            q = (qs.get("q") or [""])[0].strip()
            self._json(200, governance.stats(
                os.path.join(ROOT, "data", "governance.sqlite3"), q=q))
            return
        if u.path in ("/api/up", "/api/season"):
            qs = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/api/up":
                    mid = _req_int(qs, "mid")
                    if not mid:
                        self._json(400, {"error": "缺少或非法的 mid"})
                        return
                    tab = (qs.get("tab") or ["videos"])[0]
                    page = _int_arg((qs.get("page") or ["1"])[0], 1, 1, 200)
                    size = _int_arg((qs.get("size") or ["30"])[0], 30, 1, 50)
                    if tab == "seasons":
                        payload = bilibili.user_seasons(mid, page=page, page_size=min(size, 20))
                    elif tab == "videos":
                        payload = bilibili.user_videos(mid, page=page, page_size=size)
                    else:
                        self._json(400, {"error": f"未知 tab: {tab}"})
                        return
                    payload.update({"mid": mid, "tab": tab})
                else:
                    mid = _req_int(qs, "mid")
                    sid = _req_int(qs, "season_id")
                    if not mid or not sid:
                        self._json(400, {"error": "缺少或非法的 mid / season_id"})
                        return
                    page = _int_arg((qs.get("page") or ["1"])[0], 1, 1, 200)
                    size = _int_arg((qs.get("size") or ["50"])[0], 50, 1, 50)
                    payload = bilibili.season_archives(mid, sid, page=page, page_size=size)
                    payload.update({"mid": mid})
                self._json(200, payload)
            except bilibili.RiskControlError as exc:
                self._json(200, {"error": f"B站接口被风控，请稍后再试（{exc}）", "risk": True})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": repr(exc)})
            return
        if u.path == "/api/cache/clear":
            qs = urllib.parse.parse_qs(u.query)
            with _NETEASE_LOCK:
                n_bili = len(_BILI_CACHE)
                _BILI_CACHE.clear()
                n_net = len(_NETEASE_CACHE)
                _NETEASE_CACHE.clear()
            n_lrc = None
            if (qs.get("lyrics") or [""])[0] in ("1", "true", "on"):
                idx = Index(self.db_path)
                try:
                    n_lrc = idx.conn.execute("DELETE FROM lyrics").rowcount
                    idx.conn.commit()
                finally:
                    idx.conn.close()
            self._json(200, {"ok": True, "bili": n_bili, "netease": n_net, "lyrics": n_lrc})
            return
        if u.path in ("/api/archive", "/api/archive/recheck"):
            qs = urllib.parse.parse_qs(u.query)
            q = (qs.get("q") or [""])[0].strip()
            status = (qs.get("status") or [""])[0].strip()
            try:
                if u.path == "/api/archive":
                    limit = _int_arg((qs.get("limit") or ["50"])[0], 50, 0, 500)
                    self._json(200, {
                        "stats": archive.stats(_ARCHIVE_DB),
                        "items": archive.query(q=q, status=status, limit=limit,
                                               db_path=_ARCHIVE_DB) if limit else [],
                        "q": q, "status": status,
                    })
                else:
                    limit = _int_arg((qs.get("limit") or ["10"])[0], 10, 1, 50)
                    self._json(200, archive.recheck(limit=limit, q=q, status=status,
                                                    db_path=_ARCHIVE_DB))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": repr(exc)})
            return
        if u.path == "/api/backup":
            # 失效视频备份站：只查单个 BV 号，绝不接进 /api/search 主路径
            qs = urllib.parse.parse_qs(u.query)
            bvid = (qs.get("bvid") or [""])[0].strip()
            if not _BVID_RE.match(bvid):
                self._json(400, {"error": "缺少或非法的 bvid"})
                return
            try:
                self._json(200, backup.lookup(bvid, db_path=_BACKUP_DB))
            except Exception as exc:  # noqa: BLE001 —— lookup 本不该抛，兜一层
                self._json(500, {"error": repr(exc)})
            return
        if u.path == "/api/search":
            qs = urllib.parse.parse_qs(u.query)
            q = (qs.get("q") or [""])[0].strip()
            src = (qs.get("src") or ["all"])[0]
            typ = (qs.get("type") or [""])[0]
            if not q:
                self._json(400, {"error": "缺少 q 参数"})
                return
            try:
                t0 = time.time()
                if src == "netease":
                    if typ in ("", "song"):
                        payload = run_netease_songs(
                            q, self.db_path, dur=(qs.get("dur") or [""])[0],
                            pub=(qs.get("pub") or [""])[0],
                            sort=(qs.get("sort") or [""])[0],
                            vip=(qs.get("vip") or [""])[0],
                            orig=(qs.get("orig") or [""])[0],
                            page=(qs.get("page") or ["1"])[0],
                            pagesize=(qs.get("size") or ["30"])[0])
                    elif typ == "lyric":
                        payload = run_netease_lyric(q, self.db_path)
                    elif typ in ("album", "artist", "playlist", "podcast"):
                        payload = run_netease_typed(q, typ)
                    else:
                        self._json(400, {"error": f"未知 type: {typ}"})
                        return
                elif src == "bilibili":
                    if typ not in ("", "all", "title", "desc", "tag", "user", "uid"):
                        self._json(400, {"error": f"未知 type: {typ}"})
                        return
                    opts = {
                        "mode": typ or "all",
                        "page": (qs.get("page") or ["1"])[0],
                        "pagesize": (qs.get("size") or ["30"])[0],
                        "order": (qs.get("order") or ["totalrank"])[0],
                        "dur": (qs.get("dur") or [""])[0],
                        "pub": (qs.get("pub") or [""])[0],
                        "match": (qs.get("match") or [""])[0],
                    }
                    payload = run_bilibili(q, opts)
                elif src == "all":
                    payload = run_all(q, self.db_path)
                else:
                    self._json(400, {"error": f"未知 src: {src}"})
                    return
                payload.setdefault("q", q)
                payload.setdefault("elapsed", round(time.time() - t0, 1))
                self._json(200, payload)
            except bilibili.RiskControlError as exc:
                self._json(200, {"error": f"B站接口被风控，请稍后再试（{exc}）", "risk": True})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": repr(exc)})
            return
        self._send(404, b'{"error": "not found"}')

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)


def main(argv=None) -> None:
    import argparse
    import socket
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="usable_search serve", description="统一搜索框（M3 原型）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18788)
    ap.add_argument("--db", default="data/index.sqlite3")
    args = ap.parse_args(argv)

    # 端口预检：已有服务在监听就明确报错（避免双绑“抢答”成 404）
    pre = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    pre.settimeout(1.0)
    try:
        pre.connect((args.host, args.port))
        print(f"!! 端口 {args.host}:{args.port} 已被占用（可能是别的服务在监听）。"
              f"换 --port，或用 netstat -ano 查明占用者。", file=sys.stderr)
        sys.exit(2)
    except OSError:
        pass
    finally:
        pre.close()

    # 启动时清一次歌词缓存（TTL 过期在 get_lyric 里判，这里只做容量淘汰）
    boot = fts_gate.open_index(args.db)
    try:
        pruned = boot.prune_lyrics()
    finally:
        boot.conn.close()
    print(f"[lyrics] prune 删除 {pruned} 行", flush=True)

    # 探针库只留最近 30 天（跟歌词清理一起做，启动时一次就够）
    try:
        deleted = probe.prune(probe.MAX_KEEP_DAYS, db_path=_PROBE_DB)
    except Exception as exc:  # noqa: BLE001 —— 清理失败不能挡住服务
        deleted = 0
        print(f"[probe] prune failed: {exc!r}", flush=True)
    print(f"[probe] prune 删除 {deleted} 行", flush=True)

    Handler.db_path = args.db
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"* 统一搜索框已启动：http://{args.host}:{args.port}/  （Ctrl+C 停止）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
