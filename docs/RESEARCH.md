# 同类项目调研（2026-10）

> 结论先行：**没有任何现成项目在做"跨网易云 + 哔哩哔哩、自建索引、自定义排序、不折叠不埋没"的完整版搜索。**
> 现有项目全部可以归到三类，各自的天花板都停在"官方接口的搬运工"。

调研方法：GitHub Search API 实查（非印象流），星数为查询当日快照。

## 一、多源音乐播放器（最接近的"已实现"，但都没治病根）

- **listen1/listen1_chrome_extension / listen1_desktop / listen1_mobile**（★12.1k / 11.4k / 3.1k）
  聚合网易云、QQ、酷狗、酷我、B站、咪咕、千千共 7 家搜索。立项故事与本项目一致（"为找一首歌来回切网站"），但解法止步于"聚合官方接口"。
- **maotoumao/MusicFree**（★27.6k；桌面版 MusicFreeDesktop ★9.0k）— 插件化播放器，插件即各家 API。
- **lyswhut/lx-music-desktop / lx-music-mobile**（★54.5k / 18.7k）— 音源插件生态最大的播放器。
- **qier222/YesPlayMusic**（★33.4k）— 第三方网易云客户端。
- **CharlesPikachu/musicdl**（★6.6k）— 多平台（含 B 站）下载器。

共同硬伤：**搜索层仍是转发官方结果**，官方埋掉的条目它们照样看不见；均无自建索引。

## 二、基建层（本项目不重复造的部分）

- **Binaryify/NeteaseCloudMusicApi**（★30.2k）— 网易云 API 的事实标准；2024 年因"保护版权"清库停更，fork 仍在流通。
- **NeteaseCloudMusicApiEnhanced/api-enhanced**（★2.0k）— 接棒的活跃维护增强版（2026-10 仍在更新）。
- **UnblockNeteaseMusic/server**（★7.9k）— 解决"灰歌不能听"，**不解决"搜不到"**。
- **SocialSisterYi/bilibili-API-collect**（★20.2k）— B 站接口圣经。
- **Nemo2011/bilibili-api**（★4.2k）— Python 版 B 站 API 库。

## 三、搜索增强类（"结果拿到之后再加工"，治标）

- **the1812/Bilibili-Evolved**（★30.8k）— B 站界面增强脚本（搜索相关增强有限）。
- **Intellijc/TemperMonkeyPlugin-BilibiliSearchEnhancement** — 油猴脚本：日期筛选、按比率排序、无缝合页、关键词屏蔽——只对官方返回的那批结果再加工。
- **public-clis/bilibili-cli**（★1.1k）— 终端里搜索/浏览 B 站。
- **BewlyBewly**（★8.8k）— B 站首页重排。
- "管自己数据"系：B 站收藏夹 RAG（★1.3k）、历史记录抓取（BilibiliHistoryFetcher ★1.2k）、收藏夹备份（mybili ★0.7k）——思路可借，但都是单平台单场景。

## 结论

- 要做的事：**索引层 + 排序层**——没人做。
- 数据通道：全都有（网易云实测未登录即可取搜索/专辑/歌手/详情，见 tools/probe_netease_search.py；B 站接口文档完备）。
- 因此：自建成立，而且只需要写"平台没写的那两层"。
