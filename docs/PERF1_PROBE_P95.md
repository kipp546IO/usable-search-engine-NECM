# PERF-1 · 真实搜索 p95（probe.sqlite3 只读统计）

日期：2026-10-22 07:27–07:35 · 基线 HEAD `6ff5b55` · 开工工作区干净
数据快照：`data/probe.sqlite3` 只读 backup，快照内含数据至 2026-10-22 07:32:57

## 结论（先给判读）

- 最近窗口（实际只有 2.99 天数据）关键词搜索 HTTP 调用 **3,505 次**（全部 ok、0 风控）：
  **p50 328ms / p95 759ms / p99 1,133ms / max 15,521ms**。
- 但 `probe.ms` 是**上游 HTTP 往返墙钟**，不是 E4 门槛定义的「本地 LIKE SQL p95」。
  同一时刻按门槛口径实测 **p95 = 2.005ms**（songs 6,353、库 4,079,616B），三项全未达：
  `enable=false / reason=threshold_not_met`。
- **判定：50ms 门槛未触发。** 探针数字不能当 FTS 门槛证据；即便按端到端 759ms 看，
  LIKE 只占 p50 的 0.61%、p95 的 0.26%，开 FTS 救不了搜索体感——瓶颈在远端网络。

## 口径

- `keyword_search` = B站 `/x/web-interface/wbi/search/type` ＋ 网易云 `/api/search/get/web`、`/api/cloudsearch/pc`。
- `probe.ms` = 单次 HTTP 调用墙钟：`t0` 在重试循环外，成功行可能含此前失败重试的 1.5s（B站）/2.0s（网易云）sleep；
  **不含**本地 SQL、治理、排序耗时。见 `src/usable_search/bilibili.py:86-99`、`netease.py:57-78`。
- 样本单位是 **HTTP 调用**，不是用户搜索次数；一次分页搜索会落多条。
- `/x/space/wbi/arc/search` 是 UP 主投稿列表，不是关键词搜索，单列、不计入主统计。
- 分位口径与 `probe._percentile` 一致：排序后 `round((n-1)*p)` 取最近秩（n=3,505 时 p95 即第 3,330 个值）。

## 只读与快照

服务 8788 仍在往 probe 库写：同一统计脚本约 1 分钟内 n 从 3,487 涨到 3,505，直接查会得到漂移数字。
本轮先用 `sqlite3.Connection.backup()` 从 `mode=ro` 源连接做快照，所有数字只读快照：

- 源库 `data/probe.sqlite3` sha256（备份前后一致）：
  `35a0c6651c4ffcd71e2f33aebb22602fc0c986e2506ce324332c4fa895125a94`
- 快照 `perf1_snapshot.sqlite3` sha256：
  `41fcc2d346dd257c3931253d44b62ecc12b2aa375fbe1a396abbd1c2778a5504`
- 快照行数：**5,645**；数据首行 2026-10-19 07:42:21、末行 2026-10-22 07:32:57，跨度 **71.84h ≈ 2.99 天**。

## 数字

主统计（3 个关键词搜索端点合计，n=3,505，全部 ok=1、risk=0、code 空）：

    样本数        3,505
    p50           328 ms
    p95           759 ms
    p99           1,133 ms
    max           15,521 ms
    mean          359.0 ms
    ≥500ms        619 次（17.66%）
    ≥50ms         3,505 次（100%）

分源：

| 源 | n | p50 | p95 | p99 | max | mean |
|---|---:|---:|---:|---:|---:|---:|
| B站 `/x/web-interface/wbi/search/type` | 2,490 | 421 | 640 | 822 | 15,521 | 400.1 |
| 网易云 合计 | 1,015 | 117 | 999 | 1,342 | 1,748 | 258.2 |
| └ `/api/search/get/web` | 559 | 85 | 477 | 800 | 1,580 | 154.8 |
| └ `/api/cloudsearch/pc` | 456 | 176 | 1,211 | 1,428 | 1,748 | 385.1 |

日分布（北京时间，主统计）：

| 日期 | n | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| 2026-10-19 | 1,204 | 364 | 620 | 1,191 |
| 2026-10-20 | 6 | 294 | 661 | 661 |
| 2026-10-21 | 0 | — | — | — |
| 2026-10-22 | 2,295 | 315 | 773 | 1,096 |

尾部敏感性：剔除 4 条 ≥1,500ms 的成功行（疑似重试 sleep 计入）后 n=3,501，
p50 328 / p95 **758** / p99 1,116 —— p95 基本不动，重试不改变判读。

被排除的其它 search 路径：`/x/space/wbi/arc/search` 50 次调用，43 ok（p50 387 / p95 3,447 / max 3,450ms），
7 次 HTTP 412 风控（失败行因 3 次重试 + sleep 拉到约 4.9s）。

## 50ms 门槛判定

门槛定义在 `src/usable_search/fts_gate.py`：`songs≥100,000` 或 `库≥100MB` 或 **本地 LIKE p95≥50ms**，任一即开。
p95 口径是现场跑与 `store._LIKE_SQL` 逐字一致的 LIKE（随机 5 个真实歌名 × 3 次），不是 HTTP 时延。

现场命令与输出（只读，`index.sqlite3` sha256 前后一致 `3d2d48f3…`）：

    USABLE_SEARCH_FTS=auto PYTHONPATH=src .venv/Scripts/python.exe -m usable_search.fts_gate data/index.sqlite3
    → songs=6353, db_bytes=4079616, p95_ms=2.005, hit=[], enable=false, reason=threshold_not_met

对照：

- 探针 HTTP p95：**759ms**，是 50ms 的 **15.18×**；
- 门槛 SQL p95：**2.005ms**，是 50ms 的 1/25，**不触发**；
- 两者差 **约 379×**，口径不同，不能互换。
- 若硬把 759ms 当「search p95」判触发，会得出开 FTS 的错结论：FTS 只省 1–2ms SQL，
  而网易云 cloudsearch p95 已达 1,211ms、B站 640ms——真实体感瓶颈在远端，FTS 不是解药。

## 局限

- probe 库 2026-10-19 07:42 才建，所谓「最近 7 天」实际只有 2.99 天；10-21 零样本、10-20 仅 6 条，
  日分布不代表一周常态。
- 关键词搜索端点 100% ok 只覆盖 3 条路径；UP 投稿列表另有 7/50 的 412 风控。
- 探针不测本地 DB 时间、治理耗时、排序耗时，也不含客户端渲染与页面等待。
- 快照固定后新增调用不在统计内；重跑会得到略不同的 n（服务仍在写）。

## 复现

1. 快照（源库只读）：

       python -c "import sqlite3; s=sqlite3.connect('file:data/probe.sqlite3?mode=ro',uri=True); d=sqlite3.connect('perf1_snapshot.sqlite3'); s.backup(d)"

2. 统计：筛选 `endpoint IN ('/x/web-interface/wbi/search/type','/api/search/get/web','/api/cloudsearch/pc')`
   且 `ts >= now-7*86400`，按 `probe._percentile` 取 p50/p95/p99；本次快照的完整统计 JSON
   见 `观察/roll_search_perf.md` 附录（本文件为仓库内正式记录）。
3. 门槛：上面的 `fts_gate` 命令。

## 本轮边界

- `data/probe.sqlite3` 只用 `mode=ro` 打开 + backup 读取，源库 sha256 前后一致；未写回、未 prune。
- `src/usable_search/store.py` 一字节未改；零新依赖、零 GPU、未 push。
- 开工 `healthcheck --quick`：除 `archive` 外全部 PASS/SKIP；`archive` FAIL 是 B站 view 接口
  **412 风控**（真实 BV1Yiz3YxEJM 与伪造 …EJX 都 412），属外部抖动，与本轮只读统计无关。
