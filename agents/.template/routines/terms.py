#!/usr/bin/env python3
"""terms.py -- the chat path for user-customizable terms (DGN-1980).

The user asks to rename a word ("RIR -> <their word>" in their own
language, or "say X instead of Y"); the agent runs:

  terms.py preview --request "<the user's words>"   # or --term X --to Y
      -> deterministic registry lookup (id, every language's value,
         synonyms), then a before/after preview on 2-3 real strings that
         use the term, in the user's language.  Show it verbatim and ask.
  terms.py set --request "<same words>" --yes       # only after the user
      -> writes term.<id> into config/i18n.local/<lang>.json (the DGN-1791
         instance layer: beats every kit/pack layer and the framework
         default, survives every update) and journals the previous value.
  terms.py undo                                     # the user asks to undo
      -> restores the value before the last change.
  terms.py list [--json]                            # the registry
  terms.py block                                    # SessionStart hook
      -> one short context block naming every term whose value differs
         from its default, so model prose speaks the user's words too.
         Silent when nothing was renamed.

An unknown phrase is refused (exit 3) with the nearest registry names; a
phrase naming several terms is refused (exit 3) with the choices.  Code
emitters never need this tool: they read copy through the shared i18n
lookup, which expands [[term:<id>]] (routines/lib/i18n_lookup.py).

Options: --root <instance root> (default: this file's parent's parent),
--lang <code> (default: env DOGANY_LANG -> conf AGENT_LANG -> en), --json.
Exit: 0 ok, 2 usage, 3 unknown/ambiguous term, 4 invalid new value,
5 confirmation missing (set without --yes), 6 instance file unreadable
(never overwritten), 7 nothing to undo.
"""

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
import i18n_lookup  # noqa: E402
import term_registry as TR  # noqa: E402

JOURNAL = "terms-journal.jsonl"
PREVIEW_MAX = 3
WINDOW = 60

# DGN-1980: every owner-facing string of the chat path, one block, owner
# review before ship (config/i18n/{ko,en}.json).
COPY_KEYS_PENDING_OWNER_CONFIRM = (
    "terms.preview.header", "terms.preview.item", "terms.preview.prose_only",
    "terms.preview.confirm", "terms.done", "terms.undone",
    "terms.nothing_to_undo", "terms.unknown", "terms.unknown_none",
    "terms.ambiguous", "terms.invalid",
)


class Refusal(Exception):
    def __init__(self, code, text, data=None):
        Exception.__init__(self, text)
        self.code, self.text, self.data = code, text, data or {}


def _copy(root, lang, key, **kw):
    tpl = i18n_lookup.lookup(root, lang, key) or key
    try:
        return tpl.format(**kw)
    except (KeyError, IndexError, ValueError):
        return tpl


def _instance_path(root, lang):
    return os.path.join(root, i18n_lookup.INSTANCE_DIR, "%s.json" % lang)


def _read_instance(root, lang):
    path = _instance_path(root, lang)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        raise Refusal(6, "instance i18n file unreadable, not overwritten: %s" % path)
    return data


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write_instance(root, lang, data):
    _atomic_write(_instance_path(root, lang),
                  json.dumps(data, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------
def resolve_request(root, lang, args, reg):
    """-> (entry, phrase, new)."""
    if args.request:
        parsed = TR.parse_request(args.request)
        if not parsed:
            raise Refusal(2, "could not read a rename request from: %s" % args.request)
        phrase, new = parsed
    elif args.term and args.to is not None:
        phrase, new = args.term, args.to
    else:
        raise Refusal(2, "need --request TEXT or --term PHRASE --to NEW")
    status, ids, nearest = TR.find_term(root, phrase, reg)
    if status == "unknown":
        if nearest:
            text = _copy(root, lang, "terms.unknown", phrase=phrase,
                         nearest=", ".join(nearest))
        else:
            text = _copy(root, lang, "terms.unknown_none", phrase=phrase)
        raise Refusal(3, text, {"status": status, "phrase": phrase,
                                "nearest": nearest})
    if status == "ambiguous":
        choices = ", ".join("%s (%s)" % (TR.resolve_term(root, lang, i, reg), i)
                            for i in ids)
        raise Refusal(3, _copy(root, lang, "terms.ambiguous", phrase=phrase,
                               choices=choices),
                      {"status": status, "phrase": phrase, "ids": ids})
    new = new.strip()
    reason = TR.valid_value(new)
    if reason:
        raise Refusal(4, _copy(root, lang, "terms.invalid", new=new,
                               max=TR.MAX_TERM_LEN), {"reason": reason})
    return reg[ids[0]], phrase, new


def _window(raw, token):
    """A <= WINDOW-char slice of raw around the first token occurrence."""
    if len(raw) <= WINDOW:
        return raw
    i = raw.find(token)
    start = max(0, i - WINDOW // 3)
    end = min(len(raw), start + WINDOW + len(token))
    return ("\u2026" if start else "") + raw[start:end] + ("\u2026" if end < len(raw) else "")


def preview_items(root, lang, entry, new, reg):
    """[(before, after)] on up to PREVIEW_MAX real strings using the term:
    the term-tagged sentences of the effective i18n bundle first (what code
    emitters render), then the registry examples (prose shapes)."""
    tid = entry["id"]
    token = "[[term:%s]]" % tid
    raws = []
    bundle = i18n_lookup._raw_merged(root, lang)
    for key in sorted(bundle):
        val = bundle[key]
        if isinstance(val, str) and token in val and not key.startswith(
                i18n_lookup.TERM_KEY_PREFIX):
            raws.append(_window(val, token))
    ex = entry.get("examples")
    if isinstance(ex, dict):
        for val in ex.get(lang) or ex.get("en") or []:
            if isinstance(val, str) and token in val:
                raws.append(val)
    out = []
    for raw in raws:
        if len(out) >= PREVIEW_MAX:
            break
        before = TR.expand(raw, lambda i: TR.resolve_term(root, lang, i, reg))
        after = TR.expand(raw, lambda i: new if i == tid else
                          TR.resolve_term(root, lang, i, reg))
        if (before, after) not in out:
            out.append((before, after))
    return out


def cmd_preview(root, lang, args, reg):
    entry, phrase, new = resolve_request(root, lang, args, reg)
    cur = TR.resolve_term(root, lang, entry["id"], reg)
    items = preview_items(root, lang, entry, new, reg)
    lines = [_copy(root, lang, "terms.preview.header", old=cur, new=new)]
    for before, after in items:
        lines.append(_copy(root, lang, "terms.preview.item", before=before, after=after))
    if not items:
        lines.append(_copy(root, lang, "terms.preview.prose_only"))
    lines.append(_copy(root, lang, "terms.preview.confirm"))
    return {"status": "preview", "id": entry["id"], "key": entry["key"],
            "lang": lang, "current": cur, "new": new,
            "items": [{"before": b, "after": a} for b, a in items],
            "text": "\n\n".join(lines)}


def cmd_set(root, lang, args, reg):
    entry, phrase, new = resolve_request(root, lang, args, reg)
    if not args.yes:
        raise Refusal(5, "confirmation missing: show 'preview' to the user, "
                         "then run set with --yes only after they agree")
    data = _read_instance(root, lang)
    key = entry["key"]
    prev = data.get(key) if isinstance(data.get(key), str) else None
    legacy = entry.get("legacy_conf")
    if new == TR.default_value(root, lang, entry) and not (legacy and TR._conf(root, legacy)):
        data.pop(key, None)  # back to the default: no override needed
    else:
        data[key] = new
    _write_instance(root, lang, data)
    jpath = os.path.join(root, i18n_lookup.INSTANCE_DIR, JOURNAL)
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "lang": lang, "key": key, "prev": prev, "new": new}
    with open(jpath, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"status": "set", "id": entry["id"], "key": key, "lang": lang,
            "prev": prev, "new": new,
            "text": _copy(root, lang, "terms.done", new=new)}


def cmd_undo(root, lang, args, reg):
    jpath = os.path.join(root, i18n_lookup.INSTANCE_DIR, JOURNAL)
    try:
        with open(jpath, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().split("\n") if ln.strip()]
    except OSError:
        lines = []
    if not lines:
        raise Refusal(7, _copy(root, lang, "terms.nothing_to_undo"))
    try:
        rec = json.loads(lines[-1])
    except ValueError:
        raise Refusal(7, _copy(root, lang, "terms.nothing_to_undo"))
    rlang, key, prev = rec.get("lang") or lang, rec.get("key"), rec.get("prev")
    data = _read_instance(root, rlang)
    if prev is None:
        data.pop(key, None)
    else:
        data[key] = prev
    _write_instance(root, rlang, data)
    _atomic_write(jpath, "".join(ln + "\n" for ln in lines[:-1]))
    tid = key[len(i18n_lookup.TERM_KEY_PREFIX):]
    value = TR.resolve_term(root, rlang, tid, reg)
    return {"status": "undone", "id": tid, "key": key, "lang": rlang,
            "value": value, "text": _copy(root, rlang, "terms.undone", value=value)}


def cmd_list(root, lang, args, reg):
    rows = []
    for tid in sorted(reg):
        e = reg[tid]
        rows.append({"id": tid, "key": e["key"], "kind": "term",
                     "source": e["source"],
                     "default": TR.default_value(root, lang, e),
                     "value": TR.resolve_term(root, lang, tid, reg),
                     "where": e.get("where") or []})
    text = "\n".join("%s\t%s\t%s\t%s" % (r["id"], r["default"], r["value"],
                                         "; ".join(r["where"])) for r in rows)
    return {"status": "list", "lang": lang, "terms": rows, "text": text}


def prose_block(root, lang, reg=None):
    """The model-prose term table: '' when no term differs from its default."""
    reg = reg if reg is not None else TR.load_registry(root)
    pairs = []
    for tid in sorted(reg):
        cur = TR.resolve_term(root, lang, tid, reg)
        dflt = TR.default_value(root, lang, reg[tid])
        if cur and dflt and cur != dflt:
            pairs.append("%s -> %s" % (dflt, cur))
    if not pairs:
        return ""
    return ("[User terms] The user renamed these words. In every reply and in "
            "any text written for the user, use the user's word, never the "
            "default (identifiers and code stay unchanged): "
            + "; ".join(pairs) + ".")


def main(argv):
    ap = argparse.ArgumentParser(prog="terms.py")
    ap.add_argument("cmd", choices=["list", "parse", "preview", "set", "undo", "block"])
    ap.add_argument("text", nargs="?")
    ap.add_argument("--root", default=os.path.dirname(HERE))
    ap.add_argument("--lang")
    ap.add_argument("--request")
    ap.add_argument("--term")
    ap.add_argument("--to")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    lang = (args.lang or TR.conf_lang(root) or "en").lower()

    if args.cmd == "block":
        try:
            out = prose_block(root, lang)
        except Exception:
            out = ""  # a hook never breaks a session
        if out:
            print(out)
        return 0
    reg = TR.load_registry(root)
    try:
        if args.cmd == "parse":
            parsed = TR.parse_request(args.text or args.request or "")
            res = {"status": "parsed" if parsed else "none",
                   "old": parsed[0] if parsed else None,
                   "new": parsed[1] if parsed else None}
            res["text"] = json.dumps(res, ensure_ascii=False)
        else:
            res = {"preview": cmd_preview, "set": cmd_set, "undo": cmd_undo,
                   "list": cmd_list}[args.cmd](root, lang, args, reg)
    except Refusal as r:
        res = dict(r.data, status=r.data.get("status", "refused"), text=r.text)
        print(json.dumps(res, ensure_ascii=False) if args.json else r.text)
        return r.code
    print(json.dumps(res, ensure_ascii=False) if args.json else res["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
