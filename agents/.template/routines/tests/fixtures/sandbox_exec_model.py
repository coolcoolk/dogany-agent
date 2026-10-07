#!/usr/bin/env python3
"""Model of the macOS seatbelt profile semantics dispatch-detached.sh relies on.

Not a suite (fixtures/ is excluded from lane discovery). Two uses:

  1. As a library/CLI VERDICT tool:
       sandbox_exec_model.py verdict <profile> <op> <path>
     prints "allow" or "deny" for one operation (file-write*, file-read*,
     process-exec) on one path, applying the rules the way the kernel does:
     every rule is checked in file order and the LAST matching rule wins.

  2. As a STAND-IN for `sandbox-exec` on hosts that have none (Linux CI):
       sandbox-exec -f <profile> <cmd> [args...]
     (install a wrapper named sandbox-exec that execs this file with the
     argument "exec" first). The profile must parse (the compile check);
     mkdir/touch are evaluated against the model before running, so the
     launcher's self-check probes see the verdict the rendered profile would
     get. Every other command runs UNCONFINED -- this is a profile checker,
     not a sandbox. Kernel behaviour itself is proven only on macOS (the
     real-sandbox blocks of the suites).

Supported vocabulary = exactly what the dispatch renderers emit:
  (version 1) (allow default) (allow|deny <op> [filters...])
  filters: (subpath "p") (literal "p") (regex #"re")
"""
import os
import re
import sys


def _tokens(text):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == ";":
            while i < n and text[i] != "\n":
                i += 1
        elif c.isspace():
            i += 1
        elif c in "()":
            yield c
            i += 1
        elif c == '"' or (c == "#" and i + 1 < n and text[i + 1] == '"'):
            if c == "#":
                i += 1
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ValueError("unterminated string")
            yield ("str", "".join(buf), c == "#")
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "();":
                j += 1
            yield ("sym", text[i:j])
            i = j


def _parse(text):
    stack, top = [], []
    cur = top
    for t in _tokens(text):
        if t == "(":
            new = []
            cur.append(new)
            stack.append(cur)
            cur = new
        elif t == ")":
            if not stack:
                raise ValueError("unbalanced )")
            cur = stack.pop()
        else:
            cur.append(t)
    if stack:
        raise ValueError("unbalanced (")
    return top


def load(path):
    with open(path, encoding="utf-8") as fh:
        forms = _parse(fh.read())
    rules = []
    for f in forms:
        if not f or not isinstance(f[0], tuple):
            raise ValueError("bad form")
        head = f[0][1]
        if head == "version":
            continue
        if head not in ("allow", "deny"):
            raise ValueError("unknown form %r" % head)
        if len(f) < 2 or not isinstance(f[1], tuple):
            raise ValueError("rule without operation")
        op = f[1][1]
        filters = []
        for flt in f[2:]:
            if not isinstance(flt, list) or len(flt) != 2:
                raise ValueError("bad filter")
            kind = flt[0][1]
            arg = flt[1]
            if kind not in ("subpath", "literal", "regex") or arg[0] != "str":
                raise ValueError("bad filter %r" % kind)
            if kind == "regex":
                if not arg[2]:
                    raise ValueError("regex needs #\"...\"")
                filters.append((kind, re.compile(arg[1])))
            else:
                filters.append((kind, arg[1].replace('\\"', '"')))
        rules.append((head, op, filters))
    return rules


def _match(filters, path):
    if not filters:
        return True
    for kind, arg in filters:
        if kind == "subpath" and (path == arg or path.startswith(arg.rstrip("/") + "/")):
            return True
        if kind == "literal" and path == arg:
            return True
        if kind == "regex" and arg.search(path):
            return True
    return False


def _canonical(path):
    # The kernel compares canonical paths: resolve the parent, keep the leaf.
    path = os.path.abspath(path)
    parent, leaf = os.path.split(path)
    return os.path.join(os.path.realpath(parent), leaf)


def verdict(rules, op, path):
    path = _canonical(path)
    result = "deny"
    for head, rop, filters in rules:
        if rop == "default" or rop == op or (rop.endswith("*") and op.startswith(rop[:-1])):
            if rop == "default" or _match(filters, path):
                result = head
    return result


def _exec(argv):
    if len(argv) < 3 or argv[0] != "-f":
        sys.stderr.write("usage: sandbox-exec -f <profile> cmd...\n")
        return 64
    try:
        rules = load(argv[1])
    except (OSError, ValueError) as exc:
        sys.stderr.write("sandbox-exec: %s\n" % exc)
        return 65
    cmd = argv[2:]
    if os.path.basename(cmd[0]) in ("mkdir", "touch"):
        for arg in cmd[1:]:
            if arg.startswith("-"):
                continue
            if verdict(rules, "file-write*", arg) == "deny":
                sys.stderr.write("%s: %s: Operation not permitted\n" % (cmd[0], arg))
                return 1
    os.execvp(cmd[0], cmd)
    return 127


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "exec":
        return _exec(sys.argv[2:])
    if len(sys.argv) == 5 and sys.argv[1] == "verdict":
        print(verdict(load(sys.argv[2]), sys.argv[3], sys.argv[4]))
        return 0
    sys.stderr.write(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main())
