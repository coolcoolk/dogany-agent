#!/usr/bin/env python3
"""
Food nutrition lookup helper (Korea public food DBs).
- barcode: C005 (Food Safety Korea) -> product name -> FoodNtrCpntDbInfo02 nutrition
- name:    FoodNtrCpntDbInfo02 direct search
- cache:   local JSON cache of brand/restaurant items (grams-scaled)

API keys are NEVER hardcoded. They are read from a project .env (or the process
environment). If keys are absent, name/barcode public-DB lookups degrade to
{"found": false} instead of crashing -- the skill then falls back to LLM
estimation. The local cache path works without any keys.
"""
import sys
import os
import re
import json
import tempfile
import urllib.request
import urllib.parse

# Skill dir; PROJECT_ROOT is three levels up (.claude/skills/diet-log -> ROOT).
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))


def _env_files():
    """Candidate .env files, in priority order. PROJECT_ROOT-relative only."""
    return [
        os.environ.get("DIET_ENV_FILE", ""),
        os.path.join(_PROJECT_ROOT, ".env"),
        os.path.join(_PROJECT_ROOT, ".telegram_bot", ".env"),
        os.path.join(_PROJECT_ROOT, "runtime", ".env"),
    ]


def _load_env_key(name, default=""):
    """Read a key from a project .env, falling back to the process environment.
    No plaintext keys live in source."""
    for env_path in _env_files():
        if not env_path:
            continue
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(name + "="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return os.environ.get(name, default)


FOODSAFETY_KEY = _load_env_key("FOODSAFETY_KEY")
DATAGOV_KEY = _load_env_key("DATAGOV_KEY")
NUTR_API = "http://apis.data.go.kr/1471000/FoodNtrCpntDbInfo02/getFoodNtrCpntDbInq02"

# Brand/restaurant local cache (stores per-serving absolute amounts at `grams`).
# PROJECT_ROOT-relative: <ROOT>/database/food_cache.json (three levels up).
CACHE_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "database", "food_cache.json"))

# Nutrition field mapping (per 100g).
AMT_MAP = {
    "AMT_NUM1":  ("에너지",   "kcal"),
    "AMT_NUM3":  ("단백질",   "g"),
    "AMT_NUM4":  ("지방",     "g"),
    "AMT_NUM6":  ("탄수화물", "g"),
    "AMT_NUM7":  ("식이섬유", "g"),
    "AMT_NUM8":  ("당류",     "g"),
    "AMT_NUM13": ("나트륨",   "mg"),
    "AMT_NUM24": ("포화지방", "g"),
    "AMT_NUM25": ("트랜스지방", "g"),
}

# Cache-item nutrition keys (grams-based absolute) -> display name.
CACHE_NUTR_MAP = {
    "kcal":      "에너지",
    "carbs":     "탄수화물",
    "protein":   "단백질",
    "fat":       "지방",
    "fiber":     "식이섬유",
    "sugar":     "당류",
    "sodium_mg": "나트륨",
}


def normalize_key(name):
    """Normalize a food name into a cache key: lowercase + strip brackets/symbols
    + collapse whitespace. Same on lookup and store for consistency."""
    s = name.lower().strip()
    s = re.sub(r"[^0-9a-z가-힣ㄱ-ㅎㅏ-ㅣ\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "items" not in data:
            return {"items": {}}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"items": {}}


def save_cache(data):
    """Atomic replace so concurrent reads never see a torn file."""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def cache_lookup(name, grams):
    """Cache lookup for name. On hit, scale to requested grams; else None."""
    cache = load_cache()
    item = cache.get("items", {}).get(normalize_key(name))
    if not item:
        return None
    base_grams = item.get("grams")
    if not base_grams:
        return None
    factor = grams / float(base_grams)
    result = {
        "food_name": item.get("name"),
        "maker": None,
        "serving_size": base_grams,
        "grams": grams,
        "found": True,
        "source": "cache",
        "estimated": item.get("estimated", False),
        "updated": item.get("updated", ""),
    }
    for key, disp in CACHE_NUTR_MAP.items():
        val = item.get(key)
        result[disp] = round(float(val) * factor, 2) if val is not None else None
    for disp in ("포화지방", "트랜스지방"):
        result[disp] = None
    return result


def cache_add(payload):
    """Upsert one cache item. payload = item fields + key_name."""
    key = normalize_key(payload["key_name"])
    item = {
        "name":      payload.get("name", payload["key_name"]),
        "grams":     payload.get("grams"),
        "kcal":      payload.get("kcal"),
        "carbs":     payload.get("carbs"),
        "protein":   payload.get("protein"),
        "fat":       payload.get("fat"),
        "fiber":     payload.get("fiber"),
        "sugar":     payload.get("sugar"),
        "sodium_mg": payload.get("sodium_mg"),
        "source":    payload.get("source", ""),
        "estimated": payload.get("estimated", False),
        "updated":   payload.get("updated", ""),
    }
    cache = load_cache()
    cache.setdefault("items", {})[key] = item
    save_cache(cache)
    return key, item


def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def barcode_to_name(barcode):
    """C005: barcode -> (product name, maker). Needs FOODSAFETY_KEY."""
    if not FOODSAFETY_KEY:
        return None, None
    url = f"https://openapi.foodsafetykorea.go.kr/api/{FOODSAFETY_KEY}/C005/json/1/5/BAR_CD={barcode}"
    d = fetch(url)
    rows = d.get("C005", {}).get("row", [])
    if not rows:
        return None, None
    row = rows[0]
    return row.get("PRDLST_NM"), row.get("BSSH_NM")


def search_nutrition(keyword, maker=None, max_results=5):
    """FoodNtrCpntDbInfo02 name search. Needs DATAGOV_KEY (else empty)."""
    if not DATAGOV_KEY:
        return []
    params = {
        "serviceKey": DATAGOV_KEY,
        "pageNo": 1,
        "numOfRows": max_results,
        "type": "json",
        "FOOD_NM_KR": keyword,
    }
    if maker:
        params["MAKER_NM"] = maker
    url = NUTR_API + "?" + urllib.parse.urlencode(params)
    d = fetch(url)
    return d.get("body", {}).get("items", [])


def extract_nutrition(item, grams):
    """From an item (per-100g) pull the nutrition for `grams`."""
    ratio = grams / 100.0
    result = {
        "food_name": item.get("FOOD_NM_KR"),
        "maker": item.get("MAKER_NM"),
        "serving_size": item.get("SERVING_SIZE"),
        "grams": grams,
        "found": True,
    }
    for key, (name, unit) in AMT_MAP.items():
        val = item.get(key, "")
        if val:
            result[name] = round(float(str(val).replace(",", "")) * ratio, 2)
        else:
            result[name] = None
    return result


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"found": False, "error": "no args"}))
        sys.exit(1)

    mode = sys.argv[1]  # "barcode", "name", or "cache-add"

    if mode == "cache-add":
        if len(sys.argv) < 3:
            print(json.dumps({"found": False, "error": "cache-add: no json arg"}))
            sys.exit(1)
        try:
            payload = json.loads(sys.argv[2])
        except json.JSONDecodeError as e:
            print(json.dumps({"found": False, "error": f"json parse failed: {e}"}))
            sys.exit(1)
        if "key_name" not in payload:
            print(json.dumps({"found": False, "error": "key_name missing"}))
            sys.exit(1)
        key, item = cache_add(payload)
        print(json.dumps({"found": True, "cached": True, "key": key, "item": item}, ensure_ascii=False))
        sys.exit(0)

    query = sys.argv[2]
    grams = float(sys.argv[3]) if len(sys.argv) > 3 else 100.0

    if mode == "name":
        # 1) local cache first
        cached = cache_lookup(query, grams)
        if cached:
            print(json.dumps({"found": True, "candidates": [cached]}, ensure_ascii=False))
            sys.exit(0)

    if mode == "barcode":
        name, maker = barcode_to_name(query)
        if not name:
            print(json.dumps({"found": False, "error": "barcode lookup failed"}))
            sys.exit(0)
        items = search_nutrition(name, maker)
        if not items:
            items = search_nutrition(name)
        if not items:
            keywords = name.split()[:2]
            items = search_nutrition(" ".join(keywords))
    elif mode == "name":
        items = search_nutrition(query)
        if not items:
            words = query.split()
            for n in range(len(words) - 1, 1, -1):
                partial = " ".join(words[:n])
                items = search_nutrition(partial)
                if items:
                    break
    else:
        print(json.dumps({"found": False, "error": "mode error"}))
        sys.exit(1)

    if not items:
        print(json.dumps({"found": False}))
        sys.exit(0)

    candidates = []
    for item in items[:3]:
        candidates.append(extract_nutrition(item, grams))

    print(json.dumps({"found": True, "candidates": candidates}, ensure_ascii=False))


if __name__ == "__main__":
    main()
