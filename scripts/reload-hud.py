#!/usr/bin/env python3
"""一次性重载现有 HUD 子面板，保留 Codex 进程及各会话的 CODEX_HOME。"""
from pathlib import Path
import re
import shlex
import subprocess

TMUX = ['/opt/homebrew/bin/tmux', '-L', 'bleet-hud']
script = str(Path(__file__).with_name('bleet-hud.py'))
result = subprocess.run(TMUX + ['list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}|#{@bleet_hud_panel}'], capture_output=True, text=True, timeout=3)
for row in result.stdout.splitlines():
    pane, pid, hud = row.split('|')
    if hud != '1':
        continue
    command = subprocess.check_output(['ps', '-p', pid, '-o', 'command='], text=True, timeout=2)
    args = shlex.split(command)
    if script not in args or 'watch' not in args:
        continue
    # 只提取这一项环境变量，不输出进程环境。
    environment = subprocess.check_output(['ps', 'eww', '-p', pid, '-o', 'command='], text=True, timeout=2)
    match = re.search(r'(?:^|\s)CODEX_HOME=(.*?)(?=\s[A-Za-z_][A-Za-z_0-9]*=|$)', environment)
    root = match.group(1) if match else str(Path.home()/'.codex')
    command = shlex.join(['/usr/bin/env', 'CODEX_HOME='+root, '/usr/bin/python3', *args[1:]])
    subprocess.run(TMUX + ['respawn-pane', '-k', '-t', pane, command], check=True, timeout=3)
    print('已重载 HUD 面板', pane)
