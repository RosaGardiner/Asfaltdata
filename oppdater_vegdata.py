#!/usr/bin/env python3
"""Henter vegdekke (NVDB objekttype 241) for Oslo og Akershus og skriver vegdekke.json.

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

BASE = os.environ.get(
    "NVDB_URL",
    "https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter/241",
)
AREA = (10.05, 59.55, 11.95, 60.55)  # vest, sør, øst, nord (samme område som kartet)
COLS, ROWS = 4, 3                    # området deles i ruter for å holde hvert svar lite
OUT = os.environ.get("VEGDATA_UT", "vegdekke.json")
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


def variants(b):
    bb = "?kartutsnitt=" + ",".join("%.5f" % v for v in b)
    return [
        BASE + bb + "&srid=4326&inkluder=egenskaper,geometri&antall=500",
        BASE + bb + "&srid=4326&inkluder=egenskaper,geometri&antall=100",
        BASE + bb + "&srid=4326&inkluder=egenskaper&inkluder=geometri&antall=100",
        BASE + bb + "&srid=wgs84&inkluder=egenskaper,geometri&antall=100",
        BASE + bb + "&srid=4326&inkluder=alle&antall=100",
    ]


def first_page(b, preferred):
    vs = variants(b)
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


def main():
    w, s, e, n = AREA
    dx, dy = (e - w) / COLS, (n - s) / ROWS
    cells = [(w + c * dx, s + r * dy, w + (c + 1) * dx, s + (r + 1) * dy) for r in range(ROWS) for c in range(COLS)]
    st = {"got": 0, "asphalt": 0, "gravel": 0, "nodate": 0, "tooold": 0, "other": 0, "nogeom": 0}
    seen, found, preferred = set(), {}, 0
    for k, b in enumerate(cells, 1):
        print("Rute %d av %d ..." % (k, len(cells)), flush=True)
        data, preferred = first_page(b, preferred)
        while True:
            objs = data.get("objekter") or []
            for o in objs:
                if not o or o.get("id") in seen:
                    continue
                seen.add(o["id"])
                st["got"] += 1
                c = parse_object(o, st)
                if c:
                    found[o["id"]] = c
            nxt = ((data.get("metadata") or {}).get("neste") or {}).get("href")
            if not objs or not nxt:
                break
            data = get_json(nxt)
    items = [found[i] for i in sorted(found)]
    print("Hentet %(got)d objekter: %(asphalt)d asfalt, %(gravel)d grus, %(tooold)d eldre enn 10 år (hoppet over), "
          "%(nodate)d uten dato, %(other)d andre dekketyper, %(nogeom)d uten brukbar geometri." % st)
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

    rec = {
        "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "area": list(AREA),
        "stats": st,
        "items": items,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
    print("Skrev %s (%.1f MB, %d strekninger)" % (OUT, os.path.getsize(OUT) / 1048576, len(items)))


if __name__ == "__main__":
    main()
