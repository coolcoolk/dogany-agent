"""Framework definitions and additive kit layers.

The shipped config/framework-dictionary.json owns framework terms. Kits may
add config/packs/<id>.framework-dictionary.json in the same layer -> term ->
ko/en format. Framework ids are reserved across ALL layers, even aliases
under a kit layer; redefining one is an error, including identical values.
Locale gloss keys contain [[framework:<id>]], resolved here from one source.
"""

import glob
import json
import os
import re

FILE = "framework-dictionary.json"
TOKEN = re.compile(r"\[\[framework:([a-z][a-z0-9_]*)\]\]")
ID = re.compile(r"[a-z][a-z0-9_]*\Z")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        layers = json.load(fh)
    if not isinstance(layers, dict):
        raise ValueError("dictionary must contain layers")
    for layer, terms in layers.items():
        if not ID.fullmatch(layer) or not isinstance(terms, dict):
            raise ValueError("invalid dictionary layer")
        for term, values in terms.items():
            if not ID.fullmatch(term) or not isinstance(values, dict) or any(
                    not isinstance(values.get(lang), str) or not values[lang].strip()
                    for lang in ("ko", "en")):
                raise ValueError("invalid dictionary term: " + term)
    return layers


def load(root):
    path = os.path.join(root, "config", FILE)
    if not os.path.isfile(path):
        path = os.path.join(_ROOT, "config", FILE)
        if not os.path.isfile(path):
            path = os.path.join(_ROOT, "agents", ".template", "config", FILE)
    layers = _read(path)
    reserved = set(layers["framework"])
    sources = [layers] + [_read(p) for p in sorted(glob.glob(
        os.path.join(root, "config", "packs", "*." + FILE)))]
    result = {}
    for index, source in enumerate(sources):
        for layer, terms in source.items():
            if (index or layer != "framework") and reserved.intersection(terms):
                raise ValueError("kit dictionary redefines framework term")
            dest = result.setdefault(layer, {})
            if dest.keys() & terms.keys():
                raise ValueError("duplicate dictionary term")
            dest.update(terms)
    return result


def expand(root, lang, text):
    if not TOKEN.search(text):
        return text
    terms = load(root)["framework"]
    return TOKEN.sub(lambda m: terms[m[1]].get(lang, terms[m[1]]["en"]), text)
