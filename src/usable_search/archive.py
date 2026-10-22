"""看过的视频档案 ＋ 下架侦测（B15 第一步）.

每次 B站搜索把见过的视频元数据记进本地库（bvid/标题/UP主/封面/发布时间/播放/弹幕/首见/末见）；
复查时用 `x/web-interface/view` 判定状态：ok / gone(稿件不存在) / hidden(不可见) / error。

边界：只记元数据，不落视频内容；库在 data/archive.sqlite3（gitignore，不入仓）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

from . import bilibili

DB_DEFAULT = "data/archive.sqlite3"
_log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    bvid       TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    author     TEXT NOT NULL DEFAULT '',
    mid        INTEGER NOT NULL DEFAULT 0,
    cover      TEXT NOT NULL DEFAULT '',
    pubdate    INTEGER NOT NULL DEFAULT 0,
    play       INTEGER NOT NULL DEFAULT 0,
    danmaku    INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL DEFAULT 0,
    last_seen  INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'unknown',
    checked_at INTEGER NOT NULL DEFAULT 0,
    code       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS seen_status ON seen(status);
CREATE INDEX IF NOT EXISTS seen_checked ON seen(checked_at);
"""

_ZERO = {"total": 0, "ok": 0, "gone": 0, "hidden": 0, "error": 0, "unknown": 0, "last_check": 0}


def _conn(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def remember(videos: list, db_path: str = DB_DEFAULT) -> int:
    """把一次搜索里见过的视频记进档案（已存在则刷新末见时间与播放/弹幕数）。"""
    now = int(time.time())
    rows = [
        (v.get("bvid"), v.get("title") or "", v.get("author") or "", int(v.get("mid") or 0),
         v.get("cover") or "", int(v.get("pubdate") or 0), int(v.get("play") or 0),
         int(v.get("danmaku") or 0), now, now)
        for v in videos if v.get("bvid")
    ]
    if not rows:
        return 0
    try:
        conn = _conn(db_path)
        conn.executemany(
            """INSERT INTO seen(bvid,title,author,mid,cover,pubdate,play,danmaku,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bvid) DO UPDATE SET
                 title=excluded.title, author=excluded.author, cover=excluded.cover,
                 play=excluded.play, danmaku=excluded.danmaku, last_seen=excluded.last_seen""",
            rows,
        )
        conn.commit()
        conn.close()
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        _log.warning("档案写入失败（不影响搜索）: %r", exc)
        return 0


def stats(db_path: str = DB_DEFAULT) -> dict:
    out = dict(_ZERO)
    try:
        conn = _conn(db_path)
        for status, n in conn.execute("SELECT status, COUNT(*) FROM seen GROUP BY status"):
            key = status if status in out else "unknown"
            out[key] = int(n)
            out["total"] += int(n)
        row = conn.execute("SELECT MAX(checked_at) FROM seen").fetchone()
        out["last_check"] = int(row[0] or 0)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        _log.warning("档案统计失败: %r", exc)
    return out


def query(q: str = "", status: str = "", limit: int = 50, db_path: str = DB_DEFAULT) -> list[dict]:
    """按关键词/状态查档案；下架与不可见的排前面，其余按末见时间倒序。"""
    sql = ("SELECT bvid,title,author,mid,cover,pubdate,play,danmaku,first_seen,last_seen,"
           "status,checked_at,code FROM seen")
    wheres, args = [], []
    if q:
        wheres.append("(title LIKE ? OR author LIKE ? OR bvid = ?)")
        args += [f"%{q}%", f"%{q}%", q]
    if status:
        wheres.append("status = ?")
        args.append(status)
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += (" ORDER BY CASE status WHEN 'gone' THEN 0 WHEN 'hidden' THEN 1 ELSE 2 END,"
            " last_seen DESC LIMIT ?")
    args.append(max(0, int(limit or 0)))
    try:
        conn = _conn(db_path)
        cols = ["bvid", "title", "author", "mid", "cover", "pubdate", "play", "danmaku",
                "first_seen", "last_seen", "status", "checked_at", "code"]
        rows = [dict(zip(cols, r)) for r in conn.execute(sql, args)]
        conn.close()
        return rows
    except Exception as exc:  # noqa: BLE001
        _log.warning("档案查询失败: %r", exc)
        return []


def recheck(limit: int = 10, q: str = "", status: str = "", db_path: str = DB_DEFAULT) -> dict:
    """挑最久没复查的条目逐条验活（间隔 0.8 秒，礼貌访问）。

    返回 {"checked": n, "items": [状态有变化的条目], "stats": {...}}。
    真下架（gone）/不可见（hidden）会一直在档案里留着，供回看时追溯。
    """
    limit = max(1, min(50, int(limit or 10)))
    sql = "SELECT bvid,title,status FROM seen"
    wheres, args = [], []
    if q:
        wheres.append("(title LIKE ? OR author LIKE ? OR bvid = ?)")
        args += [f"%{q}%", f"%{q}%", q]
    if status:
        wheres.append("status = ?")
        args.append(status)
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY checked_at ASC, last_seen DESC LIMIT ?"
    args.append(limit)
    changed: list[dict] = []
    checked = 0
    try:
        conn = _conn(db_path)
        picks = list(conn.execute(sql, args))
        for bvid, title, old_status in picks:
            try:
                st = bilibili.video_status(bvid)
            except Exception as exc:  # noqa: BLE001
                _log.warning("复查失败 %s: %r", bvid, exc)
                st = {"bvid": bvid, "status": "error", "code": -1}
            checked += 1
            conn.execute(
                "UPDATE seen SET status=?, checked_at=?, code=? WHERE bvid=?",
                (st.get("status") or "error", int(time.time()), int(st.get("code") or 0), bvid),
            )
            if st.get("status") in ("gone", "hidden"):
                changed.append({"bvid": bvid, "title": title, "status": st["status"],
                                "code": st.get("code") or 0, "was": old_status})
            time.sleep(0.8)
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        _log.warning("复查中断: %r", exc)
    return {"checked": checked, "items": changed, "stats": stats(db_path)}
