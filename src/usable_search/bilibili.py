"""哔哩哔哩数据采集器（M2 · v0.1）.

只用公开 Web 接口；搜索走 WBI 签名（与官方 web 前端同款算法）。
- 视频搜索（支持 order / 发布时间窗 / 分页）
- UP 主搜索（bili_user）
- UID 资料（space/wbi/acc/info ＋ relation/stat 粉丝数）
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request

BASE = "https://api.bilibili.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com/",
}
SLEEP = 1.0  # 礼貌间隔（秒）
_log = logging.getLogger(__name__)

# WBI 打乱表（mixinKeyEncTab，官方前端同款）
_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5,
              49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24,
              55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
              57, 62, 11, 36, 20, 34, 44, 52]

_wbi_cache: dict = {"key": None, "ts": 0.0}
_cookie_cache: dict = {"value": None}


class RiskControlError(RuntimeError):
    """B站风控（-352/-412/-799）：接口暂时不可用，过一会儿再试。"""


_space_cache: dict = {}
_SPACE_CACHE_TTL = 300  # space 系接口缓存 5 分钟：减少请求、降低风控概率

try:  # 探针是旁路：导不进也不能影响采集
    from . import probe as _probe_mod
except Exception:  # noqa: BLE001
    _probe_mod = None


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _probe(endpoint: str, *, ok: bool, ms: int = 0, code: str = "",
           risk: bool = False) -> None:
    """落一条探针：只记路径不记查询串；任何异常都吞掉，绝不改变调用方行为。"""
    if _probe_mod is None:
        return
    try:
        _probe_mod.record("bilibili", endpoint, ok=ok, code=code, ms=ms, risk=risk)
    except Exception:  # noqa: BLE001
        pass


def _exc_code(exc: Exception | None) -> str:
    """HTTP 错误取状态码，其它取异常名。"""
    if exc is None:
        return "none"
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, int) else type(exc).__name__


def _is_risk(exc: Exception | None) -> bool:
    """412 与 RiskControlError 都算风控；其余一律不算。"""
    return isinstance(exc, RiskControlError) or getattr(exc, "code", None) == 412


def _request(url: str, timeout: int = 20, tries: int = 3, headers: dict | None = None) -> dict:
    if not url.startswith(("https://", "http://")):  # 仅允许 http(s)，防 file:/自定义 scheme
        raise ValueError(f"unsupported url scheme: {url!r}")
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    endpoint = urllib.parse.urlparse(url).path or "/"  # 只记路径，搜索词不入库
    t0 = time.perf_counter()
    last: Exception | None = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 —— scheme 已白名单
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            _probe(endpoint, ok=True, ms=_elapsed_ms(t0))
            return payload
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5)
    _probe(endpoint, ok=False, code=_exc_code(last), ms=_elapsed_ms(t0),
           risk=_is_risk(last))
    raise RuntimeError(f"bilibili request failed: {url} ({last!r})")


def _mixin_key(img: str, sub: str) -> str:
    raw = img + sub
    return "".join(raw[i] for i in _MIXIN_TAB)[:32]


def _get_wbi_key(force: bool = False) -> str:
    now = time.time()
    if not force and _wbi_cache["key"] and now - _wbi_cache["ts"] < 1800:
        return _wbi_cache["key"]
    data = _request(BASE + "/x/web-interface/nav")
    wbi = ((data.get("data") or {}).get("wbi_img")) or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise RuntimeError(f"wbi_img 获取失败: {str(data)[:200]}")
    _wbi_cache["key"] = _mixin_key(img, sub)
    _wbi_cache["ts"] = now
    return _wbi_cache["key"]


def wbi_sign(params: dict, force: bool = False) -> str:
    """返回签好名的查询串（含 wts / w_rid）。"""
    key = _get_wbi_key(force=force)
    p = dict(params)
    p["wts"] = int(time.time())
    items = []
    for k in sorted(p):
        v = str(p[k])
        for ch in "!'()*":
            v = v.replace(ch, "")
        items.append(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}")
    query = "&".join(items)
    w_rid = hashlib.md5((query + key).encode("utf-8"), usedforsecurity=False).hexdigest()
    return query + "&w_rid=" + w_rid


def _fetch_buvid() -> str | None:
    """取 buvid3/4 做基础 Cookie，降低被风控概率。"""
    try:
        d = _request(BASE + "/x/frontend/finger/spi")
        b3 = (d.get("data") or {}).get("b_3")
        b4 = (d.get("data") or {}).get("b_4")
        if b3:
            return f"buvid3={b3}; buvid4={b4 or ''}"
    except Exception as exc:  # noqa: BLE001
        _log.warning("buvid 获取失败（继续匿名请求）: %r", exc)
    return None


def _search(search_type: str, keyword: str, page: int = 1, page_size: int = 20, **extra) -> dict:
    """通用搜索入口（含 buvid 换身份重试）。返回 {"result": [...], "numResults": n}。"""
    if _cookie_cache["value"] is None:
        _cookie_cache["value"] = _fetch_buvid()
    err = None
    for attempt in range(3):
        params = {"search_type": search_type, "keyword": keyword,
                  "page": page, "page_size": page_size}
        for k, v in extra.items():
            if v not in (None, ""):
                params[k] = v
        qs = wbi_sign(params, force=(attempt == 2))
        url = f"{BASE}/x/web-interface/wbi/search/type?{qs}"
        headers = {"Cookie": _cookie_cache["value"]} if _cookie_cache["value"] else None
        data = _request(url, headers=headers)
        if data.get("code") == 0:
            d = data.get("data") or {}
            result = d.get("result") or []
            if result:
                return {"result": result, "numResults": d.get("numResults") or 0}
            if attempt < 2:
                _cookie_cache["value"] = _fetch_buvid()  # 换新身份
                time.sleep(1.0)
                continue
            return {"result": [], "numResults": d.get("numResults") or 0}
        err = f"code={data.get('code')} message={data.get('message')}"
        time.sleep(1.2)
    raise RuntimeError(f"bilibili 搜索失败({search_type}): {err}")


def search_videos(keyword: str, page: int = 1, page_size: int = 20) -> list:
    """视频搜索（兼容旧接口：只返回条目列表）。"""
    return _search("video", keyword, page=page, page_size=page_size)["result"]


def search_videos_meta(keyword: str, page: int = 1, page_size: int = 50,
                       order: str | None = None,
                       pub_begin: int | None = None, pub_end: int | None = None,
                       duration: str | None = None) -> dict:
    """视频搜索（带元信息与过滤参数）。

    order：totalrank 综合 / pubdate 最新 / click 播放 / dm 弹幕 / stow 收藏 / like 点赞（近似）。
    duration：平台时长档 1=10分钟以内 / 2=10~30分钟 / 3=30~60分钟 / 4=60分钟以上。
              交给平台先筛，能大幅减少本地过滤要扫的条数（窄区间尤其明显）。
    """
    return _search("video", keyword, page=page, page_size=page_size,
                   order=order, pubtime_begin_s=pub_begin, pubtime_end_s=pub_end,
                   duration=duration)


def search_users(keyword: str, page: int = 1, page_size: int = 20) -> dict:
    """UP 主搜索（bili_user）。"""
    return _search("bili_user", keyword, page=page, page_size=page_size)


def user_info(mid: int) -> dict | None:
    """按 UID 取 UP 主资料。

    实测（2026-10-17）：space/wbi/acc/info 已加风控（-352 需验证码），
    改用 x/web-interface/card（匿名可取）＋ relation/stat 粉丝数。
    """
    try:
        d = _request(f"{BASE}/x/web-interface/card?mid={int(mid)}")
        code = int(d.get("code") or 0)
        if code in (-352, -412, -799):
            raise RiskControlError(f"code={code} msg={d.get('message')!r}")
        card = (d.get("data") or {}).get("card") or {}
        if code != 0 or not card:
            return None
        fans = card.get("fans")
        try:
            f = _request(f"{BASE}/x/relation/stat?vmid={int(mid)}")
            fans = (f.get("data") or {}).get("follower", fans)
        except Exception as exc:  # noqa: BLE001
            _log.warning("relation/stat 粉丝数获取失败（卡片仍可用）: %r", exc)
        face = card.get("face") or ""
        if face.startswith("//"):
            face = "https:" + face
        return {
            "kind": "user", "mid": int(mid), "name": card.get("name") or "",
            "sign": card.get("sign") or "", "face": face, "level": card.get("level"),
            "fans": fans, "videos": None,
            "url": f"https://space.bilibili.com/{int(mid)}",
        }
    except RiskControlError:
        raise
    except Exception:  # noqa: BLE001
        return None


_TAG_RE = re.compile(r"</?em[^>]*>")


def _parse_duration_sec(s: str) -> int:
    """把 '145:54' / '1:02:03' 转秒。"""
    try:
        parts = [int(p) for p in str(s or "").split(":")]
    except ValueError:
        return 0
    if len(parts) >= 3:
        return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return 0


def video_status(bvid: str) -> dict:
    """稿件状态：ok / gone（稿件不存在）/ hidden（不可见）/ error（接口异常）。

    用于「下架侦测」：只问状态，不下载内容。
    """
    d = _request(f"{BASE}/x/web-interface/view?bvid={urllib.parse.quote(str(bvid))}")
    code = int(d.get("code") or 0)
    if code == 0:
        data = d.get("data") or {}
        owner = data.get("owner") or {}
        return {"bvid": bvid, "status": "ok", "code": 0,
                "title": data.get("title") or "", "author": owner.get("name") or "",
                "pubdate": int(data.get("pubdate") or 0)}
    status = {-404: "gone", -403: "hidden"}.get(code, "error")
    return {"bvid": bvid, "status": status, "code": code,
            "message": d.get("message") or ""}


def _cookie_headers() -> dict | None:
    """带 buvid Cookie 的请求头——space 系接口裸请求会 412 / -799。"""
    if _cookie_cache["value"] is None:
        _cookie_cache["value"] = _fetch_buvid()
    return {"Cookie": _cookie_cache["value"]} if _cookie_cache["value"] else None


def _norm_archive(a: dict) -> dict:
    st = a.get("stat") or {}
    return {
        "bvid": a.get("bvid") or "",
        "title": html.unescape(_TAG_RE.sub("", a.get("title") or "")),
        "duration": a.get("duration") or 0,
        "duration_sec": int(a.get("duration") or 0),
        "pubdate": a.get("pubdate") or 0,
        "play": st.get("view") or 0,
        "danmaku": st.get("danmaku") or 0,
        "cover": (a.get("pic") or "").replace("http://", "https://"),
        "url": f"https://www.bilibili.com/video/{a.get('bvid')}" if a.get("bvid") else "",
    }


def _space_get(url: str) -> dict:
    """space 系接口：先裸请求；遇风控码再换 buvid Cookie 重试一次。

    实测：带 buvid Cookie 反而更容易 -352，裸请求常常直接 200——所以顺序反过来。
    结果缓存 5 分钟：同一 UP/合集来回点不会反复打接口。
    """
    now = time.time()
    hit = _space_cache.get(url)
    if hit and now - hit[0] < _SPACE_CACHE_TTL:
        return hit[1]
    risk = (-352, -412, -799)
    try:
        last = _request(url)
    except Exception as exc:
        msg = str(exc)
        if any(k in msg for k in ("412", "403", "352", "799")):
            raise RiskControlError(msg[:160]) from exc
        raise
    code = int(last.get("code") or 0)
    if code != 0 and code in risk:
        _cookie_cache["value"] = _fetch_buvid()
        time.sleep(1.0)
        try:
            last = _request(url, headers=_cookie_headers())
        except Exception as exc:
            raise RiskControlError(str(exc)[:160]) from exc
        code = int(last.get("code") or 0)
    if code != 0:
        raise RiskControlError(f"code={code} msg={last.get('message')!r}")
    _space_cache[url] = (now, last)
    if len(_space_cache) > 60:
        oldest = min(_space_cache, key=lambda k: _space_cache[k][0])
        _space_cache.pop(oldest, None)
    return last


def user_videos(mid: int, page: int = 1, page_size: int = 30, order: str = "pubdate") -> dict:
    """UP主投稿列表（WBI 版 space/arc/search）。

    实测：只签 mid/pn/ps/order 才稳；带 platform/web_location/order_avoided 会 -352 风控。
    """
    page_size = max(1, min(50, int(page_size or 30)))
    q = wbi_sign({"mid": int(mid), "pn": int(page), "ps": page_size,
                  "order": order or "pubdate"})
    d = _space_get(f"{BASE}/x/space/wbi/arc/search?{q}")
    data = d.get("data") or {}
    info = data.get("page") or {}
    items = []
    for v in ((data.get("list") or {}).get("vlist") or []):
        items.append({
            "bvid": v.get("bvid") or "",
            "title": html.unescape(_TAG_RE.sub("", v.get("title") or "")),
            "author": v.get("author") or "",
            "mid": v.get("mid") or int(mid),
            "play": v.get("play") or 0,
            "danmaku": v.get("video_review") or 0,
            "duration": v.get("length") or "",
            "duration_sec": _parse_duration_sec(v.get("length") or ""),
            "pubdate": v.get("created") or 0,
            "season_id": v.get("season_id") or 0,
            "cover": (v.get("pic") or "").replace("http://", "https://"),
            "desc": html.unescape(v.get("description") or "")[:120],
            "url": f"https://www.bilibili.com/video/{v.get('bvid')}" if v.get("bvid") else "",
        })
    return {"items": items, "total": int(info.get("count") or len(items)),
            "page": int(page), "page_size": page_size}


def user_seasons(mid: int, page: int = 1, page_size: int = 20) -> dict:
    """UP主的合集/系列列表（polymer/web-space/seasons_series_list）。"""
    page_size = max(1, min(50, int(page_size or 20)))
    d = _space_get(
        f"{BASE}/x/polymer/web-space/seasons_series_list?mid={int(mid)}"
        f"&page_num={int(page)}&page_size={page_size}")
    lists = (d.get("data") or {}).get("items_lists") or {}
    seasons = []
    for s in (lists.get("seasons_list") or []):
        m = s.get("meta") or {}
        seasons.append({
            "season_id": m.get("season_id") or 0,
            "title": m.get("title") or "",
            "name": m.get("name") or "",
            "total": int(m.get("total") or 0),
            "cover": (m.get("cover") or "").replace("http://", "https://"),
            "ptime": m.get("ptime") or 0,
            "archives": [_norm_archive(a) for a in (s.get("archives") or [])],
        })
    series = []
    for s in (lists.get("series_list") or []):
        m = s.get("meta") or {}
        series.append({
            "series_id": m.get("series_id") or 0,
            "title": m.get("title") or "",
            "total": int(m.get("total") or 0),
            "archives": [_norm_archive(a) for a in (s.get("archives") or [])],
        })
    return {"seasons": seasons, "series": series, "page": int(page), "page_size": page_size}


def season_archives(mid: int, season_id: int, page: int = 1, page_size: int = 50) -> dict:
    """合集选集（polymer/web-space/seasons_archives_list）。"""
    page_size = max(1, min(50, int(page_size or 50)))
    d = _space_get(
        f"{BASE}/x/polymer/web-space/seasons_archives_list?mid={int(mid)}"
        f"&season_id={int(season_id)}&sort_reverse=false&page_num={int(page)}"
        f"&page_size={page_size}")
    data = d.get("data") or {}
    m = data.get("meta") or {}
    items = [_norm_archive(a) for a in (data.get("archives") or [])]
    return {"season_id": int(season_id), "title": m.get("title") or "",
            "total": int(m.get("total") or len(items)),
            "items": items, "page": int(page), "page_size": page_size}


def norm_video(item: dict) -> dict:
    """统一记录格式（视频条目）。"""
    title = html.unescape(_TAG_RE.sub("", item.get("title") or ""))
    pic = item.get("pic") or ""
    if pic.startswith("//"):
        pic = "https:" + pic
    return {
        "source": "bilibili",
        "bvid": item.get("bvid") or "",
        "title": title,
        "author": item.get("author") or "",
        "mid": item.get("mid"),
        "play": item.get("play") or 0,
        "danmaku": item.get("video_review") or item.get("danmaku") or 0,
        "favorites": item.get("favorites") or 0,
        "like": item.get("like") or 0,
        "duration": item.get("duration") or "",
        "duration_sec": _parse_duration_sec(item.get("duration")),
        "hit_columns": item.get("hit_columns") or [],  # B站自标：命中了哪些字段；空＝纯推送位
        "is_live_room_inline": bool(item.get("is_live_room_inline")),
        "pubdate": item.get("pubdate") or 0,
        "tag": item.get("tag") or "",
        "desc": html.unescape(item.get("description") or "")[:200],
        "cover": pic,
        "url": f"https://www.bilibili.com/video/{item.get('bvid')}" if item.get("bvid") else "",
    }


def norm_user(u: dict) -> dict:
    """统一记录格式（UP 主条目）。"""
    face = u.get("upic") or u.get("face") or ""
    if face.startswith("//"):
        face = "https:" + face
    return {
        "kind": "user", "mid": u.get("mid"),
        "name": u.get("uname") or u.get("name") or "",
        "sign": u.get("usign") or u.get("sign") or "",
        "face": face, "fans": u.get("fans"), "videos": u.get("videos"),
        "level": u.get("level"),
        "url": f"https://space.bilibili.com/{u.get('mid')}" if u.get("mid") else "",
    }
