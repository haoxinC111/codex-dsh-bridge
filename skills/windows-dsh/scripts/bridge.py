#!/usr/bin/env python3
"""Forward arguments and the current Codex environment to this user's bridge."""
import json
import os
from pathlib import Path
import sys


def main():
    installation = Path(__file__).resolve().parents[1] / 'installation.json'
    try:
        config = json.loads(installation.read_text())
        script = Path(config['bridge_script'])
        state = Path(config['state'])
        if not script.is_absolute() or not script.is_file() or not state.is_absolute():
            raise ValueError('Recorded bridge location is unavailable; inspect installation.json')
    except (OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f'Skill is not configured: {error}. Run scripts/install-skill.py from the project checkout.')
    # An argument vector preserves spaces and avoids executing shell syntax from task data.
    os.execv(sys.executable, [sys.executable, str(script), '--state', str(state), *sys.argv[1:]])


if __name__ == '__main__':
    main()
