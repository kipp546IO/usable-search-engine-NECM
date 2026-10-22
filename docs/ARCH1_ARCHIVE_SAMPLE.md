# ARCH-1 档案复查抽样 50（2026-10-22）

## 基线

- 仓库 HEAD `90962b3`，开工前 `git status` 干净；`store.py` 本轮一字节未碰。
- `python tools/healthcheck.py --quick`：结论「基线可用」，FAIL 0；`up` SKIP（B站风控 -352，8.5s）、`recall` SKIP（--quick）；`bilibili` 套件 12/12、76.4s。
- 库：`data/archive.sqlite3`，`seen` 1764 行 —— `ok` 24 / `unknown` 1740，**1740 条全部 `checked_at=0`（从未复查）**，占 98.6%。

## 抽样口径

- 取「最久未复查」：`ORDER BY checked_at ASC, first_seen ASC LIMIT 50` → 50 条全是 `checked_at=0` 的早期建档批。
- 样本特征：`first_seen` 集中在 **2026-10-18 18:18:32–18:19:35**（同一分钟内首次入档）；`last_seen` 从 2026-10-19 06:51:51 到 2026-10-22 06:46:17，**45/50 在 24 小时内还被搜到过**。
- 复查方式：只读打开库（`mode=ro`），逐条 `bilibili.video_status(bvid)`，条间 1.0s 节流，**结果不写回** archive 库。

## 结果（50/50 全跑完，退出码 0）

| 状态 | 条数 |
|---|---|
| ok | 50 |
| gone | 0 |
| hidden | 0 |
| error | 0 |

- `code` 全 0；50 个 bvid 全不重复；50/50 的 `title_now` 与库内标题逐字一致 → 响应确实对应各自 bvid，没有串号。
- 耗时：单条 min 132.3ms / p50 183.5ms / p95 412.4ms / max 728.4ms；请求总耗时 10.54s ＋ 49 次节流 49.0s = 整轮 **59.6s**（06:46:31 → 06:47:30），约 1.19s/条。
- 对照（证明 `ok` 不是兜底值）：
  - 样本首条（已存在）→ `ok` code 0
  - 变位 bvid `BV1ZZzz1Z7Zz` → `gone` code -404「啥都木有」
  - 畸形串 `NOT_A_BVID` → `error` code -400「请求错误」

## 结论与偏差

1. **「1740 条从未复查」不等于「一批死链」**：抽到的 50 条全部存活，`unknown` 只表示没查过。
2. **本轮样本有偏**：按 `checked_at` 排序先撞上「最早建档 + 仍在被重复搜到」的条目（45/50 在 24h 内 last_seen），先验存活率天然高；要真找下架，取样键应改为 `last_seen ASC`。
3. 全库 last_seen 分桶（只读）：早于 1 天 996 条、早于 3 天 **97 条**、早于 7 天 0 条；全库只覆盖 10-18 至 10-22 四天，尚无真正的「长期无人再见的旧档」。
4. 给 ARCH-2 的实测基线：1s 节流下 ≈1.19s/条 → 全量 1740 条约 **34.6 分钟**；50 条 59.6s。
5. 副作用口径：`video_status` 走模块既有 `_request`，会按既有探针机制往 `data/probe.sqlite3` 记一条路径级记录（不含 bvid、不含搜索词）；未新增写入口、未改 archive 库。

## 复现

```bash
PYTHONPATH=src .venv/Scripts/python.exe <只读抽样运行器> data/archive.sqlite3 50 1.0
```

运行器为一次性脚本（临时目录，不入仓）；`git add` 仅本文件。
