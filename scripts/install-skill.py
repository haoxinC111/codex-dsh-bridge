#!/usr/bin/env python3
"""Install the portable skill without replacing an existing personal skill."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def install(destination, state):
    source = ROOT / 'skills/windows-dsh'
    record = (json.dumps({'repository': str(ROOT), 'bridge_script': str(ROOT / 'bridge.py'),
                          'state': str(state)}, ensure_ascii=False, indent=2) + '\n').encode()
    expected = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*')
                if p.is_file() and '__pycache__' not in p.parts}
    expected['installation.json'] = record
    if destination.is_symlink():
        raise ValueError('Skill destination is a symlink; existing installation was preserved')
    if destination.exists():
        # Re-running the same installation is safe. Customizations require a deliberate migration.
        if destination.is_dir() and all((destination / name).is_file()
                                       and (destination / name).read_bytes() == data
                                       for name, data in expected.items()):
            return {'status': 'already_installed', 'skill': str(destination)}
        raise ValueError('An existing skill differs; nothing was overwritten. Choose another --destination or review it first')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bridge-skill-', dir=destination.parent) as temp:
        staged = Path(temp) / 'windows-dsh'
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (staged / 'installation.json').write_bytes(record)
        staged.rename(destination)
    return {'status': 'installed', 'skill': str(destination), 'state': str(state)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    parser.add_argument('--destination', type=Path, default=codex_home / 'skills/windows-dsh')
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/share/codex-dsh-bridge')
    args = parser.parse_args()
    # Do not resolve the final destination symlink: refuse it instead of following it.
    destination = Path(os.path.abspath(args.destination.expanduser()))
    result = install(destination, args.state.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
