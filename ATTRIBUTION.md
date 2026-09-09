# Data sources and attribution

This project's own code is MIT licensed (see `LICENSE`). The data it uses is not
— each source carries its own licence and attribution requirement. If you deploy
this publicly, these obligations are yours to meet.

## OpenStreetMap — solar features, map tiles

© OpenStreetMap contributors, licensed under the
[Open Database Licence (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

ODbL is share-alike: publishing a derived *database* obliges you to release it
under ODbL too. Displaying counts and maps, as this app does, is a "produced
work" and only requires attribution — which the UI carries on the map. If you
start exporting the underlying feature data, read the licence properly.

Map tiles come from the OpenStreetMap Foundation's public tile servers, subject
to the [Tile Usage Policy](https://operations.osmfoundation.org/policies/tiles/).
That policy does not permit heavy or commercial use; use your own tile source if
this becomes more than a personal tool.

## Marktstammdatenregister — solar park registry

Data from the [Marktstammdatenregister](https://www.marktstammdatenregister.de/)
of the Bundesnetzagentur, licensed under
[Datenlizenz Deutschland – Namensnennung – Version 2.0](https://www.govdata.de/dl-de/by-2-0)
(dl-de/by-2-0).

That licence permits commercial and non-commercial use, redistribution, and
modification, and requires naming the source. The required attribution is:

> Bundesnetzagentur — Marktstammdatenregister

The derived extract (`data/mastr_solar.sqlite3`) is not committed to this
repository; it is built locally by `scripts/build_mastr.py`.

## Nominatim — geocoding

Operated by the OpenStreetMap Foundation under the
[Nominatim Usage Policy](https://operations.osmfoundation.org/policies/nominatim/):
maximum one request per second, a genuine identifying `User-Agent`, and results
must be cached rather than re-requested. This app does all three — see
`app/geocode.py`. Bulk or commercial use requires your own instance.

## OSRM — routing

The demo server at `router.project-osrm.org` is provided for testing only, with
no availability guarantee and no heavy use. Anything beyond personal use should
run its own OSRM instance or use a commercial routing provider.

## Changing providers

Every endpoint is configurable by environment variable, so pointing at
self-hosted instances needs no code change:

```
SOLARFARM_NOMINATIM_URL       SOLARFARM_OSRM_URL
SOLARFARM_OVERPASS_ENDPOINTS  SOLARFARM_USER_AGENT
```
