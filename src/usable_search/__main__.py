"""命令行入口（M1 原型 v0.1）.

用法（仓库根目录）:
    PYTHONPATH=src python -m usable_search search "キラキラだとか夢だとか"
    PYTHONPATH=src python -m usable_search ingest-playlist 14174016119
"""
from __future__ import annotations

import argparse
import sys

from . import fts_gate, netease, pipeline, probe
from .rank import dedupe, rank
from .store import Index


def cmd_search(args: argparse.Namespace) -> None:
    idx = fts_gate.open_index(args.db)
    q = args.query

    records, stats = pipeline.collect_records(q, pages=args.pages)
    print(f"[collect] 单曲搜索扫穿: {stats['search']} 条")
    for a in stats["albums"]:
        print(f"[collect] 专辑展开: {a['name']}（{a['count']} 首）")
    for a in stats["artists"]:
        print(f"[collect] 歌手全曲: {a['name']}（{a['count']} 首）")
    if stats.get("prefer_artists"):
        print(f"[collect] 原唱线索: {'/'.join(stats['prefer_artists'])}")

    n = idx.upsert_songs(records)
    print(f"[index] upsert {n} 条（库内共 {idx.count()} 条）")

    rows = idx.search(q)
    ranked = rank(dedupe(rows), q, prefer_artists=stats.get("prefer_artists"))

    print(f"\n== 结果（自定义排序，前 {args.top}）==")
    for i, (score, kind, r) in enumerate(ranked[:args.top], 1):
        print(f"{i:>3}. [{score:>4}|{kind}] {r['title'][:58]} | {r['artists'][:24]} | {r['album'][:26]}")

    originals = [x for x in ranked
                 if x[2]["title"].lower().startswith(q.lower()) and x[1] == "原版候选"]
    print(f"\n[验收] 以关键词开头的「原版候选」共 {len(originals)} 条：")
    for _, _, r in originals[:8]:
        print(f"     · {r['title'][:64]} | {r['artists'][:26]}")


def cmd_ingest_playlist(args: argparse.Namespace) -> None:
    idx = fts_gate.open_index(args.db)
    name, ids = netease.playlist_track_ids(args.playlist_id)
    print(f"[collect] 歌单《{name}》: {len(ids)} 首")
    songs = netease.songs_detail(ids) if ids else []
    records = [netease.norm_song(s, "playlist") for s in songs]
    n = idx.upsert_songs(records)
    print(f"[index] upsert {n} 条（库内共 {idx.count()} 条）")


def cmd_serve(args: argparse.Namespace) -> None:
    from . import server
    server.main(["--host", args.host, "--port", str(args.port), "--db", args.db])


def cmd_probe_report(args: argparse.Namespace) -> None:
    """接口可用性报告：每个来源一行（成功率 / 风控数 / 耗时分位）。"""
    days = max(1, min(probe.MAX_KEEP_DAYS, int(args.days)))
    st = probe.stats(days=days)
    sources = st.get("sources") or {}
    print(f"== 接口探针（最近 {st['days']} 天，共 {st['total']} 条）==")
    if not sources:
        print("（无数据：还没有请求经过探针）")
        return
    for name in sorted(sources):
        s = sources[name]
        print(f"{name:<10}总计 {s['total']}  成功率 {s['ok_rate']:.4f}  风控 {s['risk']}  "
              f"p50 {s['p50_ms']}ms  p95 {s['p95_ms']}ms")


def main(argv=None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="usable_search", description="能用的搜索引擎 M1 原型")
    parser.add_argument("--db", default="data/index.sqlite3", help="索引文件路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("search", help="多入口聚合搜索 + 自定义排序展示")
    p1.add_argument("query")
    p1.add_argument("--pages", type=int, default=3, help="单曲搜索扫穿页数（每页100）")
    p1.add_argument("--top", type=int, default=30, help="展示条数")
    p1.set_defaults(func=cmd_search)

    p2 = sub.add_parser("ingest-playlist", help="歌单全量入库（公开歌单 id）")
    p2.add_argument("playlist_id", type=int)
    p2.set_defaults(func=cmd_ingest_playlist)

    p3 = sub.add_parser("serve", help="统一搜索框（M3 原型：本地网页，歌曲＋视频并列）")
    p3.add_argument("--host", default="127.0.0.1")
    p3.add_argument("--port", type=int, default=18788)
    p3.set_defaults(func=cmd_serve)

    p4 = sub.add_parser("probe-report", help="接口可用性探针报告（成功率 / 风控 / 分位耗时）")
    p4.add_argument("--days", type=int, default=7, help="回看天数（1..30）")
    p4.set_defaults(func=cmd_probe_report)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
