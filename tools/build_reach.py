"""Direct reach between Prague stops, for "Go to…" with one change in the PID
watch app (PID docs/specs/direct-trip.md, phase 2).

Runs in the nightly job after generate.ps1 and reuses its extracted GTFS files
(<temp>/pid-gtfs). Stops are grouped by GTFS node (U221Z1P -> 221: all
platforms of one stop). For every Prague stop name:

  docs/reach/<key>.json  stops reachable FROM this stop directly
    {"p":[platform ids], "l":[lines], "s":[own nodes],
     "r":[node, minutes, line index, platform index, ...]}
  docs/into/<key>.json   stops from which THIS stop is reached directly
    {"p":[platform ids at those stops], "l":[lines], "d":[own nodes],
     "n":[node, ...], "m":[name, ...],       (names of the nodes in "t")
     "t":[node, minutes, line index, platform index, ...]}

minutes = typical scheduled ride (median of up to 5 daytime trips per pattern),
at most MAX_MIN; per node the MAX_LINES fastest lines. Files over MAX_BYTES are
trimmed (fewer lines per node, then shorter rides). <key> = FNV-1a 32-bit of the
stop name's UTF-8 bytes, 8 hex digits (as docs/stops).
"""
import csv
import glob
import json
import os
import re
import statistics
import sys
import tempfile
from collections import defaultdict

SRC = os.path.join(tempfile.gettempdir(), "pid-gtfs")
MAX_MIN = 45
MAX_LINES = 3
MAX_BYTES = 12 * 1024
SAMPLES = 5
NODE_RE = re.compile(r"^U(\d+)Z")


def fnv1a(name):
    h = 0x811C9DC5
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def rows(name):
    with open(os.path.join(SRC, name), encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def secs(t):
    t = (t or "").strip()
    if len(t) < 8:
        return None
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def main():
    plat = {}  # stop_id -> (name, node)
    for r in rows("stops.txt"):
        if (r.get("location_type") or "0").strip() not in ("", "0"):
            continue
        if "P" not in [z.strip() for z in (r.get("zone_id") or "").split(",")]:
            continue
        m = NODE_RE.match(r["stop_id"])
        name = (r.get("stop_name") or "").strip()
        if m and name:
            plat[r["stop_id"]] = (name, int(m.group(1)))
    route = {r["route_id"]: (r.get("route_short_name") or "").strip() for r in rows("routes.txt")}
    trip_route = {}
    for t in rows("trips.txt"):
        rn = route.get(t["route_id"], "")
        if rn:
            trip_route[t["trip_id"]] = rn

    # trip -> [(seq, stop_id, arr, dep)]
    trips = defaultdict(list)
    for r in rows("stop_times.txt"):
        tid = r["trip_id"]
        if tid not in trip_route:
            continue
        sid = r["stop_id"]
        if sid not in plat:
            continue
        a = secs(r.get("arrival_time"))
        d = secs(r.get("departure_time"))
        if a is None or d is None:
            continue
        trips[tid].append((int(r["stop_sequence"]), sid, a, d))
    print(f"Prague platforms {len(plat)}, trips with Prague stops {len(trips)}")

    # Patterns: same line and Prague stop sequence; up to SAMPLES daytime trips each.
    patterns = defaultdict(list)
    for tid, st in trips.items():
        st.sort()
        key = (trip_route[tid], tuple(s[1] for s in st))
        patterns[key].append(st)
    print(f"patterns {len(patterns)}")

    # (from stop name, from platform, line, to node) -> [minutes]
    pairs = defaultdict(list)
    for (rn, stops), lst in patterns.items():
        day = [s for s in lst if 6 * 3600 <= s[0][3] <= 20 * 3600] or lst
        day.sort(key=lambda s: abs(s[0][3] - 12 * 3600))
        for st in day[:SAMPLES]:
            n = len(st)
            for i in range(n - 1):
                sid_i, dep_i = st[i][1], st[i][3]
                name_i, node_i = plat[sid_i]
                for j in range(i + 1, n):
                    mins = (st[j][2] - dep_i) / 60.0
                    if mins > MAX_MIN:
                        break
                    node_j = plat[st[j][1]][1]
                    if node_j == node_i:
                        continue
                    pairs[(name_i, sid_i, rn, node_j)].append(mins)
    print(f"pairs {len(pairs)}")

    node_name = {}
    name_nodes = defaultdict(set)
    for sid, (name, node) in plat.items():
        node_name.setdefault(node, name)
        name_nodes[name].add(node)

    # reach[name][node] -> [(minutes, line, platform)]; into[target name][node] -> [(minutes, line, platform)]
    reach = defaultdict(lambda: defaultdict(list))
    into = defaultdict(lambda: defaultdict(list))
    for (name_i, sid_i, rn, node_j), ms in pairs.items():
        m = round(statistics.median(ms))
        reach[name_i][node_j].append((m, rn, sid_i))
        node_i = plat[sid_i][1]
        into[node_name[node_j]][node_i].append((m, rn, sid_i))

    for d in ("docs/reach", "docs/into"):
        os.makedirs(d, exist_ok=True)
        for f in glob.glob(f"{d}/*.json"):
            os.remove(f)

    def entries(table, lines_per_node, max_min):
        """Flat [node, minutes, line idx, platform idx, ...] with index tables."""
        lines, plats, flat = [], [], []
        for node in sorted(table):
            best = {}
            for m, rn, sid in sorted(table[node]):
                if m > max_min:
                    continue
                if rn not in best:  # fastest platform of each line
                    best[rn] = (m, sid)
            for rn, (m, sid) in sorted(best.items(), key=lambda kv: kv[1][0])[:lines_per_node]:
                if rn not in lines:
                    lines.append(rn)
                if sid not in plats:
                    plats.append(sid)
                flat += [node, m, lines.index(rn), plats.index(sid)]
        return lines, plats, flat

    def write(path, build):
        for lines_per_node, max_min in ((MAX_LINES, MAX_MIN), (2, MAX_MIN), (1, MAX_MIN), (1, 35), (1, 25)):
            s = json.dumps(build(lines_per_node, max_min), separators=(",", ":"))
            if len(s) <= MAX_BYTES:
                break
        with open(path, "w") as f:
            f.write(s)
        return len(s)

    sizes_r, sizes_i = [], []
    for name, table in reach.items():
        def build(lpn, mm, table=table, name=name):
            lines, plats, flat = entries(table, lpn, mm)
            return {"p": plats, "l": lines, "s": sorted(name_nodes[name]), "r": flat}
        sizes_r.append((write(f"docs/reach/{fnv1a(name)}.json", build), name))
    for name, table in into.items():
        def build(lpn, mm, table=table, name=name):
            lines, plats, flat = entries(table, lpn, mm)
            nodes = sorted({flat[k] for k in range(0, len(flat), 4)})
            return {"p": plats, "l": lines, "d": sorted(name_nodes[name]),
                    "n": nodes, "m": [node_name[n] for n in nodes], "t": flat}
        sizes_i.append((write(f"docs/into/{fnv1a(name)}.json", build), name))
    for label, sz in (("reach", sizes_r), ("into", sizes_i)):
        sz.sort(reverse=True)
        print(f"{label}: {len(sz)} files, {sum(s for s, _ in sz) / 1024:.0f} kB, largest "
              + ", ".join(f"{n} {s}" for s, n in sz[:4]))
        over = [n for s, n in sz if s > MAX_BYTES]
        if over:
            print(f"WARNING {label}: {len(over)} files over {MAX_BYTES} B even trimmed: {over[:5]}", file=sys.stderr)


if __name__ == "__main__":
    main()
