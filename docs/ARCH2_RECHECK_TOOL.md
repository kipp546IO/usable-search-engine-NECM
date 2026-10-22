# ARCH-2 档案复查脚本（2026-10-22）

## 做了什么

新增 `tools/recheck_archive.py`：把「档案验活」从网页按钮搬到命令行，可定量抽样、可先跑干跑（dry-run）。

```bash
# 干跑（默认）：只读打开 data/archive.sqlite3，不写库，只打印分布
PYTHONPATH=src .venv/Scripts/python.exe tools/recheck_archive.py --limit 50 --interval 1.0

# 写回（显式）：把 status/checked_at/code 写进档案
... tools/recheck_archive.py --limit 50 --interval 1.0 --apply

# 先副本后切：写回落到副本，原库不动
... tools/recheck_archive.py --limit 50 --interval 1.0 --apply --apply-db /path/to/archive.copy.sqlite3
```

参数：`--limit`（默认 50，上限 500）、`--interval`（默认 1.0s）、`--order checked|last_seen|first_seen`、
`--status`、`--q`、`--apply`、`--apply-db`、`--out`（汇总 JSON，含标题，别落进仓库）。

## 口径

- **默认 dry-run，只读**：库用 `mode=ro` 的 sqlite URI 打开，不建表、不 UPDATE；`--apply` 是唯一写入口。
- **取样键**：`checked`（默认，`checked_at ASC` 最久未复查）/ `last_seen`（`last_seen ASC` 最久没被再搜到，找下架用这个）/
  `first_seen`（最早建档）。
- **不碰主流程**：不经 server，也不调 `archive.recheck()`（后者固定 0.8s 且无条件写回）。
- 「状态变化」只记值得看的：新判出 `gone/hidden`，或已判过的状态翻转；`unknown → ok` 属于首次建档，单独计数。
- 退出码：0 跑完；1 = 样本 ≥5 且 error 过半（疑似风控，先别 `--apply`）；2 = 库打不开/参数错误。

## 实测（2026-10-22 06:54:40 → 06:55:37）

真实库 `data/archive.sqlite3`（`seen` 1765 行：ok 24 / unknown 1741，1741 条 `checked_at=0` 从未复查）。
`--order last_seen --limit 50 --interval 1.0`，dry-run，退出码 0：

| 项 | 数字 |
|---|---|
| 抽样 | 50 条（最久未被再搜到的一批） |
| 分布 | **ok 48 · gone 1 · hidden 0 · error 1** |
| 错误码 | 0=48 · -404=1 · 62002=1 |
| 首次建档 unknown→ok | 37（另 11 条原本已是 ok） |
| 状态变化（值得看的） | 1 条：`BV1GVJB62Edt` unknown→gone，-404「啥都木有」 |
| 单条耗时 | min 111.8ms · p50 158.4ms · p95 237.5ms · max 390.8ms · mean 168.0ms |
| 整轮 | 57.4s（≈1.148s/条；全量 1741 条外推 ≈ 1999s ≈ 33.3min） |
| 标题漂移 | 0（ok 条目的现标题与库内标题逐字一致） |
| 请求异常 | 0 |

**dry-run 没写库的结构性证据**：跑前跑后 `checked_at=0` 均为 1741 行、库文件大小均 499712 字节。

**写回路径在副本上验证**（不碰真库）：`data/archive.sqlite3` 复制到临时目录，`--apply --apply-db <副本>` 跑 3 条 →
副本 `checked_at=0` 1741→1738、ok 24→27、`max(checked_at)` 更新；原库仍 1741 → `--apply-db` 确实只写副本。

## 发现（留给后续，本轮不改主流程）

1. **62002「稿件不可见」被归到 `error`**：`bilibili.video_status` 只把 -403 映射为 `hidden`，实测
   `BV1ZyJB6sEwS` 返回 code 62002 / message「稿件不可见」，状态落 `error`。语义上应算 `hidden`；否则
   `--apply` 后它每轮都会被当接口异常重试。修 `video_status` 属于主流程改动，本轮不动，记为待办。
2. **`order last_seen` 才是找下架的取样键**：`checked_at` 排序先撞「最早建档但仍在被搜到」的条目（ARCH-1 抽 50 全 ok）；
   换 `last_seen` 后首轮就捞到 1 条真下架。建议后续定期复查用 `--order last_seen`。

## 边界

- 只问状态，不下载内容；`--out` 的 JSON 含标题，只写到临时目录/观察区，不入仓。
- 复查经 `bilibili._request` 会往 `data/probe.sqlite3` 记路径级探针（无搜索词、无 bvid），与既有行为一致。
