"""本地索引：SQLite 存储 + 关键词搜索。

v0.1 用 LIKE 扫查（当前规模毫秒级，零边缘用例）；
数据量上来后再切 FTS5 + 分词（接口保持不变）。

E4-FTS5 P2a：只建「影子索引」——songs_fts 是与 songs 同步维护的副表，
search() 仍然走纯 LIKE，一个字节都没改。FTS 是否可用由 self.fts_ok 标记。

E4-FTS5 P2b：search() 双路径。开关（Index(use_fts=...) 或环境变量
USABLE_SEARCH_FTS）**默认关**；打开且查询 >= 3 字时才走 MATCH，否则仍走 LIKE。
trigram 对 < 3 字符没有 token（命中恒为 0），所以短查询必须回退 LIKE；
MATCH 任何 OperationalError 也自动回退，两条路径的返回契约完全一致。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

_log = logging.getLogger(__name__)

# 库结构版本（PRAGMA user_version）。0 = 只有 v0 列结构；1 = 已建 FTS 影子索引。
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    source   TEXT NOT NULL DEFAULT 'netease',
    id       INTEGER NOT NULL,
    title    TEXT NOT NULL,
    artists  TEXT DEFAULT '',
    album    TEXT DEFAULT '',
    album_id INTEGER,
    cover    TEXT DEFAULT '',
    duration INTEGER DEFAULT 0,
    fee      INTEGER,
    origin   TEXT DEFAULT '',
    mine     INTEGER DEFAULT 0,
    fetched_at INTEGER,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title);
CREATE TABLE IF NOT EXISTS lyrics (
    song_id    INTEGER PRIMARY KEY,
    lrc        TEXT DEFAULT '',
    fetched_at INTEGER
);
"""

_MIGRATIONS = (
    "ALTER TABLE songs ADD COLUMN mine INTEGER DEFAULT 0",
    "ALTER TABLE songs ADD COLUMN cover TEXT DEFAULT ''",
)

# ---------------------------------------------------------------- FTS 影子索引（v1）
# external content：索引本身不存第二份正文，查询时按 rowid 回 songs 取列。
_FTS_TABLE = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS songs_fts USING fts5("
    "title, artists, album, "
    "content='songs', content_rowid='rowid', tokenize='trigram')"
)

# 三个同步触发器。注意：
#  - upsert_songs 的 ON CONFLICT(source,id) DO UPDATE 走的是 UPDATE 路径，
#    rowid 不变，所以 songs_au 必须「先 delete 旧值、再 insert 新值」；
#    漏掉 delete 会让旧标题永远留在索引里（索引漂移，最难查的坑）。
#  - external content 的 'delete' 命令必须带全 old 的索引列，否则索引里会残留。
_FTS_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS songs_ai AFTER INSERT ON songs BEGIN "
    "INSERT INTO songs_fts(rowid, title, artists, album) "
    "VALUES (new.rowid, new.title, new.artists, new.album); END",
    "CREATE TRIGGER IF NOT EXISTS songs_au AFTER UPDATE ON songs BEGIN "
    "INSERT INTO songs_fts(songs_fts, rowid, title, artists, album) "
    "VALUES ('delete', old.rowid, old.title, old.artists, old.album); "
    "INSERT INTO songs_fts(rowid, title, artists, album) "
    "VALUES (new.rowid, new.title, new.artists, new.album); END",
    "CREATE TRIGGER IF NOT EXISTS songs_ad AFTER DELETE ON songs BEGIN "
    "INSERT INTO songs_fts(songs_fts, rowid, title, artists, album) "
    "VALUES ('delete', old.rowid, old.title, old.artists, old.album); END",
)

LYRIC_TTL_POSITIVE = 90 * 24 * 3600  # 正缓存：真拿到歌词，保留 90 天
LYRIC_TTL_NEGATIVE = 24 * 3600      # 负缓存：空串（无歌词/抓取失败），只保留 1 天再重试
LYRIC_MAX_ROWS = 5000               # lyrics 表行数上限，超出按 fetched_at 最旧先淘汰

# ---------------------------------------------------------------- 查询双路径（P2b）
# 两条分支的列顺序 / 排序必须逐字一致，否则调用方拿到的 dict 顺序会飘。
_SELECT_COLS = ("source, id, title, artists, album, album_id, cover, duration, fee, origin, mine")
_LIKE_SQL = (
    f"SELECT {_SELECT_COLS} FROM songs "
    "WHERE title LIKE ? ESCAPE '\\' OR artists LIKE ? ESCAPE '\\' OR album LIKE ? ESCAPE '\\' "
    "ORDER BY fetched_at DESC LIMIT ?"
)
# external content 表：MATCH 只定位 rowid，列仍从 songs 取（s. 前缀避免 id 等列名歧义）。
_FTS_SQL = (
    f"SELECT s.source, s.id, s.title, s.artists, s.album, s.album_id, s.cover, "
    "s.duration, s.fee, s.origin, s.mine "
    "FROM songs_fts JOIN songs s ON s.rowid = songs_fts.rowid "
    "WHERE songs_fts MATCH ? "
    "ORDER BY s.fetched_at DESC LIMIT ?"
)

# trigram 一个 token 至少 3 个字符；更短的查询在索引里根本不存在。
_FTS_MIN_CHARS = 3

_TRUE_FLAGS = ("1", "true", "on")


def _fts_phrase(q: str) -> str:
    """把整条查询包成 FTS5 短语：原样匹配，顺便免疫 * : OR NEAR( 这类语法。

    FTS5 规则：短语里的双引号写成两个双引号（""）转义。
    """
    return '"' + q.replace('"', '""') + '"'


def _env_fts_flag() -> bool:
    """USABLE_SEARCH_FTS=1/true/on 才开；其它（含未设）一律关。"""
    return os.environ.get("USABLE_SEARCH_FTS", "").strip().lower() in _TRUE_FLAGS


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Index:
    def __init__(self, path: str = "data/index.sqlite3", use_fts: bool | None = None):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # None = 没表态 → 看环境变量；环境变量没设也关（默认负载永远是 LIKE）
        self.use_fts = _env_fts_flag() if use_fts is None else bool(use_fts)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.fts_ok = False
        self._migrate()

    # ------------------------------------------------------------ 迁移
    def _migrate(self) -> None:
        """按 PRAGMA user_version 做版本化迁移，全程幂等。

        v0 → v1：补 mine/cover 列（老库可能缺），再建 FTS 影子表 + 三触发器。
        已有 v1 的库只做探测，不重复建对象。
        """
        version = self.conn.execute("PRAGMA user_version").fetchone()[0] or 0
        if version >= SCHEMA_VERSION:
            self.fts_ok = self._fts_probe()
            return

        # --- v0：列补齐（旧逻辑保留，重复列照样吞掉，老库不能报错）
        for ddl in _MIGRATIONS:
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

        # --- v1：FTS 影子表 + 同步触发器；失败只降级，不抛异常
        try:
            self.conn.execute(_FTS_TABLE)
            for ddl in _FTS_TRIGGERS:
                self.conn.execute(ddl)
            self.conn.commit()
        except sqlite3.OperationalError as exc:
            # 老环境没有 fts5/trigram：回滚掉半成品，保持 v0，纯 LIKE 继续可用
            self.conn.rollback()
            self.fts_ok = False
            _log.warning("FTS5 影子索引不可用，降级为纯 LIKE：%s", exc)
            return

        # 影子表刚建，已有数据还没有进索引 —— 补一次 rebuild
        if self.conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]:
            try:
                self.conn.execute("INSERT INTO songs_fts(songs_fts) VALUES('rebuild')")
            except sqlite3.OperationalError as exc:
                _log.warning("FTS5 首次 rebuild 失败：%s", exc)

        # 全部成功才推版本号：失败时留 0，下次打开会重试（自愈）
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()
        self.fts_ok = True

    def _fts_probe(self) -> bool:
        """探测已迁移库上的影子索引还在不在、能不能用。"""
        try:
            self.conn.execute("SELECT rowid FROM songs_fts LIMIT 1").fetchall()
            return True
        except sqlite3.OperationalError as exc:
            _log.warning("FTS5 影子索引不可用，降级为纯 LIKE：%s", exc)
            return False

    def rebuild_fts(self) -> bool:
        """从 songs 重建整张影子索引（修漂移 / 首次灌数据）。不可用或失败返回 False。"""
        if not getattr(self, "fts_ok", False):
            return False
        try:
            self.conn.execute("INSERT INTO songs_fts(songs_fts) VALUES('rebuild')")
            self.conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            _log.warning("FTS5 rebuild 失败：%s", exc)
            return False

    def upsert_songs(self, records: list) -> int:
        now = int(time.time())
        rows = [
            (r["source"], r["id"], r["title"], r.get("artists", ""), r.get("album", ""),
             r.get("album_id"), r.get("cover", ""), r.get("duration") or 0, r.get("fee"),
             r.get("origin", ""), int(r.get("mine") or 0), now)
            for r in records if r.get("id")
        ]
        self.conn.executemany(
            "INSERT INTO songs(source,id,title,artists,album,album_id,cover,duration,fee,origin,mine,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source,id) DO UPDATE SET title=excluded.title, artists=excluded.artists, "
            "album=excluded.album, album_id=excluded.album_id, "
            "cover=COALESCE(NULLIF(excluded.cover,''), songs.cover), "
            "duration=excluded.duration, fee=excluded.fee, origin=excluded.origin, "
            "mine=MAX(songs.mine, excluded.mine), fetched_at=excluded.fetched_at",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def mark_mine(self, ids: list) -> int:
        """把一批歌曲 id 标进「我的歌单/收藏」（mine=1）。"""
        self.conn.executemany(
            "UPDATE songs SET mine=1 WHERE source='netease' AND id=?", [(int(i),) for i in ids])
        self.conn.commit()
        return len(ids)

    def set_cover(self, song_id: int, cover: str) -> None:
        """补写封面（仅当当前缺口时）。"""
        if not cover:
            return
        self.conn.execute(
            "UPDATE songs SET cover=? WHERE source='netease' AND id=? AND (cover IS NULL OR cover='')",
            (cover, int(song_id)))
        self.conn.commit()

    def search(self, q: str, limit: int = 500) -> list:
        """关键词搜索：开关开 + 查询 >= 3 字 + 索引可用时走 FTS5，否则 LIKE。

        两条路径返回同一契约（_SELECT_COLS 顺序、fetched_at DESC、LIMIT）。
        MATCH 出任何 OperationalError 都静默回退 LIKE —— 搜索降级，绝不 500。
        """
        if self.use_fts and self.fts_ok and len(q.strip()) >= _FTS_MIN_CHARS:
            try:
                # 短语查询：整条串当一个子串找，与 LIKE 的语义对齐；
                # 参数化绑定，绝不把用户输入拼进 SQL。
                cur = self.conn.execute(_FTS_SQL, (_fts_phrase(q), limit))
                return [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError as exc:
                _log.warning("FTS5 MATCH 查询失败，本次回退 LIKE：%s", exc)
        return self._search_like(q, limit)

    def _search_like(self, q: str, limit: int = 500) -> list:
        """基准路径（P2a 及之前唯一路径）：大小写不敏感的子串扫查。"""
        like = f"%{_escape_like(q)}%"
        cur = self.conn.execute(_LIKE_SQL, (like, like, like, limit))
        return [dict(r) for r in cur.fetchall()]

    def get_lyric(self, song_id: int):
        """读歌词缓存：空串走负 TTL、非空走正 TTL；过期按未命中返回 None。

        fetched_at 缺失（NULL/0，老库）一律视为过期，允许重抓。
        """
        row = self.conn.execute(
            "SELECT lrc, fetched_at FROM lyrics WHERE song_id=?", (int(song_id),)).fetchone()
        if row is None:
            return None
        lrc = row["lrc"] or ""
        fetched_at = row["fetched_at"] or 0
        if not fetched_at:  # 没有时间戳 → 无法判断新鲜度，按过期处理
            return None
        age = int(time.time()) - int(fetched_at)
        if lrc:
            return None if age > LYRIC_TTL_POSITIVE else lrc
        return None if age > LYRIC_TTL_NEGATIVE else lrc

    def set_lyric(self, song_id: int, lrc: str) -> None:
        self.conn.execute(
            "INSERT INTO lyrics(song_id, lrc, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(song_id) DO UPDATE SET lrc=excluded.lrc, fetched_at=excluded.fetched_at",
            (int(song_id), lrc or "", int(time.time())))
        self.conn.commit()

    def prune_lyrics(self, max_rows: int | None = None) -> int:
        """歌词缓存容量清理：超上限按 fetched_at 最旧先删，返回删除行数。

        NULL/0 时间戳视为最旧；未超上限直接返回 0（不执行 DELETE），可重复调用。
        """
        limit = LYRIC_MAX_ROWS if max_rows is None else int(max_rows)
        limit = max(0, limit)
        total = self.conn.execute("SELECT COUNT(*) FROM lyrics").fetchone()[0]
        extra = total - limit
        if extra <= 0:
            return 0
        cur = self.conn.execute(
            "DELETE FROM lyrics WHERE song_id IN ("
            "  SELECT song_id FROM lyrics ORDER BY COALESCE(fetched_at, 0) ASC, song_id ASC LIMIT ?"
            ")",
            (extra,))
        self.conn.commit()
        deleted = cur.rowcount
        return deleted if isinstance(deleted, int) and deleted >= 0 else extra

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
