# usable-search-engine (Netease Cloud Music edition)

[中文](README.md) · English

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![license](https://img.shields.io/badge/license-MIT-blue)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![version](https://img.shields.io/badge/version-v0.1.0-orange)

<div align="center">
<table>
<tr><td align="center" width="20%"><a href="docs/share/wechat-qr.png"><img src="docs/share/wechat.svg" width="56" height="56" alt="微信"></a><br><sub>微信</sub></td><td align="center" width="20%"><a href="https://connect.qq.com/widget/shareqq/index.html?url=https%3A%2F%2Fgithub.com%2Fkipp546IO%2Fusable-search-engine-NECM&amp;title=%E8%83%BD%E7%94%A8%E7%9A%84%E6%90%9C%E7%B4%A2%E5%BC%95%E6%93%8E%20%C2%B7%20%E7%BD%91%E6%98%93%E4%BA%91%E7%89%88"><img src="docs/share/qq.svg" width="56" height="56" alt="QQ"></a><br><sub>QQ</sub></td><td align="center" width="20%"><a href="https://sns.qzone.qq.com/cgi-bin/qzshare/cgi_qzshare_onekey?url=https%3A%2F%2Fgithub.com%2Fkipp546IO%2Fusable-search-engine-NECM&amp;title=%E8%83%BD%E7%94%A8%E7%9A%84%E6%90%9C%E7%B4%A2%E5%BC%95%E6%93%8E%20%C2%B7%20%E7%BD%91%E6%98%93%E4%BA%91%E7%89%88&amp;summary=%E8%83%BD%E7%94%A8%E7%9A%84%E6%90%9C%E7%B4%A2%E5%BC%95%E6%93%8E%20%C2%B7%20%E7%BD%91%E6%98%93%E4%BA%91%E7%89%88"><img src="docs/share/qzone.svg" width="56" height="56" alt="QQ空间"></a><br><sub>QQ空间</sub></td><td align="center" width="20%"><a href="https://service.weibo.com/share/share.php?url=https%3A%2F%2Fgithub.com%2Fkipp546IO%2Fusable-search-engine-NECM&amp;title=%E8%83%BD%E7%94%A8%E7%9A%84%E6%90%9C%E7%B4%A2%E5%BC%95%E6%93%8E%20%C2%B7%20%E7%BD%91%E6%98%93%E4%BA%91%E7%89%88"><img src="docs/share/weibo.svg" width="56" height="56" alt="微博"></a><br><sub>微博</sub></td><td align="center" width="20%"><a href="https://tieba.baidu.com/f/commit/share/openShareApi?url=https%3A%2F%2Fgithub.com%2Fkipp546IO%2Fusable-search-engine-NECM&amp;title=%E8%83%BD%E7%94%A8%E7%9A%84%E6%90%9C%E7%B4%A2%E5%BC%95%E6%93%8E%20%C2%B7%20%E7%BD%91%E6%98%93%E4%BA%91%E7%89%88"><img src="docs/share/tieba.svg" width="56" height="56" alt="贴吧"></a><br><sub>贴吧</sub></td></tr>
<tr><td align="center" width="20%"><a href="docs/SHARE.md"><img src="docs/share/xiaohongshu.svg" width="56" height="56" alt="小红书"></a><br><sub>小红书</sub></td><td align="center" width="20%"><a href="docs/SHARE.md"><img src="docs/share/lofter.svg" width="56" height="56" alt="LOFTER"></a><br><sub>LOFTER</sub></td><td align="center" width="20%"><a href="docs/SHARE.md"><img src="docs/share/instagram.svg" width="56" height="56" alt="Instagram"></a><br><sub>Instagram</sub></td><td align="center" width="20%"><a href="https://twitter.com/intent/tweet?url=https%3A%2F%2Fgithub.com%2Fkipp546IO%2Fusable-search-engine-NECM&amp;text=%E8%83%BD%E7%94%A8%E7%9A%84%E6%90%9C%E7%B4%A2%E5%BC%95%E6%93%8E%20%C2%B7%20%E7%BD%91%E6%98%93%E4%BA%91%E7%89%88"><img src="docs/share/x.svg" width="56" height="56" alt="X"></a><br><sub>X</sub></td><td align="center" width="20%"><a href="https://t.me/share/url?url=https%3A%2F%2Fgithub.com%2Fkipp546IO%2Fusable-search-engine-NECM&amp;text=%E8%83%BD%E7%94%A8%E7%9A%84%E6%90%9C%E7%B4%A2%E5%BC%95%E6%93%8E%20%C2%B7%20%E7%BD%91%E6%98%93%E4%BA%91%E7%89%88"><img src="docs/share/telegram.svg" width="56" height="56" alt="Telegram"></a><br><sub>Telegram</sub></td></tr>
</table>
</div>

<p align="center"><sub>Scan the QR for WeChat · Xiaohongshu / LOFTER / Instagram open a ready-to-copy blurb</sub></p>

## Fighting the algorithm

A local, self-indexed search tool for NetEase Cloud Music (网易云音乐).

Platform search keeps getting worse: the song you want is buried under covers, instrumentals and promoted junk — sometimes it is not returned at all. This project does not proxy or re-skin the platform's search. It builds **its own index locally** and ranks results **with its own rules**.

Runs entirely on your machine, listens on `127.0.0.1:18788`, talks to no cloud service, uploads nothing, needs no account.

## 💥 Why

For Poppin'Party — 「キラキラだとか夢だとか ～Sing Girls～」 on NetEase Cloud Music:

- the library holds at least **3 original versions** (single / album / TV size), all playable and visible on the artist page;
- yet keyword search ranks them at **positions 227–229**, far past a first screen full of covers and instrumentals;
- pasting the full title makes the search return **no original at all**;
- the same song is found instantly from the user's own playlist — **the library is fine, the search is broken.**

Behaviour is inconsistent across tracks as well: one song's original sits at #1, another's sinks or disappears. It is per-item breakage, not user error.

## 🧩 What it does differently

Most "search fixes" are thin wrappers over the official API — if the platform buries a result, they bury it too. This project adds the two layers they don't:

- **Index layer** — everything you touch (playlists, albums, artist pages) is stored in a local SQLite index, so it stays searchable.
- **Ranking layer** — your own rules: exact hits first, all versions of a song side by side, no folding, no promoted slots.

## ✨ Features

**Search** — six precise modes: song title, lyrics (matched line highlighted and pinned), album, artist, playlist, podcast. Pagination 30 / 50 / 100 / 300 without duplicates.

**Filters** — duration, release date (last 1 / 3 / 5 / 10 years or a custom range), quality-first sorting (Master > Hi-Res > Lossless > Ex-High > Higher > Standard), and tri-state VIP / original-artist filters (any / only / exclude).

**Per-result badges** — highest available quality, VIP requirement, and version type: original / cover / adaptation / other version / unlabelled. Card is clickable; artist and album names jump to their own search views.

**Experience** — dark and light themes, in-result secondary filter (no extra request), local search history, explicit cache-clear panel.

**Engineering** — pure Python standard library (urllib / sqlite3 / http.server), zero third-party dependencies, local SQLite index, endpoint health probe (success rate, p50/p95), archive and takedown re-check, optional playback-backup site (off by default).

## ⚡ Quick start

Requires Python 3.10+. **No packages to install.**

### Windows
1. Install Python from <https://www.python.org/downloads/> — **tick `Add python.exe to PATH`** on the first screen.
2. Download this repo (`Code` → `Download ZIP`) and unpack it anywhere outside `C:\Program Files`.
3. Double-click `install.cmd`. A console window opens and your browser follows.

Keep that console window open — it *is* the server. Closing it stops the service.

### macOS / Linux
```bash
git clone https://github.com/kipp546IO/usable-search-engine-NECM.git
cd usable-search-engine
bash install.sh
```

Then open <http://127.0.0.1:18788/>.

### Manual
```bash
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate  elsewhere
pip install -e .
python -m usable_search serve
```
Or skip installation entirely: `PYTHONPATH=src python -m usable_search serve`.

Change the port with `--port <n>`.

## ❓ FAQ

**Will my account get banned?**
No. Requests are anonymous, carry no login state, and only read public search endpoints. The real risk is hammering the API — rate limits are IP-based.

**Why is a search slow or empty sometimes?**
Results are fetched live on first query. When the platform throttles or returns nothing, you get a slow or empty response. Retry later or rephrase.

**Where is my data?**
`data/` inside the project: four SQLite files. Delete the folder to reset the index; your NetEase account is untouched.

**Is my search history uploaded?**
No. History lives in your browser's localStorage. The server only logs endpoint success rates and timings, never query text.

**Port 18788 already in use?**
Start with `--port <n>`, or edit `PORT=` at the top of `install.cmd`.

**Mobile?**
Desktop web only for now. A native Android build is planned.

**Why is a song not marked "original"?**
The API only tags some tracks. Untagged ones show "unlabelled"; when another copy in the same result set is clearly the original, we infer and mark it (hover for the reason). We would rather say "unknown" than guess.

## 🚧 Disclaimer

- Scraping uses NetEase's **unofficial public endpoints**. Use it at a low rate; throttling and occasional empty results are expected.
- No audio or video content is provided, proxied or cached. Only metadata and lyric text are stored, for personal research and retrieval.
- License: MIT, see `LICENSE`.
