# 自启（本地）

`usable-search-run.vbs` → 放入 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`（登录自启）。

它隐藏运行仓库根的 `start-search-service.cmd`（守护脚本）：拉起本地搜索服务（127.0.0.1:18788），
**崩溃后 5 秒自动重拉**；端口被占用时立即退出、不自拉。

- 停止：仓库根 `stop-search-service.cmd`（写 `.stop` 标记 + 杀掉监听进程，守护下一轮退出）
- 卸载自启：删除 Startup 里的 `usable-search-run.vbs`
- 防扒：自启只起 HTTP 服务，**不发起任何平台抓取**——抓取只在用户搜索时发生，遵守既有节流与缓存
- 部署位置：把 VBS 里的 `<REPO_PATH>` 换成仓库绝对路径；路径含中文/空格时可先做 junction 别名，再填别名路径
