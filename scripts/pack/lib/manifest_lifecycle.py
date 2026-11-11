#!/usr/bin/env python3
"""DGN-1832 manifest lifecycle seam, shared by lint/install/crew update.

post_install is an ordered list of command strings. shlex only tokenizes;
there is no shell expansion or PATH lookup. The executable is a delivered
payload-relative file; cwd is the instance root. Hooks must be idempotent:
run once per successful install attempt (per member for crew updates), and
may run again on an explicit retry. Failure stops the list, loudly, without
rolling back installed bytes or earlier hook effects.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

DATA_STORE_KEYS = ('db', 'dump', 'cli', 'version_file')


class PostInstallError(ValueError):
    def __init__(self, command, returncode):
        super().__init__("post_install %r failed (rc=%d); no rollback performed" %
                         (command, returncode))
        self.returncode = returncode


def relative(value, key):
    if (not isinstance(value, str) or not value or value.startswith('/')
            or any(c in value for c in ('\\', '\n', '\r', '\x00'))
            or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('%s must be a nonempty relative path without traversal' % key)
    return value


def contained_file(base, rel, key):
    base = Path(base).resolve()
    path = base / relative(rel, key)
    if base not in path.resolve().parents or not path.is_file():
        raise ValueError('%s file missing or outside its root: %s' % (key, rel))
    return path


def validate(manifest, payload):
    keys = ('first_install_skill', 'first_install_doc')
    if any(k in manifest for k in keys):
        skill = manifest.get(keys[0])
        if not isinstance(skill, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', skill):
            raise ValueError('first_install_skill must be a skill id; both first_install keys are required')
        doc = relative(manifest.get(keys[1]), keys[1])
        contained_file(payload, 'skills-bundle/%s/SKILL.md' % skill, keys[0])
        contained_file(Path(payload) / 'skills-bundle' / skill, doc, keys[1])
    if 'data_store' in manifest:
        # DGN-1807: the kit names its own DB for database/restore-data.sh.
        # Instance-relative; cli and version_file ship in the payload.
        store = manifest['data_store']
        if not isinstance(store, dict):
            raise ValueError('data_store must be an object')
        for key in DATA_STORE_KEYS:
            relative(store.get(key), 'data_store.' + key)
        for key in ('cli', 'version_file'):
            contained_file(payload, store[key], 'data_store.' + key)
    commands = manifest.get('post_install', [])
    if not isinstance(commands, list):
        raise ValueError('post_install must be a list of command strings')
    parsed = []
    for command in commands:
        if not isinstance(command, str) or any(c in command for c in ('\n', '\r', '\x00')):
            raise ValueError('post_install entries must be command strings on one line')
        argv = shlex.split(command)
        if not argv:
            raise ValueError('post_install command must not be empty')
        script = contained_file(payload, argv[0], 'post_install')
        if not os.access(script, os.X_OK):
            raise ValueError('post_install executable is not executable: %s' % argv[0])
        parsed.append(argv)
    return parsed


def record(manifest, root):
    if manifest.get('kind') != 'kit' and not any(
            key in manifest for key in ('first_install_skill', 'first_install_doc', 'post_install',
                                        'data_store', 'requires_kit')) and not (
            Path(root) / 'config/packs' / (str(manifest.get('id')) + '.manifest.json')).is_file():
        return
    pack_id = manifest.get('id') or manifest.get('name')
    if not isinstance(pack_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', pack_id):
        raise ValueError('cannot record manifest: invalid pack id')
    directory = Path(root) / 'config/packs'
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.%s.manifest.' % pack_id, dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(manifest, stream, indent=2)
            stream.write('\n')
        os.replace(tmp, directory / (pack_id + '.manifest.json'))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(commands, root):
    root = Path(root).resolve()
    for argv in commands:
        rel = argv[0]
        # The installer places the payload skill surface under .claude/.
        if rel.startswith('skills-bundle/'):
            rel = '.claude/' + rel
        script = contained_file(root, rel, 'post_install installed executable')
        result = subprocess.run([str(script), *argv[1:]], cwd=root, shell=False)
        if result.returncode:
            raise PostInstallError(argv[0], result.returncode)


def main(argv):
    if len(argv) not in (4, 5) or argv[1] not in ('validate', 'record', 'run'):
        raise ValueError('usage: manifest_lifecycle.py validate|record|run MANIFEST PAYLOAD [INSTANCE]')
    manifest = json.loads(Path(argv[2]).read_text())
    commands = validate(manifest, argv[3])
    if argv[1] != 'validate':
        if len(argv) != 5:
            raise ValueError('instance root required')
        if argv[1] == 'record':
            record(manifest, argv[4])
        else:
            run(commands, argv[4])


if __name__ == '__main__':
    try:
        main(sys.argv)
    except PostInstallError as exc:
        print('[manifest-lifecycle] ERROR: %s' % exc, file=sys.stderr)
        sys.exit(exc.returncode)
    except (ValueError, OSError) as exc:
        print('[manifest-lifecycle] ERROR: %s' % exc, file=sys.stderr)
        sys.exit(1)
