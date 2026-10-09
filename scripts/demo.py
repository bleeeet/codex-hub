#!/usr/bin/env python3
"""用合成数据展示真实 render() 输出，不读取账号或聊天。"""
import importlib.util
from pathlib import Path
import sys
import time

spec = importlib.util.spec_from_file_location('hud', Path(__file__).with_name('bleet-hud.py'))
hud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hud)
state = hud.Rollout(Path('unused.jsonl'), 'demo').state
state.update(model='gpt-6.1-sol', effort='medium', plan='prolite', cwd='/example/bleet-hud',
             context_pct=42, total_tokens=2400000, cache_pct=92, started=time.time()-45*60,
             quotas=[dict(name='周额度', left=65, reset=time.time()+5*86400+7*3600+60)])
if '--five-hour' in sys.argv:
    state['quotas'].append(dict(name='5小时', left=80, reset=time.time()+2*3600+60))
width = 52 if '--narrow' in sys.argv else 118
lines = hud.render(state, width, (128, 14.76*1024, 18*1024))
colors = ['36'] + ['32'] * (len(lines)-3) + ['33', '36']
for color, line in zip(colors, lines):
    print(f'\033[{color}m{line}\033[0m')
