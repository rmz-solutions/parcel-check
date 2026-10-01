#!/usr/bin/env python3
"""
parcel_check.py
---------------
Check a new property-list CSV against existing parcels (Companies) in HubSpot.

For every row in your CSV it tells you whether that parcel already exists in
HubSpot -- matched on APN, owner entity, OR mailing address (each normalized so
formatting differences don't cause misses) -- and what the existing lead status
is, so you can skip parcels you're already working or in contact with.

No third-party packages required: standard library only (Python 3.8+).

USAGE
-----
1. One-time setup: create a HubSpot Service Key (or legacy Private App token)
   per README.md, then set it as an environment variable:
       export HUBSPOT_TOKEN="your-hubspot-key"          (macOS/Linux)
       setx HUBSPOT_TOKEN "your-hubspot-key"            (Windows, new shell after)

2. Find the internal names of your custom properties (run once):
       python3 parcel_check.py --list-properties

3. Put those internal names into the CONFIG block below (HS_PROP_*).

4. Run a check:
       python3 parcel_check.py my_new_list.csv
   -> writes my_new_list_checked.csv next to it.

The tool auto-detects which columns in your CSV hold APN / owner / address by
their header names; override with --apn-col / --owner-col / --address-col if
your headers are unusual.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ===========================================================================
# CONFIG  --  edit the four internal property names to match YOUR HubSpot.
# Run `python3 parcel_check.py --list-properties` to discover them.
# ===========================================================================
HS_PROP_APN = "apn"                 # internal name of your APN property
HS_PROP_OWNER = "owner_entity"      # internal name of your owner-entity property
HS_PROP_ADDRESS = "mailing_address" # internal name of your mailing-address property
HS_PROP_LEAD_STATUS = "lead_status" # internal name of your custom lead-status property
HS_PROP_NAME = "name"               # company name, for human-readable output

# Optional: lead-status values that mean "already engaged / don't re-target".
# Leave as [] to simply report status without an engaged/notengaged judgement.
ENGAGED_STATUSES = []  # e.g. ["interested", "signed", "in contact"]

API_BASE = "https://api.hubapi.com"
PAGE_LIMIT = 100  # max allowed by the list endpoint

# ===========================================================================
# Normalization
# ===========================================================================

_ENTITY_NOISE = {
    "LLC", "LLLP", "LP", "LLP", "INC", "INCORPORATED", "CORP", "CORPORATION",
    "CO", "COMPANY", "TRUST", "TR", "TRUSTEE", "TRUSTEES", "TTEE", "REVOCABLE",
    "REV", "IRREVOCABLE", "LIVING", "LIV", "FAMILY", "FAM", "THE", "ESTATE",
    "ET", "AL", "ETAL", "ETUX", "ETVIR", "JT", "JTRS", "JTWROS", "AND", "&",
}
# strip "DTD 1/1/2020", "DATED 01-01-2020", "U/T/D ...", trailing date noise
_DATE_NOISE_RE = re.compile(
    r"\b(?:DTD|DATED|UTD|U/T/D|UDT|U/D/T|DT)\b.*$", re.IGNORECASE
)

_STREET_SUFFIX = {
    "STREET": "ST", "STR": "ST", "ST": "ST",
    "AVENUE": "AVE", "AVEN": "AVE", "AVN": "AVE", "AVE": "AVE", "AV": "AVE",
    "BOULEVARD": "BLVD", "BLVD": "BLVD", "BOUL": "BLVD",
    "DRIVE": "DR", "DRV": "DR", "DR": "DR",
    "ROAD": "RD", "RD": "RD",
    "LANE": "LN", "LN": "LN",
    "COURT": "CT", "CRT": "CT", "CT": "CT",
    "PLACE": "PL", "PL": "PL",
    "TERRACE": "TER", "TERR": "TER", "TER": "TER",
    "CIRCLE": "CIR", "CIR": "CIR", "CIRC": "CIR",
    "HIGHWAY": "HWY", "HWY": "HWY",
    "PARKWAY": "PKWY", "PKWY": "PKWY", "PKY": "PKWY",
    "TRAIL": "TRL", "TRL": "TRL",
    "WAY": "WAY", "WY": "WAY",
    "SQUARE": "SQ", "SQ": "SQ",
    "LOOP": "LOOP",
    "PIKE": "PIKE",
    "ROUTE": "RTE", "RTE": "RTE", "RT": "RTE",
}
_DIRECTIONAL = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "N": "N", "S": "S", "E": "E", "W": "W", "NE": "NE", "NW": "NW",
    "SE": "SE", "SW": "SW",
}
_UNIT_WORDS = {"APT", "APARTMENT", "UNIT", "STE", "SUITE", "BLDG", "BUILDING",
               "FL", "FLOOR", "RM", "ROOM", "NO", "#"}


_SCI_APN_RE = re.compile(r"^[+-]?\d+\.\d+[eE][+-]?\d+$")


def normalize_apn(value):
    """APNs differ only in punctuation/spacing; reduce to bare alphanumerics."""
    if not value:
        return ""
    s = str(value).strip()
    # Excel sometimes writes a numeric APN to CSV in scientific notation
    # (e.g. "1.23456789012E+11"). If the saved text still holds full precision,
    # expand it back to plain digits. (Precision Excel already rounded away
    # when storing it as a number cannot be recovered here -- keep APNs as TEXT.)
    if _SCI_APN_RE.match(s):
        try:
            from decimal import Decimal
            s = format(Decimal(s), "f").split(".")[0]
        except Exception:
            pass
    bare = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    # Spreadsheets routinely drop leading zeros from APN columns, so compare
    # without them (e.g. "000112233" == "00112233" == "112233").
    return bare.lstrip("0") or bare


def normalize_county(value):
    """Collapse 'Maricopa County' / 'MARICOPA CO.' -> 'MARICOPA' for scoping."""
    if not value:
        return ""
    s = re.sub(r"[^A-Za-z0-9 ]", " ", str(value)).upper()
    s = re.sub(r"\bCOUNTY\b|\bCO\b", " ", s)
    return re.sub(r"\s+", "", s)


def normalize_entity(value):
    """
    Normalize an owner entity so formatting/order/legal-suffix differences
    collapse together. Tokens are sorted so 'JOHN SMITH' == 'SMITH JOHN' and
    'SMITH FAMILY TRUST' == 'SMITH TRUST'. Recall-favoring on purpose.
    """
    if not value:
        return ""
    s = str(value).upper()
    s = _DATE_NOISE_RE.sub(" ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)        # drop punctuation
    tokens = [t for t in s.split() if t and t not in _ENTITY_NOISE]
    tokens = [t for t in tokens if not t.isdigit() or len(t) > 4]  # drop stray date digits
    if not tokens:
        return ""
    return " ".join(sorted(set(tokens)))


def normalize_address(value):
    """Standardize a US mailing address for comparison (suffixes/directionals)."""
    if not value:
        return ""
    s = str(value).upper()
    s = re.sub(r"[.,#]", " ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    out = []
    for tok in s.split():
        if tok in _UNIT_WORDS:
            continue
        tok = _DIRECTIONAL.get(tok, tok)
        tok = _STREET_SUFFIX.get(tok, tok)
        out.append(tok)
    return " ".join(out)


# ===========================================================================
# HubSpot API
# ===========================================================================

def _request(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or 500 <= e.code < 600:
                wait = int(e.headers.get("Retry-After", 0)) or (2 ** attempt)
                sys.stderr.write(f"  rate/server limit ({e.code}); waiting {wait}s...\n")
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", "replace")
            raise SystemExit(f"\nHubSpot API error {e.code}: {body}\n")
        except urllib.error.URLError as e:
            wait = 2 ** attempt
            sys.stderr.write(f"  network issue ({e.reason}); retrying in {wait}s...\n")
            time.sleep(wait)
    raise SystemExit("Giving up after repeated HubSpot API failures.")


def list_company_properties(token):
    url = f"{API_BASE}/crm/v3/properties/companies"
    data = _request(url, token)
    return sorted(
        (p.get("name", ""), p.get("label", "")) for p in data.get("results", [])
    )


def get_company_properties_detailed(token):
    """Return [{name,label,type,options:[...]}] for the Settings UI."""
    url = f"{API_BASE}/crm/v3/properties/companies"
    data = _request(url, token)
    out = []
    for p in data.get("results", []):
        out.append({
            "name": p.get("name", ""),
            "label": p.get("label", "") or p.get("name", ""),
            "type": p.get("type", ""),
            "options": [o.get("label", o.get("value", ""))
                        for o in (p.get("options") or [])],
        })
    out.sort(key=lambda x: x["label"].lower())
    return out


def fetch_all_companies(token, props):
    """Page through every company, requesting only the props we need."""
    records = []
    after = None
    page = 0
    while True:
        params = {"limit": PAGE_LIMIT, "properties": ",".join(props), "archived": "false"}
        if after:
            params["after"] = after
        url = f"{API_BASE}/crm/v3/objects/companies?{urllib.parse.urlencode(params)}"
        data = _request(url, token)
        results = data.get("results", [])
        records.extend(results)
        page += 1
        sys.stderr.write(f"\r  fetched {len(records)} companies ({page} pages)...")
        sys.stderr.flush()
        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break
    sys.stderr.write("\n")
    return records


# ===========================================================================
# Indexing & matching
# ===========================================================================

def build_indexes(companies, props=None):
    # props maps logical field -> HubSpot internal name; defaults to CONFIG above
    props = props or {
        "name": HS_PROP_NAME, "apn": HS_PROP_APN, "owner": HS_PROP_OWNER,
        "address": HS_PROP_ADDRESS, "status": HS_PROP_LEAD_STATUS,
    }
    county_prop = props.get("county") or ""   # optional
    idx = {"apn": {}, "owner": {}, "address": {}}
    for c in companies:
        p = c.get("properties", {}) or {}
        rec = {
            "id": c.get("id", ""),
            "name": p.get(props["name"], "") or "",
            "apn": p.get(props["apn"], "") or "",
            "owner": p.get(props["owner"], "") or "",
            "address": p.get(props["address"], "") or "",
            "status": p.get(props["status"], "") or "",
            "county": (p.get(county_prop, "") or "") if county_prop else "",
        }
        apn_n = normalize_apn(rec["apn"])
        if apn_n:
            # scope APN by county when a county property is configured
            key = (normalize_county(rec["county"]) + "::" + apn_n) if county_prop else apn_n
            idx["apn"].setdefault(key, []).append(rec)
        for ikey, normfn in (("owner", normalize_entity), ("address", normalize_address)):
            nv = normfn(rec[ikey])
            if nv:  # never index empty -> empty must not match everything
                idx[ikey].setdefault(nv, []).append(rec)
    return idx


# Header keywords for auto-locating CSV columns (normalized: lowercase alnum).
# The app calls detect_columns() via /api/detect so the logic lives only here.
COLUMN_CANDIDATES = {
    "apn": ["apn", "parcelnumber", "parcelno", "parcelid", "parcel", "ain", "pin"],
    "owner": ["ownerentity", "ownername", "owner", "grantee", "entity",
              "taxpayer", "ownedby"],
    "address": ["mailingaddress", "mailaddress", "owneraddress", "mailingaddr",
                "mailing", "address", "addr"],
    "county": ["county", "cnty"],
}


def _norm_header(h):
    return re.sub(r"[^a-z0-9]", "", str(h).lower())


def detect_column(headers, candidates):
    norm = [(h, _norm_header(h)) for h in headers]
    for h, n in norm:                       # exact match wins
        if n in candidates:
            return h
    for h, n in norm:                       # then startswith
        if any(n.startswith(c) for c in candidates):
            return h
    for h, n in norm:                       # then contains
        if any(c in n for c in candidates):
            return h
    return None


def detect_columns(headers):
    """Auto-locate every known field in a CSV's headers -> {field: header|None}."""
    return {f: detect_column(headers, cands) for f, cands in COLUMN_CANDIDATES.items()}


def match_row(row, cols, idx, county_active=False):
    hits = []          # (match_type, record)
    types = []
    # APN (optionally county-scoped)
    apn_col = cols.get("apn")
    if apn_col:
        nv = normalize_apn(row.get(apn_col, ""))
        if nv:
            if county_active:
                cnty = normalize_county(row.get(cols.get("county") or "", ""))
                key = cnty + "::" + nv
            else:
                key = nv
            if key in idx["apn"]:
                types.append("APN")
                for rec in idx["apn"][key]:
                    hits.append(("APN", rec))
    # owner + address (never county-scoped)
    for mtype, ck, normfn, ikey in (
        ("OWNER", "owner", normalize_entity, "owner"),
        ("ADDRESS", "address", normalize_address, "address"),
    ):
        col = cols.get(ck)
        if not col:
            continue
        nv = normfn(row.get(col, ""))
        if nv and nv in idx[ikey]:
            types.append(mtype)
            for rec in idx[ikey][nv]:
                hits.append((mtype, rec))
    by_id = {}
    for mtype, rec in hits:
        entry = by_id.setdefault(rec["id"], {"rec": rec, "via": set()})
        entry["via"].add(mtype)
    return types, list(by_id.values())


# ===========================================================================
# Main
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(description="Check a CSV against HubSpot parcels.")
    ap.add_argument("csv", nargs="?", help="input CSV of new parcels")
    ap.add_argument("-o", "--output", help="output CSV path")
    ap.add_argument("--apn-col", help="CSV column holding the APN")
    ap.add_argument("--owner-col", help="CSV column holding the owner entity")
    ap.add_argument("--address-col", help="CSV column holding the mailing address")
    ap.add_argument("--list-properties", action="store_true",
                    help="print all HubSpot company property internal names and exit")
    args = ap.parse_args()

    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        raise SystemExit("Set HUBSPOT_TOKEN environment variable first (see SETUP.md).")

    if args.list_properties:
        print(f"{'INTERNAL NAME':<40} LABEL")
        print("-" * 70)
        for name, label in list_company_properties(token):
            print(f"{name:<40} {label}")
        return

    if not args.csv:
        raise SystemExit("Provide an input CSV, or use --list-properties.")

    # read input CSV
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)
    if not headers:
        raise SystemExit("Input CSV appears to be empty.")

    cols = detect_columns(headers)
    if args.apn_col:
        cols["apn"] = args.apn_col
    if args.owner_col:
        cols["owner"] = args.owner_col
    if args.address_col:
        cols["address"] = args.address_col
    sys.stderr.write("Column mapping (auto-located):\n")
    for k in ("apn", "owner", "address", "county"):
        v = cols.get(k)
        sys.stderr.write(f"  {k:<8} -> {v if v else '(none found)'}\n")
    if not any(cols.get(k) for k in ("apn", "owner", "address")):
        raise SystemExit("Could not find APN/owner/address columns. Use --apn-col etc.")

    sys.stderr.write("Fetching parcels from HubSpot...\n")
    companies = fetch_all_companies(
        token, [HS_PROP_APN, HS_PROP_OWNER, HS_PROP_ADDRESS,
                HS_PROP_LEAD_STATUS, HS_PROP_NAME]
    )
    idx = build_indexes(companies)
    sys.stderr.write(
        f"Indexed: {len(idx['apn'])} APNs, {len(idx['owner'])} owners, "
        f"{len(idx['address'])} addresses.\n"
    )

    out_path = args.output or re.sub(r"\.csv$", "", args.csv, flags=re.I) + "_checked.csv"
    extra = ["match_found", "matched_on", "matched_lead_status",
             "matched_hs_name", "matched_hs_id"]
    if ENGAGED_STATUSES:
        extra.insert(3, "already_engaged")

    n_matched = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers + extra)
        writer.writeheader()
        for row in rows:
            types, matches = match_row(row, cols, idx)
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
                if ENGAGED_STATUSES:
                    eng = any(s.strip().lower() in
                              {x.lower() for x in ENGAGED_STATUSES} for s in statuses)
                    out["already_engaged"] = "YES" if eng else "no"
            else:
                out["match_found"] = "no"
                for col in extra:
                    out.setdefault(col, "")
            writer.writerow(out)

    sys.stderr.write(
        f"\nDone. {n_matched} of {len(rows)} rows matched an existing parcel.\n"
        f"Output: {out_path}\n"
    )


if __name__ == "__main__":
    main()
