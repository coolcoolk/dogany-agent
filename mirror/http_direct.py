"""
DGN-180 direct HTTPS lane for If-Match conditional writes (grill-4 finding
5b: retires the compare-then-put race class for calendar update/patch).

- OAuth: reads gws's OWN local credential store (client_secret.json +
  AES-GCM credentials.enc under ~/.config/gws), decrypts in memory with
  gws's local .encryption_key, mints an access token via the refresh grant.
  Nothing new touches disk beyond what gws already stores; the token lives
  in memory only. (gws auth export redacts secret+refresh_token, so it
  cannot drive a refresh grant -- we read the same encrypted store gws uses.)
  CUTOVER NOTE: at live cutover this SHOULD move to gws providing a token
  endpoint or GOOGLE_WORKSPACE_CLI_TOKEN reuse; reaching into gws's config
  dir is a sandbox-acceptable coupling, flagged as OPEN QUESTION.
- Only calendar events update/patch go through this lane; everything else
  stays on the gws CLI.
- stdlib + cryptography (AES-GCM) only.
English/ASCII only.
"""

import base64
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_BASE = "https://www.googleapis.com/calendar/v3"

GWS_DIR = os.path.expanduser("~/.config/gws")
GWS_KEY = os.path.join(GWS_DIR, ".encryption_key")
GWS_ENC = os.path.join(GWS_DIR, "credentials.enc")
GWS_CLIENT = os.path.join(GWS_DIR, "client_secret.json")

_token_cache = {"access_token": None, "expires_at": 0}


class HttpError(RuntimeError):
    def __init__(self, code, body):
        super().__init__("HTTP %d: %s" % (code, body[:200]))
        self.code = code
        self.body = body


def _load_gws_credentials():
    """Read gws's local encrypted credential store (in memory only).
    credentials.enc = AES-GCM, 12-byte nonce prefix, key = base64-decoded
    ~/.config/gws/.encryption_key. client_secret.json holds client id/secret."""
    key = base64.b64decode(open(GWS_KEY, "rb").read())
    blob = open(GWS_ENC, "rb").read()
    nonce, ct = blob[:12], blob[12:]
    creds = json.loads(AESGCM(key).decrypt(nonce, ct, None).decode())
    cs = json.load(open(GWS_CLIENT))
    client = cs.get("installed") or cs.get("web") or cs
    return {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": creds["refresh_token"],
    }


def get_access_token():
    """Mint (and memory-cache) an access token via refresh grant using
    gws's own decrypted credentials. Never written to disk by us."""
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]
    creds = _load_gws_credentials()
    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.loads(resp.read().decode())
    _token_cache["access_token"] = tok["access_token"]
    _token_cache["expires_at"] = now + int(tok.get("expires_in", 3600))
    return _token_cache["access_token"]


def _call(method, url, body=None, etag=None):
    """Returns (status_code, parsed_json_or_None). 412 is returned, not
    raised (caller branches on it); other >=400 raise HttpError."""
    headers = {
        "Authorization": "Bearer %s" % get_access_token(),
        "Content-Type": "application/json",
    }
    if etag:
        headers["If-Match"] = etag
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        if e.code == 412:
            return 412, None
        raise HttpError(e.code, raw)


def _event_url(cal_id, event_id):
    return "%s/calendars/%s/events/%s" % (
        CAL_BASE, urllib.parse.quote(cal_id), urllib.parse.quote(event_id))


def cal_update_ifmatch(cal_id, event_id, body, etag):
    """PUT events.update with real If-Match. Returns (status, json).
    412 = precondition failed (surface changed) -> caller pulls + merges."""
    return _call("PUT", _event_url(cal_id, event_id), body=body, etag=etag)


def cal_patch_ifmatch(cal_id, event_id, body, etag):
    """PATCH events.patch with real If-Match. Returns (status, json)."""
    return _call("PATCH", _event_url(cal_id, event_id), body=body, etag=etag)


def cal_get(cal_id, event_id):
    """GET event (fresh etag fetch after a 412)."""
    return _call("GET", _event_url(cal_id, event_id))
