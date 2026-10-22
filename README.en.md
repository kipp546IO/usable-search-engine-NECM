# usable-search-engine (Netease Cloud Music edition)

[中文](README.md) · English

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![license](https://img.shields.io/badge/license-MIT-blue)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![version](https://img.shields.io/badge/version-v0.1.0-orange)

<div align="center">
<table>
<tr>
<td align="center" valign="middle"><a href="https://github.com/kipp546IO/usable-search-engine-NECM/releases/download/v0.1.0/usable-search-netease-v0.1.0.zip"><img src="https://img.shields.io/badge/%E2%AC%87%EF%B8%8F%20%E7%82%B9%E8%BF%99%E9%87%8C%E4%B8%8B%E8%BD%BD%20ZIP-%E4%B8%80%E9%94%AE%E5%90%AF%E5%8A%A8-2ea44f?style=for-the-badge" alt="Download ZIP"></a></td>
<td align="center" valign="middle"><a href="https://kipp546IO.github.io/usable-search-engine-NECM/share/"><img src="docs/share/arrow.svg" width="52" height="52" alt="Share"></a><br><sub>分享功能</sub></td>
</tr>
</table>
</div>

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
