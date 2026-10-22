"""E4-AUDIT-2 · 真库 LIKE vs MATCH 结果集对比（只读副本，零新依赖，不改主流程）。

目的：给 E4-P3「能否打开 USABLE_SEARCH_FTS」一个直接证据——同一批真实查询下，
LIKE 路径与 FTS5(trigram) 路径的**结果集**是否等价。

做法：
  1. 真库用 `mode=ro` 打开 + `backup()` 复制到临时副本（真库一字节不动，副本随便折腾）。
  2. 50 条查询从**真库数据**里按固定种子采样（标题/标题中段/艺人/专辑/专辑片段/2 字片段），
     另加 6 条手工边界（大小写、`*`、空格、单字、`%`、引号），合计 50。
  3. 每条查询跑两条路径：
     - LIKE：`Index.search(q, limit=1e6)`（use_fts=False）
     - MATCH：`Index.search(q, limit=1e6)`（use_fts=True）——但 search() 对 <3 字会回退 LIKE，
       所以额外用**裸 SQL**直接跑一次 `songs_fts MATCH`，把「代码门」和「索引语义」分开看。
  4. 比对：全量 rowid 集（LIKE 全集 vs MATCH 全集）、以及线上真正会返回的 top-500 集合与顺序。
     差异行逐条回查正文，判断是「真丢结果」还是「词法差异」。
  5. 计时：每条查询 LIKE/MATCH 各跑 N 次取 p50/p95（热缓存）。

只读保证：源库 `mode=ro`；副本用完即删。输出只含查询串与条数/rowid 摘要
（查询串取自本机索引，落盘前用 `--out` 时可关闭），不含任何 UID/接口数据。

用法（仓库根目录）：
    PYTHONUTF8=1 python tools/compare_fts_like.py
    PYTHONUTF8=1 python tools/compare_fts_like.py --limit 50 --reps 5 --out cmp.json

退出码：0 = 50 条全部等价（>=3 字查询全量集合与 top-500 集合都相等）；1 = 有差异（照样出数字）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from usable_search import store as store_mod  # noqa: E402
from usable_search.store import Index  # noqa: E402

SCHEMA_VERSION = 1
HARD_LIMIT = 1_000_000

_COLS = ("title", "artists", "album")


def _like_pattern(q: str) -> str:
    return f"%{store_mod._escape_like(q)}%"


def _fts_phrase(q: str) -> str:
    return store_mod._fts_phrase(q)


def copy_db_readonly(src: Path) -> Path:
    """mode=ro 打开源库 + backup() 到临时文件；源库零写入。"""
    src_uri = f"file:{src.as_posix()}?mode=ro"
    tmp_dir = Path(tempfile.mkdtemp(prefix="e4_audit2_"))
    dst = tmp_dir / "index_copy.sqlite3"
    src_conn = sqlite3.connect(src_uri, uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return dst


def build_queries(conn: sqlite3.Connection, seed: int = 20261022) -> list[dict]:
    """从真库采样 44 条 + 6 条手工边界 = 50。全部确定性（固定 seed、固定排序）。"""
    rng = random.Random(seed)
    rows = conn.execute(
        "SELECT title, artists, album FROM songs WHERE title IS NOT NULL AND title <> '' "
        "ORDER BY rowid"
    ).fetchall()
    titles = [r["title"] for r in rows]
    artists = sorted({r["artists"] for r in rows if r["artists"]})
    albums = sorted({r["album"] for r in rows if r["album"]})

    def mid_frag(s: str, lo: int, hi: int) -> str | None:
        s = s.strip()
        if len(s) < lo:
            return None
        n = min(len(s), hi)
        start = 0 if n == len(s) else rng.randint(0, len(s) - n)
        return s[start:start + n]

    out: list[dict] = []

    def add(kind: str, q: str) -> None:
        q = q.strip()
        if q:
            out.append({"kind": kind, "q": q})

    for t in rng.sample(titles, 12):
        add("title_full", t)
    picked = 0
    for t in rng.sample(titles, 60):
        frag = mid_frag(t, 3, 6)
        if frag:
            add("title_mid", frag)
            picked += 1
        if picked >= 8:
            break
    for a in rng.sample(artists, 8):
        add("artist_full", a)
    for a in rng.sample(artists, 40):
        frag = mid_frag(a, 3, 5)
        if frag:
            add("artist_mid", frag)
        if sum(1 for x in out if x["kind"] == "artist_mid") >= 4:
            break
    for al in rng.sample(albums, 4):
        add("album_full", al)
    picked = 0
    for al in rng.sample(albums, 40):
        frag = mid_frag(al, 3, 5)
        if frag:
            add("album_mid", frag)
            picked += 1
        if picked >= 4:
            break
    picked = 0
    for t in rng.sample(titles, 40):
        t = t.strip()
        if len(t) >= 2:
            start = rng.randint(0, len(t) - 2)
            add("two_char", t[start:start + 2])
            picked += 1
        if picked >= 4:
            break

    hard = ["MyGO", "mygo", "DECO*27", "春 日影", "無", "%"]
    for q in hard:
        add("hard", q)

    # 去重（保序），不足 50 从标题里补
    seen, uniq = set(), []
    for item in out:
        if item["q"] not in seen:
            seen.add(item["q"])
            uniq.append(item)
    for t in rng.sample(titles, 200):
        if len(uniq) >= 50:
            break
        if t.strip() and t.strip() not in seen:
            seen.add(t.strip())
            uniq.append({"kind": "fill", "q": t.strip()})
    return uniq[:50]


def run_compare(db_copy: Path, queries: list[dict], reps: int) -> dict:
    log_capture: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):  # noqa: D401
            log_capture.append(record.getMessage())

    handler = _Cap()
    store_mod._log.addHandler(handler)
    store_mod._log.setLevel(logging.WARNING)

    idx = Index(str(db_copy), use_fts=True)
    assert idx.fts_ok, "副本上的 FTS 影子索引不可用"
    conn = idx.conn
    traced: list[str] = []
    conn.set_trace_callback(lambda s: traced.append(s) if "songs_fts" in s else None)

    results = []
    for item in queries:
        q = item["q"]
        like_pat = _like_pattern(q)
        phrase = _fts_phrase(q)

        # --- 全量集合（裸 SQL，避免 LIMIT 干扰）
        like_rows = conn.execute(
            f"SELECT rowid, {', '.join(_COLS)} FROM songs "
            f"WHERE title LIKE ? ESCAPE '\\' OR artists LIKE ? ESCAPE '\\' "
            f"OR album LIKE ? ESCAPE '\\'", (like_pat, like_pat, like_pat)).fetchall()
        like_set = {r["rowid"] for r in like_rows}

        match_err = None
        try:
            match_rows = conn.execute(
                f"SELECT s.rowid, s.{', s.'.join(_COLS)} FROM songs_fts "
                f"JOIN songs s ON s.rowid = songs_fts.rowid "
                f"WHERE songs_fts MATCH ?", (phrase,)).fetchall()
            match_set = {r["rowid"] for r in match_rows}
        except sqlite3.OperationalError as exc:
            match_err = str(exc)
            match_set = set()

        only_like = sorted(like_set - match_set)
        only_match = sorted(match_set - like_set)

        def _text(rowid: int) -> str:
            r = conn.execute(
                f"SELECT {', '.join(_COLS)} FROM songs WHERE rowid=?", (rowid,)).fetchone()
            return " | ".join(str(r[c] or "") for c in _COLS)

        only_like_detail = [{"rowid": rid, "text": _text(rid)} for rid in only_like[:5]]
        only_match_detail = [{"rowid": rid, "text": _text(rid)} for rid in only_match[:5]]

        # 差异归因：正文里到底有没有这个子串（ASCII 大小写不敏感，对齐 LIKE 语义）
        def _contains(text: str) -> bool:
            return q.lower() in text.lower() if q.isascii() else q in text

        only_like_explained = [d for d in only_like_detail if _contains(d["text"])]
        only_match_explained = [d for d in only_match_detail if _contains(d["text"])]

        # --- 线上真实路径（Index.search），top-500
        traced.clear()
        t0 = time.perf_counter()
        fast = idx.search(q, limit=HARD_LIMIT)[:500]
        t_fast_s = time.perf_counter() - t0
        fast_rowids = [r["id"] for r in fast]
        fast_used_fts = any("songs_fts" in s and "MATCH" in s for s in traced)
        fast_warn = list(log_capture)
        log_capture.clear()

        slow = idx._search_like(q, limit=500)
        slow_rowids = [r["id"] for r in slow]

        t_like, t_fts = [], []
        for _ in range(reps):
            s0 = time.perf_counter()
            idx._search_like(q, limit=500)
            t_like.append((time.perf_counter() - s0) * 1000)
            s1 = time.perf_counter()
            idx.conn.execute(
                f"SELECT s.rowid FROM songs_fts JOIN songs s ON s.rowid = songs_fts.rowid "
                f"WHERE songs_fts MATCH ? LIMIT 500", (phrase,)).fetchall()
            t_fts.append((time.perf_counter() - s1) * 1000)

        results.append({
            "q": q,
            "kind": item["kind"],
            "chars": len(q),
            "like_total": len(like_set),
            "match_total": len(match_set),
            "only_like": only_like,
            "only_match": only_match,
            "only_like_detail": only_like_detail,
            "only_match_detail": only_match_detail,
            "only_like_explained": len(only_like_explained),
            "only_match_explained": len(only_match_explained),
            "match_error": match_err,
            "top500_set_equal": set(fast_rowids) == set(slow_rowids),
            "top500_pos_equal": fast_rowids == slow_rowids,
            "top500_only_fts": sorted(set(fast_rowids) - set(slow_rowids)),
            "top500_only_like": sorted(set(slow_rowids) - set(fast_rowids)),
            "search_used_fts": fast_used_fts,
            "search_warnings": fast_warn,
            "ms_like_p50": _pct(t_like, 50),
            "ms_fts_p50": _pct(t_fts, 50),
            "ms_like_p95": _pct(t_like, 95),
            "ms_fts_p95": _pct(t_fts, 95),
        })

    probes = unicode_probes(conn)
    store_mod._log.removeHandler(handler)
    conn.close()
    return {"fts_ok": idx.fts_ok, "results": results, "unicode_probes": probes}


def unicode_probes(conn: sqlite3.Connection) -> list[dict]:
    """边界探针（不计入 50 条）：非 ASCII 大小写 / 全角半角 / 罗马字附加符号。

    LIKE 的内建大小写折叠只覆盖 ASCII；trigram 分词器默认走 Unicode 折叠。
    这一节就是去证实/证伪这条差异，给「开 flag 会不会多出结果」一个实测口径。
    """
    cases = [
        ("cyrillic_swapcase", "Белая кошка (rowid 612)", "бЕЛАЯ КОШКА"),
        ("cyrillic_upper", "Белая кошка (rowid 612)", "БЕЛАЯ КОШКА"),
        ("cyrillic_lower_control", "Белая кошка (rowid 612)", "Белая кошка"),
        ("accent_upper", "Gymnopédie (rowid 533)", "GYMNOPÉDIE"),
        ("accent_stripped_control", "Gymnopédie (rowid 533)", "gymnopedie"),
        ("greek_swapcase", "Dieπ.wav (rowid 456)", "dIEΠ.WAV"),
        ("width_control", "MyGO (多行)", "ＭｙＧＯ"),
    ]
    out = []
    for name, anchor, q in cases:
        like_pat = _like_pattern(q)
        like_n = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE title LIKE ? ESCAPE '\\' OR artists LIKE ? ESCAPE '\\' "
            "OR album LIKE ? ESCAPE '\\'", (like_pat, like_pat, like_pat)).fetchone()[0]
        try:
            match_n = conn.execute(
                "SELECT COUNT(*) FROM songs_fts WHERE songs_fts MATCH ?", (_fts_phrase(q),)).fetchone()[0]
            err = None
        except sqlite3.OperationalError as exc:
            match_n, err = -1, str(exc)
        out.append({"probe": name, "anchor": anchor, "q": q,
                    "like": like_n, "match": match_n, "equal": like_n == match_n, "error": err})
    return out


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return round(s[k], 4)


def summarize(payload: dict) -> dict:
    res = payload["results"]
    ge3 = [r for r in res if r["chars"] >= 3]
    lt3 = [r for r in res if r["chars"] < 3]
    full_diff = [r for r in ge3 if r["only_like"] or r["only_match"] or r["match_error"]]
    top_diff = [r for r in ge3 if not r["top500_set_equal"]]
    pos_diff = [r for r in ge3 if not r["top500_pos_equal"]]
    fallbacks = [r for r in ge3 if not r["search_used_fts"]]
    summary = {
        "queries": len(res),
        "ge3": len(ge3),
        "lt3": len(lt3),
        "ge3_full_set_diff": len(full_diff),
        "ge3_top500_set_diff": len(top_diff),
        "ge3_top500_order_diff": len(pos_diff),
        "ge3_used_fts": len(ge3) - len(fallbacks),
        "ge3_fell_back": len(fallbacks),
        "lt3_like_only_by_design": len(lt3),
        "lt3_match_zero": sum(1 for r in lt3 if r["match_total"] == 0),
        "like_p50_ms": _pct([r["ms_like_p50"] for r in res], 50),
        "fts_p50_ms": _pct([r["ms_fts_p50"] for r in res], 50),
        "like_p95_ms": _pct([r["ms_like_p95"] for r in res], 95),
        "fts_p95_ms": _pct([r["ms_fts_p95"] for r in res], 95),
        "warnings": sum(len(r["search_warnings"]) for r in res),
        "diff_queries": [
            {"q": r["q"], "kind": r["kind"], "like": r["like_total"], "match": r["match_total"],
             "only_like": r["only_like"][:10], "only_match": r["only_match"][:10],
             "match_error": r["match_error"], "top500_set_equal": r["top500_set_equal"]}
            for r in full_diff + [x for x in top_diff if x not in full_diff]
        ],
    }
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="真库 LIKE vs FTS5 MATCH 结果集对比（只读）")
    ap.add_argument("--db", default=str(ROOT / "data" / "index.sqlite3"))
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", default=None, help="把完整 JSON 落盘（含查询串）")
    ap.add_argument("--keep", action="store_true", help="保留临时副本")
    args = ap.parse_args(argv)

    src = Path(args.db)
    if not src.exists():
        print(f"找不到库：{src}", file=sys.stderr)
        return 2
    src_hash = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    t_copy = time.perf_counter()
    tmp = copy_db_readonly(src)
    copy_ms = round((time.perf_counter() - t_copy) * 1000, 1)

    try:
        probe = sqlite3.connect(f"file:{tmp.as_posix()}?mode=ro", uri=True)
        probe.row_factory = sqlite3.Row
        version = probe.execute("PRAGMA user_version").fetchone()[0]
        songs = probe.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        fts_rows = probe.execute("SELECT COUNT(*) FROM songs_fts").fetchone()[0]
        queries = build_queries(probe)
        probe.close()

        payload = run_compare(tmp, queries, args.reps)
        summary = summarize(payload)
        out = {
            "db": str(src),
            "db_sha256_16": src_hash,
            "db_bytes": src.stat().st_size,
            "user_version": version,
            "songs": songs,
            "songs_fts": fts_rows,
            "copy_ms": copy_ms,
            "fts_ok": payload["fts_ok"],
            "unicode_probes": payload["unicode_probes"],
            "summary": summary,
            "results": payload["results"],
        }
        print(json.dumps({k: v for k, v in out.items() if k not in ("results",)},
                         ensure_ascii=False, indent=2))
        s = summary
        print(f"\n50 条里 >=3 字 {s['ge3']} 条：全量集合差异 {s['ge3_full_set_diff']} 条、"
              f"top500 集合差异 {s['ge3_top500_set_diff']} 条、top500 顺序不同 {s['ge3_top500_order_diff']} 条；"
              f"实走 MATCH {s['ge3_used_fts']}/{s['ge3']}，回退 {s['ge3_fell_back']}，warning {s['warnings']}")
        if s["diff_queries"]:
            print("\n差异清单：")
            for d in s["diff_queries"]:
                print(f"  [{d['kind']}] {d['q']!r} like={d['like']} match={d['match']} "
                      f"only_like={d['only_like']} only_match={d['only_match']} "
                      f"err={d['match_error']} top500_eq={d['top500_set_equal']}")
        if args.out:
            Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nJSON → {args.out}")

        ok = (s["ge3_full_set_diff"] == 0 and s["ge3_top500_set_diff"] == 0
              and s["ge3_fell_back"] == 0 and s["warnings"] == 0)
        return 0 if ok else 1
    finally:
        for d in {tmp.parent}:
            if not args.keep:
                for f in d.glob("*"):
                    f.unlink(missing_ok=True)
                d.rmdir()


if __name__ == "__main__":
    sys.exit(main())
