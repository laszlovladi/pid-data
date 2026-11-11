# pid-data

Lines and directions per platform for every Prague stop of the PID public transport
network, built every night from the official PID timetable (GTFS,
<https://pid.cz/en/opendata/>) and published with GitHub Pages for the PID watch app
(Garmin Venu Sq 2).

- `generate.ps1` – downloads `https://data.pid.cz/PID_GTFS.zip` and writes
  `docs/lines/000.json` … `127.json` and `docs/meta.json`.
- `.github/workflows/nightly.yml` – runs it every night and commits changes.
- Also `docs/shapes/<shape_id>.json`: route shapes of the trips that stop in Prague,
  simplified to 10 m: `{"p":[lat0,lon0,dlat,dlon,...]}` in 1/100000 degree (first point
  absolute, then differences to the previous point).
- Pages: `https://laszlovladi.github.io/pid-data/lines/NNN.json`,
  `https://laszlovladi.github.io/pid-data/shapes/L12V2.json`.

File format: a stop is in file (sum of the character codes of its name) mod 128:

```json
{ "Florenc": [ ["U689Z2P", "B", ["3", "6", "8"], ["Kobylisy", "Lehovec"]] ] }
```

Prague = stops in fare zone P; a trip that ends at a stop is not counted there.

Data source: PID open data (ROPID), provided under the terms on pid.cz/opendata.
