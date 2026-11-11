"""bash_safety -- D3 classifier: what a Bash command would do to services,
remotes and files.  v2.7, wired in DETECT mode (bash-safety-gate.py).  Spec:
docs/v27-D3-D4-spec.md section 3 (reworked after docs/v27-D3-D4-grill.md and
docs/v27-D3-D4-grill-r2.md: R2-5 symlink scratch, R2-8 knobs, R2-9 git
config / send-pack / gh api).

classify(command, ctx) -> Result
  Result.findings  [Finding]: every gated thing the command does
  Result.rewrite   the command with each top-level `rm` replaced by the trash
                   helper, or None when nothing was rewritten
  Result.allowed   [str]: notes for allowed-by-exception hits (canary only)

The tokenizer (tokens / segments) is shared with D4 (write-paths-d4.py) so
both gates read a command the same way: quotes (incl. ANSI-C $'..'),
operators, redirections, heredoc bodies, `$(..)` / backticks / `bash -c` /
`eval` / `ssh` bodies, a heredoc fed to a shell or ssh, xargs / find -exec /
launchctl asuser inner commands (classified as nested commands; a nested hit
is never rewritten), prefix commands with their own arguments (sudo -u X,
nice -n N, env -u V, timeout -s S ...), and `bash|sh|zsh [-flags] <script>`
/ `source` / `.` wrappers (unwrapped to the script).

Rows the hook cannot see well moved out (grill section D): release /
publish scripts check a recorded owner approval themselves
(scripts/release-approval.py), the push remote URL and tag refs are judged
by git-hooks/pre-push, and bridge/self_restart.sh guards its owner-only
flags in its own parser.  What stays here: service ops with no script in
between, destructive git / disk ops, force / delete pushes, the rm rewrite,
gh release / package publish (no script, no pre-push), and anything that
disarms those devices (gate-switch off, --no-verify, core.hooksPath).

HONEST LIMITS (DGN-1194 discipline).  This is a syntactic gate: it is
closed only on the shapes it parses.  Not seen: write-then-run (`echo 'rm
-rf x' > /tmp/s.sh && bash /tmp/s.sh` -- the script's text is data when
written), interpreter one-liners (`python3 -c "shutil.rmtree(..)"`, `perl
-e unlink`), truncation by redirection (`: > file`), `mv` / `cp` over an
existing file, `rsync --delete`, aliases and shell functions.  The
backstops that do not depend on parsing: git-hooks/pre-push (remote URL +
tag refs), release-approval.py (release / publish scripts), the DGN-1504
worker sandbox, and self-update.sh's dirty-checkout refusal, which surfaces
Bash-side framework edits after the fact.

Pure stdlib.  English/ASCII only.
"""

import itertools
import os
import re
import subprocess

# --- tokenizer ---------------------------------------------------------------

OPS2 = ("&&", "||", ";;", "$(", "<(", ">(")
SEP_OPS = {"&&", "||", ";", ";;", "|", "&", "\n", "(", ")", "$(", "`",
           "<(", ">("}
REDIR_RE = re.compile(r"(\d*|&)(>>|>\||>&|>|<<<|<<-|<<|<&|<>|<)")
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while",
            "until", "for", "case", "esac", "{", "}", "!", "in", "function"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
WORD_END = " \t\n;|&()<>`"

# Prefix commands and the flags that take a value (F8).  An unknown flag on a
# prefix makes the segment unparsed (denied only when it names a gated verb).
PREFIX_ARG = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-D", "-T",
             "-R", "--user", "--group", "--host", "--prompt", "--chdir",
             "--role", "--type", "--close-from", "--other-user", "--chroot",
             "--command-timeout"},
    "nice": {"-n", "--adjustment"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "exec": {"-a"},
    "caffeinate": {"-t", "-w"},
    "ionice": {"-c", "-n", "-p"},
    "nohup": set(), "time": set(), "command": set(), "builtin": set(),
}
PREFIX_NOARG = {
    "sudo": {"-E", "-H", "-n", "-S", "-k", "-K", "-b", "-i", "-s", "-P",
             "-A", "-B", "-N", "--preserve-env", "--set-home",
             "--non-interactive", "--stdin", "--background", "--login",
             "--shell", "--askpass"},
    "env": {"-i", "-0", "-v", "--ignore-environment", "--null", "--debug",
            "-"},
    "timeout": {"--preserve-status", "--foreground", "-v", "--verbose"},
    "time": {"-p", "-l", "-v"},
    "command": {"-p"},
    "exec": {"-c", "-l"},
    "caffeinate": {"-d", "-i", "-m", "-s", "-u"},
    "ionice": {"-t"},
    "nice": set(), "stdbuf": set(), "nohup": set(), "builtin": set(),
}
SHELL_ARG_FLAGS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}


class Tok(object):
    __slots__ = ("kind", "value", "start", "end", "quoted", "body")

    def __init__(self, kind, value, start, end, quoted=False):
        self.kind, self.value, self.start, self.end = kind, value, start, end
        self.quoted, self.body = quoted, None

    def __repr__(self):
        return "%s(%r)" % (self.kind, self.value)


class ParseError(ValueError):
    pass


def _heredoc_word(s, i):
    """(strip_tabs, word, end) for a heredoc operator ending at s[i]."""
    n, strip = len(s), False
    if s.startswith("-", i):
        strip, i = True, i + 1
    while i < n and s[i] in " \t":
        i += 1
    k = i
    while k < n and s[k] not in " \t\n;|&<>()":
        k += 1
    return strip, s[i:k].replace("\\", "").strip("'\""), k


def _skip_heredocs(s, i, pending):
    """Consume the heredoc bodies that start at s[i]; (new i, [bodies])."""
    n, bodies = len(s), []
    for strip, word in pending:
        lines = []
        while i < n:
            j = s.find("\n", i)
            j = n if j < 0 else j
            line = s[i:j]
            i = j + 1
            if (line.lstrip("\t") if strip else line) == word:
                break
            lines.append(line)
        bodies.append("\n".join(lines))
    return min(i, n), bodies


def _ansi_end(s, i):
    """Index just past the closing quote of an ANSI-C $'..' body at s[i]."""
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "'":
            return i + 1
        i += 1
    raise ParseError("unbalanced $'")


def _dquote_end(s, i):
    """Index just past the '"' closing a double-quoted string at s[i]."""
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if s.startswith("$(", i):
            i = _match_paren(s, i + 2)
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j < 0:
                raise ParseError("unbalanced backtick")
            i = j + 1
            continue
        i += 1
    raise ParseError("unbalanced quote")


def _match_paren(s, i):
    """Index just past the ')' closing the '(' at s[i-1].  Quote-, escape-,
    ANSI-C-, comment- and heredoc-aware (F12: an apostrophe inside a
    `$(cat <<'EOF' ... EOF)` commit message is heredoc text, not a quote)."""
    depth, n, pending = 1, len(s), []
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ParseError("unbalanced quote")
            i = j + 1
            continue
        if s.startswith("$'", i):
            i = _ansi_end(s, i + 2)
            continue
        if c == '"':
            i = _dquote_end(s, i + 1)
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j < 0:
                raise ParseError("unbalanced backtick")
            i = j + 1
            continue
        if s.startswith("<<", i) and not s.startswith("<<<", i):
            strip, word, i = _heredoc_word(s, i + 2)
            pending.append((strip, word))
            continue
        if c == "\n" and pending:
            i, _ = _skip_heredocs(s, i + 1, pending)
            pending = []
            continue
        if c == "#" and (i == 0 or s[i - 1] in " \t\n;("):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ParseError("unbalanced $(")


def _regex_word_end(s, i):
    """End of the regex operand of `[[ x =~ RE ]]` starting at s[i]: bash
    reads ( ) | inside it as regex text, not operators (review bug 12).
    Returns `i` itself (no special word) when the regex holds a command
    substitution, which the ordinary tokenizer must see."""
    n, depth, start = len(s), 0, i
    while i < n:
        c = s[i]
        if c == "`" or s.startswith("$(", i):
            return start
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c in "'\"":
            j = s.find(c, i + 1)
            if j < 0:
                raise ParseError("unbalanced quote")
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        elif c in " \t\n" and depth == 0:
            break
        i += 1
    return i


def tokenize(s):
    """(tokens, nested_texts).  Raises ParseError on unbalanced quotes.
    A heredoc redirection token carries its body (.body)."""
    toks, nested = [], []
    i, n = 0, len(s)
    heredocs = []  # pending (strip_tabs, word, redir token)
    while i < n:
        c = s[i]
        if toks and toks[-1].kind == "word" and toks[-1].value == "=~" \
                and not toks[-1].quoted and c not in " \t\n":
            end = _regex_word_end(s, i)
            if end > i:
                toks.append(Tok("word", s[i:end], i, end, True))
                i = end
                continue
        if c == "\n":
            toks.append(Tok("op", "\n", i, i + 1))
            i += 1
            if heredocs:
                i, bodies = _skip_heredocs(
                    s, i, [(st, w) for st, w, _t in heredocs])
                for (_st, _w, tok), body in zip(heredocs, bodies):
                    tok.body = body
                heredocs = []
            continue
        if c in " \t":
            i += 1
            continue
        if c == "\\" and s.startswith("\\\n", i):
            i += 2  # line continuation
            continue
        if c == "#":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if s.startswith(OPS2, i):
            op = s[i:i + 2]
            toks.append(Tok("op", op, i, i + 2))
            if op in ("$(", "<(", ">("):
                end = _match_paren(s, i + 2)
                nested.append(s[i + 2:end - 1])
                toks.append(Tok("op", ")", end - 1, end))
                i = end
            else:
                i += 2
            continue
        if c in ";|&()`":
            toks.append(Tok("op", c, i, i + 1))
            if c == "`":
                j = s.find("`", i + 1)
                if j < 0:
                    raise ParseError("unbalanced backtick")
                nested.append(s[i + 1:j])
                toks.append(Tok("op", "`", j, j + 1))
                i = j + 1
            else:
                i += 1
            continue
        m = REDIR_RE.match(s, i)
        if m:
            op = m.group(2)
            i = m.end()
            if op in ("<<", "<<-"):
                strip, word, k = _heredoc_word(s, i - (1 if op == "<<-" else 0))
                tok = Tok("redir", op, m.start(), k)
                heredocs.append((strip, word, tok))
                toks.append(tok)
                toks.append(Tok("word", word, i, k, True))
                i = k
                continue
            toks.append(Tok("redir", op, m.start(), i))
            continue
        # a word
        start, buf, quoted = i, [], False
        while i < n and s[i] not in WORD_END:
            c = s[i]
            if c == "\\" and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
                quoted = True
            elif c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    raise ParseError("unbalanced quote")
                buf.append(s[i + 1:j])
                i, quoted = j + 1, True
            elif s.startswith("$'", i):
                j = _ansi_end(s, i + 2)
                buf.append(s[i + 2:j - 1])
                i, quoted = j, True
            elif c == '"':
                j, part = i + 1, []
                while j < n and s[j] != '"':
                    if s[j] == "\\" and j + 1 < n:
                        part.append(s[j + 1])
                        j += 2
                        continue
                    if s.startswith("$(", j):
                        end = _match_paren(s, j + 2)
                        nested.append(s[j + 2:end - 1])
                        part.append(s[j:end])
                        j = end
                        continue
                    if s[j] == "`":
                        k = s.find("`", j + 1)
                        if k < 0:
                            raise ParseError("unbalanced backtick")
                        nested.append(s[j + 1:k])
                        part.append(s[j:k + 1])
                        j = k + 1
                        continue
                    part.append(s[j])
                    j += 1
                if j >= n:
                    raise ParseError("unbalanced quote")
                buf.append("".join(part))
                i, quoted = j + 1, True
            elif c == "$" and s.startswith("$(", i):
                end = _match_paren(s, i + 2)
                nested.append(s[i + 2:end - 1])
                buf.append(s[i:end])
                i = end
            else:
                buf.append(c)
                i += 1
        if i == start:
            raise ParseError("stray %r at %d" % (s[i], i))
        toks.append(Tok("word", "".join(buf), start, i, quoted))
    if heredocs:
        _i, bodies = _skip_heredocs(s, n, [(st, w) for st, w, _t in heredocs])
        for (_st, _w, tok), body in zip(heredocs, bodies):
            tok.body = body
    return toks, nested


class Segment(object):
    """One simple command: argv word tokens + redirections."""
    __slots__ = ("words", "redirs", "nested", "heredocs", "context",
                 "scratch_ok", "dyn", "pipe", "scope")

    def __init__(self, nested=False, scope=()):
        self.words, self.redirs, self.nested = [], [], nested
        self.heredocs, self.context, self.scratch_ok = [], None, False
        self.dyn = False  # a $(..) / `..` in command position
        self.pipe = False  # its stdout feeds the next segment (`a | b`)
        self.scope = scope  # the ( .. ) subshells it sits in, outermost first

    @property
    def argv(self):
        return [t.value for t in self.words]


def _synth(argv, context=None, scratch_ok=False):
    seg = Segment(True)
    seg.words = [Tok("word", a, 0, 0) for a in argv]
    seg.context, seg.scratch_ok = context, scratch_ok
    return seg


def _skip_nested(toks, i):
    """Index past the close of the $( / <( / >( / ` opened at toks[i]."""
    close = ")" if toks[i].value != "`" else "`"
    j = i + 1
    while j < len(toks) and not (toks[j].kind == "op"
                                 and toks[j].value == close):
        j += 1
    return j + 1


def segments(command, _depth=0):
    """Every simple command of `command`, nested bodies included (marked
    nested).  Raises ParseError.  A `case` subject and its patterns are
    not commands (review bug 12); a heredoc / herestring piped into a shell
    is (review bug 9)."""
    toks, nested_texts = tokenize(command)
    scope, opened, parens = (), 0, []
    out, cur = [], Segment(_depth > 0)
    # case: "head" = `case WORD in`, "pat" = a pattern up to its `)`
    case_state, cases = None, 0
    i = 0
    while i < len(toks):
        t = toks[i]
        if case_state is not None:
            if t.kind == "op" and t.value in ("$(", "<(", ">(", "`"):
                i = _skip_nested(toks, i)
                continue
            if t.kind == "word" and not t.quoted:
                if case_state == "head" and t.value == "in":
                    case_state = "pat"
                elif case_state == "pat" and t.value == "esac":
                    case_state, cases = None, cases - 1
            elif case_state == "pat" and t.kind == "op" and t.value == ")":
                case_state = None
            i += 1
            continue
        if t.kind == "op":
            if t.value in ("$(", "<(", ">(", "`"):
                # the nested body is classified separately; skip to its close
                if not cur.words:
                    cur.dyn = True
                i = _skip_nested(toks, i)
                continue
            if cur.words or cur.redirs:
                cur.pipe = t.value == "|"
                out.append(cur)
            if t.value == "(":
                # a subshell only at command start (not `f() {`)
                parens.append(not cur.words)
                if parens[-1]:
                    opened += 1
                    scope = scope + (opened,)
            elif t.value == ")" and parens:
                if parens.pop():
                    scope = scope[:-1]
            elif t.value == ";;" and cases:
                case_state = "pat"
            cur = Segment(_depth > 0, scope)
            i += 1
            continue
        if t.kind == "redir":
            tgt = toks[i + 1] if i + 1 < len(toks) and \
                toks[i + 1].kind == "word" else None
            cur.redirs.append((t.value, tgt))
            if t.body is not None:
                cur.heredocs.append(t.body)
            elif t.value == "<<<" and tgt is not None:
                cur.heredocs.append(tgt.value)  # a herestring is stdin too
            i += 2 if tgt is not None else 1
            continue
        if not cur.words and not t.quoted and t.value in ("case", "esac") \
                and not cur.redirs:
            if t.value == "case":
                case_state, cases = "head", cases + 1
            elif cases:
                cases -= 1
            i += 1
            continue
        cur.words.append(t)
        i += 1
    if cur.words or cur.redirs:
        out.append(cur)
    if _depth < 4:
        top = len(out)
        for text in nested_texts:
            out.extend(segments(text, _depth + 1))
        for k, seg in enumerate(list(out)):
            nxt = out[k + 1] if seg.pipe and k + 1 < top else None
            out.extend(_expand(seg, command, _depth, nxt))
    return out


def _expand(seg, command, depth, piped_to=None):
    """Commands a segment runs inside itself: a shell body (bash -c / eval /
    ssh host cmd), a heredoc or herestring fed to a shell or ssh (F9) --
    directly or through a pipe (`cat <<EOF | bash`, review bug 9) -- and
    the command of xargs / find -exec / launchctl asuser (F10)."""
    if depth >= 4:
        return []
    out = []
    body = shell_body(seg.argv)
    if body is not None:
        out.extend(segments(body, depth + 1))
    if seg.heredocs and (stdin_shell(seg.argv) or (
            piped_to is not None and stdin_shell(piped_to.argv))):
        for b in seg.heredocs:
            out.extend(segments(b, depth + 1))
    for inner in inner_commands(seg, command):
        out.append(inner)
        out.extend(_expand(inner, command, depth + 1))
    return out


class Info(object):
    """What a segment runs: name / args after prefixes and wrappers."""
    __slots__ = ("name", "args", "ntok", "prefixes", "unknown", "dynamic",
                 "wrapped", "assigns")

    def __init__(self):
        self.name, self.args, self.ntok = None, [], None
        self.prefixes, self.unknown, self.dynamic = [], False, False
        self.wrapped, self.assigns = False, []


def _prefix_flags(base, words, info):
    """Pop a prefix command's own flags (and their values) off `words`."""
    arg, noarg = PREFIX_ARG.get(base, set()), PREFIX_NOARG.get(base, set())
    while words:
        v = words[0].value
        if v == "--":
            words.pop(0)
            break
        if not v.startswith("-") or v == "-" and base != "env":
            if base == "timeout" and re.match(r"^\d", v):
                words.pop(0)  # the duration
            break
        if v in arg:
            if base == "env" and v in ("-u", "--unset"):
                info.assigns.append("-u")
                if len(words) > 1:
                    info.assigns.append("-u:" + words[1].value)
            del words[:2]
            continue
        if base == "env" and v in ("-i", "--ignore-environment", "-"):
            info.assigns.append("-i")
        if v.startswith("--") and "=" in v:
            if v.split("=", 1)[0] in arg | noarg:
                words.pop(0)
                continue
            info.unknown = True
            return
        if v in noarg or (base == "nice" and re.match(r"^-\d+$", v)):
            words.pop(0)
            continue
        if not v.startswith("--") and v[:2] in arg:
            words.pop(0)  # attached value: -uroot, -oL
            continue
        if not v.startswith("--") and len(v) > 2 and \
                all("-" + ch in noarg for ch in v[1:]):
            words.pop(0)  # combined short flags: -En
            continue
        info.unknown = True
        return


def _shell_script_index(words):
    """Index of the script operand of `bash|sh|zsh [-flags] <script> ...`
    (F7), or None for `bash -c X` and a stdin shell (`bash`, `bash -s`,
    `sh -`)."""
    k = 0
    while k < len(words):
        v = words[k].value
        if v in SHELL_ARG_FLAGS:
            k += 2
            continue
        if v.startswith("--"):
            k += 1
            continue
        if v.startswith(("-", "+")):
            if v == "-" or (v[0] == "-" and ("c" in v[1:] or "s" in v[1:])):
                return None
            k += 1
            continue
        return k
    return None


def command_info(seg):
    info = Info()
    words = list(seg.words)
    while words:
        v = words[0].value
        if v in KEYWORDS:
            words.pop(0)
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", v):
            info.assigns.append(v.split("=", 1)[0])
            words.pop(0)
            continue
        base = os.path.basename(v)
        if base in PREFIX_ARG:
            if base == "command" and len(words) > 1 and \
                    words[1].value in ("-v", "-V"):
                return info  # a lookup, runs nothing
            info.prefixes.append(base)
            words.pop(0)
            _prefix_flags(base, words, info)
            if info.unknown:
                return info
            continue
        if base in SHELLS:
            k = _shell_script_index(words[1:])
            if k is not None:
                info.wrapped = True
                words = words[1 + k:]
            break
        if base in ("source", ".") and len(words) > 1:
            info.wrapped = True
            words = words[1:]
            break
        break
    if not words:
        info.dynamic = seg.dyn
        return info
    info.ntok = words[0]
    v = words[0].value
    if seg.dyn or v.startswith("$") or "$(" in v or "`" in v:
        info.dynamic = True
    info.name = os.path.basename(v)
    info.args = [w.value for w in words[1:]]
    return info


def command_words(seg):
    """(name, args, name_token) after env assignments / prefix commands /
    shell-script wrappers."""
    info = command_info(seg)
    return info.name, info.args, info.ntok


def shell_body(argv):
    """The command string run by `bash -c X`, `eval X`, `ssh host X`."""
    info = command_info(_synth(argv))
    name, args = info.name, info.args
    if name in SHELLS:
        for k, a in enumerate(args):
            if a.startswith("-") and "c" in a[1:] and not a.startswith("--"):
                return args[k + 1] if k + 1 < len(args) else None
        return None
    if name == "eval" and args:
        return " ".join(args)
    if name == "ssh":
        rest = _ssh_rest(args)
        return " ".join(rest[1:]) if len(rest) > 1 else None
    return None


SSH_ARG_FLAGS = set("BbcDEeFIiJLlmOopQRSWw")


def _ssh_rest(args):
    rest, k = [], 0
    while k < len(args):
        a = args[k]
        if a.startswith("-") and len(a) == 2 and a[1] in SSH_ARG_FLAGS:
            k += 2
            continue
        if a.startswith("-"):
            k += 1
            continue
        rest = args[k:]
        break
    return rest


def stdin_shell(argv):
    """True when the segment is a shell (or ssh with no remote command)
    that reads its commands from stdin: `bash`, `bash -s`, `sh -`,
    `ssh host`."""
    info = command_info(_synth(argv))
    if info.name in SHELLS and not info.wrapped:
        return not any(a.startswith("-") and not a.startswith("--")
                       and "c" in a[1:] for a in info.args)
    if info.name == "ssh":
        return len(_ssh_rest(info.args)) == 1
    return False


XARGS_ARG = {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "-R", "-S",
             "--max-args", "--max-procs", "--max-lines", "--delimiter",
             "--arg-file", "--replace"}
FIND_EXEC = ("-exec", "-execdir", "-ok", "-okdir")


def _find_roots(args):
    roots = []
    for a in args:
        if a.startswith("-") or a in ("(", "!"):
            break
        roots.append(a)
    return roots


def inner_commands(seg, command):
    """Synthesized nested segments for the command xargs / find -exec /
    launchctl asuser run (F10)."""
    info = command_info(seg)
    name, args = info.name, info.args
    out = []
    if name == "xargs":
        k = 0
        while k < len(args) and args[k].startswith("-"):
            a = args[k]
            k += 2 if a in XARGS_ARG else 1
        if k < len(args):
            out.append(_synth(args[k:], context=command))
    elif name == "find":
        roots = _find_roots(args)
        k = 0
        while k < len(args):
            if args[k] in FIND_EXEC:
                j = k + 1
                while j < len(args) and args[j] not in (";", "+"):
                    j += 1
                if j > k + 1:
                    seg2 = _synth(args[k + 1:j], context=command)
                    seg2.scratch_ok = list(roots)  # judged by _mark_find_scratch
                    out.append(seg2)
                k = j + 1
                continue
            k += 1
    elif name == "launchctl" and args[:1] == ["asuser"] and len(args) > 2:
        out.append(_synth(args[2:], context=command))
    return out


# --- context ------------------------------------------------------------------

class Ctx(object):
    """What the classifier may know about the machine.  Every probe is
    injectable so fixtures never touch the real machine."""

    def __init__(self, root, cwd=None, env=None, remote_url=None,
                 pid_command=None, armed=None):
        self.root = os.path.normpath(root)
        self.cwd = cwd or root
        self.env = os.environ if env is None else env
        self._remote_url = remote_url
        self._pid_command = pid_command
        self._armed = armed
        # R2-5: a command that creates a symlink gets no scratch exemption
        self.no_scratch = False

    def conf(self, key, default):
        v = self.env.get(key)
        return default if v is None else v

    def scratch_roots(self):
        roots = [os.path.join(self.root, "files", "tmp"), "/tmp",
                 "/private/tmp", "/var/folders", "/private/var/folders"]
        for k in ("TMPDIR", "CLAUDE_SCRATCHPAD"):
            if self.env.get(k):
                roots.append(self.env[k])
        out = []
        for r in roots:
            for x in (os.path.normpath(r), os.path.realpath(r)):
                if x not in out:
                    out.append(x)
        return out

    def pid_command(self, pid):
        if self._pid_command is not None:
            return self._pid_command(pid)
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=2)
            return out.stdout.strip() or None
        except Exception:
            return None

    def armed(self, name):
        if self._armed is not None:
            return name in self._armed
        state = self.env.get("DMQ_CLI_STATE") or os.path.expanduser(
            "~/.dogany/cli-updates")
        return os.path.exists(os.path.join(state, name))

    def abspath(self, p):
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        return os.path.normpath(p)

    def _real(self, p):
        """Realpath of the operand's PARENT + its name (F16: `ln -s ~ /tmp/h;
        rm -rf /tmp/h/agent/identity` is not scratch).  R2-5: an operand
        ending in `/`, `/.` or `/*`, or naming an existing symlink, is judged
        on the realpath of the WHOLE operand (GNU `rm -rf link/` empties the
        link's target)."""
        a = self.abspath(p)
        raw = os.path.expanduser(p)
        if raw.endswith(("/", "/.", "/*")) or os.path.islink(a):
            return os.path.realpath(a)
        return os.path.join(os.path.realpath(os.path.dirname(a)),
                            os.path.basename(a))

    def is_scratch(self, p):
        if not p or "$" in p or "`" in p or self.no_scratch:
            return False
        a = self._real(p)
        return any(a == r or a.startswith(r + os.sep)
                   for r in self.scratch_roots())

    def worker_worktree(self):
        """The dispatch worker's own worktree when the DGN-1504 write
        sandbox is in force (dispatch-detached.sh exports
        DOGANY_DISPATCH_WORKTREE only in write mode, under sandbox-exec) and
        the cwd is inside it; else None."""
        wt = self.env.get("DOGANY_DISPATCH_WORKTREE")
        if not self.env.get("DOGANY_DISPATCH_WORKER") or not wt:
            return None
        wt = os.path.realpath(wt)
        cwd = os.path.realpath(self.cwd)
        if cwd == wt or cwd.startswith(wt + os.sep):
            return wt
        return None

    def in_worktree(self, p, wt):
        if not p or "$" in p or "`" in p:
            return False
        a = self._real(p)
        return a == wt or a.startswith(wt + os.sep)


# --- findings -----------------------------------------------------------------

class Finding(object):
    __slots__ = ("cls", "row", "detail")

    def __init__(self, cls, row, detail):
        self.cls, self.row, self.detail = cls, row, detail

    def __repr__(self):
        return "Finding(%s, %s, %r)" % (self.cls, self.row, self.detail)


class Result(object):
    def __init__(self):
        self.findings, self.allowed, self.rewrite = [], [], None
        self.parse_error = None


# F14: the bridge and its helpers only -- a `claude -p` worker or anything
# named dogany is the agent's own housekeeping (dispatch has its own cancel).
BOT_RE = re.compile(r"sdk_bridge|bridge[/.]|bot\.py|telegram|watchdog|"
                    r"newbridge|mirror-poll", re.I)
BOT_VAR_RE = re.compile(r"^\$\{?[A-Za-z_]*(BRIDGE|BOT|WATCHDOG|MIRROR)", re.I)
LAUNCHCTL_MUT = {"kickstart", "stop", "start", "unload", "load", "bootout",
                 "bootstrap", "kill", "disable", "enable", "remove",
                 "setenv", "unsetenv", "submit"}
SYSTEMCTL_MUT = {"start", "stop", "restart", "reload", "try-restart",
                 "reload-or-restart", "try-reload-or-restart", "enable",
                 "disable", "mask", "unmask", "kill", "daemon-reload",
                 "edit", "set-property", "link", "reenable", "preset",
                 "isolate", "revert"}
SERVICE_MUT = {"start", "stop", "restart", "reload", "force-reload"}
CTL_READ = {"status", "list", "show", "ls", "help", "--help", "-h", "check",
            "print", "doctor"}
DRY_FLAGS = {"--dry-run", "-n", "--check", "--plan", "--print"}
SQL_DESTRUCT = re.compile(r"\b(drop\s+(table|database|index|view)|"
                          r"truncate\s+table|delete\s+from\s+\w+\s*(;|$))",
                          re.I)
# the scripts whose own owner check an overridden CLAUDECODE would skip
GUARDED_SCRIPTS = {"self_restart.sh"}
HOOK_KEYS = re.compile(r"^(core\.hookspath|dogany\.|include\.path|"
                       r"includeif\.|alias\.)", re.I)
# R2-8: env names that switch a device off, redirect its trust root or hide
# the agent caller.  Setting, exporting or unsetting one ANYWHERE in a
# command is a gate-switch finding (path A only): a cheap signal, the
# devices themselves read modes from config/agent.conf and trust paths from
# their own location.
KNOB_RE = re.compile(r"^(\w*_GATE_MODE|\w*_APPROVAL_MODE|SELF_RESTART_\w+|"
                     r"DOGANY_OWNER_QUOTE_CHECK|"

                     r"DOGANY_BOT_DATA_DIR|DOGANY_TEST_MODE|CLAUDE_CONFIG_DIR|"
                     r"CLAUDE_PROJECT_DIR|HOME|CLAUDECODE|GIT_CONFIG\w*|"
                     r"BASH_GATE_\w+|WRITE_PATHS_\w+|GATE_[A-Z0-9_]+)$")
KNOB_WORD_RE = re.compile(r"\b(\w*_GATE_MODE|\w*_APPROVAL_MODE|"
                          r"SELF_RESTART_\w+|DOGANY_OWNER_QUOTE_CHECK|"
                          r"DOGANY_PRIVATE_REMOTE_URLS|DOGANY_TEST_MODE|"
                          r"CLAUDECODE|GIT_CONFIG_\w+)\b")
ENV_BUILTINS = {"export", "unset", "declare", "typeset", "readonly", "local"}
INTERPRETERS = {"python3", "python", "perl", "ruby", "node"}
GH_API_MUT = re.compile(r"(^|/)(git/refs|git/tags|releases)(/|$)")


# --- shared with D4 (write-paths-d4.py) ---------------------------------------

def gate_mode(env, key):
    """The one parse of a hook gate's mode knob (BASH_GATE_MODE,
    WRITE_PATHS_D4_MODE): off | detect | block, case- and
    whitespace-insensitive; unset, empty or unknown -> detect.
    Deliberately NOT device_gate.device_mode, where unknown is block: a
    hook's mode comes from the settings line the agent's Bash cannot set,
    and an unknown value there must not turn a detect-mode hook into a
    blocking one; a script device's mode is read from agent.conf and may
    only tighten (R2-8)."""
    mode = (env.get(key) or "").strip().lower()
    return "off" if mode == "off" else "detect"


class GitArgv(object):
    __slots__ = ("globals", "sub", "rest", "where")

    def __init__(self, globals_, sub, rest, where):
        self.globals, self.sub, self.rest, self.where = \
            globals_, sub, rest, where


GIT_GLOBAL_ARG = ("-C", "-c", "--git-dir", "--work-tree", "--config-env")


def git_argv(args, cwd="."):
    """Split `git <args>` past git's global options (-C dir, -c k=v,
    --no-pager ...).  .globals = [(flag, value or None)], .sub = the
    subcommand (None when there is none), .rest = its arguments, .where =
    the work dir after every -C (joined onto `cwd`)."""
    glob, where, k = [], cwd, 0
    while k < len(args) and args[k].startswith("-"):
        a = args[k]
        if a in GIT_GLOBAL_ARG:
            val = args[k + 1] if k + 1 < len(args) else None
            glob.append((a, val))
            if a == "-C" and val is not None:
                where = os.path.join(where, val)
            k += 2
            continue
        glob.append((a, None))
        k += 1
    if k >= len(args):
        return GitArgv(glob, None, [], where)
    return GitArgv(glob, args[k], args[k + 1:], where)


class GitConfigWrite(object):
    __slots__ = ("key", "file", "scope", "edit")

    def __init__(self, key, file_, scope, edit):
        self.key, self.file, self.scope, self.edit = key, file_, scope, edit


GIT_CONFIG_VALUE_FLAGS = ("-f", "--file", "--blob", "--type", "--default",
                          "--comment")
GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--list", "-l",
                    "--show-origin", "--name-only"}
GIT_CONFIG_WRITES = {"--unset", "--unset-all", "--add", "--replace-all"}
GIT_CONFIG_SCOPES = ("--global", "--system", "--local", "--worktree")


def git_config_write(rest):
    """The write a `git config <rest>` makes, or None for a read.  R2-9:
    --file X / -f X / --blob X / --type T ... take a value, so the key is
    the first word AFTER them; after the key every word is its value.
    .key (None for --edit), .file (the --file target or None), .scope
    (--global / --system / --local / --worktree or None), .edit."""
    keys, target, scope, j = [], None, None, 0
    while j < len(rest):
        a = rest[j]
        if keys:
            keys.append(a)
        elif a in GIT_CONFIG_VALUE_FLAGS:
            if a in ("-f", "--file") and j + 1 < len(rest):
                target = rest[j + 1]
            j += 2
            continue
        elif a.startswith("--file="):
            target = a.split("=", 1)[1]
        elif a in GIT_CONFIG_SCOPES:
            scope = a
        elif not a.startswith("-"):
            keys.append(a)
        j += 1
    fl = {a for a in rest if a.startswith("-")}
    if fl & {"-e", "--edit"}:
        return GitConfigWrite(None, target, scope, True)
    if not keys or fl & GIT_CONFIG_READS or \
            not (len(keys) > 1 or fl & GIT_CONFIG_WRITES):
        return None
    return GitConfigWrite(keys[0], target, scope, False)


def _flags(args):
    return {a for a in args if a.startswith("-")}


def _short_has(args, letter):
    return any(a.startswith("-") and not a.startswith("--") and letter in a
               for a in args)


def _git(args, ctx, res):
    # -c core.hooksPath=<x> disarms git-hooks/pre-push (F18); so do
    # include.path / includeIf / alias.* and --config-env (R2-9)
    g = git_argv(args)
    for flag, val in g.globals:
        if flag == "-c" and val is not None and HOOK_KEYS.match(val):
            res.findings.append(Finding(
                "gate-switch", "HA28", "git -c %s (bypasses git-hooks/pre-push)"
                % val.split("=", 1)[0]))
        if flag.startswith("--config-env"):
            res.findings.append(Finding(
                "gate-switch", "HA28", "git --config-env (config from env "
                "can disarm git-hooks/pre-push)"))
    if g.sub is None:
        return
    sub, rest = g.sub, g.rest
    fl = _flags(rest)
    if sub == "push":
        _git_push(rest, ctx, res)
        return
    if sub == "send-pack":
        res.findings.append(Finding(
            "gate-switch", "HA28", "git send-pack (pushes without "
            "git-hooks/pre-push)"))
        return
    if sub == "config":
        w = git_config_write(rest)
        if w is not None and w.edit:
            res.findings.append(Finding(
                "gate-switch", "HA28", "git config --edit (hand edit of the "
                "config git-hooks/pre-push trusts)"))
        elif w is not None and HOOK_KEYS.match(w.key):
            res.findings.append(Finding(
                "gate-switch", "HA28", "git config %s (disarms or widens "
                "git-hooks/pre-push)" % w.key))
        return
    destruct = None
    if sub == "reset" and "--hard" in fl:
        destruct = "git reset --hard"
    elif sub == "clean" and (_short_has(rest, "f") or "--force" in fl):
        destruct = "git clean -f"
    elif sub == "checkout" and ("--" in rest or "." in rest or
                                fl & {"-f", "--force"}):
        destruct = "git checkout (discards working-tree changes)"
    elif sub == "restore" and [a for a in rest if not a.startswith("-")] \
            and not ("--staged" in fl and not fl & {"--worktree", "-W"}) \
            and not ("-S" in fl and not fl & {"--worktree", "-W"}):
        destruct = "git restore (discards working-tree changes)"
    elif sub == "branch" and ("-D" in rest or (
            fl & {"-d", "--delete"} and fl & {"-f", "--force"})):
        destruct = "git branch -D"
    elif sub == "stash" and rest[:1] and rest[0] in ("drop", "clear"):
        destruct = "git stash %s" % rest[0]
    elif sub in ("filter-branch", "filter-repo"):
        destruct = "git %s" % sub
    elif sub == "reflog" and rest[:1] == ["expire"]:
        destruct = "git reflog expire"
    elif sub == "update-ref" and "-d" in rest:
        destruct = "git update-ref -d"
    if destruct:
        if ctx.worker_worktree():
            res.allowed.append("worker-sandbox:%s" % destruct)
        else:
            res.findings.append(Finding("destructive", "HF32b", destruct))


PUSH_ARG = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def _git_push(rest, ctx, res):
    fl = _flags(rest)
    if fl & {"-n", "--dry-run"}:
        return
    pos, k = [], 0
    while k < len(rest):
        a = rest[k]
        if a in PUSH_ARG:
            k += 2
            continue
        if not a.startswith("-"):
            pos.append(a)
        k += 1
    remote = pos[0] if pos else "(upstream)"
    refspecs = pos[1:]
    force = any(a == "-f" or a.startswith("--force") or a == "--mirror"
                or (a.startswith("-") and not a.startswith("--")
                    and "f" in a[1:]) for a in fl) or \
        any(r.startswith("+") for r in refspecs)
    delete = bool(fl & {"-d", "--delete", "--prune"}) or \
        any(r.startswith(":") for r in refspecs)
    if force or delete:
        res.findings.append(Finding(
            "destructive", "HA28",
            "git push %s to %s" % ("--force" if force else "--delete",
                                   remote)))
    if "--no-verify" in fl:
        res.findings.append(Finding(
            "gate-switch", "HA28",
            "git push --no-verify (skips git-hooks/pre-push, the device for "
            "the remote URL and tag refs)"))


def _kill(name, args, seg, full, ctx, res):
    own = " ".join(seg.argv)
    if BOT_RE.search(own) or (seg.context and BOT_RE.search(seg.context)):
        res.findings.append(Finding("service", "HF11",
                                    "%s targeting a bot/bridge process" % name))
        return
    if name != "kill":
        return
    for a in args:
        if BOT_VAR_RE.match(a):
            res.findings.append(Finding(
                "service", "HF11", "kill %s (a bridge pid variable)" % a))
            return
        if a.startswith("-") or a.startswith("%") or a.startswith("$"):
            continue
        if a.isdigit():
            cmd = ctx.pid_command(int(a))
            if cmd and BOT_RE.search(cmd):
                res.findings.append(Finding(
                    "service", "HF11", "kill %s (%s)" % (a, cmd[:60])))
                return


GH_FIELD = {"-f", "-F", "--field", "--raw-field", "--input"}


def _gh_api(rest, res):
    """R2-9: `gh api` writing refs / tags / releases on the server -- a tag
    or release created this way never meets pre-push."""
    path, method, fields, j = None, None, False, 0
    while j < len(rest):
        a = rest[j]
        if a in ("-X", "--method"):
            method = (rest[j + 1] if j + 1 < len(rest) else "").upper()
            j += 2
            continue
        if a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
        elif a in GH_FIELD or a.startswith(("--field=", "--raw-field=")):
            fields = True
            j += 1 if "=" in a else 2
            continue
        elif not a.startswith("-") and path is None:
            path = a
        elif a.startswith("-") and j + 1 < len(rest) and a in (
                "-H", "--header", "-q", "--jq", "-t", "--template",
                "--hostname", "--cache", "-p", "--preview"):
            j += 2
            continue
        j += 1
    mutating = (method or ("POST" if fields else "GET")) != "GET"
    if path and mutating and GH_API_MUT.search(path.split("?", 1)[0]):
        res.findings.append(Finding(
            "release", "TP08", "gh api %s %s (writes refs / releases on the "
            "server; pre-push never sees it)" % (method or "POST", path)))


def _curl_api(args, res):
    url = next((a for a in args if "api.github.com" in a), None)
    if not url or not GH_API_MUT.search(url.split("?", 1)[0]):
        return
    method = None
    for j, a in enumerate(args):
        if a in ("-X", "--request") and j + 1 < len(args):
            method = args[j + 1].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
    data = any(a.startswith(("-d", "--data", "-F", "--form", "--json"))
               for a in args)
    if (method or ("POST" if data else "GET")) != "GET":
        res.findings.append(Finding(
            "release", "TP08", "curl %s to the GitHub refs / releases API"
            % (method or "POST")))


RM_FLAG = re.compile(r"^-[rRfivdIP]+$")


def _rm(seg, info, ctx, res, rewrites):
    name, args, ntok = info.name, info.args, info.ntok
    ops = [a for a in args if a != "--" and not RM_FLAG.match(a)
           and a not in ("--recursive", "--force", "--verbose", "--dir")]
    if not ops and seg.context is None:
        return
    if not ops:
        # `... | xargs rm`: the operands come from stdin, unknown
        res.findings.append(Finding("destructive", "HF32a",
                                    "xargs %s (operands from stdin)" % name))
        return
    if seg.scratch_ok:
        return  # find -exec rm under scratch roots (judged in _classify_seg)
    if all(ctx.is_scratch(o) for o in ops):
        return  # scratch: plain rm stays
    wt = ctx.worker_worktree()
    if wt and all(ctx.in_worktree(o, wt) for o in ops):
        # F16: a sandboxed dispatch worker in its own worktree -- the kernel
        # profile owns that boundary, and the trash home is outside it
        res.allowed.append("worker-sandbox:%s" % name)
        return
    if "sudo" in info.prefixes:
        res.findings.append(Finding(
            "destructive", "HF32a",
            "sudo %s (not rewritten: root-owned trash breaks later trash "
            "moves)" % name))
        return
    if seg.nested or ntok is None or ntok.start == ntok.end:
        res.findings.append(Finding(
            "destructive", "HF32a",
            "%s inside a nested command cannot be rewritten to trash" % name))
        return
    rewrites.append(ntok)


MV_ARG = {"-t", "--target-directory", "-S", "--suffix"}


def _mv_instance(info, ctx, res):
    """DGN-1812: `mv <agent root> ...` is a hand retirement.  It moves the
    body and leaves the estate records naming it behind (install registry,
    the main's peer entry, launchd units, observer labels, crew rows).  The
    one retire path is scripts/pack/mint_run.sh retire."""
    ops, target, i = [], None, 0
    args = info.args
    while i < len(args):
        a = args[i]
        if a == "--":
            ops += args[i + 1:]
            break
        if a in MV_ARG:
            if a in ("-t", "--target-directory") and i + 1 < len(args):
                target = args[i + 1]
            i += 2
            continue
        if a.startswith("--target-directory="):
            target = a.split("=", 1)[1]
        elif not a.startswith("-"):
            ops.append(a)
        i += 1
    sources = ops if target is not None else ops[:-1]
    for src in sources:
        if not src or "$" in src or "`" in src or ctx.is_scratch(src):
            continue
        if os.path.isfile(os.path.join(ctx.abspath(src), ".instance.conf")):
            res.findings.append(Finding(
                "destructive", "DGN-1812", "mv of an agent root (%s) is a hand "
                "retirement" % src))
            return


def _seg_text(seg):
    return " ".join(seg.argv)


def _env_names(name, args):
    """Names an export / unset / declare ... builtin sets or clears."""
    out = []
    for a in args:
        if a.startswith("-"):
            continue
        out.append(a.split("=", 1)[0])
    return out


def _knobs(seg, info, res):
    """R2-8: a device knob set / exported / unset / dropped anywhere in the
    command, `env -i` or `sudo` in front of a guarded script, an inline
    interpreter touching a knob."""
    names = [a for a in info.assigns if not a.startswith("-")]
    names += [a[3:] for a in info.assigns if a.startswith("-u:")]
    if info.name in ENV_BUILTINS:
        names += _env_names(info.name, info.args)
    hit = [n for n in names if KNOB_RE.match(n)]
    if hit:
        res.findings.append(Finding(
            "gate-switch", "HF31", "%s set or unset (switches off, redirects "
            "or hides from a safety device)" % ", ".join(sorted(set(hit)))))
        return
    guarded = info.name in GUARDED_SCRIPTS or (
        info.name == "git" and "push" in info.args) or (
        info.name in ("python3", "python") and info.args and
        os.path.basename(info.args[0]) == "owner_quote_check.py")
    if guarded and ("-i" in info.assigns or "sudo" in info.prefixes):
        res.findings.append(Finding(
            "gate-switch", "HF31", "%s in front of %s (drops the env the "
            "script's own owner check reads)" % (
                "env -i" if "-i" in info.assigns else "sudo", info.name)))
        return
    if info.name in INTERPRETERS:
        code = " ".join(a for a in info.args) + " " + " ".join(seg.heredocs)
        if KNOB_WORD_RE.search(code) and re.search(r"environ|\benv\b|ENV\[",
                                                   code):
            res.findings.append(Finding(
                "gate-switch", "HF31", "%s code that edits a device knob in "
                "its env" % info.name))


def _routine_label(value, ctx, plist_only=False):
    """Match routine-ctl's instance/name mapping, not the bridge namespace."""
    # Same identity source and fallback as lib/routine-ctl.sh. Routine names
    # are open-ended kebab-case; shipped plist lists omit custom routines.
    agent = ""
    try:
        with open(os.path.join(ctx.root, ".instance.conf")) as stream:
            for line in stream:
                if line.startswith("DOGANY_AGENT_NAME="):
                    agent = line.rstrip("\n").split("=")[1]
                    break
    except OSError:
        pass
    agent = agent or os.path.basename(ctx.root)
    label = os.path.basename(value)
    if label.endswith(".plist"):
        label = label[:-6]
    elif plist_only:
        return False
    prefix = "com.telegram-skill-bot.%s." % agent
    if not label.startswith(prefix):
        return False
    name = label[len(prefix):]
    # <agent> is the legacy bridge; <agent>.newbridge is the current bridge.
    return name != "newbridge" and re.fullmatch(r"[a-z0-9-]+", name) is not None


def _classify_seg(seg, ctx, res, rewrites, full):
    info = command_info(seg)
    if info.unknown:
        if DANGER_RE.search(_seg_text(seg)):
            res.findings.append(Finding(
                "unparsed", "D3", "a prefix command with an unknown flag "
                "hides the command it runs"))
        return
    name, args, ntok = info.name, info.args, info.ntok
    _knobs(seg, info, res)
    if info.dynamic:
        if DANGER_RE.search(full):
            res.findings.append(Finding(
                "unparsed", "D3", "a dynamic command name in a command "
                "naming a gated verb"))
        return
    if not name:
        return
    base = name
    routine_op = (base == "launchctl" and args
                  and args[0] in {"bootstrap", "bootout", "load", "unload", "remove"}
                  and any(_routine_label(a, ctx) for a in args[1:]))
    # Copying a plist to the login loader bypasses the retirement owner too.
    copy_to_loader = (base == "cp" and len(args) >= 2
                      and any(_routine_label(a, ctx, plist_only=True)
                              for a in args[:-1])
                      and re.search(r"/Library/LaunchAgents(?:/|$)", args[-1]))
    if routine_op or copy_to_loader:
        res.findings.append(Finding(
            "service", "ROUTINE_CTL", "raw routine scheduling bypass"))
    elif base == "launchctl" and args and args[0] in LAUNCHCTL_MUT:
        res.findings.append(Finding("service", "HF11",
                                    "launchctl %s" % args[0]))
    elif base == "systemctl":
        verbs = [a for a in args if not a.startswith("-")]
        if verbs and verbs[0] in SYSTEMCTL_MUT:
            res.findings.append(Finding("service", "HF11",
                                        "systemctl %s" % verbs[0]))
    elif base == "service" and len(args) > 1 and args[1] in SERVICE_MUT:
        res.findings.append(Finding("service", "HF11",
                                    "service %s %s" % (args[0], args[1])))
    elif base == "brew" and args[:1] == ["services"] and len(args) > 1 \
            and args[1] in ("start", "stop", "restart", "run", "kill"):
        res.findings.append(Finding("service", "HF11",
                                    "brew services %s" % args[1]))
    elif base in ("kill", "pkill", "killall"):
        _kill(base, args, seg, full, ctx, res)
    elif base in ("start.sh", "watchdog.sh", "watchdog_setup.sh") and \
            "bridge" in (ntok.value if ntok else ""):
        res.findings.append(Finding("service", "HF11", base))
    elif base in ("routine-ctl.sh", "bundle-routine-ctl.sh",
                  "brief-slot-ctl.sh"):
        if args and args[0] not in CTL_READ:
            res.findings.append(Finding("service", "HF11",
                                        "%s %s" % (base, args[0])))
    elif base in ("self-update.sh", "update.sh"):
        if not (_flags(args) & DRY_FLAGS):
            res.findings.append(Finding("service", "HF11",
                                        "%s (update restarts the bridge)" % base))
    elif base == "dawn-memory-queue.sh" or (
            base in ("python3", "python") and args and
            os.path.basename(args[0]) == "cli-updater.py"):
        live = "--live" in args or ("--mode" in args and "install" in args)
        if live:
            if ctx.armed("armed"):
                res.allowed.append("dawn-queue:armed (updater idle guard)")
            else:
                res.findings.append(Finding(
                    "service", "HF11", "CLI updater live run while not armed"))
    elif base in ("gate-switch.py",) or (
            base in ("python3", "python") and args and
            os.path.basename(args[0]) == "gate-switch.py"):
        a = args[1:] if base != "gate-switch.py" else args
        if a[:1] == ["off"]:
            res.findings.append(Finding(
                "gate-switch", "HF31", "gate-switch off %s" % " ".join(a[1:])))
    elif base == "gh" and args[:1] == ["release"] and len(args) > 1 and \
            args[1] in ("create", "delete", "edit", "upload"):
        # gh creates / deletes the tag on the server: pre-push never sees it
        res.findings.append(Finding("release", "TP08",
                                    "gh release %s" % args[1]))
    elif base == "gh" and args[:1] == ["api"]:
        _gh_api(args[1:], res)
    elif base == "curl":
        _curl_api(args, res)
    elif base in ("npm", "pnpm", "yarn", "cargo", "twine") and \
            ("publish" in args[:1] or "upload" in args[:1]):
        res.findings.append(Finding("release", "TP08",
                                    "%s %s" % (base, args[0])))
    elif base == "git":
        _git(args, ctx, res)
    elif base in ("rm", "unlink"):
        _rm(seg, info, ctx, res, rewrites)
    elif base == "mv":
        _mv_instance(info, ctx, res)
    elif base == "find":
        roots = _find_roots(args)
        scratch = bool(roots) and all(ctx.is_scratch(r) for r in roots)
        if "-delete" in args and not scratch:
            res.findings.append(Finding("destructive", "HF32a",
                                        "find -delete"))
    elif base in ("dd", "shred") or base.startswith("mkfs") or (
            base == "diskutil" and args and args[0].startswith("erase")):
        res.findings.append(Finding("destructive", "HF32b", base))
    elif base == "crontab" and "-r" in args:
        res.findings.append(Finding("destructive", "HF32b", "crontab -r"))
    elif base == "truncate":
        ops = [a for a in args if not a.startswith("-") and not
               re.match(r"^[+-]?\d", a)]
        if not all(ctx.is_scratch(o) for o in ops):
            res.findings.append(Finding("destructive", "HF32b", "truncate"))
    elif base == "sqlite3" and SQL_DESTRUCT.search(" ".join(args)):
        res.findings.append(Finding("destructive", "HF32b",
                                    "sqlite3 DROP/DELETE without WHERE"))


DANGER_RE = re.compile(
    r"\b(launchctl|systemctl|pkill|killall|kill|self_restart|push|"
    r"release|publish|reset\s+--hard|clean\s+-|rm|unlink|gate-switch|"
    r"update\.sh|mkfs|dd|shred|hooksPath|send-pack|CLAUDECODE)\b")
REPAIR_CHARS = ("'", '"', ")", "`")


def _makes_symlink(segs):
    """R2-5: `ln -s` / `cp -s` anywhere in the command (the link may not
    exist yet at hook time, so its target cannot be read)."""
    for seg in segs:
        name, args, _ = command_words(seg)
        if name == "ln" and (any(a.startswith("-") and not a.startswith("--")
                                 and "s" in a[1:] for a in args)
                             or "--symbolic" in args):
            return True
        if name == "cp" and ("-s" in args or "--symbolic-link" in args or
                             any(a.startswith("-") and not a.startswith("--")
                                 and "s" in a[1:] for a in args)):
            return True
    return False


def _run(segs, ctx, command):
    res, rewrites = Result(), []
    ctx.no_scratch = ctx.no_scratch or _makes_symlink(segs)
    for seg in segs:
        _classify_seg(seg, ctx, res, rewrites, command)
    return res, rewrites


def _mark_find_scratch(segs, ctx):
    """find -exec inner segments inherit the scratch verdict of the find's
    roots (scratch_ok was set to the roots list by inner_commands)."""
    for seg in segs:
        roots = seg.scratch_ok
        if isinstance(roots, list):
            seg.scratch_ok = bool(roots) and all(ctx.is_scratch(r)
                                                 for r in roots)


SUBCOMMAND_TOOLS = {"git", "gh", "npm", "pnpm", "yarn", "cargo", "brew",
                    "launchctl", "systemctl", "service", "python3", "python"}


def _danger_position(seg):
    """A gated verb in COMMAND position: the command name, or the
    subcommand of a tool that has one (`git push`) -- never an argument
    such as the text of a commit message."""
    info = command_info(seg)
    words = [info.name or ""]
    if info.name in SUBCOMMAND_TOOLS:
        sub = [a for a in info.args if not a.startswith("-")][:1]
        words += [os.path.basename(a) for a in sub]
    return any(DANGER_RE.search(w) for w in words)


def _unparsed(command, ctx, res):
    """F12: the command did not tokenize.  Repair it minimally (close the
    open quote / paren) and judge what then sits in COMMAND position; deny
    only on a gated verb there (or on any finding), never on a word inside
    a commit message.  No repair -> the raw-text prefilter, as before."""
    for n in (1, 2, 3):
        for tail in itertools.product(REPAIR_CHARS, repeat=n):
            try:
                segs = segments(command + "".join(tail))
            except ParseError:
                continue
            _mark_find_scratch(segs, ctx)
            got, rewrites = _run(segs, ctx, command)
            res.findings.extend(got.findings)
            res.allowed.extend(got.allowed)
            if rewrites:
                res.findings.append(Finding(
                    "destructive", "HF32a",
                    "rm in a command the gate cannot parse cannot be "
                    "rewritten to trash"))
            if not res.findings and any(_danger_position(s) for s in segs):
                res.findings.append(Finding(
                    "unparsed", "D3",
                    "unparseable command with a gated verb in command "
                    "position"))
            return res
    if DANGER_RE.search(command):
        res.findings.append(Finding(
            "unparsed", "D3", "unparseable command naming a gated verb"))
    return res


def classify(command, ctx, trash_cmd=None):
    res = Result()
    try:
        segs = segments(command)
    except ParseError as exc:
        res.parse_error = str(exc)
        return _unparsed(command, ctx, res)
    _mark_find_scratch(segs, ctx)
    got, rewrites = _run(segs, ctx, command)
    res.findings, res.allowed = got.findings, got.allowed
    if rewrites and trash_cmd:
        out = command
        for tok in sorted(rewrites, key=lambda t: -t.start):
            out = out[:tok.start] + trash_cmd + out[tok.end:]
        res.rewrite = out
    elif rewrites:
        res.findings.append(Finding("destructive", "HF32a",
                                    "rm with no trash helper available"))
    return res
