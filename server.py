"""IPO Tracker backend.

Pulls IPO dates and price bands from NSE's public JSON endpoints, merges in
GMP / listing dates from overrides.json (no official GMP source exists),
and serves the frontend at /.

Run:  pip install -r requirements.txt && python server.py
Open: http://localhost:8000
"""
import json, os, re, time
from html.parser import HTMLParser
from datetime import datetime
import requests
from flask import Flask, jsonify, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(HERE, "static"))

CACHE_SECONDS = 600
_cache = {"t": 0, "data": None}

NSE_HOME = "https://www.nseindia.com"
NSE_ENDPOINTS = [
    NSE_HOME + "/api/ipo-current-issue",
    NSE_HOME + "/api/all-upcoming-issues?category=ipo",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/market-data/all-upcoming-issues-ipo",
}


def norm(name):
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    n = re.sub(r"\b(limited|ltd|pvt|private|india|ipo|and)\b", "", n)
    return re.sub(r"\s+", "", n)


def to_iso(s):
    if not s:
        return ""
    s = str(s).strip()
    for f in ("%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def parse_band(text):
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", str(text or ""))]
    if not nums:
        return 0, 0
    return min(nums), max(nums)


def pick(d, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def extract_rows(payload):
    """NSE returns either a list or a dict wrapping a list; be tolerant."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def fetch_nse():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=15)  # sets the cookies NSE expects
    out, raw = [], {}
    for url in NSE_ENDPOINTS:
        try:
            r = s.get(url, timeout=15)
            r.raise_for_status()
            payload = r.json()
        except Exception as e:  # keep going if one endpoint fails
            raw[url] = "ERROR: %s" % e
            continue
        raw[url] = payload
        for row in extract_rows(payload):
            name = pick(row, "companyName", "company", "name", "symbol")
            if not name:
                continue
            lo, hi = parse_band(pick(row, "issuePrice", "priceBand", "price"))
            out.append({
                "id": "nse-" + norm(name),
                "name": name,
                "lo": lo, "hi": hi,
                "gmp": 0, "lot": 0,
                "open": to_iso(pick(row, "issueStartDate", "openDate", "biddingStartDate")),
                "close": to_iso(pick(row, "issueEndDate", "closeDate", "biddingEndDate")),
                "allot": "", "list": "",
            })
    return out, raw


# ---------------------------------------------------------------- GMP source
GMP_URLS = [
    "https://www.niftytrader.in/ipo/gmp/mainboard",
    "https://www.niftytrader.in/ipo/gmp",
]


class _TableParser(HTMLParser):
    """Collects every <tr> on the page as a list of cell texts."""
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", " ".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_gmp_value(text):
    m = re.search(r"([+\-\u2212\u2013]?)\s*\u20b9?\s*(\d+(?:\.\d+)?)", str(text or ""))
    if not m:
        return 0.0
    v = float(m.group(2))
    return -v if m.group(1) in ("-", "\u2212", "\u2013") else v


def parse_gmp_html(html):
    p = _TableParser()
    p.feed(html)
    header, idx = None, {}
    wanted = {"company": "name", "gmp": "gmp", "lot size": "lot", "open": "open",
              "close": "close", "allotment": "allot", "listing": "list",
              "est listing": "est"}
    for row in p.rows:
        cleaned = [re.sub(r"[^a-z ]", "", c.lower()).strip() for c in row]
        if "company" in cleaned and "gmp" in cleaned:
            header = cleaned
            idx = {wanted[h]: i for i, h in enumerate(cleaned) if h in wanted}
            break
    if not header or "name" not in idx:
        return []
    out = []
    started = False
    for row in p.rows:
        if not started:
            started = [re.sub(r"[^a-z ]", "", c.lower()).strip() for c in row] == header
            continue
        if len(row) < len(header):
            continue
        g = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else ""
        lot = re.sub(r"[^\d]", "", g("lot"))
        m = re.search(r"\u20b9\s*([\d,]+(?:\.\d+)?)", g("est"))
        est = float(m.group(1).replace(",", "")) if m else 0.0
        out.append({
            "name": g("name"),
            "gmp": parse_gmp_value(g("gmp")), "est": est,
            "lot": int(lot) if lot else 0,
            "open": to_iso(g("open")), "close": to_iso(g("close")),
            "allot": to_iso(g("allot")), "list": to_iso(g("list")),
        })
    return out


def get_gmp_html(url):
    r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"],
                                   "Accept": "text/html,application/xhtml+xml"}, timeout=20)
    r.raise_for_status()
    return r.text


def fetch_gmp():
    rows, seen, notes = [], set(), []
    for url in GMP_URLS:
        try:
            got = parse_gmp_html(get_gmp_html(url))
        except Exception as e:
            notes.append("%s: %s" % (url, e))
            continue
        for r in got:
            k = norm(r["name"])
            if k and k not in seen:
                seen.add(k)
                rows.append(r)
    return rows, ("; ".join(notes) or None)


# ------------------------------------------------------- subscription source
SUB_URLS = [
    "https://www.niftytrader.in/ipo/subscription-status/mainboard",
    "https://www.niftytrader.in/ipo/subscription-status/sme",
    "https://www.niftytrader.in/ipo/subscription-status",
]


def _num(text):
    m = re.search(r"\d+(?:\.\d+)?", str(text or ""))
    return float(m.group(0)) if m else None


def parse_sub_html(html):
    p = _TableParser()
    p.feed(html)
    idx, out = None, []
    for row in p.rows:
        cleaned = [re.sub(r"[^a-z ]", "", c.lower()).strip() for c in row]
        if any(c.startswith("company") for c in cleaned) and any(c.startswith("total") for c in cleaned):
            idx = {}
            for i, h in enumerate(cleaned):
                if h.startswith("company"):
                    idx.setdefault("name", i)
                elif h in ("qib", "retail"):
                    idx.setdefault(h, i)
                elif h in ("hni", "nii", "hninii"):
                    idx.setdefault("hni", i)
                elif h.startswith("total"):
                    idx.setdefault("total", i)
            continue
        if not idx or len(row) <= max(idx.values()) or not row[idx["name"]]:
            continue
        rec = {"name": row[idx["name"]]}
        for k in ("qib", "hni", "retail", "total"):
            rec[k] = _num(row[idx[k]]) if k in idx else None
        out.append(rec)
    return out


def fetch_subs():
    rows, seen, notes = [], set(), []
    for url in SUB_URLS:
        try:
            got = parse_sub_html(get_gmp_html(url))
        except Exception as e:
            notes.append("%s: %s" % (url, e))
            continue
        for r in got:
            k = norm(r["name"])
            if k and k not in seen:
                seen.add(k)
                rows.append(r)
    return rows, ("; ".join(notes) or None)


def debug_subs():
    out = {}
    for url in SUB_URLS:
        try:
            html = get_gmp_html(url)
            out[url] = {"bytes": len(html), "has_table": "<table" in html.lower(),
                        "parsed": parse_sub_html(html)[:5]}
        except Exception as e:
            out[url] = "ERROR: %s" % e
    return out


def load_overrides():
    p = os.path.join(HERE, "overrides.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build():
    error = None
    try:
        items, raw = fetch_nse()
    except Exception as e:
        items, raw, error = [], {}, str(e)

    by_key = {norm(i["name"]): i for i in items}
    items = list(by_key.values())

    gmp_note = None
    try:
        rows, gmp_note = fetch_gmp()
        matched = added = 0
        for r in rows:
            key = norm(r["name"])
            it = by_key.get(key)
            if it:
                matched += 1
            else:  # NSE didn't list it (or NSE failed): build the row from NiftyTrader
                hi = r["est"] - r["gmp"] if r["est"] else 0
                hi = hi if hi > 0 else 0
                it = {"id": "nt-" + key, "name": re.sub(r"\s+IPO$", "", r["name"]).strip(),
                      "lo": hi, "hi": hi, "gmp": 0, "lot": 0,
                      "open": "", "close": "", "allot": "", "list": ""}
                by_key[key] = it
                items.append(it)
                added += 1
            it["gmp"] = r["gmp"]
            if r["lot"]:
                it["lot"] = r["lot"]
            for k in ("open", "close", "allot", "list"):
                if r[k] and not it.get(k):
                    it[k] = r[k]
                elif r[k] and k in ("allot", "list"):
                    it[k] = r[k]
        if rows:
            gmp_note = "%d IPOs from NiftyTrader (%d matched NSE, %d added)%s" % (
                len(rows), matched, added, "" if items and matched else "; NSE returned nothing")
        else:
            gmp_note = "GMP source returned no data (%s)" % gmp_note
    except Exception as e:
        gmp_note = "GMP fetch failed: %s" % e

    sub_note = None
    try:
        srows, sub_note = fetch_subs()
        n = 0
        for r in srows:
            it = by_key.get(norm(r["name"]))
            if it:
                n += 1
                for k in ("total", "retail", "hni", "qib"):
                    if r.get(k) is not None:
                        it["sub_" + k] = r[k]
        sub_note = ("Subscription data matched for %d IPOs" % n) if srows else \
                   "Subscription source returned no data (%s)" % sub_note
    except Exception as e:
        sub_note = "Subscription fetch failed: %s" % e

    for name, o in load_overrides().items():
        if name.startswith("_"):
            continue
        key = norm(name)
        if key in by_key:
            by_key[key].update({k: v for k, v in o.items() if v not in (None, "")})
        elif o.get("hi") and o.get("open"):  # manual-only entry
            e = {"id": "man-" + key, "name": name, "lo": 0, "hi": 0, "gmp": 0, "lot": 0,
                 "open": "", "close": "", "allot": "", "list": ""}
            e.update(o)
            items.append(e)
    return {"items": items, "updated": int(time.time()), "error": error, "gmp_note": gmp_note, "sub_note": sub_note}, raw


def get_data(force=False):
    if force or not _cache["data"] or time.time() - _cache["t"] > CACHE_SECONDS:
        data, raw = build()
        if data["items"] or not _cache["data"]:  # keep last good data if NSE fails
            _cache.update(t=time.time(), data=data, raw=raw)
    return _cache["data"]


@app.route("/api/ipos")
def api_ipos():
    return jsonify(get_data())


@app.route("/api/raw")
def api_raw():
    """Debug: shows what NSE actually returned so field names can be checked."""
    get_data(force=True)
    return jsonify(_cache.get("raw", {}))


@app.route("/api/gmpdebug")
def api_gmpdebug():
    """Debug: shows what the GMP source returned and how it was parsed."""
    out = {}
    for url in GMP_URLS:
        try:
            html = get_gmp_html(url)
            out[url] = {"bytes": len(html), "has_table": "<table" in html.lower(),
                        "parsed": parse_gmp_html(html)[:5]}
        except Exception as e:
            out[url] = "ERROR: %s" % e
    return jsonify(out)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
