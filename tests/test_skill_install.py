import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge skill ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / 'skills' / 'windows-dsh'
        self.state = self.root / 'private state'

    def install(self):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/install-skill.py'),
                               '--destination', str(self.destination), '--state', str(self.state)],
                              capture_output=True, text=True)

    def test_installed_launcher_uses_selected_state_and_preserves_current_thread(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = self.destination / 'scripts' / 'bridge.py'
        env = {**os.environ, 'CODEX_THREAD_ID': 'test-origin-thread'}
        run = subprocess.run([sys.executable, str(launcher), 'init', '--host', '127.0.0.1',
                              '--worker-ip', '127.0.0.1', '--codex-cli', sys.executable],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        config = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(config['thread_id'], 'test-origin-thread')
        self.assertEqual(Path(json.loads(run.stdout)['state']), self.state.resolve())
        self.assertFalse((self.destination / 'config.json').exists())
        again = self.install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout)['status'], 'already_installed')

    def test_existing_custom_skill_is_never_overwritten(self):
        self.destination.mkdir(parents=True)
        original = self.destination / 'SKILL.md'
        original.write_text('Existing user skill')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(original.read_text(), 'Existing user skill')
        self.assertFalse((self.destination / 'installation.json').exists())
        self.assertFalse(self.state.exists())

    def test_existing_empty_or_symlink_destination_is_refused(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.mkdir()
        self.assertNotEqual(self.install().returncode, 0)
        self.destination.rmdir()
        target = self.root / 'elsewhere'
        self.destination.symlink_to(target, target_is_directory=True)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
