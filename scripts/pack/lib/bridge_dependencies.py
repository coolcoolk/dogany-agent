#!/usr/bin/env python3
"""Check direct Python imports across the incoming/held bridge boundary.

Consumes the actual reconcile report (LANDED/NEW), never reclassifies files.
No imports of payload code. Exit 4 blocks a missing or statically uncertain
binding; exit 2 is a scanner/input error. Exit 0 covers only static imports,
not runtime attributes, importlib, exec, cycles or third-party dependencies.
"""
import argparse
import ast
import json
from pathlib import Path
import sys


def module_path(root, module):
    stem = root.joinpath(*module.split('.'))
    for path in (stem.with_suffix('.py'), stem / '__init__.py'):
        if path.is_file():
            return path
    return None


def bindings(tree):
    """Unconditional module bindings; conditional/dynamic exports are unknown."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != '*':
                    names.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.AnnAssign) and node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(n.id for n in ast.walk(target)
                             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        elif isinstance(node, ast.Delete):
            names.difference_update(n.id for n in ast.walk(node) if isinstance(n, ast.Name))
    return names


def check(incoming, local, report):
    landed = set()
    for line in report.splitlines():
        state, _, rel = line.partition(' ')
        if state in {'LANDED', 'NEW'} and rel.endswith('.py'):
            path = Path(rel)
            if path.is_absolute() or '..' in path.parts or path.parts[0] != 'bridge':
                raise ValueError('invalid reconcile path')
            landed.add(Path(*path.parts[1:]))
    issues = []
    cache = {}
    for rel in sorted(landed):
        tree = ast.parse((incoming / rel).read_bytes(), filename=str(rel))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ImportFrom, ast.Import)):
                continue
            if isinstance(node, ast.ImportFrom):
                parts = list(rel.parent.parts)
                if node.level:
                    if node.level > len(parts) + 1:
                        continue
                    parts = parts[:len(parts) - node.level + 1]
                    module = '.'.join(parts + ([node.module] if node.module else []))
                else:
                    module = node.module or ''
                requests = [(module, alias.name) for alias in node.names]
                # "from . import sibling" names a submodule, not a binding.
                if not node.module:
                    requests = [('.'.join(filter(None, (module, a.name))), None)
                                for a in node.names]
            else:
                requests = [(a.name, None) for a in node.names]
            for module, name in requests:
                if module.startswith('bridge.'):
                    module = module[len('bridge.'):]
                provider = module_path(incoming, module)
                if provider is None:
                    continue  # external to this bridge payload
                provider_rel = provider.relative_to(incoming)
                if provider_rel in landed:
                    continue
                held = local / provider_rel
                if held.is_file() and held.read_bytes() == provider.read_bytes():
                    continue
                reason = None
                if not held.is_file():
                    reason = 'missing-provider'
                elif name is not None:
                    if held not in cache:
                        cache[held] = bindings(ast.parse(held.read_bytes(), filename=str(held)))
                    if name == '*':
                        reason = 'unverified-star-import'
                    elif name not in cache[held]:
                        # Package exports can be submodules.
                        child = module_path(local, module + '.' + name)
                        if provider_rel.name != '__init__.py' or child is None:
                            reason = 'missing-or-unverified-binding'
                else:
                    # Module import succeeds but its runtime attribute contract
                    # cannot be proven by checking exported names alone.
                    reason = 'unverified-module-import'
                if reason:
                    issues.append(dict(consumer='bridge/' + rel.as_posix(),
                                       line=node.lineno,
                                       provider='bridge/' + provider_rel.as_posix(),
                                       symbol=name, reason=reason))
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--incoming', type=Path, required=True)
    parser.add_argument('--local', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        issues = check(args.incoming, args.local, args.report.read_text())
    except (OSError, SyntaxError, ValueError) as exc:
        print(json.dumps({'status': 'scanner-error', 'error': str(exc)}), file=sys.stderr)
        return 2
    if issues:
        print(json.dumps({'status': 'dependency-block', 'issues': issues}, sort_keys=True),
              file=sys.stderr)
        return 4
    return 0


if __name__ == '__main__':
    sys.exit(main())
