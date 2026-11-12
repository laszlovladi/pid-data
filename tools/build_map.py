"""Prague scheme for the PID watch app's route map (PID docs/specs/prague-map.md).

Downloads river, roads, rail and tram lines inside Prague from Overpass, joins
connected ways of the same class into long lines, simplifies them
(Douglas-Peucker) and writes:

  docs/map/s_<x>_<y>.json  close-up set, all classes, 2 km squares, 10 m
  docs/map/overview.json   river + motorway/trunk + primary, 40 m, all Prague
  docs/map/meta.json       date, grid, file list and sizes

Line format, integers in 1/100000 degree (as the route shapes):
  {"l":[[class, lat0, lon0, dlat1, dlon1, ...], ...]}
  class: 0 river, 1 motorway/trunk, 2 primary, 3 secondary, 4 tertiary, 5 tram, 6 rail

Data (c) OpenStreetMap contributors, ODbL.
"""
import glob
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

OUT = "docs/map"
PRAGUE_AREA = 3600435514  # relation 435514 "Praha" + 3600000000
# Grid: origin and square size in 1/100000 degree (~2 km x 2 km at 50.08 N).
ORIGIN_LAT = 4994000
ORIGIN_LON = 1422000
SQ_LAT = 1797
SQ_LON = 2804
CLOSE_TOL = 10
OVERVIEW_TOL = 40
OVERVIEW_CLASSES = (0, 1, 2)
M_LAT = 1.1132  # metres per 1/100000 degree of latitude
M_LON = 1.1132 * math.cos(math.radians(50.08))

CLASSES = [
    'way["waterway"="river"]',
    'way["highway"~"^(motorway|trunk|motorway_link|trunk_link)$"]',
    'way["highway"~"^(primary|primary_link)$"]',
    'way["highway"~"^(secondary|secondary_link)$"]',
    'way["highway"="tertiary"]',
    'way["railway"="tram"]',
    'way["railway"="rail"]["usage"~"^(main|branch)$"]',
]
SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def overpass(flt):
    q = f"[out:json][timeout:180];area({PRAGUE_AREA})->.p;({flt}(area.p););out geom;"
    data = urllib.parse.urlencode({"data": q}).encode()
    for attempt in range(9):
        url = SERVERS[attempt % len(SERVERS)]
        req = urllib.request.Request(url, data=data,
                                     headers={"User-Agent": "pid-data map (github.com/laszlovladi/pid-data)"})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                d = json.load(r)
            # A busy server can answer 200 with no elements and a "remark".
            if d.get("elements"):
                return d
            print("overpass empty", attempt, url, d.get("remark", ""), file=sys.stderr)
            time.sleep(20)
        except Exception as ex:  # noqa: BLE001
            print("overpass retry", attempt, url, ex, file=sys.stderr)
            time.sleep(20)
    raise SystemExit("Overpass failed")


def join(ways):
    """Joins ways that meet end to end where exactly two ways of the class meet."""
    ends = {}
    for i, w in enumerate(ways):
        for p in (w[0], w[-1]):
            ends.setdefault(p, []).append(i)
    used = [False] * len(ways)

    def other(node, cur):
        lst = ends.get(node, [])
        if len(lst) != 2:
            return None
        j = lst[0] if lst[1] == cur else lst[1]
        return None if used[j] or j == cur else j

    out = []
    for i in range(len(ways)):
        if used[i]:
            continue
        used[i] = True
        line = list(ways[i])
        for forward in (True, False):
            cur = i
            while True:
                node = line[-1] if forward else line[0]
                j = other(node, cur)
                if j is None:
                    break
                used[j] = True
                w = list(ways[j])
                if forward:
                    if w[0] != node:
                        w.reverse()
                    line += w[1:]
                else:
                    if w[-1] != node:
                        w.reverse()
                    line = w[:-1] + line
                cur = j
        out.append(line)
    return out


def simplify(line, tol):
    if len(line) < 3:
        return line
    pts = [(p[1] * M_LON, p[0] * M_LAT) for p in line]
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
    return [line[i] for i, k in enumerate(keep) if k]


def encode(cls, line):
    out = [cls, line[0][0], line[0][1]]
    for i in range(1, len(line)):
        out += [line[i][0] - line[i - 1][0], line[i][1] - line[i - 1][1]]
    return out


def square(p):
    return ((p[1] - ORIGIN_LON) // SQ_LON, (p[0] - ORIGIN_LAT) // SQ_LAT)


def squares_of_segment(a, b):
    x0, y0 = square(a)
    x1, y1 = square(b)
    return {(x, y) for x in range(min(x0, x1), max(x0, x1) + 1)
            for y in range(min(y0, y1), max(y0, y1) + 1)}


def write(path, lines):
    s = json.dumps({"l": lines}, separators=(",", ":"))
    with open(path, "w") as f:
        f.write(s)
    return len(s)


def main():
    raw = []
    for cls, flt in enumerate(CLASSES):
        d = overpass(flt)
        ways = [[(round(p["lat"] * 1e5), round(p["lon"] * 1e5)) for p in el["geometry"]]
                for el in d["elements"] if el.get("type") == "way" and len(el.get("geometry", [])) >= 2]
        joined = join(ways)
        print(f"class {cls}: {len(ways)} ways -> {len(joined)} lines, {sum(len(w) for w in ways)} points")
        raw.append(joined)
        time.sleep(5)
    if sum(len(r) for r in raw) < 1000:
        raise SystemExit("Too little data: Prague area query failed?")

    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(f"{OUT}/*.json"):
        os.remove(f)

    # Close-up squares.
    sq = {}
    seg_count = {}
    for cls, lines in enumerate(raw):
        for line in lines:
            s = simplify(line, CLOSE_TOL)
            runs = {}  # square -> (last segment index, current polyline)
            for i in range(1, len(s)):
                for key in squares_of_segment(s[i - 1], s[i]):
                    last, poly = runs.get(key, (-2, None))
                    if last == i - 1 and poly is not None:
                        poly.append(s[i])
                    else:
                        poly = [s[i - 1], s[i]]
                        sq.setdefault(key, []).append((cls, poly))
                    runs[key] = (i, poly)
                    seg_count[key] = seg_count.get(key, 0) + 1
    files = []
    for (x, y), polys in sorted(sq.items()):
        size = write(f"{OUT}/s_{x}_{y}.json", [encode(c, p) for c, p in polys])
        files.append([x, y, size])

    # Overview.
    ov = []
    for cls in OVERVIEW_CLASSES:
        for line in raw[cls]:
            s = simplify(line, OVERVIEW_TOL)
            if len(s) >= 2:
                ov.append(encode(cls, s))
    ov_size = write(f"{OUT}/overview.json", ov)

    total = sum(f[2] for f in files)
    meta = {
        "generated": time.strftime("%Y-%m-%d", time.gmtime()),
        "origin": [ORIGIN_LAT, ORIGIN_LON],
        "square": [SQ_LAT, SQ_LON],
        "squares": files,
        "overview": ov_size,
    }
    with open(f"{OUT}/meta.json", "w") as f:
        json.dump(meta, f, separators=(",", ":"))
    big = max(files, key=lambda f: f[2])
    print(f"Squares: {len(files)} files, {total / 1024:.0f} kB, largest s_{big[0]}_{big[1]} {big[2] / 1024:.1f} kB, "
          f"max segments in a square {max(seg_count.values())}")
    print(f"Overview: {ov_size / 1024:.1f} kB, {len(ov)} lines")


if __name__ == "__main__":
    main()
