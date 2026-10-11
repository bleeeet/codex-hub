# Codex Hub

- 使用中文沟通；改动只围绕终端 HUD，不修改官方 Codex 二进制，不引入常驻服务。
- 主程序在 `scripts/bleet-hud.py`，安装入口为 `scripts/install.py`。
- 修改后运行 `python3 -m unittest discover -s scripts/tests -p 'test_*.py'`；三个进度条必须始终显示且起点对齐，窄窗口仅缩短进度条。
- 说明及截图同步更新 README；截图使用 `scripts/demo.py` 的合成数据，不发布真实账号、日志或聊天。
- 修改并通过相关检查后，用 `scripts/reload-hud.py` 一次性重载现有底栏；只重启底栏，保留聊天进程及各会话的 `CODEX_HOME`。安装与会话绑定细节见 `~/.codex/docs/bleet-HUD.md`。
- 维护者的 Codex Stop 前台同步负责发布已授权的本地修改；双边冲突先核验合并，不手工覆盖，不添加文件监听、定时拉取或开机自启。
