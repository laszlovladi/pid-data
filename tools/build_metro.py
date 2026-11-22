"""Metro travel times between all Prague metro stations, for "Go to…" trips with
changes through the metro in the PID watch app (PID docs/specs/direct-trip.md,
phase 3a).

Runs in the nightly job after generate.ps1 and reuses its GTFS files
(<temp>/pid-gtfs). Writes docs/metro.json:

  {"date": "...",
   "n": [node, ...],          GTFS node of each station (U1040Z101P -> 1040)
   "m": [name, ...],          station names
   "l": ["A", "AB", ...],     metro lines at each station
   "s": "..."}                n*n minutes as characters chr(48 + minutes) ("/" = none,
                              i == j), from station i to station j: ride, plus at a
                              metro-to-metro change the transfer walk and half the
                              second line's interval

Minutes are typical daytime values (medians); the watch adds the wait for the
first metro itself.
"""
import csv
import datetime
import json
import os
import re
import statistics
import tempfile
from collections import defaultdict

SRC = os.path.join(tempfile.gettempdir(), "pid-gtfs")
NODE_RE = re.compile(r"^U(\d+)Z")
DEFAULT_TRANSFER_MIN = 4


def rows(name):
    path = os.path.join(SRC, name)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def secs(t):
    h, m, s = (t or "0:0:0").strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def simplify(line, tol):
    """Douglas-Peucker on (lat, lon) in 1/100000 degree, tolerance in metres."""
    if len(line) < 3:
        return line
    my, mx = 1.1132, 1.1132 * 0.642
    pts = [(p[1] * mx, p[0] * my) for p in line]
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
            t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
            d = ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [line[i] for i, k in enumerate(keep) if k]


def write_shapes(seq, trips, trip_shape, nodes, names, lines_at, pos):
    """docs/metro_shapes.json: {"l": {"A": [lat0, lon0, dlat, dlon, ...], ...},
    "m": [station names], "p": [lat, lon, ...]} in 1/100000 degree (the trip map)."""
    # Per line the shape of its longest trip (the whole line).
    best = {}
    for t, st in seq.items():
        line = trips[t][0]
        sh = trip_shape.get(t, "")
        if sh and len(st) > best.get(line, ("", 0))[1]:
            best[line] = (sh, len(st))
    need = {sh for sh, _ in best.values()}
    pts = defaultdict(list)
    for r in rows("shapes.txt"):
        if r["shape_id"] in need:
            pts[r["shape_id"]].append((int(r["shape_pt_sequence"]), round(float(r["shape_pt_lat"]) * 1e5),
                                       round(float(r["shape_pt_lon"]) * 1e5)))
    lines = {}
    for line, (sh, _) in sorted(best.items()):
        p = [(a, b) for _, a, b in sorted(pts[sh])]
        p = simplify(p, 20)
        enc = [p[0][0], p[0][1]]
        for i in range(1, len(p)):
            enc += [p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1]]
        lines[line] = enc
    flat = []
    for x in nodes:
        ps = pos.get(x) or [(0.0, 0.0)]
        flat += [round(sum(a for a, _ in ps) / len(ps) * 1e5), round(sum(b for _, b in ps) / len(ps) * 1e5)]
    out = {"l": lines, "m": [names[x] for x in nodes], "p": flat}
    s = json.dumps(out, separators=(",", ":"))
    with open("docs/metro_shapes.json", "w") as f:
        f.write(s)
    print(f"Metro shapes: {', '.join(f'{k} {len(v) // 2} points' for k, v in lines.items())}, {len(s)} bytes")


def main():
    metro_routes = {r["route_id"]: r["route_short_name"].strip() for r in rows("routes.txt")
                    if (r.get("route_type") or "").strip() == "1"}
    trips = {t["trip_id"]: (metro_routes[t["route_id"]], t.get("service_id", ""), t.get("direction_id", ""))
             for t in rows("trips.txt") if t["route_id"] in metro_routes}
    trip_shape = {t["trip_id"]: (t.get("shape_id") or "").strip()
                  for t in rows("trips.txt") if t["route_id"] in metro_routes}
    print(f"metro lines {sorted(set(metro_routes.values()))}, trips {len(trips)}")

    stops = {}
    pos = defaultdict(list)  # node -> [(lat, lon)] of its platforms
    for s in rows("stops.txt"):
        m = NODE_RE.match(s["stop_id"])
        if m:
            stops[s["stop_id"]] = (int(m.group(1)), (s.get("stop_name") or "").strip())
            try:
                pos[int(m.group(1))].append((float(s["stop_lat"]), float(s["stop_lon"])))
            except (KeyError, ValueError):
                pass

    seq = defaultdict(list)  # trip -> [(seq, node, arr, dep)]
    for r in rows("stop_times.txt"):
        t = r["trip_id"]
        if t in trips and r["stop_id"] in stops:
            seq[t].append((int(r["stop_sequence"]), stops[r["stop_id"]][0],
                           secs(r["arrival_time"]), secs(r["departure_time"])))

    names, lines_at = {}, defaultdict(set)
    ride = defaultdict(list)  # (line, from node, to node) -> minutes
    deps = defaultdict(list)  # (line, direction, node, service) -> departure seconds (daytime)
    for t, st in seq.items():
        st.sort()
        line, service, direction = trips[t]
        if not (6 * 3600 <= st[0][3] <= 21 * 3600):
            continue
        for i in range(len(st)):
            node = st[i][1]
            lines_at[node].add(line)
            if 7 * 3600 <= st[i][3] <= 19 * 3600:
                # Per service day: the feed holds several days (calendar may be empty).
                deps[(line, direction, node, service)].append(st[i][3])
            for j in range(i + 1, len(st)):
                ride[(line, node, st[j][1])].append((st[j][2] - st[i][3]) / 60.0)
    for sid, (node, name) in stops.items():
        if node in lines_at:
            names.setdefault(node, name)

    ride_min = {k: statistics.median(v) for k, v in ride.items()}
    # Interval per line: the busiest station's daytime departures per direction.
    interval = {}
    for line in sorted(set(metro_routes.values())):
        best = []
        for (l, d, node, service), ds in deps.items():
            if l == line and len(ds) > len(best):
                best = sorted(ds)
        gaps = [b - a for a, b in zip(best, best[1:]) if b > a]
        interval[line] = statistics.median(gaps) / 60.0 if gaps else 6.0
    print("intervals (min):", {k: round(v, 1) for k, v in interval.items()})

    # Transfer walks between metro platforms of different lines.
    transfer = {}
    for r in rows("transfers.txt"):
        a, b = stops.get(r.get("from_stop_id")), stops.get(r.get("to_stop_id"))
        mt = (r.get("min_transfer_time") or "").strip()
        if a and b and a[0] == b[0] and a[0] in lines_at and len(lines_at[a[0]]) > 1 and mt:
            transfer[a[0]] = max(transfer.get(a[0], 0), int(mt) / 60.0)
    print("transfers (min):", {names[k]: round(v, 1) for k, v in transfer.items()})

    nodes = sorted(lines_at, key=lambda n: (sorted(lines_at[n]), names.get(n, "")))
    n = len(nodes)
    table = []
    for a in nodes:
        for b in nodes:
            if a == b:
                table.append(-1)
                continue
            best = None
            for la in lines_at[a]:
                if la in lines_at[b] and (la, a, b) in ride_min:
                    best = min(best or 1e9, ride_min[(la, a, b)])
                for x in nodes:
                    for lb in lines_at[b]:
                        if lb == la or la not in lines_at[x] or lb not in lines_at[x]:
                            continue
                        if (la, a, x) in ride_min and (lb, x, b) in ride_min:
                            v = (ride_min[(la, a, x)] + transfer.get(x, DEFAULT_TRANSFER_MIN)
                                 + interval[lb] / 2 + ride_min[(lb, x, b)])
                            best = min(best or 1e9, v)
            table.append(round(best) if best is not None else -1)

    write_shapes(seq, trips, trip_shape, nodes, names, lines_at, pos)

    # Minutes packed as one character each (chr(48 + minutes), "/" = none): one short
    # string instead of n*n numbers, which ran out of memory on the watch.
    packed = "".join("/" if v < 0 else chr(48 + min(v, 120)) for v in table)
    out = {"date": datetime.date.today().isoformat(), "n": nodes, "m": [names[x] for x in nodes],
           "l": ["".join(sorted(lines_at[x])) for x in nodes], "s": packed,
           "w": {k: round(v / 2) for k, v in interval.items()}}
    s = json.dumps(out, separators=(",", ":"))
    with open("docs/metro.json", "w") as f:
        f.write(s)
    missing = sum(1 for k, v in enumerate(table) if v < 0) - n
    print(f"Metro: {n} stations, {len(s)} bytes, {missing} pairs without a time")
    for a, b in (("Ládví", "Anděl"), ("Dejvická", "Florenc"), ("Háje", "Hradčanská"), ("Černý Most", "Zličín")):
        try:
            ia = [names[x] for x in nodes].index(a)
            ib = [names[x] for x in nodes].index(b)
            print(f"Check: {a} -> {b}: {table[ia * n + ib]} min")
        except ValueError:
            print(f"Check: {a} or {b} not found")


if __name__ == "__main__":
    main()
