#!/usr/bin/env python3
"""Henter data fra Nasjonal vegdatabank (NVDB) for Oslo og Akershus:

  * vegdekke (objekttype 241)            -> vegdekke.json  (når asfalten ble lagt)
  * motorveg/motortrafikkveg (595) og
    trafikkreguleringer (856) som
    forbyr sykling                        -> ikke-sykkel.json  (veier der sykling er forbudt)

Asfaltdata på veier der sykling er forbudt (for eksempel E6) tas ikke med i vegdekke.json.

Kjøres av GitHub Actions når du velger det (se publiser.yml), eller lokalt:
    python3 oppdater_vegdata.py

Bruker bare Pythons standardbibliotek. Skriver ikke over eksisterende fil hvis resultatet ser
mistenkelig lite ut (for eksempel hvis Vegvesenet svarer med feil).
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("NVDB_API", "https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter")
AREA = (10.05, 59.55, 11.95, 60.55)  # vest, sør, øst, nord (samme område som kartet)
COLS, ROWS = 4, 3                    # området deles i ruter for å holde hvert svar lite
OUT = os.environ.get("VEGDATA_UT", "vegdekke.json")
OUT_BAN = os.environ.get("IKKESYKKEL_UT", "ikke-sykkel.json")
TOL_M = 8       # en asfaltstrekning regnes som «på» en forbudt vei hvis den ligger innenfor så mange meter
SHARE_ON = 0.8  # ... for minst denne andelen av punktene
YEAR = datetime.date.today().year
KEEP_YEARS = 10  # asfalt lagt for mer enn så mange år siden importeres aldri


def _cutoff():
    t = datetime.date.today()
    try:
        return t.replace(year=t.year - KEEP_YEARS)
    except ValueError:  # 29. februar
        return t.replace(year=t.year - KEEP_YEARS, day=28)


CUTOFF = _cutoff()


def too_old(date, year):
    """Sant hvis asfalten ble lagt for mer enn KEEP_YEARS år siden."""
    try:
        return datetime.date.fromisoformat(str(date)[:10]) < CUTOFF
    except ValueError:
        return year is not None and year < YEAR - KEEP_YEARS
NOT_ASPHALT = re.compile(r"^(betong|valsebetong|brostein|gatestein|belegningsstein|stålgitter|tre$)", re.I)
HEADERS = {"Accept": "application/json", "X-Client": "asfaltkart (GitHub Actions)"}


def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            err = RuntimeError("http %s: %s" % (e.code, body))
            err.status = e.code
            if e.code < 500:
                raise err  # klientfeil gir ingen mening å prøve på nytt
            last = err
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(2 * (i + 1))
    raise last


def variants(b, typeid):
    base = "%s/%d" % (API, typeid)
    bb = "?kartutsnitt=" + ",".join("%.5f" % v for v in b)
    return [
        base + bb + "&srid=4326&inkluder=egenskaper,geometri&antall=500",
        base + bb + "&srid=4326&inkluder=egenskaper,geometri&antall=100",
        base + bb + "&srid=4326&inkluder=egenskaper&inkluder=geometri&antall=100",
        base + bb + "&srid=wgs84&inkluder=egenskaper,geometri&antall=100",
        base + bb + "&srid=4326&inkluder=alle&antall=100",
    ]


def first_page(b, preferred, typeid):
    vs = variants(b, typeid)
    order = [preferred] + [i for i in range(len(vs)) if i != preferred]
    last = None
    for i in order:
        try:
            data = get_json(vs[i])
            return data, i
        except RuntimeError as e:
            last = e
            if getattr(e, "status", None) == 400:
                print("  variant %d ga 400: %s" % (i, e), file=sys.stderr)
                continue
            raise
    raise last


def parse_wkt(wkt):
    """WKT til liste med linjestykker [lat, lon, lat, lon, ...]. Tåler Z/M og både lat,lon og lon,lat."""
    if not isinstance(wkt, str):
        return None
    m = re.match(r"^\s*(MULTI)?LINESTRING\s*(ZM|Z|M)?\s*\(([\s\S]*)\)\s*$", wkt, re.I)
    if not m:
        return None
    rings = re.split(r"\)\s*,\s*\(", m.group(3)) if m.group(1) else [m.group(3)]
    parts = []
    for ring in rings:
        out = []
        for pt in ring.replace("(", "").replace(")", "").split(","):
            p = pt.split()
            try:
                a, b = float(p[0]), float(p[1])
            except (ValueError, IndexError):
                continue
            if abs(a) > 360 or abs(b) > 360:
                return None  # ikke WGS84
            if 50 <= a <= 72 and 3 <= b <= 32:
                lat, lon = a, b
            else:
                lat, lon = b, a
            out += [round(lat, 5), round(lon, 5)]
        if len(out) >= 4:
            parts.append(out)
    return parts or None


def prop_val(e):
    v = e.get("verdi") if isinstance(e, dict) else None
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("verdi", v.get("navn"))
    return v


def parse_object(o, st):
    date = mass = lag = thick = None
    for e in o.get("egenskaper") or []:
        nm = str(e.get("navn") or "").lower()
        if e.get("id") == 5136 or nm == "dekkeleggingsdato":
            date = prop_val(e)
        elif e.get("id") == 1216 or nm == "massetype":
            mass = prop_val(e)
        elif e.get("id") == 8741 or nm == "lagtype":
            lag = prop_val(e)
        elif e.get("id") == 1642 or nm == "dekketykkelse":
            thick = prop_val(e)
    g = o.get("geometri") or (o.get("lokasjon") or {}).get("geometri")
    parts = parse_wkt(g.get("wkt")) if g else None
    if not parts:
        st["nogeom"] += 1
        return None
    mass = "" if mass is None else str(mass)
    if re.match(r"^grus$", mass, re.I):
        st["gravel"] += 1
        return {"id": o["id"], "t": "g", "y": None, "d": None, "m": mass, "l": lag, "k": thick, "p": parts}
    if NOT_ASPHALT.match(mass):
        st["other"] += 1
        return None
    try:
        year = int(str(date)[:4])
    except (TypeError, ValueError):
        year = None
    if year is None or year < 1900 or year > YEAR:
        st["nodate"] += 1
        return None
    if too_old(date, year):
        st["tooold"] += 1
        return None
    st["asphalt"] += 1
    return {"id": o["id"], "t": "a", "y": year, "d": date, "m": mass, "l": lag, "k": thick, "p": parts}


def cells():
    w, s, e, n = AREA
    dx, dy = (e - w) / COLS, (n - s) / ROWS
    return [(w + c * dx, s + r * dy, w + (c + 1) * dx, s + (r + 1) * dy) for r in range(ROWS) for c in range(COLS)]


def fetch_type(typeid, handle, label):
    """Henter alle objekter av en type for hele området og kaller handle(objekt) én gang per unike objekt."""
    seen, preferred = set(), PREFERRED.get(typeid, 0)
    cs = cells()
    for k, b in enumerate(cs, 1):
        print("%s: rute %d av %d ..." % (label, k, len(cs)), flush=True)
        data, preferred = first_page(b, preferred, typeid)
        while True:
            objs = data.get("objekter") or []
            for o in objs:
                if not o or o.get("id") in seen:
                    continue
                seen.add(o["id"])
                handle(o)
            nxt = ((data.get("metadata") or {}).get("neste") or {}).get("href")
            if not objs or not nxt:
                break
            data = get_json(nxt)
    PREFERRED[typeid] = preferred
    return len(seen)


PREFERRED = {}


# ---------- Veier der sykling er forbudt ----------
CELL = 0.002  # grader, ca. 220 m


def build_index(parts_list):
    idx = {}
    for parts in parts_list:
        for flat in parts:
            for i in range(0, len(flat) - 2, 2):
                a_lat, a_lon, b_lat, b_lon = flat[i], flat[i + 1], flat[i + 2], flat[i + 3]
                x0, x1 = sorted((int(a_lat // CELL), int(b_lat // CELL)))
                y0, y1 = sorted((int(a_lon // CELL), int(b_lon // CELL)))
                for x in range(x0, x1 + 1):
                    for y in range(y0, y1 + 1):
                        idx.setdefault((x, y), []).append((a_lat, a_lon, b_lat, b_lon))
    return idx


def point_near(lat, lon, idx, tol=TOL_M):
    import math
    k = math.cos(math.radians(lat)) * 111320.0
    cx, cy = int(lat // CELL), int(lon // CELL)
    for x in (cx - 1, cx, cx + 1):
        for y in (cy - 1, cy, cy + 1):
            for a_lat, a_lon, b_lat, b_lon in idx.get((x, y), ()):
                ax, ay = (a_lon - lon) * k, (a_lat - lat) * 110574.0
                bx, by = (b_lon - lon) * k, (b_lat - lat) * 110574.0
                dx, dy = bx - ax, by - ay
                l2 = dx * dx + dy * dy
                t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
                px, py = ax + t * dx, ay + t * dy
                if px * px + py * py <= tol * tol:
                    return True
    return False


def on_banned(parts, idx):
    pts = [(flat[i], flat[i + 1]) for flat in parts for i in range(0, len(flat), 2)]
    if not pts:
        return False
    hit = sum(1 for lat, lon in pts if point_near(lat, lon, idx))
    return hit / len(pts) >= SHARE_ON


BAN_RE = re.compile(r"^\s*forbudt for (gående og )?syklende\s*$", re.I)


def fetch_bans():
    """Motorveg/motortrafikkveg (595) og skiltet forbud mot sykling (856)."""
    bans, st = {}, {"motorveg": 0, "forbud": 0}

    def motorveg(o):
        g = o.get("geometri") or (o.get("lokasjon") or {}).get("geometri")
        parts = parse_wkt(g.get("wkt")) if g else None
        if not parts:
            return
        typ = ""
        for e in o.get("egenskaper") or []:
            if str(e.get("navn") or "").lower() == "motorvegtype":
                typ = str(prop_val(e) or "")
        bans[("m", o["id"])] = {"id": o["id"], "k": "m", "m": typ, "p": parts}
        st["motorveg"] += 1

    def forbud(o):
        banned, label = False, "Forbudt for syklende"
        for e in o.get("egenskaper") or []:
            v = prop_val(e)
            if e.get("enum_id") == 18303 or (isinstance(v, str) and BAN_RE.match(v)):
                banned = True
                if isinstance(v, str) and v.strip():
                    label = v.strip()
        if not banned:
            return
        g = o.get("geometri") or (o.get("lokasjon") or {}).get("geometri")
        parts = parse_wkt(g.get("wkt")) if g else None
        if not parts:
            return
        bans[("f", o["id"])] = {"id": o["id"], "k": "f", "m": label, "p": parts}
        st["forbud"] += 1

    n595 = fetch_type(595, motorveg, "Motorveg")
    if st["motorveg"] == 0:
        print("FEIL: fant ingen motorveg i området (%d objekter hentet). Avbryter, så asfalt ikke vises på E6 og lignende." % n595, file=sys.stderr)
        sys.exit(1)
    try:
        fetch_type(856, forbud, "Trafikkreguleringer")
    except RuntimeError as e:
        print("ADVARSEL: kunne ikke hente trafikkreguleringer (%s). Fortsetter bare med motorveg." % e, file=sys.stderr)
    return list(bans.values()), st


def main():
    st = {"got": 0, "asphalt": 0, "gravel": 0, "nodate": 0, "tooold": 0, "nocycle": 0, "other": 0, "nogeom": 0}
    found = {}

    def vegdekke(o):
        st["got"] += 1
        c = parse_object(o, st)
        if c:
            found[o["id"]] = c

    fetch_type(241, vegdekke, "Vegdekke")
    preferred = PREFERRED.get(241, 0)

    bans, bst = fetch_bans()
    idx = build_index([x["p"] for x in bans])
    # Fjern asfaltdata på veier der sykling er forbudt (grus beholdes ikke heller: ingen mening å vise «mangler asfalt» der)
    for i in list(found):
        if on_banned(found[i]["p"], idx):
            del found[i]
            st["nocycle"] += 1
    items = [found[i] for i in sorted(found)]
    print("Hentet %(got)d vegdekker: %(asphalt)d asfalt, %(gravel)d grus (før fjerning av forbudte veier). Hoppet over: "
          "%(tooold)d eldre enn 10 år, %(nodate)d uten dato, %(other)d andre dekketyper, %(nogeom)d uten geometri." % st)
    print("Fjernet %d strekninger på veier der sykling er forbudt. Forbudte veier: %d motorveg, %d skiltet forbud." % (st["nocycle"], bst["motorveg"], bst["forbud"]))
    print("Brukte forespørselsvariant", preferred)

    # Sikring: ikke skriv over en god fil med en mistenkelig liten
    old = 0
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old_items = json.load(f).get("items", [])
            old = sum(1 for i in old_items if i.get("t") == "g" or not too_old(i.get("d"), i.get("y")))
        except (ValueError, OSError):
            old = 0
    if len(items) < 50 or (old and len(items) < old * 0.5):
        print("FEIL: bare %d strekninger (forrige fil hadde %d). Skriver ikke over %s." % (len(items), old, OUT), file=sys.stderr)
        sys.exit(1)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    rec = {"fetched": now, "area": list(AREA), "stats": st, "items": items}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
    print("Skrev %s (%.1f MB, %d strekninger)" % (OUT, os.path.getsize(OUT) / 1048576, len(items)))
    with open(OUT_BAN, "w", encoding="utf-8") as f:
        json.dump({"fetched": now, "area": list(AREA), "stats": bst, "items": bans}, f, ensure_ascii=False, separators=(",", ":"))
    print("Skrev %s (%.1f MB, %d strekninger)" % (OUT_BAN, os.path.getsize(OUT_BAN) / 1048576, len(bans)))


if __name__ == "__main__":
    main()
