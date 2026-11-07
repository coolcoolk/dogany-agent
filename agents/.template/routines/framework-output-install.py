#!/usr/bin/env python3
"""FDGN-579: activate and verify the installed framework output hooks.

No services or user-level config are changed. Existing project Codex config
is extended once; Codex's runtime trust remains verified by hooks/list at
spawn time. Claude settings are already rebuilt by mint/update.
"""
import argparse
import json
import os
import re
from pathlib import Path
import tempfile

# Rework A: the answer-end output check is retired. This is the block older
# installs wrote; install() strips it, nothing writes it again.
RETIRED_CODEX_STOP = '''
# Framework output gate (FDGN-579).
[[hooks.Stop]]
hooks = [
  { type = "command", command = "python3 routines/codex-hook-adapter.py --output-stop", timeout = 20 },
]
'''
# Claude Stop commands that used to hold a finished answer. None may remain.
RETIRED_CLAUDE_STOP = (
    'skill-feedback-gate.py --stop', 'why-frame-gate.py --stop',
    'onboarding-gate-stop.py', 'output-gate-stop.py', 'text-seat-stop.py',
    'owner-ask-stop.py', 'decision-ledger.py stop',
)
CODEX_TRACK = '''
[[hooks.PreToolUse]]
matcher = "*"
hooks = [
  { type = "command", command = "python3 routines/skill-update-track.py", timeout = 15 },
]
'''


def active_text(text):
    # A commented example is not active wiring. Runtime trust is still checked
    # by Codex hooks/list; this installer only manages its project commands.
    return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))


RETIRED_PRE = (
    'why-frame-gate.py', 'token-gate.py', 'tmp-artifact-gate.py',
    'skill-landing-gate.py', 'bg-job-notice.py --gate', 'decision-ledger.py pre',
)


def retired(event, command):
    names = RETIRED_CLAUDE_STOP if event == 'Stop' else RETIRED_PRE if event == 'PreToolUse' else ()
    return any(re.search(r'(?:^|[/\s])' + re.escape(name) + r'(?=$|[\s"\'])', command)
               for name in names)


def scrub_settings(doc):
    """Remove retired commands, preserving instance hooks and sibling entries."""
    hooks = doc.get('hooks', {})
    for event, groups in hooks.items():
        kept = []
        for group in groups:
            if 'command' in group:
                if not retired(event, group['command']):
                    kept.append(group)
                continue
            group['hooks'] = [h for h in group.get('hooks', [])
                              if not retired(event, h.get('command', ''))]
            for hook in group['hooks']:
                if event == 'PreToolUse':
                    hook['command'] = re.sub(
                        r'\b(BASH_GATE_MODE|WRITE_PATHS_D4_MODE)=\S+',
                        r'\1=detect', hook.get('command', ''))
            if group['hooks']:
                kept.append(group)
        hooks[event] = kept
    return doc


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
        if path.exists():
            os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def migrate(root):
    """Also cover preserved settings and previously carried local hooks."""
    for rel in ('.claude/settings.json', '.claude/settings.local.json'):
        path = root / rel
        if not path.exists():
            continue
        old = path.read_text()
        doc = json.loads(old)
        before = json.dumps(doc, sort_keys=True)
        scrub_settings(doc)
        if json.dumps(doc, sort_keys=True) != before:
            atomic_write(path, json.dumps(doc, indent=2) + '\n')
    path = root / 'config/agent.conf'
    if path.exists():
        old = path.read_text()
        out = []
        for line in old.splitlines(keepends=True):
            match = re.match(r'\s*(?:export\s+)?([A-Z0-9_]+)\s*=', line)
            key = match.group(1) if match else ''
            value = None
            if key.startswith('GATE_STOP_') and key != 'GATE_STOP_DECOMP_LEDGER':
                value = 'off'
            elif key in ('GATE_TMP_ARTIFACT', 'GATE_SKILL_LANDING', 'GATE_BG_JOB_NAME',
                         'OWNER_ASK_MODE', 'OUTPUT_GATE_WHAT_PICK_MODE'):
                value = 'off'
            elif key in ('BASH_GATE_MODE', 'WRITE_PATHS_D4_MODE', 'SELF_RESTART_OWNER_GATE'):
                value = 'detect'
            elif key == 'FINAL_LANG_GATE':
                value = '0'
            out.append(key + '=' + value + '\n' if value is not None else line)
        new = ''.join(out)
        if new != old:
            atomic_write(path, new)


def install(root):
    migrate(root)
    path = root / '.codex/config.toml'
    text = path.read_text() if path.exists() else ''
    # Preserve unrelated tables and custom hooks sharing a framework group.
    groups = re.split(r'(?=^\s*\[)', text, flags=re.M)
    cleaned = []
    def retired_codex(item):
        return 'codex-hook-adapter.py' in item and bool(re.search(
            r'--(?:output-stop|stop)(?=[\s"\']|$)|'
            r'--gate(?:=|\s+)(?:skill-landing|tmp-artifact|why-frame)(?=[\s"\']|$)', item))
    for group in groups:
        if group.lstrip().startswith('[[hooks.'):
            group = re.sub(r'\{[^{}]*\}\s*,?',
                lambda m: '' if retired_codex(m.group()) else m.group(), group)
            if re.search(r'hooks\s*=\s*\[\s*\]', active_text(group)):
                continue
        cleaned.append(group)
    new = ''.join(cleaned)
    if 'routines/skill-update-track.py' not in active_text(new):
        new += CODEX_TRACK
    if new != text:
        atomic_write(path, new)


def verify(root):
    doc = json.loads((root / '.claude/settings.json').read_text())
    bad = []
    # Claude merges both files. Checking only framework settings can report
    # success while a previously carried local hook still blocks the owner.
    for rel in ('.claude/settings.json', '.claude/settings.local.json'):
        path = root / rel
        if not path.exists():
            continue
        settings = json.loads(path.read_text())
        for event, groups in settings.get('hooks', {}).items():
            for group in groups:
                for hook in ([group] if 'command' in group else group.get('hooks', [])):
                    command = hook.get('command', '')
                    if retired(event, command):
                        bad.append(f'{rel}: retired {event} hook still wired: {command}')
    tracks = [h for group in doc['hooks'].get('PreToolUse', [])
              if group.get('matcher') == '*' for h in group['hooks']
              if h.get('command', '').endswith('/routines/skill-update-track.py')]
    if len(tracks) != 1:
        bad.append('Claude pre-tool instruction snapshot must be active exactly once')
    for rel in ('routines/lib/output_gate.py',
                'routines/lib/hook_paths.py',
                'routines/skill-layer.py', 'routines/skill-landing-gate.py',
                'routines/skill-update-track.py',
                'routines/codex-hook-adapter.py'):
        if not (root / rel).is_file():
            bad.append('missing ' + rel)
    for rel, marker in (('routines/lib/output_gate.py', 'def recurring_behavior_unkept'),
                        ('routines/skill-layer.py', 'def check_update'),
                        ('routines/codex-hook-adapter.py', 'def run_output_stop')):
        if (root / rel).is_file() and marker not in (root / rel).read_text():
            bad.append('outdated gate implementation: ' + rel)
    codex = active_text((root / '.codex/config.toml').read_text())
    if '--output-stop' in codex:
        bad.append('Codex project hook still wires the retired --output-stop')
    for required in ('routines/skill-update-track.py',):
        if required not in codex:
            bad.append('Codex project hook missing: ' + required)
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True, type=Path)
    ap.add_argument('--wire', action='store_true')
    args = ap.parse_args()
    try:
        if args.wire:
            install(args.root)
        bad = verify(args.root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        bad = [str(exc)]
    print(json.dumps({'ok': not bad, 'diagnostics': bad,
                      'runtime_trust': 'requires trusted engine write/snapshot hooks in native hooks/list; '
                      'legacy managed verification alone does not prove the project snapshot hook'}))
    return int(bool(bad))


if __name__ == '__main__':
    raise SystemExit(main())
