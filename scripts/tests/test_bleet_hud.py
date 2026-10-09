"""防止累计 Token 被当上下文、额度窗口错标、会话串线及监听残留。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import select
from contextlib import closing
import tempfile
import time
import unittest
from unittest.mock import patch
from io import StringIO

SCRIPT = Path(__file__).resolve().parents[1] / 'bleet-hud.py'


def records(session='session-a', primary_minutes=10080):
    return [
        {'type': 'session_meta', 'payload': {'id': session, 'source': 'cli', 'cwd': '/project'}},
        {'type': 'turn_context', 'payload': {'model': 'gpt-test', 'effort': 'medium'}},
        {'timestamp': '2026-10-08T07:00:00Z', 'type': 'event_msg', 'payload': {
            'type': 'token_count', 'info': {
                'model_context_window': 100000,
                'total_token_usage': {'input_tokens': 800000, 'cached_input_tokens': 400000,
                                      'output_tokens': 100000, 'total_tokens': 900000},
                'last_token_usage': {'total_tokens': 25000}},
            'rate_limits': {'primary': {'used_percent': 6, 'window_minutes': primary_minutes,
                                        'resets_at': 1792047077}, 'secondary': None,
                            'plan_type': 'prolite'}}}]


class HudTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'HUD 实现尚未存在')
        spec = importlib.util.spec_from_file_location('bleet_hud', SCRIPT)
        self.hud = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hud)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session.jsonl'
        self.path.write_text(''.join(json.dumps(x) + '\n' for x in records()))

    def snapshot(self):
        reader = self.hud.Rollout(self.path, 'session-a')
        reader.update()
        return reader.state

    def test_codex_home_is_used_in_subprocess(self):
        root = Path(self.tmp.name)
        folder = root / 'sessions'
        folder.mkdir()
        path = folder / 'rollout-session-a.jsonl'
        path.write_bytes(self.path.read_bytes())
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), 'once', '--session', 'session-a'],
            env=dict(os.environ, CODEX_HOME=str(root)), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('gpt-test', result.stdout)

    def test_reader_resets_on_truncation_and_same_size_replacement(self):
        reader = self.hud.Rollout(self.path, 'session-a')
        reader.update()
        self.path.write_text(json.dumps(records()[0])+'\n')
        reader.update()
        self.assertEqual(reader.state['total_tokens'], 0)
        self.assertIsNone(reader.state['context_pct'])
        rows = records()
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        reader.update()
        replacement = self.path.with_suffix('.new')
        rows[-1]['payload']['info']['last_token_usage']['total_tokens'] = 50000
        replacement.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        replacement.replace(self.path)
        reader.update()
        self.assertEqual(reader.state['context_pct'], 50)

    def test_owner_resolves_resumed_session_from_process_log(self):
        root = Path(self.tmp.name)
        folder = root / 'sessions' / '2026/01/01'
        folder.mkdir(parents=True)
        rows = records()
        rows[0]['payload']['originator'] = 'codex-tui'
        path = folder / 'rollout-session-a.jsonl'
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        with closing(sqlite3.connect(root/'logs_2.sqlite')) as db:
            db.execute('CREATE TABLE logs(ts INTEGER, process_uuid TEXT, thread_id TEXT)')
            db.execute('INSERT INTO logs VALUES(?,?,?)', (1791480213, 'pid:123:abc', 'session-a'))
            db.execute('INSERT INTO logs VALUES(?,?,?)', (1791480213, 'pid:999:abc', 'foreign'))
            db.commit()
        with patch.object(self.hud, 'ROOT', root), patch.object(self.hud, 'process_started', return_value=1791480212):
            self.assertEqual(self.hud.owner_session(123), (path, 'session-a'))

    def test_missing_or_corrupt_auth_does_not_break_quota_reader(self):
        root = Path(self.tmp.name)
        with patch.object(self.hud, 'ROOT', root):
            self.assertIsNone(self.hud.latest_account_limits())
            (root/'auth.json').write_text('{')
            self.assertIsNone(self.hud.latest_account_limits())

    def test_expired_existing_quota_is_removed(self):
        state = self.snapshot()
        state['quotas'][0]['reset'] = 1
        self.hud.apply_account_limits(state, None)
        self.assertEqual(state['quotas'], [])

    def test_hook_failure_does_not_abort_codex(self):
        event = StringIO(json.dumps({'session_id': 'future-session'}))
        with patch.dict(os.environ, BLEET_HUD='1', TMUX_PANE='%0'), \
                patch.object(self.hud.sys, 'stdin', event), \
                patch.object(self.hud, 'find_file', return_value=None), \
                patch.object(self.hud, 'tmux', side_effect=subprocess.CalledProcessError(1, 'tmux')):
            self.hud.hook()

    def test_launch_runs_native_codex_when_tmux_is_unavailable(self):
        root = Path(self.tmp.name)
        for name, body in [('tmux', 'exit 1'), ('codex', 'printf "NATIVE %s\\n" "$*"')]:
            path = root/name
            path.write_text('#!/bin/sh\n'+body+'\n')
            path.chmod(0o755)
        env = dict(os.environ, PATH=str(root))
        env.pop('TMUX', None)
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), 'launch', '--model', 'test'],
            env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('NATIVE --model test', result.stdout)

    def test_watch_survives_failed_memory_sampler_and_keeps_updating(self):
        root = Path(self.tmp.name)
        ps = root/'ps'
        ps.write_text('#!/bin/sh\nexit 1\n')
        ps.chmod(0o755)
        proc = subprocess.Popen(['/usr/bin/python3', str(SCRIPT), 'watch', '--file', str(self.path),
            '--session', 'session-a', '--pid', str(os.getpid())],
            env=dict(os.environ, CODEX_HOME=str(root), PATH=str(root)+':'+os.environ['PATH']),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observed = ''
        try:
            with self.path.open('a') as f:
                f.write(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-recovered'}})+'\n')
            deadline = time.monotonic() + 5
            while 'gpt-recovered' not in observed and time.monotonic() < deadline:
                if select.select([proc.stdout], [], [], .1)[0]:
                    observed += os.read(proc.stdout.fileno(), 65536).decode()
                if proc.poll() is not None:
                    break
            self.assertIsNone(proc.poll())
        finally:
            proc.terminate()
            output, errors = proc.communicate(timeout=3)
        self.assertIn('gpt-recovered', observed + output)
        self.assertNotIn('Traceback', errors)

    def test_context_uses_latest_not_cumulative_tokens(self):
        state = self.snapshot()
        self.assertEqual(state['context_pct'], 25)
        self.assertEqual(state['total_tokens'], 900000)
        self.assertEqual(state['cache_pct'], 50)

    def test_weekly_primary_is_not_labelled_five_hour(self):
        state = self.snapshot()
        self.assertEqual(state['quotas'][0]['name'], '周额度')
        self.assertEqual(state['quotas'][0]['left'], 94)
        text = '\n'.join(self.hud.render(state, 118, None))
        self.assertNotIn('5H', text)
        self.assertNotIn('暂无逐次账单', text)
        self.assertNotIn('$0', text)

    def test_both_windows_follow_duration_not_slot_order(self):
        items = records(primary_minutes=10080)
        items[-1]['payload']['rate_limits']['secondary'] = {'window_minutes': 300, 'used_percent': 20}
        self.path.write_text(''.join(json.dumps(x) + '\n' for x in items))
        quotas = {q['name']: q['left'] for q in self.snapshot()['quotas']}
        self.assertEqual(quotas, {'周额度': 94, '5小时': 80})

    def test_incremental_read_keeps_partial_line_until_complete(self):
        reader = self.hud.Rollout(self.path, 'session-a')
        reader.update()
        event = json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count',
                            'info': {'last_token_usage': {'total_tokens': 50000}}}})
        with self.path.open('a') as f:
            f.write(event[:30])
        reader.update()
        self.assertEqual(reader.state['context_pct'], 25)
        with self.path.open('a') as f:
            f.write(event[30:] + '\n')
        reader.update()
        self.assertEqual(reader.state['context_pct'], 50)

    def test_wrong_session_rejected_even_when_file_was_supplied(self):
        with self.assertRaises(ValueError):
            self.hud.Rollout(self.path, 'different-session').update()

    def test_tools_track_completion_without_retaining_content(self):
        with self.path.open('a') as f:
            for payload in [
                {'type': 'function_call', 'name': 'exec_command', 'call_id': 'a', 'arguments': 'SECRET'},
                {'type': 'function_call_output', 'call_id': 'a', 'output': 'SECRET'}]:
                f.write(json.dumps({'type': 'response_item', 'payload': payload}) + '\n')
        state = self.snapshot()
        self.assertEqual(state['pending'], {})
        self.assertNotIn('SECRET', repr(state))

    def test_missing_context_and_rate_limits_are_unknown_not_zero(self):
        self.path.write_text(json.dumps(records()[0]) + '\n')
        state = self.snapshot()
        self.assertIsNone(state['context_pct'])
        text = '\n'.join(self.hud.render(state, 80, None))
        self.assertIn('上下文 等待数据', text)
        self.assertNotIn('剩余100%', text)

    def test_layout_groups_weekly_reset_context_tokens_and_optional_five_hour(self):
        state = self.snapshot()
        state['cwd'] = '/tmp/花园项目'
        state['quota_updated'] = '2026-10-08T07:00:00Z'
        state['quotas'][0]['reset'] = 176400
        with patch.object(self.hud.time, 'time', return_value=0):
            lines = self.hud.render(state, 160, None)
        self.assertEqual(len(lines), 4)
        self.assertIn('中等推理 │ 📁 花园项目', lines[0])
        self.assertNotIn('prolite', lines[0])
        self.assertIn('$100', lines[0])
        self.assertNotIn('额度更新', lines[0])
        self.assertIn('周额度', lines[1])
        self.assertIn('2天1小时后重置', lines[1])
        self.assertIn('上下文', lines[2])
        self.assertIn('Token 900.0K', lines[2])
        self.assertIn('缓存 50%', lines[2])
        self.assertTrue(lines[3].startswith('⚙  内存量'))
        state['quotas'].append({'name': '5小时', 'left': 80, 'reset': 3600})
        with patch.object(self.hud.time, 'time', return_value=0):
            lines = self.hud.render(state, 160, None)
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[2].startswith('5H '))
        self.assertIn('剩余80%', lines[2])
        self.assertIn('0天1小时后重置', lines[2])
        self.assertTrue(lines[3].startswith('🧠 上下文'))

    def test_project_name_follows_turn_working_directory(self):
        reader = self.hud.Rollout(self.path, 'session-a')
        reader.update()
        self.assertIn('project', self.hud.render(reader.state, 160, None)[0])
        with self.path.open('a') as f:
            f.write(json.dumps({'type': 'turn_context', 'payload': {'cwd': '/tmp/新项目'}})+'\n')
        reader.update()
        self.assertIn('新项目', self.hud.render(reader.state, 160, None)[0])

    def test_narrow_layout_keeps_reset_and_context_numbers(self):
        state = self.snapshot()
        state['quotas'][0]['reset'] = 176400
        with patch.object(self.hud.time, 'time', return_value=0):
            lines = self.hud.render(state, 52, None)
        self.assertIn('重置2天1小时', lines[1])
        self.assertRegex(lines[1], r'\d{2}/\d{2} \d{2}:\d{2}$')
        self.assertIn('Token 900.0K', lines[2])
        self.assertIn('Token 900.0K', lines[2])
        self.assertIn('░', lines[1])
        self.assertIn('░', lines[2])

    def test_subscription_amounts_follow_user_labels(self):
        state = self.snapshot()
        for plan, amount in [('prolite', '$100'), ('pro', '$200'), ('plus', '$19'), ('go', '$8')]:
            state['plan'] = plan
            first = self.hud.render(state, 160, None)[0]
            self.assertIn(amount, first)
        state['plan'] = 'unknown'
        self.assertNotIn('$', self.hud.render(state, 160, None)[0])

    def test_rows_fit_terminal_cells_and_strip_control_sequences(self):
        state = self.snapshot()
        state['model'] = 'evil\x1b[2J\nmodel'
        for width in (40, 80, 118):
            lines = self.hud.render(state, width, (9, 12*1024, 18*1024))
            self.assertEqual(len(lines), 4)
            self.assertTrue(all(self.hud.cells(line) <= width for line in lines))
            self.assertNotIn('\x1b', ''.join(lines))
            self.assertNotIn('\n', ''.join(lines))

    def test_once_exits_without_creating_background_process(self):
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), 'once', '--file', str(self.path),
                                 '--session', 'session-a'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('gpt-test', result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 4)

    def test_memory_bar_shows_system_and_codex_separately(self):
        line = self.hud.render(self.snapshot(), 118, (125, 9*1024, 18*1024))[3]
        self.assertIn('█████░░░░░ 已用 50%', line)
        self.assertIn(' 9.0/ 18GB', line)
        self.assertIn('Codex   125MB', line)
        lines = self.hud.render(self.snapshot(), 118, (125, 9*1024, 18*1024))
        starts = [self.hud.cells(row[:row.index('█')]) for row in (lines[1], lines[2], lines[3])]
        self.assertEqual(starts, [10, 10, 10])

    def test_gear_has_no_emoji_width_variant(self):
        line = self.hud.render(self.snapshot(), 118, (125, 9000, 18000))[-1]
        self.assertTrue(line.startswith('⚙  内存量 '))
        self.assertNotIn('\ufe0f', self.hud.paint_row(4, line, '36'))

    def test_memory_fields_stay_in_place_when_numbers_change(self):
        state = self.snapshot()
        lines = [self.hud.render(state, 118, value)[-1] for value in
                 [(9, 9*1024, 18*1024), (99, 10*1024, 18*1024), (1000, 18*1024, 18*1024)]]
        self.assertEqual(len({self.hud.cells(line) for line in lines}), 1)
        self.assertEqual(len({line.index('·') for line in lines}), 1)
        self.assertEqual(len({line.index('Codex') for line in lines}), 1)
        self.assertEqual(len({line.index('MB') for line in lines}), 1)

    def test_system_memory_uses_physical_compressor_not_uncompressed_size(self):
        vm = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Anonymous pages: 100.
Pages wired down: 20.
Pages occupied by compressor: 30.
Pages stored in compressor: 1000.
Pages purgeable: 5.
File-backed pages: 200.
'''
        self.assertEqual(self.hud.used_memory(vm, 10000000), 145*16384)

    def test_hook_starts_before_transcript_exists(self):
        event = StringIO(json.dumps({'session_id': 'future-session', 'transcript_path': None}))
        with patch.dict(os.environ, BLEET_HUD='1', TMUX_PANE='%0'), \
                patch.object(self.hud.sys, 'stdin', event), \
                patch.object(self.hud, 'find_file', return_value=None), \
                patch.object(self.hud, 'tmux', return_value='123'), \
                patch.object(self.hud, 'start') as start:
            self.hud.hook()
        self.assertEqual(start.call_args.args[0].session, 'future-session')
        self.assertIsNone(start.call_args.args[0].file)

    def test_pending_binds_unique_terminal_session(self):
        root = Path(self.tmp.name)
        folder = root / 'sessions' / '2026/10/09'
        folder.mkdir(parents=True)
        meta = dict(id='mine', originator='codex-tui', timestamp='2026-10-09T01:23:35+08:00')
        path = folder / 'rollout-mine.jsonl'
        path.write_text(json.dumps(dict(payload=meta)) + '\n')
        with patch.object(self.hud, 'ROOT', root), patch.object(self.hud, 'process_started', return_value=1791480212):
            self.assertEqual(self.hud.owner_session(123), (path, 'mine'))
            other = folder / 'rollout-other.jsonl'
            meta['id'] = 'other'
            other.write_text(json.dumps(dict(payload=meta)) + '\n')
            self.assertIsNone(self.hud.owner_session(123))
            other.unlink()
            meta['originator'] = 'codex-desktop'
            path.write_text(json.dumps(dict(payload=meta)) + '\n')
            self.assertIsNone(self.hud.owner_session(123))

    def test_new_session_reads_only_current_account_quota(self):
        root = Path(self.tmp.name)
        (root / 'auth.json').write_text(json.dumps({'tokens': {'account_id': 'mine'}}))
        folder = root / 'sessions' / self.hud.dt.date.today().strftime('%Y/%m/%d')
        folder.mkdir(parents=True)
        for name, account, stamp, used in [('a', 'mine', '2026-10-08T07:00:00Z', 9),
                                           ('b', 'other', '2026-10-08T08:00:00Z', 80)]:
            rows = records()
            rows[0]['payload']['creator_account_id'] = account
            rows[-1]['timestamp'] = stamp
            rows[-1]['payload']['rate_limits']['primary']['used_percent'] = used
            (folder / (name+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows))
        with patch.object(self.hud, 'ROOT', root):
            snapshot = self.hud.latest_account_limits()
        state = self.snapshot()
        state['total_tokens'], state['context_pct'] = 0, None
        self.hud.apply_account_limits(state, snapshot)
        self.assertEqual(state['quotas'][0]['left'], 91)
        self.assertEqual(state['total_tokens'], 0)
        self.assertIsNone(state['context_pct'])
        self.assertNotIn('额度更新', self.hud.render(state, 118, None)[0])
        self.assertIn('剩余91%', self.hud.render(state, 118, None)[1])

    def test_quota_cache_tracks_appends_partial_lines_and_account_switch(self):
        root = Path(self.tmp.name)
        auth = root / 'auth.json'
        auth.write_text(json.dumps({'tokens': {'account_id': 'mine'}}))
        folder = root / 'sessions' / self.hud.dt.date.today().strftime('%Y/%m/%d')
        folder.mkdir(parents=True)
        path = folder / 'a.jsonl'
        rows = records()
        rows[0]['payload']['creator_account_id'] = 'mine'
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        cache = {}
        with patch.object(self.hud, 'ROOT', root):
            first = self.hud.latest_account_limits(cache)
            self.assertEqual(first['limits']['primary']['used_percent'], 6)
            self.assertEqual(self.hud.latest_account_limits(cache), first)
            rows[-1]['timestamp'] = '2026-10-08T09:00:00Z'
            rows[-1]['payload']['rate_limits']['primary']['used_percent'] = 21
            event = json.dumps(rows[-1])
            with path.open('a') as f:
                f.write(event[:40])
            self.assertEqual(self.hud.latest_account_limits(cache), first)
            with path.open('a') as f:
                f.write(event[40:]+'\n')
            self.assertEqual(self.hud.latest_account_limits(cache)['limits']['primary']['used_percent'], 21)
            auth.write_text(json.dumps({'tokens': {'account_id': 'other'}}))
            self.assertIsNone(self.hud.latest_account_limits(cache))
            auth.write_text(json.dumps({'tokens': {'account_id': 'mine'}}))
            path.write_text(''.join(json.dumps(r)+'\n' for r in records()))
            self.assertIsNone(self.hud.latest_account_limits(cache))

    def test_expired_snapshot_does_not_claim_old_remaining_quota(self):
        state = self.snapshot()
        state['quotas'] = []
        self.hud.apply_account_limits(state, {'timestamp': '2026-01-01T00:00:00Z',
            'limits': {'primary': {'used_percent': 5, 'window_minutes': 10080, 'resets_at': 1}}})
        self.assertEqual(state['quotas'], [])

    def test_watch_pane_exits_when_owner_dies_and_start_is_idempotent(self):
        socket = 'bleet-hud-test-' + str(os.getpid())
        def tmux(*args):
            return subprocess.check_output(['tmux', '-L', socket, *args], text=True).strip()
        tmux('new-session', '-d', '-s', 'test', '-x', '118', '-y', '30', 'sleep 60')
        self.addCleanup(lambda: subprocess.run(['tmux', '-L', socket, 'kill-server'], capture_output=True))
        pane = tmux('display-message', '-p', '-t', 'test', '#{pane_id}')
        pid = tmux('display-message', '-p', '-t', pane, '#{pane_pid}')
        socket_path = tmux('display-message', '-p', '#{socket_path}')
        env = dict(os.environ, TMUX=socket_path + ',0,0', BLEET_HUD='1', CODEX_HOME=self.tmp.name)
        args = ['/usr/bin/python3', str(SCRIPT), 'start', '--file', str(self.path),
                '--session', 'session-a', '--pane', pane, '--pid', pid]
        for _ in range(2):
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
        panes = tmux('list-panes', '-t', 'test', '-F', '#{pane_id}').splitlines()
        self.assertEqual(len(panes), 2)
        hud_pane = next(p for p in panes if p != pane)
        time.sleep(1)
        self.assertIn('bleet', tmux('capture-pane', '-p', '-t', hud_pane))
        self.assertEqual(tmux('display-message', '-p', '-t', hud_pane, '#{pane_height}'), '4')
        for width, height in [(60, 50), (118, 30)]:
            tmux('resize-window', '-t', 'test', '-x', str(width), '-y', str(height))
            time.sleep(1.3)
            self.assertEqual(tmux('display-message', '-p', '-t', hud_pane, '#{pane_height}'), '4')
            screen = tmux('capture-pane', '-p', '-t', hud_pane)
            self.assertIn('内存量', screen)
            self.assertIn('上下文', screen)
        event = records()[-1]
        event['payload']['rate_limits']['secondary'] = {'window_minutes': 300, 'used_percent': 20}
        with self.path.open('a') as f:
            f.write(json.dumps(event)+'\n')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if tmux('display-message', '-p', '-t', hud_pane, '#{pane_height}') == '5':
                break
            time.sleep(.1)
        lines = tmux('capture-pane', '-p', '-t', hud_pane).splitlines()
        self.assertIn('5H ', lines[2])
        self.assertIn('上下文', lines[3])
        self.assertIn('内存量', lines[4])
        starts = [self.hud.cells(lines[i][:lines[i].index('█')]) for i in (1, 3, 4)]
        self.assertEqual(len(set(starts)), 1)
        event['payload']['rate_limits']['secondary'] = None
        with self.path.open('a') as f:
            f.write(json.dumps(event)+'\n')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if tmux('display-message', '-p', '-t', hud_pane, '#{pane_height}') == '4':
                break
            time.sleep(.1)
        output = tmux('capture-pane', '-p', '-t', hud_pane)
        self.assertNotIn('5H ', output)
        self.assertIn('内存量', output)
        tmux('set-option', '-p', '-t', pane, 'remain-on-exit', 'on')
        os.kill(int(pid), 15)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if hud_pane not in tmux('list-panes', '-t', 'test', '-F', '#{pane_id}').splitlines():
                break
            time.sleep(.2)
        self.assertEqual(tmux('list-panes', '-t', 'test', '-F', '#{pane_id}'), pane)


if __name__ == '__main__':
    unittest.main()
