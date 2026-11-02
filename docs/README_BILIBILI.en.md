# usable-search-engine (Bilibili edition)

[中文](README.md) · English

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![license](https://img.shields.io/badge/license-MIT-blue)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![version](https://img.shields.io/badge/version-v0.1.0-orange)

## Fighting the algorithm

A local, self-indexed search tool for Bilibili. Six search dimensions: everything / title / description / tag / uploader / UID.

Platform search does not fail to *find* things; it ranks them for its own reasons — fresh low-effort re-uploads above older originals, tangentially related videos above the uploader you typed, and description or tags not treated as searchable at all. This project keeps the same public metadata and ranks it **by rules you choose**: relevance, where the keyword hit, play count, publish date.

Runs entirely on your machine, listens on `127.0.0.1:18788`, uploads nothing, needs no account.

## What you get

- **Six dimensions** — everything / title / description / tag / uploader / UID
- **Filters** — duration (presets or custom minutes), publish date (today … older than a year, or custom range)
- **Sorting** — relevance, heat, newest, oldest, play count, danmaku, favourites
- **Match strictness** — standard (title/description/tag) · standard without tags · strict (title only) · loose (platform order)
- **Hit visibility** — every card says whether it matched on title, description or tag
- **Local archive** — every video seen is recorded; one click re-checks each one and classifies it as alive / deleted / private / unknown (unknown is never reported as deleted)
- **Backup-site lookup** — off by default; HEAD requests only, one per second, serial, cached forever on hit
- **Probe log** — success rate, p50, p95 per endpoint

## Run it

```
git clone https://github.com/kipp546IO/usable-search-engine-BILI.git
cd usable-search-engine-BILI
python -m venv .venv
.venv\Scripts\activate          # Windows; source .venv/bin/activate on macOS/Linux
pip install -e .
python -m usable_search serve
```

Then open <http://127.0.0.1:18788/>. On Windows you can just double-click `install.cmd`.

Pure Python standard library only — no third-party packages. Data lives in `data/` (gitignored). The service binds to localhost only.

## Known limits

- Uploader *video list* is not implemented: that endpoint is behind Bilibili's risk control and returns errors reliably enough that shipping half of it would be worse than not shipping it.
- First search per keyword scans several pages and can take a few seconds; repeated queries hit the local cache for 10 minutes.
- Only public metadata is indexed. No login, no cookies, no video content.

MIT licensed.
