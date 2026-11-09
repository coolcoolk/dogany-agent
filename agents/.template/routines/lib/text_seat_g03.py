"""G03 (IDLE_PROMISE) generic-vocabulary prototype (v2.7 D2) -- NOT SHIPPED.

output_gate.py carries instance vocabulary in three regexes: the user's
form of address and the word "owner" in the exemptions (E2/E4), the
instance's nickname for its delegates in E4, and the instance's verb for
dispatch in the dispatch shape.  The re-tune moves those words out of the
framework and into config; the framework keeps "the user" words only.

This module builds the generic regexes from a Vocab and can rebind them on
an imported output_gate module for replay (apply_vocab).  It does not edit
output_gate.py: the spec (docs/v27-D2-spec.md, G03 re-tune) lists the
in-place change, gated on the measurement this module makes possible.
"""
import re

# Framework words for "the user" in Korean replies.  Instance words come in
# through Vocab.owner_terms (voice_vocab.owner_terms: manifest label, owner
# term config, persona "Call the user" line).
GENERIC_USER_WORDS = ("사용자", "유저")
# Framework words for a dispatch (the promise noun of the dispatch shape).
GENERIC_DISPATCH_NOUNS = ("위임", "디스패치")


class Vocab:
    def __init__(self, owner_terms=(), delegate_terms=(), dispatch_nouns=()):
        self.owner_terms = tuple(t for t in owner_terms if t)
        self.delegate_terms = tuple(t for t in delegate_terms if t)
        self.dispatch_nouns = tuple(t for t in dispatch_nouns if t)


def _alt(words):
    words = sorted(set(words), key=len, reverse=True)
    return "|".join(re.escape(w) for w in words)


def build(vocab):
    """{name: compiled regex} for the three vocabulary-bearing patterns."""
    users = _alt(GENERIC_USER_WORDS + vocab.owner_terms)
    exempt = (r'(아침에|내일|다음 세션|(?:%s) (확인|승인|판단|결정)|(?:%s) 게이트|'
              r'돌고 있|가동 중|' % (users, users))
    if vocab.delegate_terms:
        exempt += r'(?:%s)(?:이|가) (돌|작업)|' % _alt(vocab.delegate_terms)
    exempt += r'대기|\[\[OPTIONS\]\])'
    # "<word>가" only for configured owner terms: "사용자가" in a reply is
    # usually the product's end user, not the person being answered
    # (measured: the one current-vs-generic diff on the corpus).
    owner_ga = ("(?:%s)가|" % _alt(vocab.owner_terms)) if vocab.owner_terms else ""
    trigger = (r'([가-힣]시면|[가-힣]시는 대로|주시면|주실 때|실 ?거면|시려면|실 경우|'
               r'시겠다면|시다면|(?:%s)(이|께서)|%s(?:%s) (말씀|답)|'
               r'원하시|말씀(하|주)시|승인|확인받|허락)' % (users, owner_ga, users))
    nouns = _alt(GENERIC_DISPATCH_NOUNS + vocab.dispatch_nouns)
    dispatch = (r'(?:%s)(하겠|합니다|해야겠|하러|로 돌아가|를? ?(넣|붙이|띄우|올리)겠)'
                % nouns)
    return {
        "IDLE_PROMISE_EXEMPT_RE": re.compile(exempt),
        "IDLE_PROMISE_OWNER_TRIGGER_RE": re.compile(trigger),
        "dispatch_promise": re.compile(dispatch),
    }


def apply_vocab(output_gate, vocab):
    """Rebind the generic patterns on an imported output_gate module."""
    pats = build(vocab)
    output_gate.IDLE_PROMISE_EXEMPT_RE = pats["IDLE_PROMISE_EXEMPT_RE"]
    output_gate.IDLE_PROMISE_OWNER_TRIGGER_RE = \
        pats["IDLE_PROMISE_OWNER_TRIGGER_RE"]
    actions = []
    for kind, promise_re, tool_re in output_gate.IDLE_PROMISE_ACTIONS:
        if kind == "dispatch":
            promise_re = pats["dispatch_promise"]
        actions.append((kind, promise_re, tool_re))
    output_gate.IDLE_PROMISE_ACTIONS = tuple(actions)
    return output_gate


def tool_blob(tools, strip_heredocs=None):
    """The output-gate turn blob (CMD / TOOL / BG lines) from text_seat Tools."""
    lines = []
    for t in tools:
        if t.name == "Bash" and t.command:
            cmd = strip_heredocs(t.command) if strip_heredocs else \
                " ".join(t.command.split("\n"))
            lines.append("CMD " + cmd)
            if t.bg:
                lines.append("BG Bash")
        else:
            lines.append("TOOL %s %s" % (t.name, t.path))
            if t.bg:
                lines.append("BG %s" % t.name)
    return "\n".join(lines)
