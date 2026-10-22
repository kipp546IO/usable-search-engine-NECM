' autostart: usable-search-engine local service (E1)
' copy this file into %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
' 部署时把下一行的 <REPO_PATH> 换成仓库绝对路径（本文件是模板；实际副本在 Startup，不入库）
Set sh = CreateObject("WScript.Shell")
sh.Run """<REPO_PATH>\start-search-service.cmd""", 0, False
