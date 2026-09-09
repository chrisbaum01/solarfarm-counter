# Solarfarm Counter

Counts ground-mounted solar parks along the Autobahn between two German cities.

```
München → Nürnberg
21 solar parks along 156.1 km of Autobahn (A 9, A 73)
```

No AI involved, and none needed — this is a deterministic geospatial problem. Route geometry
comes from OSRM, solar features from OpenStreetMap, and the rest is arithmetic. An LLM would
only add latency, cost, and answers that change between runs.

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000>.

The first search across a region fetches OpenStreetMap data live and takes a minute or two.
Everything after that is served from a local SQLite cache in well under a second.

### Optional but strongly recommended: add the official registry

OpenStreetMap alone **misses real solar parks** — it only contains what a volunteer has
traced, and commercial parks are often never drawn. The two ANUMAR fields on the A 8 at
Bergkirchen are plainly visible on satellite imagery and have no OSM presence at all.

The [Marktstammdatenregister](https://www.marktstammdatenregister.de/) (MaStR) is the
Bundesnetzagentur's register of every generating unit in Germany. Registration is legally
mandatory, so it covers what OSM misses:

```bash
# ~3 GB one-time download from marktstammdatenregister.de/MaStR/Datendownload
python scripts/build_mastr.py ~/Downloads/Gesamtdatenexport_*.zip
```

That distils the archive into `data/mastr_solar.sqlite3` (a few MB); the archive can then be
deleted. Without it the app still works, but says so in a warning on every search.

## How it works

1. **Geocode** both cities with Nominatim.
2. **Route** between them with OSRM, requesting per-step detail.
3. **Isolate the Autobahn.** OSRM tags every step with its road reference (`A 9`, `A 73`), so
   the motorway portions are extracted exactly — no map matching or guesswork. On the München →
   Nürnberg route this is 156.1 km of the 170.6 km trip; the city streets at either end are
   dropped.
4. **Fetch solar features** from Overpass, tile by tile.
5. **Filter and cluster** locally into distinct parks.

### Why the raw OSM count is not the answer

OpenStreetMap maps a single large solar park as anything from one tagged polygon to dozens of
separate panel-row ways, and it maps a great deal of rooftop PV that is not a park at all.
On München → Nürnberg:

| stage | count |
| --- | --- |
| raw OSM elements in the scanned tiles | 791 |
| after excluding rooftop PV | 363 |
| after keeping only what is within 500 m of the Autobahn | 49 |
| after merging features within 300 m of each other | 30 |
| after dropping clusters under 10,000 m² | **21** |

Every one of those steps is reported in the API response under `stats`, so the headline number
is always traceable back to the raw data.

### Definitions you can change

| Parameter | Default | Meaning |
| --- | --- | --- |
| `corridor_m` | 500 | Max distance from the motorway centerline |
| `link_m` | 300 | Features closer than this merge into one park |
| `min_area_m2` | 10000 | Clusters smaller than this are not counted |
| `include_bundesstrasse` | false | Also scan B-roads, for routes with no Autobahn |
| `use_mastr` | true | Also use the official registry, not just OpenStreetMap |

These genuinely move the answer, which is why they are exposed rather than hard-coded:

```
min_area_m2   0 → 30 parks      corridor_m  200 → 18 parks
          10000 → 21 parks                  500 → 21 parks
          20000 → 16 parks                 1000 → 23 parks
```

## API

```
GET /api/search?from=München&to=Nürnberg
GET /api/search?from=Hamburg&to=Berlin&corridor_m=1000&min_area_m2=20000
GET /api/health
```

Interactive docs at `/docs`. The UI mirrors its state into the URL, so any search can be
bookmarked or shared.

## Two sources, one count

Each park records where it came from, shown as a chip in the table and a colour on the map:

| Chip | Meaning |
| --- | --- |
| `OSM` | Traced outline in OpenStreetMap — area is **measured** from the polygon |
| `MaStR` | Registry only — position is a registered point, area is the operator's **declared** figure |
| `both` | Found in each source, counted once |

Both sources are fed through a **single clustering pass**, which is what prevents double
counting: a park that is traced in OSM *and* registered in MaStR merges into one park whose
provenance lists both. Where the two disagree on size, the measured geometry wins — a declared
figure is never added to a measured one.

Registry-only parks are the honest weak spot: MaStR gives one coordinate per registered unit,
not an outline, and that point can sit some way off the actual array. They are marked rather
than blended in, so you can always tell which numbers rest on traced geometry.

## Design notes

**Distance is measured from a park's nearest edge, not its centre.** A 12-hectare park whose
boundary runs 50 m from the carriageway is plainly next to the Autobahn even when its centroid
sits 600 m away.

**Disjoint motorway runs are kept as separate polylines.** Concatenating them would create a
phantom straight segment across the gap between them and drag unrelated features into the
corridor.

**Overpass tiles use a fixed 0.1° grid**, not route-shaped bounding boxes, so tiles are shared
between routes — München → Nürnberg and München → Berlin reuse the same A 9 tiles. An early
prototype using route-shaped boxes took 215 s and was repeatedly rate-limited.

**Failures are reported, never hidden.** If a tile cannot be fetched after retries across three
mirrors, the response still returns, with a warning that the count is a lower bound. Parks
mapped only as points have no measurable area; they are returned with `area_m2: null` and
flagged, rather than being silently dropped by the size filter or counted as zero.

## Tests

```bash
pytest
```

49 tests, fully offline — they run against recorded OSRM and Overpass fixtures in
`tests/fixtures/`, including regression checks pinning the real München → Nürnberg counts.

## Limitations

- Coverage depends on OpenStreetMap. A park nobody has mapped cannot be counted, and the
  rooftop/ground distinction relies on how diligently each feature was tagged.
- Germany only (geocoding is country-restricted).
- Capacity in MW is not reported: `generator:output:electricity` is too sparsely tagged to total
  honestly.
- Uses public demo servers for routing and Overpass. Fine for personal use; a deployment with
  real traffic should self-host or use a commercial endpoint.
