import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

INSTALL = Path(__file__).resolve().parents[1] / 'install.py'

class InstallTests(unittest.TestCase):
    def test_install_is_idempotent_and_uninstall_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            codex = root/'.codex'
            codex.mkdir()
            original = {'type': 'command', 'command': 'keep-other-hook'}
            (codex/'hooks.json').write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [original]}]}}))
            (codex/'config.toml').write_text('model = "test"\n[features]\nhooks = false\nkeep = true\n[tui]\nalternate_screen = "never"\n')
            (root/'.zshrc').write_text('# keep my shell\n')
            for name in ('codex', 'tmux'):
                path = root/name
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)
            env = dict(os.environ, HOME=folder, CODEX_HOME=str(codex), PATH=folder, SHELL='/bin/zsh')
            for _ in range(2):
                subprocess.run(['/usr/bin/python3', str(INSTALL)], env=env, check=True, capture_output=True)
            events = json.loads((codex/'hooks.json').read_text())['hooks']['SessionStart']
            self.assertEqual(len(events), 2)
            self.assertEqual((root/'.zshrc').read_text().count('source '), 1)
            self.assertIn('hooks = true', (codex/'config.toml').read_text())
            self.assertTrue((codex/'scripts/bleet-hud.py').exists())
            subprocess.run(['/usr/bin/python3', str(INSTALL), '--uninstall'], env=env, check=True, capture_output=True)
            self.assertEqual(json.loads((codex/'hooks.json').read_text())['hooks']['SessionStart'], [{'hooks': [original]}])
            self.assertIn('keep = true', (codex/'config.toml').read_text())
            self.assertIn('alternate_screen = "never"', (codex/'config.toml').read_text())
            self.assertEqual((root/'.zshrc').read_text().strip(), '# keep my shell')
            self.assertFalse((codex/'scripts/bleet-hud.py').exists())
