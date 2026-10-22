"""E4-B1 · 10 万行 FTS5 双路径基准（临时合成库，零新依赖，不改主流程）。

背景：E4-P3 的启用门槛是「songs>=10 万 或 库>=100MB 或 search p95>=50ms」，
但从来没有 10 万行规模的真实数字。本脚本用**合成数据**灌一个临时 SQLite 库
（不含真实歌名/UP 名/UID），跑同一批查询，分别计时 LIKE 与 FTS5(trigram) 路径，
输出 p50/p95，给门槛校准一个实证。

- 建库走真实代码路径 `usable_search.store.Index`（v1 迁移 + songs_fts 影子表 +
  三触发器），插入走 `Index.upsert_songs`——测的是线上同一套 SQL，不是另写的。
- 查询走 `Index.search` 公共接口（use_fts=False / True），不改 store.py。
- FTS 路径必须自证没偷偷回退 LIKE：断言 `fts_ok`、抓 `usable_search.store` 的
  warning、并用 trace 回调确认执行的是 `songs_fts MATCH ?`。
- 输出只有条数/耗时/字节数；合成词不落库。默认跑完删临时库，`--keep` 才保留。

用法（仓库根目录）：
    PYTHONUTF8=1 python tools/bench_fts5.py
    PYTHONUTF8=1 python tools/bench_fts5.py --rows 100000 --reps 25 --out bench.json

退出码：0 = FTS 可用且两路径命中逐条一致；1 = 有不一致 / FTS 不可用（数字照打）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from usable_search.store import Index  # noqa: E402  （必须在 sys.path 之后导入）

LOG = logging.getLogger("bench_fts5")

# 合成字池：常用汉字，拼出来的词没有语义，只为造出真实的字面重合分布。
_CJK_POOL = list(dict.fromkeys(
    "春夏秋冬日夜风雨花雪月光影梦歌心言葉空海山街星恋愛詩旋律青白黒紅紫銀金木水火土"
    "天地人音声鳥雲虹灯窓道橋川島森林原野朝暮夕暁宵静寂喧騒流浪旅帰郷故郷未来過去"
    "永遠瞬間記憶忘却約束嘘真実希望絶望孤独自由檻翼羽根飛翔墜落叫喚囁微笑涙汗血呼吸"
    "温度距離境界線水平線地平線"
))
_LATIN = ("echo", "aria", "muse", "nova", "pulse", "drift", "zero",
          "light", "rain", "code", "solo", "duo", "loop", "blue", "mono")

# 高频短语：真实曲库里「春日影」这类词会出现在成千上万条标题里，
# 这是 LIKE 最坏情况（要排序全部命中再取前 500），基准必须覆盖。
_HOT = (("雪月花", 0.12), ("流光曲", 0.05))


# ------------------------------------------------------------------ 合成数据
def _make_words(rng: random.Random, n: int) -> list[str]:
    """造 n 个互不相同的 2–4 字词。"""
    words: set[str] = set()
    while len(words) < n:
        ln = rng.choice((2, 2, 3, 3, 4))
        words.add("".join(rng.choice(_CJK_POOL) for _ in range(ln)))
    return sorted(words)


def _make_entities(rng: random.Random, n: int, words: list[str]) -> list[str]:
    out: set[str] = set()
    while len(out) < n:
        k = rng.choice((1, 2, 2, 3))
        out.add(" ".join(rng.choice(words) for _ in range(k)))
    return sorted(out)


def _zipf_choices(rng: random.Random, words: list[str], k: int) -> list[str]:
    """按 1/rank 权重抽词：词频长尾分布，避免人人等概率的假均匀。"""
    ordered = list(words)
    rng.shuffle(ordered)
    weights = [1.0 / (i + 1) for i in range(len(ordered))]
    return rng.choices(ordered, weights=weights, k=k)


def synth_rows(rows: int, seed: int) -> tuple[list[dict], dict]:
    """生成 rows 条合成记录，返回 (records, 选好的查询集)。"""
    rng = random.Random(seed)
    words = _make_words(rng, 600)
    artists = _make_entities(rng, 180, words)
    albums = _make_entities(rng, 260, words)

    titles: list[str] = []
    records: list[dict] = []
    for i in range(rows):
        parts = _zipf_choices(rng, words, rng.choice((1, 1, 2, 2, 3)))
        title = "".join(parts)
        # 高频短语：按概率拼进标题，制造真实曲库那种「同一词出现在上万条」的分布
        for token, p in _HOT:
            if rng.random() < p:
                title += token
        # 5% 标题带拉丁词，验证大小写/跨字符集路径
        latin = rng.choice(_LATIN) if rng.random() < 0.05 else ""
        titles.append(title)
        records.append({
            "source": "bench",
            "id": i + 1,
            "title": title + (f" {latin}" if latin else ""),
            "artists": rng.choice(artists),
            "album": rng.choice(albums),
            "album_id": 100000 + i,
            "cover": "",
            "duration": rng.randint(30, 600),
            "fee": rng.choice((0, 0, 0, 1, 8)),
            "origin": "",
            "mine": 0,
        })

    # 统计 3 字窗口与 2 字窗口的行覆盖，用来挑高/中/低频查询
    probe: dict[str, int] = {}
    probe2: dict[str, int] = {}
    for t in titles:
        for j in range(0, max(0, len(t) - 2)):
            probe[t[j:j + 3]] = probe.get(t[j:j + 3], 0) + 1
        for j in range(0, max(0, len(t) - 1)):
            probe2[t[j:j + 2]] = probe2.get(t[j:j + 2], 0) + 1

    rare_pool = sorted(k for k, v in probe.items() if 3 <= v <= 20)
    mid_pool = sorted(k for k, v in probe.items() if 100 <= v <= 1000)
    rare = rare_pool[0] if rare_pool else "qqzxw"
    mid = mid_pool[len(mid_pool) // 2] if mid_pool else max(probe, key=lambda k: probe[k])
    top2 = max(probe2, key=lambda k: probe2[k])
    exact = max(titles, key=len)

    quotes = [
        {"name": "hot_3char", "q": _HOT[0][0], "fts": True,
         "note": "高频词，~12% 行命中（LIKE 最坏情况）"},
        {"name": "hot_4char", "q": _HOT[1][0], "fts": True,
         "note": "高频词，~5% 行命中"},
        {"name": "mid_3char", "q": mid, "fts": True,
         "note": f"中等频率 3 字窗口（{probe.get(mid, 0)} 行）"},
        {"name": "rare_3char", "q": rare, "fts": True,
         "note": f"低频 3 字窗口（{probe.get(rare, 0)} 行）"},
        {"name": "exact_title", "q": exact, "fts": True, "note": "最长标题整串"},
        {"name": "mixed_5char", "q": exact[:5], "fts": True, "note": "标题前 5 字"},
        {"name": "latin_lower", "q": "aria", "fts": True,
         "note": "小写拉丁，验证大小写行为"},
        {"name": "no_hit", "q": "qqzxw", "fts": True, "note": "必然 0 命中"},
        {"name": "short_2char", "q": top2, "fts": False,
         "note": f"最高频 2 字（{probe2.get(top2, 0)} 行）：trigram 无 token，FTS 按设计不走"},
    ]
    return records, {"queries": quotes, "word_count": len(words)}


# ------------------------------------------------------------------ 计时工具
def _pct(values: list[float], p: float) -> float:
    """线性插值分位数（与 numpy 默认口径一致）。"""
    if not values:
        return float("nan")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * (p / 100.0)
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


class _WarnCatcher(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def _trace_uses_match(idx: Index, q: str) -> bool:
    """跑一次查询并抓实际执行的 SQL，确认走的是 MATCH 而不是悄悄回落 LIKE。"""
    seen: list[str] = []
    idx.conn.set_trace_callback(seen.append)
    try:
        idx.search(q, 5)
    finally:
        idx.conn.set_trace_callback(None)
    return any("songs_fts" in s and "MATCH" in s for s in seen)


def _bench(records: list[dict], queries: list[dict], reps: int, limit: int,
           db_path: str) -> dict:
    idx_like = Index(db_path, use_fts=False)
    idx_fts = Index(db_path, use_fts=True)
    if not idx_fts.fts_ok:
        raise SystemExit("FTS5/trigram 不可用：影子表探测失败，基准无法进行")

    catcher = _WarnCatcher()
    logging.getLogger("usable_search.store").addHandler(catcher)

    results = []
    for item in queries:
        q = item["q"]
        like_rows = idx_like.search(q, limit)
        like_ids = [r["id"] for r in like_rows]
        entry = {"name": item["name"], "query": q, "note": item["note"],
                 "hits": len(like_rows), "like": {}, "fts": {}, "same": None}

        for _ in range(3):  # 预热：把两边的页缓存都焐热，别让第一枪决定中位数
            idx_like.search(q, limit)
            if item["fts"]:
                idx_fts.search(q, limit)

        like_times: list[float] = []
        fts_times: list[float] = []
        fts_ids: list[int] = []
        for _ in range(reps):
            t0 = time.perf_counter()
            idx_like.search(q, limit)
            like_times.append((time.perf_counter() - t0) * 1000)
            if item["fts"]:
                t0 = time.perf_counter()
                rows = idx_fts.search(q, limit)
                fts_times.append((time.perf_counter() - t0) * 1000)
                fts_ids = [r["id"] for r in rows]

        entry["like"] = {"p50": round(_pct(like_times, 50), 3),
                         "p95": round(_pct(like_times, 95), 3),
                         "min": round(min(like_times), 3),
                         "max": round(max(like_times), 3)}
        if item["fts"]:
            entry["fts"] = {"p50": round(_pct(fts_times, 50), 3),
                            "p95": round(_pct(fts_times, 95), 3),
                            "min": round(min(fts_times), 3),
                            "max": round(max(fts_times), 3)}
            entry["same"] = like_ids == fts_ids
            entry["match_sql"] = _trace_uses_match(idx_fts, q)
        else:
            entry["fts"] = None
            entry["same"] = None
            entry["match_sql"] = None
        results.append(entry)

    logging.getLogger("usable_search.store").removeHandler(catcher)
    idx_like.conn.close()
    idx_fts.conn.close()

    fts_entries = [r for r in results if r["fts"]]
    return {
        "results": results,
        "warnings": catcher.records,
        "worst_like_p95": max((r["like"]["p95"] for r in results), default=None),
        "worst_fts_p95": max((r["fts"]["p95"] for r in fts_entries), default=None),
        "same_all": all(r["same"] for r in fts_entries),
        "match_sql_all": all(r["match_sql"] for r in fts_entries),
        "fts_faster_all": all(r["fts"]["p50"] < r["like"]["p50"] for r in fts_entries),
    }


def _index_bytes(db_path: str) -> dict:
    """影子索引占用：优先 dbstat，取不到就只报整库字节数。"""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute(
            "SELECT SUM(pgsize) FROM dbstat WHERE name LIKE 'songs_fts%'").fetchone()[0]
    except sqlite3.OperationalError:
        total = None
    conn.close()
    return {"db_bytes": os.path.getsize(db_path),
            "fts_bytes": total,
            "fts_ratio": round(total / os.path.getsize(db_path), 3) if total else None}


# ------------------------------------------------------------------ 主流程
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E4-B1 10 万行 LIKE vs FTS5 基准")
    ap.add_argument("--rows", type=int, default=100_000, help="合成行数（默认 100000）")
    ap.add_argument("--reps", type=int, default=25, help="每条查询计时次数（默认 25）")
    ap.add_argument("--seed", type=int, default=20261022, help="随机种子")
    ap.add_argument("--limit", type=int, default=500, help="search limit（默认 500）")
    ap.add_argument("--out", default=None, help="把 JSON 结果写到这个路径")
    ap.add_argument("--keep", action="store_true", help="保留临时库（默认删除）")
    args = ap.parse_args(argv)
    if args.rows <= 0 or args.reps < 5:
        ap.error("--rows 必须 > 0，--reps 至少 5")

    tmpdir = tempfile.mkdtemp(prefix="bench-fts5-")
    db_path = os.path.join(tmpdir, "bench.sqlite3")
    try:
        t0 = time.perf_counter()
        records, meta = synth_rows(args.rows, args.seed)
        gen_ms = (time.perf_counter() - t0) * 1000

        # 建库 + 灌数据：全部走真实 Index 路径（含 FTS 影子表与触发器）
        t0 = time.perf_counter()
        idx = Index(db_path, use_fts=False)
        batch = 5000
        inserted = 0
        for i in range(0, len(records), batch):
            inserted += idx.upsert_songs(records[i:i + batch])
        build_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        rebuilt = idx.rebuild_fts()
        rebuild_ms = (time.perf_counter() - t0) * 1000
        index_ok = idx.fts_ok and rebuilt
        count = idx.count()
        idx.conn.close()

        bench = _bench(records, meta["queries"], args.reps, args.limit, db_path)
        sizes = _index_bytes(db_path)

        payload = {
            "task": "E4-B1",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rows_requested": args.rows,
            "rows_inserted": inserted,
            "rows_count": count,
            "reps": args.reps,
            "seed": args.seed,
            "limit": args.limit,
            "synth_ms": round(gen_ms, 1),
            "build_ms": round(build_ms, 1),
            "rebuild_ms": round(rebuild_ms, 1),
            "fts_index_ok": index_ok,
            **sizes,
            **bench,
        }

        # 人读输出
        print("== E4-B1 · LIKE vs FTS5(trigram) ==")
        print(f"行数 {count:,} · 建库 {build_ms / 1000:.1f}s · 索引重建 "
              f"{rebuild_ms / 1000:.1f}s · 库 {sizes['db_bytes'] / 1048576:.1f}MB")
        print(f"{'查询':<14}{'命中':>6}{'LIKE p50':>11}{'LIKE p95':>11}"
              f"{'FTS p50':>10}{'FTS p95':>10}{'加速':>8}  一致")
        for r in bench["results"]:
            fts = r["fts"]
            if fts:
                speed = f"{r['like']['p50'] / fts['p50']:.1f}x" if fts["p50"] else "n/a"
                print(f"{r['name']:<14}{r['hits']:>6}{r['like']['p50']:>11.3f}"
                      f"{r['like']['p95']:>11.3f}{fts['p50']:>10.3f}{fts['p95']:>10.3f}"
                      f"{speed:>8}  {'是' if r['same'] else '否'}")
            else:
                print(f"{r['name']:<14}{r['hits']:>6}{r['like']['p50']:>11.3f}"
                      f"{r['like']['p95']:>11.3f}{'—':>10}{'—':>10}{'—':>8}  跳过")
        print(f"最差 p95：LIKE {bench['worst_like_p95']:.3f}ms · "
              f"FTS {bench['worst_fts_p95']:.3f}ms")
        print(f"命中/顺序逐条一致：{'是' if bench['same_all'] else '否'} · "
              f"确走 MATCH：{'是' if bench['match_sql_all'] else '否'} · "
              f"FTS 全胜：{'是' if bench['fts_faster_all'] else '否'} · "
              f"降级 warning {len(bench['warnings'])} 条")
        gate = bench["worst_like_p95"] >= 50.0
        print(f"门槛校准：10 万行 LIKE 最差 p95 {bench['worst_like_p95']:.1f}ms → "
              f"{'已达 50ms 阈值' if gate else '未达 50ms 阈值'}")

        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            print(f"JSON → {args.out}")

        ok = index_ok and bench["same_all"] and bench["match_sql_all"] and not bench["warnings"]
        return 0 if ok else 1
    finally:
        if args.keep:
            print(f"临时库保留：{db_path}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
