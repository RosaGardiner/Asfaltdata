#!/usr/bin/env python3
"""Lager planlagt.json: fylkeskommunens asfaltplan tegnet på kartet.

Leser planlagt_2026.json (veinummer og lengder fra Akershus fylkeskommune) og henter
geometrien til hver fylkesvei innenfor Oslo og Akershus fra NVDB (vegnett API v4).

Listen oppgir ikke start og slutt, så HELE veien tegnes. I appen kan du trimme hver
strekning til der arbeidet faktisk er, og lagre det som planlagt-trim.json.

Kjøres av GitHub Actions sammen med de andre dataene, eller lokalt:
    python3 hent_planlagte_veier.py
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

VEGNETT = os.environ.get("NVDB_VEGNETT", "https://nvdbapiles.atlas.vegvesen.no/vegnett/api/v4")
INN = os.environ.get("PLANLAGT_INN", "planlagt_2026.json")
UT = os.environ.get("PLANLAGT_UT", "planlagt.json")
AREA = (10.05, 59.55, 11.95, 60.55)
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
                raise err
            last = err
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(2 * (i + 1))
    raise last


def parse_wkt(wkt):
    """WKT til liste med linjestykker [lat, lon, lat, lon, ...]."""
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
                return None
            lat, lon = (a, b) if 50 <= a <= 72 and 3 <= b <= 32 else (b, a)
            out += [round(lat, 5), round(lon, 5)]
        if len(out) >= 4:
            parts.append(out)
    return parts or None


def variants(veg):
    """Vegnett-tjenesten tar ikke parameteren «inkluder»; geometrien følger med av seg selv."""
    bb = "kartutsnitt=" + ",".join("%.5f" % v for v in AREA) + "&srid=4326"
    num = veg[2:] if veg[:2].upper() == "FV" else veg
    base = VEGNETT + "/veglenkesekvenser/segmentert?"
    return [
        base + bb + "&vegsystemreferanse=FV" + num + "&antall=1000",
        base + bb + "&vegsystemreferanse=Fv" + num + "&antall=1000",
        base + bb + "&vegsystemreferanse=fv" + num + "&antall=1000",
        base + bb + "&vegsystemreferanse=FV" + num,
        base + bb + "&vegsystemreferanse=FV" + num + "&geometri=true&antall=1000",
    ]


def obj_parts(o):
    """Henter geometri fra et vegnett-objekt, uansett om den ligger på objektet eller på hver lenke."""
    out = []
    g = o.get("geometri")
    if isinstance(g, dict):
        p = parse_wkt(g.get("wkt"))
        if p:
            out.append(p)
    for key in ("veglenker", "superstedfesting", "segmenter"):
        sub = o.get(key)
        if isinstance(sub, list):
            for x in sub:
                if isinstance(x, dict):
                    out.extend(obj_parts(x))
    return out


PREFERRED = [0]


def fetch_road(veg):
    """Alle linjestykker for én fylkesvei innenfor området."""
    vs = variants(veg)
    order = [PREFERRED[0]] + [i for i in range(len(vs)) if i != PREFERRED[0]]
    last = None
    for i in order:
        try:
            data = get_json(vs[i])
        except RuntimeError as e:
            last = e
            if getattr(e, "status", None) == 400:
                continue
            raise
        PREFERRED[0] = i
        parts, seen, sample = [], 0, None
        while True:
            objs = data.get("objekter") or data.get("vegnett") or data.get("veglenkesekvenser") or []
            for o in objs:
                seen += 1
                if sample is None:
                    sample = o
                for p in obj_parts(o):
                    parts.extend(p)
            nxt = ((data.get("metadata") or {}).get("neste") or {}).get("href")
            if not objs or not nxt:
                break
            data = get_json(nxt)
        if not parts and sample is not None:
            print("  svaret hadde ingen geometri. Felter i første objekt: %s" % ", ".join(sorted(sample.keys())), file=sys.stderr)
        return parts, seen
    raise last


def simplify(parts, tol=0.00002):
    """Fjerner punkter som ligger nesten på linje, så filen ikke blir unødig stor."""
    out = []
    for flat in parts:
        keep = [flat[0], flat[1]]
        for i in range(2, len(flat) - 2, 2):
            ax, ay = keep[-2], keep[-1]
            bx, by = flat[i], flat[i + 1]
            cx, cy = flat[i + 2], flat[i + 3]
            cross = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))
            if cross > tol * tol:
                keep += [bx, by]
        keep += [flat[-2], flat[-1]]
        if len(keep) >= 4:
            out.append(keep)
    return out


def main():
    with open(INN, encoding="utf-8") as f:
        plan = json.load(f)
    roads = sorted({v["veg"] for v in plan["veier"]})
    geo, failed = {}, []
    for k, veg in enumerate(roads, 1):
        print("Henter %s (%d av %d) ..." % (veg, k, len(roads)), flush=True)
        try:
            parts, seen = fetch_road(veg)
        except (RuntimeError, urllib.error.URLError) as e:
            print("  klarte ikke å hente %s: %s" % (veg, e), file=sys.stderr)
            failed.append(veg)
            continue
        if not parts:
            print("  fant ingen geometri for %s (%d segmenter i svaret)" % (veg, seen), file=sys.stderr)
            failed.append(veg)
            continue
        geo[veg] = simplify(parts)
        print("  %d linjestykker" % len(geo[veg]))

    items = []
    for v in plan["veier"]:
        p = geo.get(v["veg"])
        if not p:
            continue
        items.append({
            "id": v["id"], "veg": v["veg"], "omrade": v["omrade"],
            "type": v["type"], "lengde": v["lengde"], "p": p,
        })

    if len(items) < len(plan["veier"]) * 0.5:
        print("FEIL: fikk geometri for bare %d av %d oppføringer. Skriver ikke over %s."
              % (len(items), len(plan["veier"]), UT), file=sys.stderr)
        sys.exit(1)

    rec = {
        "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "aar": plan.get("aar"),
        "kilde": plan.get("kilde"),
        "url": plan.get("url"),
        "mangler": failed,
        "items": items,
    }
    with open(UT, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
    print("Skrev %s (%.1f MB, %d oppføringer, %d veier%s)"
          % (UT, os.path.getsize(UT) / 1048576, len(items), len(geo),
             ", manglet: " + ", ".join(failed) if failed else ""))


if __name__ == "__main__":
    main()
