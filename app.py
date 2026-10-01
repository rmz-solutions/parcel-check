#!/usr/bin/env python3
"""
app.py -- local point-and-click UI for the parcel checker.

Run it (or double-click a launcher) and it opens in your browser:
    python3 app.py

Why a local server and not just a web page: the page can't call HubSpot
directly (your token would be exposed and HubSpot blocks browser calls), so
this small program runs on your own computer, holds the token locally, talks
to HubSpot for you, and shows the results in your browser. Nothing leaves your
machine except the read-only calls to HubSpot.

Standard library only. Python 3.8+.
"""

import csv
import io
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import parcel_check as pc


def resource_dir():
    """Where bundled files (ui.html) live -- a temp dir when packaged."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_dir():
    """A persistent, writable place for settings (survives app restarts)."""
    d = os.path.join(os.path.expanduser("~"), ".parcelcheck")
    os.makedirs(d, exist_ok=True)
    return d


HERE = resource_dir()
UI_PATH = os.path.join(HERE, "ui.html")
CONFIG_PATH = os.path.join(config_dir(), "config.json")
CACHE_FILE = os.path.join(config_dir(), "parcels_cache.json")
META_FILE = os.path.join(config_dir(), "parcels_meta.json")
PORT = 8765

DEFAULT_CONFIG = {
    "token": "",
    "props": {
        "apn": "apn", "owner": "owner_entity", "address": "mailing_address",
        "status": "lead_status", "name": "name", "county": "",
    },
    "engaged_statuses": [],
    "max_cache_minutes": 60,
}

# ---- shared parcel cache (in-memory; backed by a disk file across launches) ----
_LOCK = threading.Lock()
_CACHE = {"idx": None, "count": 0, "synced_at": 0,
          "apn": 0, "owner": 0, "address": 0}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(CONFIG_PATH):
        try:
            saved = json.load(open(CONFIG_PATH, encoding="utf-8"))
            cfg.update({k: saved.get(k, cfg[k]) for k in cfg})
            cfg["props"] = {**DEFAULT_CONFIG["props"], **(saved.get("props") or {})}
        except Exception:
            pass
    return cfg


def save_config(cfg):
    json.dump(cfg, open(CONFIG_PATH, "w", encoding="utf-8"), indent=2)


def get_token(cfg):
    return os.environ.get("HUBSPOT_TOKEN") or cfg.get("token") or ""


def _needed_props(props):
    names = [props.get("apn"), props.get("owner"), props.get("address"),
             props.get("status"), props.get("name")]
    if props.get("county"):
        names.append(props["county"])
    return [n for n in names if n]


def _save_disk_cache(companies, props_fetched):
    saved_at = time.time()
    try:
        json.dump({"saved_at": saved_at, "props_fetched": props_fetched,
                   "companies": companies}, open(CACHE_FILE, "w"))
        # small sidecar so the UI can show count + last-sync time instantly,
        # without parsing the (potentially large) full cache file
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
    """count + last-sync time read from the lightweight meta file (or None)."""
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
    """True if a cache timestamp is older than max_age seconds (0 = never expire)."""
    if max_age <= 0:
        return False
    return (not ts) or (time.time() - ts) > max_age


def do_sync(cfg, force=False):
    """Return indexed parcels. Order of preference: in-memory cache, then the
    on-disk cache (instant, no network), then a fresh HubSpot fetch. A cache
    older than max_cache_minutes is treated as stale and refreshed automatically."""
    token = get_token(cfg)
    if not token:
        raise ValueError("No HubSpot token set. Add it under Settings.")
    max_age = float(cfg.get("max_cache_minutes", 60)) * 60
    with _LOCK:
        if _CACHE["idx"] is not None and not force and not _stale(_CACHE["synced_at"], max_age):
            return _summary()
        props = cfg["props"]
        need = _needed_props(props)
        # disk cache: usable only if it holds every property we need AND is fresh
        if not force:
            cached = _load_disk_cache()
            if (cached and set(need).issubset(set(cached.get("props_fetched", [])))
                    and not _stale(cached.get("saved_at", 0), max_age)):
                idx = pc.build_indexes(cached["companies"], props)
                _set_cache(idx, len(cached["companies"]), cached.get("saved_at", 0))
                return _summary()
        # fresh fetch from HubSpot (the slow path)
        companies = pc.fetch_all_companies(token, need)
        _save_disk_cache(companies, need)
        idx = pc.build_indexes(companies, props)
        _set_cache(idx, len(companies), time.time())
        return _summary()


def _summary():
    return {"count": _CACHE["count"], "synced_at": _CACHE["synced_at"],
            "apn": _CACHE["apn"], "owner": _CACHE["owner"],
            "address": _CACHE["address"]}


def run_check(cfg, csv_text, client_cols=None):
    if _CACHE["idx"] is None:
        raise ValueError("Parcels not loaded yet.")
    idx = _CACHE["idx"]["_idx"]
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    rows = list(reader)
    if not headers:
        raise ValueError("That file has no header row / columns.")

    # The app auto-locates columns and lets the user confirm/correct them, so a
    # confirmed mapping arrives from the client. Fall back to auto-detect.
    auto = pc.detect_columns(headers)
    cols = {}
    for f in ("apn", "owner", "address", "county"):
        v = (client_cols or {}).get(f)
        cols[f] = v if v else auto.get(f)
    # only honor county scoping if a county HubSpot property is configured
    county_active = bool(cfg["props"].get("county"))

    engaged = {s.strip().lower() for s in cfg.get("engaged_statuses", []) if s.strip()}

    extra = ["match_found", "matched_on", "matched_lead_status"]
    if engaged:
        extra.append("already_engaged")
    extra += ["matched_hs_name", "matched_hs_id"]

    out_rows, n_matched, n_engaged = [], 0, 0
    for row in rows:
        types, matches = pc.match_row(row, cols, idx, county_active=county_active)
        out = dict(row)
        if matches:
            n_matched += 1
            statuses, names, ids, vias = [], [], [], set()
            for m in matches:
                rec = m["rec"]
                if rec["status"]:
                    statuses.append(rec["status"])
                names.append(rec["name"])
                ids.append(str(rec["id"]))
                vias |= m["via"]
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
        else:
            out["match_found"] = "no"
            for col in extra:
                out.setdefault(col, "")
        out_rows.append(out)

    # build output CSV
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers + extra)
    w.writeheader()
    w.writerows(out_rows)

    return {
        "total": len(rows), "matched": n_matched, "engaged": n_engaged,
        "columns": headers + extra, "rows": out_rows[:200],
        "truncated": len(out_rows) > 200,
        "csv": buf.getvalue(), "cols_used": cols,
        "engaged_enabled": bool(engaged),
    }


# ===========================================================================
# HTTP handler
# ===========================================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet console
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/config":
            cfg = load_config()
            safe = {"props": cfg["props"],
                    "engaged_statuses": cfg["engaged_statuses"],
                    "max_cache_minutes": cfg.get("max_cache_minutes", 60),
                    "token_set": bool(get_token(cfg)),
                    "token_from_env": bool(os.environ.get("HUBSPOT_TOKEN")),
                    "cache": _summary() if _CACHE["idx"] is not None else _disk_summary()}
            return self._json(200, safe)
        return self._send(404, "Not found", "text/plain")

    def do_POST(self):
        try:
            if self.path == "/api/config":
                body = self._body()
                cfg = load_config()
                if "props" in body:
                    cfg["props"] = {**cfg["props"], **body["props"]}
                if "engaged_statuses" in body:
                    cfg["engaged_statuses"] = body["engaged_statuses"]
                if "max_cache_minutes" in body:
                    try:
                        cfg["max_cache_minutes"] = max(0, int(body["max_cache_minutes"]))
                    except (TypeError, ValueError):
                        pass
                if "token" in body and body["token"] != "":
                    cfg["token"] = body["token"]
                save_config(cfg)
                return self._json(200, {"ok": True})

            if self.path == "/api/properties":
                cfg = load_config()
                token = get_token(cfg)
                if not token:
                    return self._json(400, {"error": "No token set yet."})
                props = pc.get_company_properties_detailed(token)
                return self._json(200, {"properties": props})

            if self.path == "/api/sync":
                cfg = load_config()
                force = self._body().get("force", False)
                return self._json(200, do_sync(cfg, force=force))

            if self.path == "/api/detect":
                headers = self._body().get("headers", [])
                return self._json(200, {"cols": pc.detect_columns(headers)})

            if self.path == "/api/check":
                cfg = load_config()
                body = self._body()
                result = run_check(cfg, body.get("csv_text", ""),
                                   client_cols=body.get("cols"))
                return self._json(200, result)

            if self.path == "/api/quit":
                self._json(200, {"ok": True})
                threading.Thread(
                    target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True
                ).start()
                return

            return self._send(404, "Not found", "text/plain")
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except SystemExit as e:
            return self._json(502, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})


PAGE = ""  # injected below from ui.html at startup


def find_port(start):
    for p in range(start, start + 25):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return start


def main():
    global PAGE
    PAGE = open(UI_PATH, encoding="utf-8").read()
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
    port = find_port(PORT)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Parcel Check is running.\n  Open this in your browser:  {url}\n"
          f"  (Use the Quit button in the app, or close this window, to stop.)\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
