#!/usr/bin/env python3
"""安装/卸载 HUD；只合并自己的 shell 入口及 SessionStart 钩子。"""
import json
import os
from pathlib import Path
import re
import shutil
import sys

source = Path(__file__).resolve().parent
home = Path.home()
target = home / '.codex' / 'scripts'
codex_home = Path(os.environ.get('CODEX_HOME') or home / '.codex').expanduser()
shell_file = home / ('.bashrc' if os.environ.get('SHELL', '').endswith('bash') else '.zshrc')
shell_line = '[ -f "$HOME/.codex/scripts/hud-shell.zsh" ] && source "$HOME/.codex/scripts/hud-shell.zsh"'
hook_file = codex_home / 'hooks.json'
command = '/usr/bin/python3 "$HOME/.codex/scripts/bleet-hud.py" hook'
files = ('bleet-hud.py', 'hud-shell.zsh', 'bleet-hud.tmux.conf', 'reload-hud.py')
uninstall = '--uninstall' in sys.argv

if not uninstall:
    for executable in ('codex', 'tmux'):
        if not shutil.which(executable):
            sys.exit('请先安装 ' + executable + '；见 README。')
    target.mkdir(parents=True, exist_ok=True)
    for name in files:
        shutil.copyfile(source / name, target / name)

shell = shell_file.read_text() if shell_file.exists() else ''
if uninstall:
    shell = shell.replace('\n# bleet HUD\n' + shell_line + '\n', '\n')
    shell = '\n'.join(line for line in shell.split('\n') if line != shell_line)
elif shell_line not in shell:
    shell += '\n# bleet HUD\n' + shell_line + '\n'
shell_file.write_text(shell)

hooks = json.loads(hook_file.read_text()) if hook_file.exists() else {}
events = hooks.setdefault('hooks', {}).setdefault('SessionStart', [])
for event in events:
    event['hooks'] = [hook for hook in event.get('hooks', []) if hook.get('command') != command]
events[:] = [event for event in events if event['hooks']]
if not uninstall:
    events.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 8, 'async': False}]})
codex_home.mkdir(parents=True, exist_ok=True)
hook_file.write_text(json.dumps(hooks, ensure_ascii=False, indent=2) + '\n')

if uninstall:
    for name in files:
        (target / name).unlink(missing_ok=True)
    print('已移除 HUD 入口、钩子和脚本；Codex 及其他配置保留。')
else:
    config_file = codex_home / 'config.toml'
    text = config_file.read_text() if config_file.exists() else ''
    match = re.search(r'(?m)^\[features\][ \t]*$', text)
    if match:
        following = re.search(r'(?m)^\[', text[match.end():])
        end = match.end() + following.start() if following else len(text)
        block = text[match.end():end]
        block = re.sub(r'(?m)^hooks\s*=.*$', 'hooks = true', block) if re.search(r'(?m)^hooks\s*=', block) else '\nhooks = true' + block
        text = text[:match.end()] + block + text[end:]
    else:
        text += '\n[features]\nhooks = true\n'
    config_file.write_text(text)
    print('HUD 已安装。新终端运行 codex；现有会话可运行 scripts/reload-hud.py 重载。')
