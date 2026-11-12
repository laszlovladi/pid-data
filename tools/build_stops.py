"""Line maps of Prague stops for the PID watch app (PID docs/specs/stop-map.md).

Runs in the nightly job after generate.ps1 and reuses its extracted GTFS files
(<temp>/pid-gtfs). For every Prague stop name it writes the lines leaving the
stop, each as the usual street path from the stop onward:

  docs/stops/<key>.json    spider: paths cut 3.5 km from the stop, 15 m
  docs/stops/<key>_w.json  whole: paths to the terminus, 50 m

  {"s":[lat,lon], "l":[[route, headsign, platformId, lat0, lon0, dlat1, dlon1, ...], ...]}

Integers in 1/100000 degree. <key> = FNV-1a 32-bit of the stop name's UTF-8
bytes, 8 hex digits (the watch computes the same).
"""
import csv
import glob
import json
import math
import os
import sys
import tempfile
from collections import Counter, defaultdict

SRC = os.path.join(tempfile.gettempdir(), "pid-gtfs")
OUT = "docs/stops"
SPIDER_M = 3500
SPIDER_TOL = 15
WHOLE_TOL = 50
MAX_BYTES = 12 * 1024
M_LAT = 1.1132
M_LON = 1.1132 * math.cos(math.radians(50.08))


def fnv1a(name):
    h = 0x811C9DC5
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def rows(name):
    with open(os.path.join(SRC, name), encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


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


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * M_LAT, (a[1] - b[1]) * M_LON)


def encode(head, line):
    out = list(head) + [line[0][0], line[0][1]]
    for i in range(1, len(line)):
        out += [line[i][0] - line[i - 1][0], line[i][1] - line[i - 1][1]]
    return out


def write(path, stop, entries):
    s = json.dumps({"s": list(stop), "l": entries}, separators=(",", ":"))
    with open(path, "w") as f:
        f.write(s)
    return len(s)


def main():
    plat = {}  # stop_id -> (name, (lat, lon))
    for r in rows("stops.txt"):
        if (r.get("location_type") or "0").strip() not in ("", "0"):
            continue
        if "P" not in [z.strip() for z in (r.get("zone_id") or "").split(",")]:
            continue
        name = (r.get("stop_name") or "").strip()
        if name:
            plat[r["stop_id"]] = (name, (round(float(r["stop_lat"]) * 1e5), round(float(r["stop_lon"]) * 1e5)))
    route = {r["route_id"]: (r.get("route_short_name") or "").strip() for r in rows("routes.txt")}
    trips = {}
    for t in rows("trips.txt"):
        rn = route.get(t["route_id"], "")
        if rn and t.get("shape_id"):
            trips[t["trip_id"]] = (rn, (t.get("trip_headsign") or "").strip(), t["shape_id"].strip())
    print(f"Prague platforms {len(plat)}, trips {len(trips)}")

    # (platform, route, headsign) -> Counter(shape_id); (platform, shape_id) -> shape_dist_traveled
    use = defaultdict(Counter)
    dist_at = {}
    n = 0
    for r in rows("stop_times.txt"):
        n += 1
        sid = r["stop_id"]
        if sid not in plat:
            continue
        t = trips.get(r["trip_id"])
        if t is None:
            continue
        use[(sid, t[0], t[1])][t[2]] += 1
        d = (r.get("shape_dist_traveled") or "").strip()
        if d and (sid, t[2]) not in dist_at:
            dist_at[(sid, t[2])] = float(d)
    print(f"stop_times rows {n}, platform/line/direction combinations {len(use)}")

    best = {k: c.most_common(1)[0][0] for k, c in use.items()}
    need = set(best.values())
    shapes = defaultdict(list)  # shape_id -> [(seq, lat, lon, dist)]
    for r in rows("shapes.txt"):
        sh = r["shape_id"]
        if sh in need:
            d = (r.get("shape_dist_traveled") or "").strip()
            shapes[sh].append((int(r["shape_pt_sequence"]), round(float(r["shape_pt_lat"]) * 1e5),
                               round(float(r["shape_pt_lon"]) * 1e5), float(d) if d else None))
    for sh in shapes.values():
        sh.sort()

    by_name = defaultdict(list)  # stop name -> [(platform, route, headsign, shape)]
    for (sid, rn, head), sh in best.items():
        by_name[plat[sid][0]].append((sid, rn, head, sh))

    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(f"{OUT}/*.json"):
        os.remove(f)
    keys = {}
    sizes = []
    for name, items in sorted(by_name.items()):
        key = fnv1a(name)
        if key in keys:
            print(f"HASH COLLISION {name} / {keys[key]}", file=sys.stderr)
            continue
        keys[key] = name
        spider, whole = [], []
        lats, lons = [], []
        for sid, rn, head, sh in sorted(items, key=lambda i: (i[1], i[2], i[0])):
            pts = shapes.get(sh)
            if not pts:
                continue
            p0 = plat[sid][1]
            lats.append(p0[0])
            lons.append(p0[1])
            # Start: shape point at the stop's shape_dist_traveled, else the nearest point.
            d0 = dist_at.get((sid, sh))
            if d0 is not None and pts[0][3] is not None:
                start = min(range(len(pts)), key=lambda i: abs(pts[i][3] - d0))
            else:
                start = min(range(len(pts)), key=lambda i: dist_m(pts[i][1:3], p0))
            path = [(p[1], p[2]) for p in pts[start:]]
            if len(path) < 2:
                continue  # terminus: nothing leaves the stop
            cut = [path[0]]
            for p in path[1:]:
                cut.append(p)
                if dist_m(p, path[0]) > SPIDER_M:
                    break
            head_ = [rn, head, sid]
            spider.append(encode(head_, simplify(cut, SPIDER_TOL)))
            whole.append(encode(head_, simplify(path, WHOLE_TOL)))
        if not spider:
            continue
        centre = (round(sum(lats) / len(lats)), round(sum(lons) / len(lons)))
        a = write(f"{OUT}/{key}.json", centre, spider)
        b = write(f"{OUT}/{key}_w.json", centre, whole)
        tol = WHOLE_TOL
        while b > MAX_BYTES and tol < 400:
            tol *= 2
            # Re-simplify the whole paths from their decoded form.
            data = json.load(open(f"{OUT}/{key}_w.json"))["l"]
            redone = []
            for e in data:
                lat, lon = e[3], e[4]
                line = [(lat, lon)]
                for i in range(5, len(e) - 1, 2):
                    lat += e[i]
                    lon += e[i + 1]
                    line.append((lat, lon))
                redone.append(encode(e[:3], simplify(line, tol)))
            b = write(f"{OUT}/{key}_w.json", centre, redone)
        sizes.append((max(a, b), a, b, name))
    sizes.sort(reverse=True)
    print(f"Stops: {len(sizes)}, spider {sum(s[1] for s in sizes) / 1024:.0f} kB, "
          f"whole {sum(s[2] for s in sizes) / 1024:.0f} kB")
    for s in sizes[:5]:
        print(f"  {s[3]}: spider {s[1]} B, whole {s[2]} B")
    over = [s for s in sizes if s[1] > MAX_BYTES]
    if over:
        print(f"WARNING: {len(over)} spider files over {MAX_BYTES} B", file=sys.stderr)
    for name in ("Anděl", "Florenc", "Hlavní nádraží"):
        print(f"Check: {name} -> {fnv1a(name)}")


if __name__ == "__main__":
    main()
