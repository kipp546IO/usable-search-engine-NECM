"""网易云数据采集器（M1 · v0.1）.

只用公开 Web 接口（未登录可用）；所有请求带礼貌间隔与重试。
多入口：单曲搜索（分页扫穿）/ 专辑搜索 / 歌手搜索 / 歌手热歌 / 专辑全曲 / 歌单全量。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://music.163.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://music.163.com/",
}
SLEEP = 1.2  # 礼貌间隔（秒）
_log = logging.getLogger(__name__)

try:  # 探针是旁路：导不进也不能影响采集
    from . import probe as _probe_mod
except Exception:  # noqa: BLE001
    _probe_mod = None


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _probe(endpoint: str, *, ok: bool, ms: int = 0, code: str = "",
           risk: bool = False) -> None:
    """落一条探针：只记路径不记查询串（歌名不进库）；异常全吞，不改调用方行为。"""
    if _probe_mod is None:
        return
    try:
        _probe_mod.record("netease", endpoint, ok=ok, code=code, ms=ms, risk=risk)
    except Exception:  # noqa: BLE001
        pass


def _exc_code(exc: Exception | None) -> str:
    """HTTP 错误取状态码，其它取异常名。"""
    if exc is None:
        return "none"
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, int) else type(exc).__name__


def _request(url: str, data: bytes | None = None, tries: int = 3, timeout: int = 20) -> dict:
    if not url.startswith(("https://", "http://")):  # 仅允许 http(s)
        raise ValueError(f"unsupported url scheme: {url!r}")
    endpoint = urllib.parse.urlparse(url).path or "/"  # 只记路径，搜索词不入库
    t0 = time.perf_counter()
    last: Exception | None = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 —— scheme 已白名单
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            _probe(endpoint, ok=True, ms=_elapsed_ms(t0))
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403, 404, 405):  # 4xx 不会自愈，立即交给上层（如备用接口）
                _probe(endpoint, ok=False, code=str(exc.code), ms=_elapsed_ms(t0),
                       risk=exc.code in (403, 412))
                raise
            last = exc
            time.sleep(2.0)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2.0)
    _probe(endpoint, ok=False, code=_exc_code(last), ms=_elapsed_ms(t0),
           risk=getattr(last, "code", None) == 412)
    raise RuntimeError(f"request failed: {url} ({last!r})")


_SEARCH_KEYS = {1: "songs", 10: "albums", 100: "artists",
                1000: "playlists", 1006: "songs", 1009: "djRadios"}


def _search(query: str, kind: int, *, pages: int = 1, limit: int = 100) -> list:
    out: list = []
    for page in range(pages):
        offset = page * limit
        url = (f"{BASE}/api/search/get/web?csrf_token=&s={urllib.parse.quote(query)}"
               f"&type={kind}&offset={offset}&total=true&limit={limit}")
        data = _request(url)
        result = data.get("result") or {}
        batch = result.get(_SEARCH_KEYS[kind]) or []
        out.extend(batch)
        if len(batch) < limit:
            break
        if page != pages - 1:
            time.sleep(SLEEP)
    return out


def _cloudsearch(query: str, kind: int, *, pages: int = 1, limit: int = 100) -> list:
    """备用搜索接口（cloudsearch/pc，POST）。主接口失效时顶上。"""
    out: list = []
    for page in range(pages):
        offset = page * limit
        body = urllib.parse.urlencode({
            "s": query, "type": kind, "limit": limit, "offset": offset, "total": "true",
        }).encode()
        data = _request(f"{BASE}/api/cloudsearch/pc", data=body)
        result = data.get("result") or {}
        batch = result.get(_SEARCH_KEYS[kind]) or []
        out.extend(batch)
        if len(batch) < limit:
            break
        if page != pages - 1:
            time.sleep(SLEEP)
    return out


def _search_any(query: str, kind: int, *, pages: int = 1, limit: int = 100) -> list:
    """搜索：主接口 → 备用接口 自动切换。

    实测主接口（search/get/web）会整段失效（HTTP 405 / 空结果）；
    备用接口返回结构兼容（歌曲有 ar/al 与 artists/album 两种写法，norm_song 均已兼容）。
    """
    try:
        out = _search(query, kind, pages=pages, limit=limit)
        if out:
            return out
    except Exception as exc:  # noqa: BLE001
        _log.warning("主搜索接口失败，切备用 cloudsearch: %r", exc)
    return _cloudsearch(query, kind, pages=pages, limit=limit)


def search_songs(query: str, pages: int = 3) -> list:
    """单曲搜索（分页扫穿——官方把很多条目埋在很后面，必须扫到底）。"""
    return _search_any(query, 1, pages=pages)


def search_albums(query: str, pages: int = 1) -> list:
    return _search_any(query, 10, pages=pages)


def search_artists(query: str, pages: int = 1) -> list:
    return _search_any(query, 100, pages=pages)


def search_lyric(query: str, pages: int = 1) -> list:
    """歌词搜索（type=1006）：返回歌词里包含关键词的歌曲（实测 cloudsearch 可用）。"""
    return _search_any(query, 1006, pages=pages)


def search_playlists(query: str, pages: int = 1) -> list:
    return _search_any(query, 1000, pages=pages)


def search_radios(query: str, pages: int = 1) -> list:
    """播客/电台搜索（type=1009）。"""
    return _search_any(query, 1009, pages=pages)


def song_lyric(song_id: int) -> tuple[str, str]:
    """取歌词：返回 (原文 LRC, 翻译 LRC)。"""
    data = _request(f"{BASE}/api/song/lyric?id={song_id}&lv=-1&kv=-1&tv=-1")
    lrc = ((data.get("lrc") or {}).get("lyric")) or ""
    tlyric = ((data.get("tlyric") or {}).get("lyric")) or ""
    return lrc, tlyric


def artist_hot_songs(artist_id: int) -> list:
    data = _request(f"{BASE}/api/artist/{artist_id}")
    return data.get("hotSongs") or []


def artist_all_songs(artist_id: int, pages: int = 2, limit: int = 200) -> list:
    """歌手全曲（/api/v2/artist/songs，分页）。

    实测可覆盖：单曲版 / 专辑版 / OST 版 / TV Size 等全部收录版本——
    比 hotSongs（50 首）更全，是被埋原唱的关键回收通道。
    """
    out: list = []
    for page in range(pages):
        offset = page * limit
        data = _request(f"{BASE}/api/v2/artist/songs?id={artist_id}&limit={limit}&offset={offset}")
        songs = data.get("songs") or []
        out.extend(songs)
        if len(songs) < limit or data.get("more") is False:
            break
        if page != pages - 1:
            time.sleep(SLEEP)
    return out


def album_songs(album_id: int) -> list:
    """专辑全曲目。

    注意：`/api/album` 在反爬状态下会返回 code!=200（实测 -462），
    此时兜底走 `/api/v1/album`（返回结构里 songs 在顶层）。
    """
    data = _request(f"{BASE}/api/album/{album_id}")
    if data.get("code") != 200:
        data = _request(f"{BASE}/api/v1/album/{album_id}")
    album = data.get("album") or {}
    return album.get("songs") or data.get("songs") or []


def playlist_track_ids(playlist_id: int) -> tuple[str, list]:
    """公开歌单：返回 (歌单名, 全部歌曲 id)。"""
    data = _request(f"{BASE}/api/v6/playlist/detail?id={playlist_id}")
    pl = data.get("playlist") or {}
    name = pl.get("name") or ""
    ids = [t.get("id") for t in (pl.get("trackIds") or []) if t.get("id")]
    if not ids:
        ids = [t.get("id") for t in (pl.get("tracks") or []) if t.get("id")]
    return name, ids


def songs_detail(ids: list, chunk: int = 100) -> list:
    out: list = []
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        q = urllib.parse.quote(json.dumps(part))
        data = _request(f"{BASE}/api/song/detail?ids={q}")
        out.extend(data.get("songs") or [])
        if i + chunk < len(ids):
            time.sleep(SLEEP)
    return out


def song_privileges(ids: list, chunk: int = 100, timeout: int = 12) -> dict:
    """批量取每首歌的最高可用音质和付费标记。

    数据来自 v3 详情的 privileges 数组（一条请求最多 100 首）：
      {"maxBrLevel": "jymaster|hires|lossless|exhigh|higher|standard", "fee": 0/1/4/8, "st": 0}
    返回 {song_id: {"level": str, "fee": int, "st": int}}；失败就返回已拿到的部分，调用方按未知处理。
    """
    out: dict = {}
    clean = [int(i) for i in ids if i]
    for i in range(0, len(clean), chunk):
        part = clean[i:i + chunk]
        c = urllib.parse.quote(json.dumps([{"id": x} for x in part]))
        try:
            data = _request(f"{BASE}/api/v3/song/detail?c={c}", timeout=timeout)
        except Exception:
            continue
        songs = {s.get("id"): s for s in (data.get("songs") or [])}
        for pr in (data.get("privileges") or []):
            sid = pr.get("id")
            if not sid:
                continue
            lvl = (pr.get("maxBrLevel") or "").strip().lower()
            if lvl in ("", "none", "null"):
                # 老接口可能不回 maxBrLevel：退一步看有没有码率
                lvl = "standard" if int(pr.get("maxbr") or 0) > 0 else "none"
            song = songs.get(sid) or {}
            # originCoverType: 2=改编/翻唱；originSongSimpleData 有值时会写明原曲与原作者
            osd = song.get("originSongSimpleData") or None
            oart = ""
            if osd:
                oart = "/".join(a.get("name", "") for a in (osd.get("artists") or []) if a.get("name"))
            out[sid] = {"level": lvl, "fee": int(pr.get("fee") or 0), "st": int(pr.get("st") or 0),
                        "cover_type": int(song.get("originCoverType") or 0),
                        "orig_artists": oart}
        if i + chunk < len(clean):
            time.sleep(SLEEP)
    return out


def norm_song(song: dict, origin: str) -> dict:
    """统一记录格式。origin: search / album / artist / playlist"""
    artists = song.get("artists") or song.get("ar") or []
    album = song.get("album") or song.get("al") or {}
    return {
        "source": "netease",
        "id": song.get("id"),
        "title": song.get("name") or "",
        "artists": "/".join(a.get("name", "") for a in artists if a.get("name")),
        "album": album.get("name") or "",
        "album_id": album.get("id"),
        "cover": album.get("picUrl") or "",
        "duration": song.get("duration") or song.get("dt") or 0,
        # 发行时间（毫秒）：搜索/专辑/歌手接口都随 album 带回，本地过滤「发行时间」用
        "publish_time": album.get("publishTime") or song.get("publishTime") or 0,
        "fee": song.get("fee"),
        "origin": origin,
    }
