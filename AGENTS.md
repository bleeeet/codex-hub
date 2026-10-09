# bleet HUD

- 使用中文沟通；改动只围绕终端 HUD，不修改官方 Codex 二进制，不引入常驻服务。
- 主程序在 `scripts/bleet-hud.py`，安装入口为 `scripts/install.py`。
- 修改后运行 `python3 -m unittest discover -s scripts/tests -p 'test_*.py'`；三个进度条必须始终显示且起点对齐，窄窗口仅缩短进度条。
- 说明及截图同步更新 README；截图使用 `scripts/demo.py` 的合成数据，不发布真实账号、日志或聊天。
- 已安装的 HUD 可用 `scripts/reload-hud.py` 重载；只重启底栏，不重启聊天。
- 维护者的 Codex Stop 同步负责发布本地修改；不添加文件监听、定时拉取或开机自启。
