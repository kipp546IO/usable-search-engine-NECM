"""搜索结果治理层（反推送机制）.

定位：**每一次搜索都必须经过的独立治理层**——不是某个模式的临时补丁。
规则按顺序应用，命中即剔除并记账：
  R1 push   纯推送位：平台 hit_columns 为空（连分词都没命中的推荐卡）
  R2 live   直播内嵌卡：搜索结果里插播的直播间卡
  R3 phrase 全词命中：标题/简介/标签必须含完整关键词（match=std）；
            match=std2 只看标题/简介（不算标签命中）；strict 时只看标题
口径：
  - match=std / std2 / strict → 治理开启（默认 std）
  - match 为空（宽松） → 治理整体关闭（对照口径，结果=平台原样）
账本：data/governance.sqlite3（每次实际抓取一行，供 B5 定量核查与趋势观察）
扩展：新规则加进 RULES 并在 Governor.check 里判定；软广/付费推广等待核查后新增。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

from . import fuzzy as fuzzy_mod

DB_DEFAULT = "data/governance.sqlite3"
_log = logging.getLogger(__name__)

RULES = [
    {"id": "push", "name": "纯推送位（零命中推荐卡）"},
    {"id": "live", "name": "直播内嵌卡"},
    {"id": "phrase", "name": "全词命中"},
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, source TEXT, q TEXT, mode TEXT, params TEXT,
    scanned INTEGER, kept INTEGER, dropped TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_q ON runs(q, ts);
"""


class Governor:
    """单个搜索请求的治理器：逐条判定 + 计数。"""

    def __init__(self, q: str, match: str = "std", source: str = "bilibili",
                 fuzzy: bool | None = None):
        self.q = (q or "").lower()
        self.match = match or ""
        self.source = source
        self.enabled = self.match in ("std", "std2", "strict")  # 宽松 = 治理关闭（对照口径）
        # 模糊命中判据（X3-3）：只加保留、不新增剔除；显式传 bool 优先于环境开关
        self.fuzzy = fuzzy_mod.enabled() if fuzzy is None else bool(fuzzy)
        self.fuzzy_kept = 0
        self.dropped = {r["id"]: 0 for r in RULES}
        self.kept = 0

    def check(self, v: dict) -> str | None:
        """逐条治理：返回剔除原因（规则 id）；保留返回 None。"""
        if not self.enabled:
            self.kept += 1
            return None
        # R1 纯推送位：平台自己都没标命中字段——连分词都没碰到，纯塞进来的
        if not (v.get("hit_columns") or []):
            self.dropped["push"] += 1
            return "push"
        # R2 直播内嵌卡
        if v.get("is_live_room_inline"):
            self.dropped["live"] += 1
            return "live"
        # R3 全词命中
        ql = self.q
        if self.match == "strict":
            ok = ql in (v.get("title") or "").lower()
        elif self.match == "std2":
            # std2：只看标题/简介——标签命中的卡片用户看不见依据，不认
            ok = (ql in (v.get("title") or "").lower()
                  or ql in (v.get("desc") or "").lower())
        else:
            ok = (ql in (v.get("title") or "").lower()
                  or ql in (v.get("desc") or "").lower()
                  or ql in (v.get("tag") or "").lower())
        if not ok:
            # R3 模糊分支（X3-3）：同音/错字的「近似命中」只判保留并标注，绝不新增剔除。
            if self.fuzzy and self._fuzzy_hit(v):
                v["fuzzy"] = 1
                self.fuzzy_kept += 1
                self.kept += 1
                return None
            self.dropped["phrase"] += 1
            return "phrase"
        self.kept += 1
        return None

    def _fuzzy_hit(self, v: dict) -> bool:
        """词与标题/简介/标签的最优滑窗相似度达到阈值即算模糊命中。"""
        thr = fuzzy_mod.threshold()
        return max(fuzzy_mod.score(self.q, v.get("title") or ""),
                   fuzzy_mod.score(self.q, v.get("desc") or ""),
                   fuzzy_mod.score(self.q, v.get("tag") or "")) >= thr

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "match": self.match,
            "kept": self.kept,
            "fuzzy": self.fuzzy,
            "fuzzy_kept": self.fuzzy_kept,
            "dropped": dict(self.dropped),
            "dropped_total": sum(self.dropped.values()),
            "rules": [dict(r, dropped=self.dropped[r["id"]]) for r in RULES],
        }


def log_run(db_path: str, source: str, q: str, mode: str, params: dict,
            scanned: int, gov_summary: dict) -> None:
    """治理账本：每次实际抓取落一行（失败不影响搜索本身）。"""
    try:
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO runs(ts, source, q, mode, params, scanned, kept, dropped) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (int(time.time()), source, q, mode, json.dumps(params, ensure_ascii=False),
             int(scanned), int(gov_summary.get("kept") or 0),
             json.dumps(gov_summary.get("dropped") or {}, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        _log.warning("治理账本写入失败（不影响搜索本身）: %r", exc)


def stats(db_path: str, q: str = "", limit: int = 30) -> dict:
    """账本汇总：最近若干次 + 按规则合计（供 /api/governance 与 B5 核查）。"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        if q:
            rows = conn.execute(
                "SELECT ts, q, mode, scanned, kept, dropped FROM runs "
                "WHERE q=? ORDER BY id DESC LIMIT ?", (q, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, q, mode, scanned, kept, dropped FROM runs "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        agg: dict = {}
        for r in rows:
            try:
                d = json.loads(r["dropped"] or "{}")
            except Exception:  # noqa: BLE001
                d = {}
            for k, val in d.items():
                agg[k] = agg.get(k, 0) + int(val or 0)
        conn.close()
        return {"runs": [dict(r) for r in rows], "agg": agg}
    except Exception:  # noqa: BLE001
        return {"runs": [], "agg": {}}
