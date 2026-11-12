"""Prague scheme probe: how big is a simplified OSM scheme of Prague for the watch?

Downloads river, roads and rail lines inside Prague from Overpass, simplifies
them (Douglas-Peucker) at a few tolerances, splits them into 2 km squares and
reports points, bytes and segments per layer. Draws preview PNGs at the watch's
two map scales. Output: probe/ (report.md, *.png).

Data (c) OpenStreetMap contributors, ODbL.
"""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw

OUT = "probe"
BBOX = (49.94, 14.22, 50.18, 14.71)  # S, W, N, E around Prague
TILE_M = 2000
TOLERANCES = [5, 10, 20]
LAT0 = 50.08
M_LAT = 111_320.0
M_LON = 111_320.0 * math.cos(math.radians(LAT0))

# layer -> (Overpass filter, colour, pen width)
LAYERS = {
    "river": ('way["waterway"="river"]', (40, 110, 220), 3),
    "motorway": ('way["highway"~"^(motorway|trunk)$"]', (230, 230, 230), 2),
    "primary": ('way["highway"="primary"]', (200, 200, 200), 2),
    "secondary": ('way["highway"="secondary"]', (150, 150, 150), 1),
    "tertiary": ('way["highway"="tertiary"]', (100, 100, 100), 1),
    "rail": ('way["railway"="rail"]["usage"~"^(main|branch)$"]', (120, 80, 160), 1),
    "metro": ('way["railway"="subway"]', (200, 60, 60), 1),
    "tram": ('way["railway"="tram"]', (150, 100, 60), 1),
}


def overpass(filter_):
    s, w, n, e = BBOX
    q = f'[out:json][timeout:180][bbox:{s},{w},{n},{e}];({filter_};);out geom;'
    data = urllib.parse.urlencode({"data": q}).encode()
    servers = ["https://overpass-api.de/api/interpreter",
               "https://overpass.kumi.systems/api/interpreter",
               "https://overpass.private.coffee/api/interpreter"]
    for attempt in range(6):
        url = servers[attempt % len(servers)]
        req = urllib.request.Request(url, data=data,
                                     headers={"User-Agent": "pid-data map probe (github.com/laszlovladi/pid-data)"})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r)
        except Exception as ex:  # noqa: BLE001
            print("overpass retry", attempt, url, ex, file=sys.stderr)
            time.sleep(20)
    raise SystemExit("Overpass failed")


def xy(lat, lon):
    return ((lon - BBOX[1]) * M_LON, (lat - BBOX[0]) * M_LAT)


def dp(pts, tol):
    """Douglas-Peucker on metric points, returns kept indexes."""
    if len(pts) < 3:
        return list(range(len(pts)))
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
                d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [i for i, k in enumerate(keep) if k]


def encode(line):
    """[lat0, lon0, dlat, dlon, ...] in 1/100000 degree, like the route shapes."""
    out, pl, po = [], 0, 0
    for lat, lon in line:
        a, b = round(lat * 1e5), round(lon * 1e5)
        out += [a - pl, b - po]
        pl, po = a, b
    return out


def tiles_of(line):
    t = set()
    for lat, lon in line:
        x, y = xy(lat, lon)
        t.add((int(x // TILE_M), int(y // TILE_M)))
    return t


def main():
    os.makedirs(OUT, exist_ok=True)
    raw = {}
    for name, (flt, _, _) in LAYERS.items():
        d = overpass(flt)
        raw[name] = [[(p["lat"], p["lon"]) for p in el["geometry"]]
                     for el in d["elements"] if el.get("type") == "way" and "geometry" in el]
        print(name, len(raw[name]), "ways", sum(len(l) for l in raw[name]), "points")
        time.sleep(5)

    rows = []
    simplified = {}
    for tol in TOLERANCES:
        for name, lines in raw.items():
            pts = 0
            size = 0
            tiles = {}
            simp = []
            for line in lines:
                m = [xy(*p) for p in line]
                s = [line[i] for i in dp(m, tol)]
                simp.append(s)
                pts += len(s)
                size += len(json.dumps(encode(s), separators=(",", ":")))
                for t in tiles_of(s):
                    tiles[t] = tiles.get(t, 0) + len(s) - 1
            simplified[(tol, name)] = simp
            rows.append((tol, name, len(lines), sum(len(l) for l in lines), pts, size,
                         len(tiles), max(tiles.values()) if tiles else 0))

    with open(f"{OUT}/report.md", "w") as f:
        f.write("# Prague scheme probe\n\n")
        f.write(f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. Data (c) OpenStreetMap contributors.\n\n")
        f.write("Size = JSON of integer deltas (1/100000 degree), one array per way, before merging ways.\n")
        f.write(f"Squares of {TILE_M} m; max segments = most line segments of that layer in one square.\n\n")
        f.write("| Tolerance m | Layer | Ways | Raw points | Points | Size kB | Squares | Max segments / square |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5] / 1024:.1f} | {r[6]} | {r[7]} |\n")
        for tol in TOLERANCES:
            tot = sum(r[5] for r in rows if r[0] == tol)
            core = sum(r[5] for r in rows if r[0] == tol and r[1] in ("river", "motorway", "primary", "secondary"))
            f.write(f"\nTolerance {tol} m: all layers {tot / 1024:.0f} kB; river + motorway/trunk + primary + secondary {core / 1024:.0f} kB.\n")

    # Previews: whole city (~40 m/px) and close-ups 1.5 km across (as on the watch).
    views = {
        "whole": (50.06, 14.45, 25000),
        "florenc": (50.0905, 14.4395, 1500),
        "andel": (50.0710, 14.4040, 1500),
        "barrandov": (50.0320, 14.3800, 1500),
    }
    for tol in (10, 20):
        for vname, (clat, clon, width_m) in views.items():
            W, H = 320, 280
            k = W / width_m
            img = Image.new("RGB", (W, H))
            d = ImageDraw.Draw(img)
            cx, cy = xy(clat, clon)
            for name, (_, col, pen) in reversed(list(LAYERS.items())):
                for line in simplified[(tol, name)]:
                    pts = []
                    for lat, lon in line:
                        x, y = xy(lat, lon)
                        pts.append((W / 2 + (x - cx) * k, H / 2 - (y - cy) * k))
                    if len(pts) >= 2:
                        d.line(pts, fill=col, width=pen)
            img.save(f"{OUT}/{vname}-{tol}m.png")


if __name__ == "__main__":
    main()
