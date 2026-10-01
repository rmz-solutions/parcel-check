#!/usr/bin/env python3
"""
app.py -- Parcel Check web app.

Upload a property-list CSV and see which parcels are already in HubSpot
(matched on APN, owner entity, or mailing address), their lead status, whether
mail to them came back undeliverable, and when they were last mailed.

Runs two ways:
  * Hosted (Railway): the platform sets $PORT. Binds 0.0.0.0, reads
    HUBSPOT_TOKEN from the environment, and gates the link behind APP_PASSWORD.
  * Local: `python3 app.py` -- binds 127.0.0.1 and opens a browser tab.

Standard library only. Python 3.8+.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import parcel_check as pc

VERSION = "2.0.2"

HOSTED = bool(os.environ.get("PORT"))
PORT = int(os.environ.get("PORT") or os.environ.get("PARCEL_CHECK_PORT") or "8765")
HOST = "0.0.0.0" if HOSTED else "127.0.0.1"

# Shared-password gate. Set APP_PASSWORD in Railway; empty = no gate (local use).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
_AUTH_SECRET = os.environ.get("APP_SECRET", "").strip() or (APP_PASSWORD + "::oncore-parcel-check")

MAX_UPLOAD_BYTES = 60 * 1024 * 1024


def auth_enabled():
    return bool(APP_PASSWORD)


def _auth_token():
    return hmac.new(_AUTH_SECRET.encode(), b"authed", hashlib.sha256).hexdigest()


def resource_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_dir():
    """Writable settings/cache folder. CONFIG_DIR wins (e.g. a Railway volume)."""
    candidates = []
    if os.environ.get("CONFIG_DIR"):
        candidates.append(os.environ["CONFIG_DIR"])
    home = os.path.expanduser("~")
    if home and home != "~" and os.path.isdir(home):
        candidates.append(os.path.join(home, ".parcelcheck"))
    candidates.append(os.path.join(resource_dir(), ".parcelcheck"))
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".write-test")
            open(test, "w").close()
            os.remove(test)
            return d
        except OSError:
            continue
    return os.getcwd()


UI_PATH = os.path.join(resource_dir(), "ui.html")
CONFIG_PATH = os.path.join(config_dir(), "config.json")
CACHE_FILE = os.path.join(config_dir(), "parcels_cache.json")
META_FILE = os.path.join(config_dir(), "parcels_meta.json")

# Defaults match the onCORE HubSpot portal, so a fresh deploy (Railway's disk is
# wiped on every redeploy) works without anyone touching Settings.
DEFAULT_CONFIG = {
    "token": "",
    "props": {
        "apn": "apn_1", "owner": "owner", "address": "mailing_address",
        "status": "hs_lead_status", "name": "name", "county": "county_name",
        "undeliverable": "mail_undeliverable", "last_mailed": "date_last_mailed",
    },
    "engaged_statuses": [
        "Interested", "Not Interested", "Future Maybe", "Bad Property",
        "Lead Submitted", "Lead Accepted", "Lead Rejected", "LOI Sent",
        "LOI Signed", "Lease Sent", "Redlines Received/Active Negotiation",
        "Lease Signed", "Dead (went dark)", "Dead (went with competitor)",
        "Dead (not agreeable on $)", "Dead (option term too long)",
        "Dead (bad zoning)", "Dead (bad IX)", "Dead (environmental)",
        "Dead (space too limited)", "Dead (market closed)",
        "Dead (Client reason other)",
        "Dead - landowner reason other (describe in notes)",
        "Dead - self developing/prefer to keep existing use/anti-technology",
        "Dead", "On hold", "Complete", "In Permitting", "Do NOT Call",
    ],
    "max_cache_minutes": 60,
}

_LOCK = threading.Lock()
_CACHE = {"idx": None, "count": 0, "synced_at": 0,
          "apn": 0, "owner": 0, "address": 0}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            saved = json.load(open(CONFIG_PATH, encoding="utf-8"))
            cfg.update({k: saved.get(k, cfg[k]) for k in cfg})
            cfg["props"] = {**DEFAULT_CONFIG["props"], **(saved.get("props") or {})}
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        json.dump(cfg, open(CONFIG_PATH, "w", encoding="utf-8"), indent=2)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def token_from_env():
    return os.environ.get("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_ACCESS_TOKEN") or ""


def get_token(cfg):
    return token_from_env() or cfg.get("token") or ""


def _needed_props(props):
    keys = ("apn", "owner", "address", "status", "name", "county",
            "undeliverable", "last_mailed")
    return [props[k] for k in keys if props.get(k)]


def _save_disk_cache(companies, props_fetched):
    saved_at = time.time()
    try:
        json.dump({"saved_at": saved_at, "props_fetched": props_fetched,
                   "companies": companies}, open(CACHE_FILE, "w"))
        json.dump({"saved_at": saved_at, "count": len(companies),
                   "props_fetched": props_fetched}, open(META_FILE, "w"))
    except Exception:
        pass


def _load_disk_cache():
    try:
        if os.path.exists(CACHE_FILE):
            return json.load(open(CACHE_FILE))
    except Exception:
        pass
    return None


def _disk_summary():
    try:
        if os.path.exists(META_FILE):
            m = json.load(open(META_FILE))
            return {"count": m.get("count", 0), "synced_at": m.get("saved_at", 0),
                    "apn": 0, "owner": 0, "address": 0, "from_disk": True}
    except Exception:
        pass
    return None


def _set_cache(idx, count, synced_at):
    _CACHE.update(idx={"_idx": idx}, count=count, synced_at=synced_at,
                  apn=len(idx["apn"]), owner=len(idx["owner"]),
                  address=len(idx["address"]))


def _stale(ts, max_age):
    if max_age <= 0:
        return False
    return (not ts) or (time.time() - ts) > max_age


def invalidate_cache():
    with _LOCK:
        _CACHE["idx"] = None


_FETCH_LOCK = threading.Lock()   # one HubSpot pull at a time


def do_sync(cfg, force=False):
    """Indexed parcels. Checks never wait on a refresh: while a cache exists it
    is served as-is and the background loop (or a forced refresh) swaps in the
    new index when the pull finishes."""
    token = get_token(cfg)
    if not token:
        raise ValueError("No HubSpot token set." + ("" if HOSTED else " Add it under Settings."))
    max_age = float(cfg.get("max_cache_minutes", 60)) * 60
    if _CACHE["idx"] is not None and not force:
        return _summary()
    props = cfg["props"]
    need = _needed_props(props)
    with _FETCH_LOCK:
        # another request may have finished a pull while we waited
        if _CACHE["idx"] is not None and not force:
            return _summary()
        if not force:
            cached = _load_disk_cache()
            if (cached and set(need).issubset(set(cached.get("props_fetched", [])))
                    and not _stale(cached.get("saved_at", 0), max_age)):
                idx = pc.build_indexes(cached["companies"], props)
                with _LOCK:
                    _set_cache(idx, len(cached["companies"]), cached.get("saved_at", 0))
                return _summary()
        t0 = time.time()
        companies = pc.fetch_all_companies(token, need)
        idx = pc.build_indexes(companies, props)
        with _LOCK:
            _set_cache(idx, len(companies), time.time())
        print("Pulled %d parcels from HubSpot in %.0fs" % (len(companies), time.time() - t0))
        _save_disk_cache(companies, need)
        return _summary()


def _summary():
    return {"count": _CACHE["count"], "synced_at": _CACHE["synced_at"],
            "apn": _CACHE["apn"], "owner": _CACHE["owner"],
            "address": _CACHE["address"],
            "loading": _CACHE["idx"] is None, "refreshing": _FETCH_LOCK.locked()}


def start_sync(cfg, force=False):
    """Kick off a pull in a background thread and return the current state at
    once, so no HTTP request ever waits minutes on HubSpot."""
    if not get_token(cfg):
        raise ValueError("No HubSpot token set." + ("" if HOSTED else " Add it under Settings."))
    if force or _CACHE["idx"] is None:
        if not _FETCH_LOCK.locked():
            def job():
                try:
                    do_sync(cfg, force=force)
                except BaseException as e:
                    print("Parcel refresh failed: %s" % e)
            threading.Thread(target=job, daemon=True).start()
            time.sleep(0.05)
    return _summary()


def run_check(cfg, csv_text, client_cols=None):
    if _CACHE["idx"] is None:
        raise ValueError("Parcels not loaded yet.")
    idx = _CACHE["idx"]["_idx"]
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    rows = list(reader)
    if not headers:
        raise ValueError("That file has no header row / columns.")

    auto = pc.detect_columns(headers)
    cols = {}
    for f in ("apn", "owner", "address", "county"):
        v = (client_cols or {}).get(f)
        cols[f] = v if v else auto.get(f)
    county_active = bool(cfg["props"].get("county"))

    engaged = {s.strip().lower() for s in cfg.get("engaged_statuses", []) if s.strip()}
    mail_enabled = bool(cfg["props"].get("undeliverable") or cfg["props"].get("last_mailed"))

    extra = ["match_found", "matched_on", "matched_lead_status"]
    if engaged:
        extra.append("already_engaged")
    if mail_enabled:
        extra += ["mail_undeliverable", "date_last_mailed"]
    extra += ["matched_hs_name", "matched_hs_id"]

    out_rows, n_matched, n_engaged, n_undeliv, n_mailed = [], 0, 0, 0, 0
    for row in rows:
        types, matches = pc.match_row(row, cols, idx, county_active=county_active)
        out = dict(row)
        if matches:
            n_matched += 1
            statuses, names, ids, vias, mailed = [], [], [], set(), []
            undeliv = False
            for m in matches:
                rec = m["rec"]
                if rec["status"]:
                    statuses.append(rec["status"])
                names.append(rec["name"])
                ids.append(str(rec["id"]))
                vias |= m["via"]
                undeliv = undeliv or rec.get("undeliverable", False)
                if rec.get("last_mailed"):
                    mailed.append(rec["last_mailed"])
            out["match_found"] = "YES"
            out["matched_on"] = ";".join(sorted(vias))
            out["matched_lead_status"] = ";".join(sorted(set(statuses)))
            out["matched_hs_name"] = ";".join(dict.fromkeys(names))
            out["matched_hs_id"] = ";".join(dict.fromkeys(ids))
            if engaged:
                is_eng = any(s.strip().lower() in engaged for s in statuses)
                out["already_engaged"] = "YES" if is_eng else "no"
                if is_eng:
                    n_engaged += 1
            if mail_enabled:
                out["mail_undeliverable"] = "YES" if undeliv else ""
                out["date_last_mailed"] = max(mailed) if mailed else ""
                n_undeliv += 1 if undeliv else 0
                n_mailed += 1 if mailed else 0
        else:
            out["match_found"] = "no"
            for col in extra:
                out.setdefault(col, "")
        out_rows.append(out)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers + extra)
    w.writeheader()
    w.writerows(out_rows)

    return {
        "total": len(rows), "matched": n_matched, "engaged": n_engaged,
        "undeliverable": n_undeliv, "mailed": n_mailed,
        "columns": headers + extra, "rows": out_rows[:200],
        "truncated": len(out_rows) > 200,
        "csv": buf.getvalue(), "cols_used": cols,
        "engaged_enabled": bool(engaged), "mail_enabled": mail_enabled,
    }


def _warm_loop():
    """Pull parcels at startup, then re-pull in the background whenever the
    cache passes max_cache_minutes, so checks always run against memory."""
    while True:
        cfg = load_config()
        minutes = float(cfg.get("max_cache_minutes", 60))
        try:
            if get_token(cfg):
                stale = _CACHE["idx"] is None or _stale(_CACHE["synced_at"], minutes * 60)
                if stale:
                    do_sync(cfg, force=_CACHE["idx"] is not None)
        except BaseException as e:  # pc raises SystemExit on API failure
            print("Parcel refresh failed: %s" % e)
        time.sleep(60)


# ===========================================================================
# HTTP
# ===========================================================================

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Parcel Check</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:#EEF1EF;color:#16242B;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .box{background:#fff;border:1px solid #DBE3E0;border-radius:12px;padding:26px 24px;width:92%;max-width:340px;
    box-shadow:0 1px 2px rgba(16,40,45,.06),0 8px 24px rgba(16,40,45,.05)}
  h1{font-size:20px;margin:0 0 16px}
  input{width:100%;box-sizing:border-box;font-size:16px;padding:10px 12px;border:1px solid #DBE3E0;
    border-radius:8px;margin-bottom:12px}
  button{width:100%;font:inherit;font-weight:650;padding:11px;border:0;border-radius:9px;
    background:#1F6F6B;color:#fff;cursor:pointer}
  .err{color:#7A271A;font-size:13px;min-height:18px;margin-bottom:6px}
</style></head><body>
<form class="box" id="f">
  <h1>Parcel Check</h1>
  <div class="err" id="e"></div>
  <input type="password" id="p" placeholder="Team password" autofocus aria-label="Password">
  <button type="submit">Enter</button>
</form>
<script>
document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault();
  const r = await fetch("/api/login", {method:"POST",headers:{"Content-Type":"application/json"},
    body: JSON.stringify({password: document.getElementById("p").value})});
  if (r.ok) { location.href = "/"; }
  else { document.getElementById("e").textContent = "Wrong password."; document.getElementById("p").select(); }
});
</script></body></html>
"""

PAGE = ""


class Handler(BaseHTTPRequestHandler):
    server_version = "ParcelCheck/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj, headers=None):
        self._send(code, json.dumps(obj), headers=headers)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD_BYTES:
            raise ValueError("File is too large (limit %d MB)." % (MAX_UPLOAD_BYTES // 1024 // 1024))
        return json.loads(self.rfile.read(n) or b"{}")

    def _authed(self):
        if not auth_enabled():
            return True
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get("parcel_auth")
        return bool(morsel and hmac.compare_digest(morsel.value, _auth_token()))

    def _path(self):
        return self.path.split("?", 1)[0]

    def do_GET(self):
        path = self._path()
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if not self._authed():
            if path in ("/", "/index.html"):
                return self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
            return self._json(401, {"error": "Login required."})
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/config":
            cfg = load_config()
            safe = {"props": cfg["props"],
                    "engaged_statuses": cfg["engaged_statuses"],
                    "max_cache_minutes": cfg.get("max_cache_minutes", 60),
                    "token_set": bool(get_token(cfg)),
                    "token_from_env": bool(token_from_env()),
                    "hosted": HOSTED, "version": VERSION,
                    "cache": _summary() if _CACHE["idx"] is not None else _disk_summary()}
            return self._json(200, safe)
        return self._send(404, "Not found", "text/plain")

    def do_POST(self):
        path = self._path()
        try:
            if path == "/api/login":
                pw = (self._body().get("password") or "").strip()
                if auth_enabled() and hmac.compare_digest(pw, APP_PASSWORD):
                    cookie = ("parcel_auth=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                              % _auth_token())
                    if HOSTED:
                        cookie += "; Secure"
                    return self._json(200, {"ok": True}, headers={"Set-Cookie": cookie})
                return self._json(401, {"error": "Wrong password."})

            if not self._authed():
                return self._json(401, {"error": "Login required."})

            if path == "/api/config":
                body = self._body()
                cfg = load_config()
                old_props = dict(cfg["props"])
                if "props" in body:
                    cfg["props"] = {**cfg["props"], **body["props"]}
                if "engaged_statuses" in body:
                    cfg["engaged_statuses"] = body["engaged_statuses"]
                if "max_cache_minutes" in body:
                    try:
                        cfg["max_cache_minutes"] = max(0, int(body["max_cache_minutes"]))
                    except (TypeError, ValueError):
                        pass
                if "token" in body and body["token"] != "" and not token_from_env():
                    cfg["token"] = body["token"]
                save_config(cfg)
                if cfg["props"] != old_props:
                    invalidate_cache()
                return self._json(200, {"ok": True})

            if path == "/api/properties":
                cfg = load_config()
                token = get_token(cfg)
                if not token:
                    return self._json(400, {"error": "No token set yet."})
                return self._json(200, {"properties": pc.get_company_properties_detailed(token)})

            if path == "/api/sync":
                cfg = load_config()
                force = self._body().get("force", False)
                return self._json(200, start_sync(cfg, force=force))

            if path == "/api/detect":
                headers = self._body().get("headers", [])
                return self._json(200, {"cols": pc.detect_columns(headers)})

            if path == "/api/check":
                cfg = load_config()
                body = self._body()
                return self._json(200, run_check(cfg, body.get("csv_text", ""),
                                                 client_cols=body.get("cols")))

            if path == "/api/quit" and not HOSTED:
                self._json(200, {"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
                return

            return self._send(404, "Not found", "text/plain")
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except SystemExit as e:
            return self._json(502, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})


def main():
    global PAGE
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    PAGE = open(UI_PATH, encoding="utf-8").read().replace("__VERSION__", VERSION)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Parcel Check v%s" % VERSION)
    if HOSTED:
        print("Listening on %s:%d (hosted)" % (HOST, PORT))
        if not auth_enabled():
            print("WARNING: APP_PASSWORD is not set -- the link is open to anyone who has it.")
        if not token_from_env():
            print("WARNING: no HUBSPOT_TOKEN in the environment -- checks will fail.")
    else:
        url = "http://127.0.0.1:%d/" % PORT
        print("Open: %s   (Ctrl+C to quit)" % url)
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_warm_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
