#!/usr/bin/env python3
"""本地 Codex HUD：只保存统计值；面板随所属进程退出。"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import sqlite3
from contextlib import closing
import time
import unicodedata

ROOT = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()
SCRIPT = Path(__file__).resolve()


def clean(value):
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value))
    return ''.join(c for c in value if unicodedata.category(c)[0] != 'C')


def cells(value):
    return sum(0 if unicodedata.combining(c) or c in '\ufe0e\ufe0f' else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in value)


def fit(value, width):
    result = ''
    for c in clean(value):
        if cells(result + c) > width:
            break
        result += c
    return result


class Rollout:
    def __init__(self, path, session):
        self.path, self.session, self.offset = Path(path), session, 0
        self.inode = None
        self.state = dict(model='等待模型', effort='', plan='', context_pct=None,
                          context_window=None, total_tokens=0, cache_pct=None,
                          quotas=[], pending={}, cwd=os.getcwd(), started=time.time())

    def update(self):
        if not self.path.exists():
            if self.inode is not None:
                self.__init__(self.path, self.session)
                raise FileNotFoundError(self.path)
            return
        with self.path.open('rb') as f:
            stat = os.fstat(f.fileno())
            if self.inode != stat.st_ino or stat.st_size < self.offset:
                self.__init__(self.path, self.session)
                self.inode = stat.st_ino
            f.seek(self.offset)
            while True:
                pos = f.tell()
                line = f.readline()
                if not line or not line.endswith(b'\n'):
                    self.offset = pos
                    break
                self.offset = f.tell()
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(item, dict) or not isinstance(item.get('payload', {}), dict):
                    continue
                p = item.get('payload', {})
                kind = item.get('type')
                if kind in ('session_meta', 'turn_context') and p.get('cwd'):
                    self.state['cwd'] = p['cwd']
                if kind == 'session_meta':
                    if (p.get('id') or p.get('session_id')) != self.session:
                        self.__init__(self.path, self.session)
                        raise ValueError('会话 ID 不匹配，拒绝串线')
                    if p.get('timestamp'):
                        try:
                            self.state['started'] = dt.datetime.fromisoformat(p['timestamp'].replace('Z', '+00:00')).timestamp()
                        except ValueError:
                            pass
                elif kind == 'turn_context':
                    self.state['model'] = p.get('model') or self.state['model']
                    self.state['effort'] = p.get('effort') or p.get('reasoning_effort') or ''
                elif kind == 'event_msg' and p.get('type') == 'token_count':
                    info = p.get('info') or {}
                    total = info.get('total_token_usage') or {}
                    window = info.get('model_context_window') or self.state['context_window']
                    self.state['context_window'] = window
                    last = (info.get('last_token_usage') or {}).get('total_tokens')
                    if window and last is not None:
                        self.state['context_pct'] = min(100, max(0, 100 * last / window))
                    if total:
                        self.state['total_tokens'] = total.get('total_tokens', 0)
                        inputs = total.get('input_tokens', 0)
                        self.state['cache_pct'] = 100 * total.get('cached_input_tokens', 0) / inputs if inputs else None
                    limits = p.get('rate_limits')
                    if limits:
                        self.state['plan'] = limits.get('plan_type') or ''
                        self.state['quotas'] = []
                        for key in ('primary', 'secondary'):
                            q = limits.get(key)
                            if not q or q.get('used_percent') is None:
                                continue
                            minutes = q.get('window_minutes')
                            name = {300: '5小时', 10080: '周额度'}.get(minutes, f'{minutes}分钟额度')
                            self.state['quotas'].append(dict(name=name, left=100-q['used_percent'], reset=q.get('resets_at')))
                elif kind == 'response_item':
                    if p.get('type') in ('function_call', 'custom_tool_call'):
                        self.state['pending'][p.get('call_id')] = clean(p.get('name', '工具'))
                    elif p.get('type') in ('function_call_output', 'custom_tool_call_output'):
                        self.state['pending'].pop(p.get('call_id'), None)


def number(value):
    return f'{value/1000000:.2f}M' if value >= 1000000 else f'{value/1000:.1f}K' if value >= 1000 else str(value)


def render(state, width, memory):
    effort = {'low': '低', 'medium': '中等', 'high': '高', 'xhigh': '极高', 'max': '最高', 'ultra': '最高'}.get(state['effort'], state['effort'])
    elapsed = max(0, int((time.time()-state['started'])/60))
    project = Path(state.get('cwd') or os.getcwd()).name or '/'
    plan = {'prolite': '$100', 'pro': '$200', 'plus': '$19', 'go': '$8'}.get(state['plan'], '')
    first = f"{state['model']} · {effort}推理 │ 📁 {project}"
    if plan:
        first += ' │ ' + plan
    first += f" │ {os.uname().nodename.split('.')[0]} │ {elapsed}分钟"
    lines = [first]
    bar_size = 10 if width >= 64 else 4
    for name, label in (('周额度', '📊 周额度'), ('5小时', '5H       ')):
        q = next((q for q in state['quotas'] if q['name'] == name), None)
        if not q:
            if name == '周额度':
                lines.append(f'{label} 未提供')
            continue
        filled = round(min(100, max(0, 100-q['left'])) * bar_size / 100)
        bar = '█' * filled + '░' * (bar_size-filled)
        line = f"{label} {bar} 剩余{q['left']:.0f}%"
        if q.get('reset'):
            hours = max(0, int((q['reset'] - time.time()) / 3600))
            days, hours = divmod(hours, 24)
            reset_at = dt.datetime.fromtimestamp(q['reset']).strftime('%m/%d %H:%M')
            countdown = f'{days}天{hours}小时后重置' if width >= 64 else f'重置{days}天{hours}小时'
            line += f' │ {countdown} · {reset_at}'
        lines.append(line)
    pct = state['context_pct']
    if pct is None:
        context = '🧠 上下文 等待数据'
    else:
        bar = '█' * round(pct * bar_size / 100) + '░' * (bar_size-round(pct * bar_size / 100))
        context = f'🧠 上下文 {bar} 已用{pct:.0f}%'
    context += f" │ Token {number(state['total_tokens'])}"
    if state['cache_pct'] is not None:
        context += f" │ 缓存 {state['cache_pct']:.0f}%"
    if state['pending']:
        context += ' │ 运行 ' + ','.join(set(state['pending'].values()))
    if state.get('error'):
        context = state['error'] + ' │ ' + context
    lines.append(context)
    last = '⚙️  内存量 等待数据'
    if memory:
        codex, used, total = memory
        pct = min(100, max(0, 100 * used / total))
        bar = '█' * round(pct * bar_size / 100) + '░' * (bar_size-round(pct * bar_size / 100))
        last = f'⚙️  内存量 {bar} 已用{pct:3.0f}% · {used/1024:4.1f}/{total/1024:3.0f}GB │ Codex {codex:5.0f}MB'
    lines.append(last)
    return [fit(line, max(1, width-1)) for line in lines]


def tmux(*args):
    exe = shutil.which('tmux') or '/opt/homebrew/bin/tmux'
    return subprocess.check_output([exe, *args], text=True, stderr=subprocess.DEVNULL, timeout=2).strip()


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False


def process_memory(pid):
    rows = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,rss='], text=True, timeout=2)
    processes = [tuple(map(int, row.split())) for row in rows.splitlines()]
    owners = {pid}
    while True:
        children = {p for p, parent, _ in processes if parent in owners}
        if children <= owners:
            break
        owners |= children
    total = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True, timeout=2))
    vm = subprocess.check_output(['vm_stat'], text=True, timeout=2)
    used = used_memory(vm, total)
    return (sum(rss for p, _, rss in processes if p in owners)/1024, used/1024**2, total/1024**2)


def used_memory(vm, total):
    page_size = int(re.search(r'page size of (\d+) bytes', vm).group(1))
    pages = {key.strip('"'): int(value) for key, value in re.findall(r'^(.+?):\s+(\d+)\.', vm, re.M)}
    # 应用驻留页 + wired + 压缩器实际物理页；扣除可清除页，不计文件缓存。
    used = pages['Anonymous pages'] + pages['Pages wired down'] + pages['Pages occupied by compressor'] - pages['Pages purgeable']
    return min(total, max(0, used * page_size))


def start(args):
    existing = tmux('show-options', '-p', '-v', '-t', args.pane, '@bleet_hud') if has_option(args.pane) else ''
    panes = tmux('list-panes', '-t', args.pane, '-F', '#{pane_id}').splitlines()
    if existing in panes:
        if tmux('show-options', '-p', '-v', '-t', args.pane, '@bleet_hud_session') == args.session:
            return
    command = ['/usr/bin/env', 'CODEX_HOME=' + str(ROOT), '/usr/bin/python3', str(SCRIPT), 'watch', '--session', args.session, '--pid', str(args.pid)]
    if args.file:
        command += ['--file', str(args.file)]
    cmd = shlex.join(command)
    if existing in panes:
        child = existing
        tmux('respawn-pane', '-k', '-t', child, cmd)
    else:
        child = tmux('split-window', '-d', '-v', '-l', '5', '-t', args.pane, '-P', '-F', '#{pane_id}', cmd)
    tmux('set-option', '-p', '-t', args.pane, '@bleet_hud', child)
    tmux('set-option', '-p', '-t', args.pane, '@bleet_hud_session', args.session)
    tmux('set-option', '-p', '-t', child, '@bleet_hud_panel', '1')
    tmux('set-option', '-p', '-t', child, 'remain-on-exit', 'off')


def has_option(pane):
    try:
        return bool(tmux('show-options', '-p', '-v', '-t', pane, '@bleet_hud'))
    except subprocess.CalledProcessError:
        return False


def find_file(session):
    for folder in ('sessions', 'archived_sessions'):
        path = next((ROOT / folder).glob(f'**/*{session}*.jsonl'), None)
        if path:
            return path


def process_started(pid):
    value = subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True, timeout=2).strip()
    return dt.datetime.strptime(value, '%a %b %d %H:%M:%S %Y').timestamp()


def owner_session(pid):
    """共享 app-server 没有终端环境时，仅绑定启动时唯一的 TUI 会话。"""
    started = process_started(pid)
    # 只读进程自己的日志映射，可绑定创建于很久以前的恢复会话。
    databases = sorted(ROOT.glob('logs_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
    if databases:
        try:
            children = subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True, timeout=2)
            family = [int(pid)] + [int(p) for p in children.stdout.split()]
            ranges = ' OR '.join('(process_uuid >= ? AND process_uuid < ?)' for _ in family)
            params = [started - 2] + [v for p in family for v in (f'pid:{p}:', f'pid:{p};')]
            with closing(sqlite3.connect(databases[0].as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                deadline = time.monotonic() + .2
                db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
                ids = db.execute(f'SELECT DISTINCT thread_id FROM logs WHERE ts >= ? AND ({ranges}) AND thread_id IS NOT NULL ORDER BY ts DESC LIMIT 20', params).fetchall()
            for (session,) in ids:
                path = find_file(session)
                if path:
                    with path.open() as f:
                        meta = json.loads(f.readline()).get('payload', {})
                    if meta.get('originator') == 'codex-tui' and not isinstance(meta.get('source'), dict):
                        return path, session
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            pass
    matches = []
    for day in {dt.datetime.fromtimestamp(started + n).strftime('%Y/%m/%d') for n in (0, 15)}:
        for path in (ROOT / 'sessions' / day).glob('*.jsonl'):
            try:
                with path.open() as f:
                    meta = json.loads(f.readline()).get('payload', {})
            except (FileNotFoundError, ValueError):
                continue
            if meta.get('originator') != 'codex-tui' or not meta.get('timestamp'):
                continue
            stamp = dt.datetime.fromisoformat(meta['timestamp'].replace('Z', '+00:00')).timestamp()
            if 0 <= stamp - started <= 15:
                matches.append((path, meta.get('id') or meta['session_id']))
    return matches[0] if len(matches) == 1 else None


def latest_account_limits(cache=None):
    """新会话未调用时，读取同一账号最近确认的额度；不借用其他会话的 Token。"""
    try:
        account = json.loads((ROOT / 'auth.json').read_text()).get('tokens', {}).get('account_id')
    except (OSError, ValueError, AttributeError):
        if cache is not None:
            cache.clear()
        return None
    if cache is None:
        cache = {}
    if cache.get('account') != account:
        cache.clear()
        cache['account'] = account
    if not account:
        return None
    cached_files = cache.setdefault('files', {})
    dates = [dt.date.today() - dt.timedelta(days=n) for n in (0, 1)]
    files = [p for day in dates for p in (ROOT / 'sessions' / day.strftime('%Y/%m/%d')).glob('*.jsonl')]
    latest = None
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    for stale in set(cached_files) - set(files):
        del cached_files[stale]
    for path in files:
        stat = path.stat()
        entry = cached_files.get(path)
        if entry is None or entry['inode'] != stat.st_ino or stat.st_size < entry['offset']:
            with path.open('rb') as f:
                meta = json.loads(f.readline()).get('payload', {})
                offset = f.tell()
                if stat.st_size - offset > 1024*1024:
                    f.seek(stat.st_size - 1024*1024)
                    f.readline()
                    offset = f.tell()
            entry = cached_files[path] = dict(inode=stat.st_ino, offset=offset,
                account=meta.get('creator_account_id'), snapshot=None)
        if entry['account'] != account:
            continue
        if stat.st_size > entry['offset']:
            with path.open('rb') as f:
                f.seek(entry['offset'])
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if not line or not line.endswith(b'\n'):
                        entry['offset'] = pos
                        break
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    p = item.get('payload', {})
                    if item.get('type') == 'event_msg' and p.get('type') == 'token_count' and p.get('rate_limits'):
                        stamp = item.get('timestamp', '')
                        previous = entry['snapshot']
                        if stamp and (previous is None or stamp > previous['timestamp']):
                            entry['snapshot'] = dict(timestamp=stamp, limits=p['rate_limits'])
        snapshot = entry['snapshot']
        if snapshot and (latest is None or snapshot['timestamp'] > latest['timestamp']):
            latest = snapshot
    return latest


def apply_account_limits(state, snapshot):
    state['quotas'] = [q for q in state['quotas'] if not q.get('reset') or q['reset'] > time.time()]
    if not snapshot:
        return
    limits = snapshot['limits']
    quotas = []
    for slot in ('primary', 'secondary'):
        q = limits.get(slot)
        if q and q.get('used_percent') is not None and (not q.get('resets_at') or q['resets_at'] > time.time()):
            minutes = q.get('window_minutes')
            name = {300: '5小时', 10080: '周额度'}.get(minutes, f'{minutes}分钟额度')
            quotas.append(dict(name=name, left=100-q['used_percent'], reset=q.get('resets_at')))
    if quotas:
        state['quotas'] = quotas
        state['plan'] = limits.get('plan_type') or state['plan']
        state['quota_updated'] = snapshot['timestamp']


def hook():
    if not os.environ.get('BLEET_HUD') or not os.environ.get('TMUX_PANE'):
        return
    try:
        event = json.load(sys.stdin)
        session = event['session_id']
        path = event.get('transcript_path') or find_file(session)
        if path and Path(path).exists():
            with Path(path).open() as f:
                meta = json.loads(f.readline()).get('payload', {})
            if isinstance(meta.get('source'), dict) or meta.get('originator') not in (None, 'codex-tui'):
                return
        pane = os.environ['TMUX_PANE']
        pid = tmux('display-message', '-p', '-t', pane, '#{pane_pid}')
        if not path:
            try:
                match = owner_session(int(pid))
                if match:
                    path, session = match
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        start(argparse.Namespace(pane=pane, pid=pid, file=path, session=session))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('bleet HUD 暂不可用，Codex 可继续运行。', file=sys.stderr)


def launch():
    """前台启动原生 Codex；关闭终端时销毁所属 tmux 会话。"""
    codex = shutil.which('codex')
    args = sys.argv[2:]
    if os.environ.get('TMUX'):
        os.execvpe(codex, [codex, *args], dict(os.environ, BLEET_HUD='1'))
    tmux_exe = shutil.which('tmux') or '/opt/homebrew/bin/tmux'
    try:
        subprocess.run([tmux_exe, '-V'], check=True, capture_output=True, timeout=2)
        SCRIPT.with_suffix('.tmux.conf').read_text()
    except (OSError, subprocess.SubprocessError):
        print('bleet HUD 暂不可用，启动原生 Codex。', file=sys.stderr)
        os.execvpe(codex, [codex, *args], dict(os.environ, BLEET_HUD='0'))
    name = 'codex-' + str(os.getpid())
    command = 'exec ' + shlex.join([codex, *args])
    bootstrap = shlex.join(['/usr/bin/python3', str(SCRIPT), 'bootstrap', '--pane', '#{pane_id}'])
    os.execvp(shutil.which('tmux') or '/opt/homebrew/bin/tmux', [
        'tmux', '-L', 'bleet-hud', '-f', str(SCRIPT.with_suffix('.tmux.conf')),
        'new-session', '-s', name,
        '-e', 'BLEET_HUD=1', '-e', 'CODEX_HOME=' + str(ROOT), command, ';', 'set-option', '-t', name,
        'destroy-unattached', 'on', ';', 'set-option', '-t', name, 'status', 'off',
        ';', 'run-shell', '-t', name, bootstrap])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'launch':
        launch()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['once', 'watch', 'start', 'hook', 'bootstrap'])
    parser.add_argument('--file', type=Path)
    parser.add_argument('--session')
    parser.add_argument('--pane')
    parser.add_argument('--pid', type=int)
    args = parser.parse_args()
    if args.command == 'bootstrap':
        args.pid = int(tmux('display-message', '-p', '-t', args.pane, '#{pane_pid}'))
        args.session = 'pending'
        start(args)
        return
    if args.command == 'hook':
        hook()
        return
    if args.command == 'start':
        start(args)
        return
    reader = Rollout(args.file or ROOT / 'sessions' / 'pending-session.jsonl', args.session)
    quota_checked = memory_checked = discovery_checked = float('-inf')
    snapshot, memory, quota_cache, previous_lines = None, None, {}, []
    if args.command == 'watch' and sys.stdout.isatty():
        print('\x1b[?1049h\x1b[?25l', end='', flush=True)
    try:
        while args.command == 'once' or not args.pid or alive(args.pid):
            if args.file and not args.file.exists():
                args.file = None
            if not args.file and time.monotonic() - discovery_checked >= 5:
                discovery_checked = time.monotonic()
                if args.pid:
                    try:
                        match = owner_session(args.pid)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        match = None
                    if match:
                        args.file, args.session = match
                if not args.file:
                    args.file = find_file(args.session)
                if args.file:
                    reader = Rollout(args.file, args.session)
            reader.state.pop('error', None)
            try:
                reader.update()
            except ValueError:
                args.file = None
                reader.state['error'] = '会话数据等待重新绑定'
            except (OSError, TypeError, KeyError, AttributeError):
                reader.state['error'] = '会话数据暂不可用'
            if args.command == 'watch':
                if time.monotonic() - quota_checked >= 5:
                    try:
                        snapshot = latest_account_limits(quota_cache)
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        snapshot = None
                        reader.state['error'] = '额度数据暂不可用'
                    quota_checked = time.monotonic()
                apply_account_limits(reader.state, snapshot)
            width = shutil.get_terminal_size((118, 4)).columns
            if args.pid and time.monotonic() - memory_checked >= 5:
                try:
                    memory = process_memory(args.pid)
                except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                    memory = None
                memory_checked = time.monotonic()
            lines = render(reader.state, width, memory)
            if args.command == 'once':
                print('\n'.join(lines))
                return
            if sys.stdout.isatty():
                if len(lines) != len(previous_lines):
                    if os.environ.get('TMUX_PANE'):
                        try:
                            tmux('resize-pane', '-t', os.environ['TMUX_PANE'], '-y', str(len(lines)))
                        except (OSError, subprocess.SubprocessError):
                            pass
                    print('\x1b[2J', end='')
                colors = ['36'] + ['32'] * (len(lines)-3) + ['33', '36']
                for row, (color, line) in enumerate(zip(colors, lines), 1):
                    if len(previous_lines) == len(lines) and previous_lines[row-1] == line:
                        continue
                    print(f'\x1b[{row};1H\x1b[2K\x1b[{color}m{line}\x1b[0m', end='')
            else:
                print('\n'.join(lines))
            previous_lines = lines
            sys.stdout.flush()
            time.sleep(1)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        if args.command == 'watch' and sys.stdout.isatty():
            print('\x1b[?25h\x1b[?1049l', end='', flush=True)


if __name__ == '__main__':
    main()
