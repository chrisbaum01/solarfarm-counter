"""MaStR extraction, lookup, and cross-source deduplication."""

import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from app import mastr
from app.cluster import build_parks, select_features
from app.geometry import PolylineIndex, projection_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<Katalogwerte>
  <Katalogwert><Id>852</Id><Wert>Freiflächensolaranlage</Wert><KatalogKategorieId>1</KatalogKategorieId></Katalogwert>
  <Katalogwert><Id>853</Id><Wert>Bauliche Anlagen (Hausdach, Gebäude und Fassade)</Wert><KatalogKategorieId>1</KatalogKategorieId></Katalogwert>
  <Katalogwert><Id>35</Id><Wert>In Betrieb</Wert><KatalogKategorieId>2</KatalogKategorieId></Katalogwert>
  <Katalogwert><Id>31</Id><Wert>Endgültig stillgelegt</Wert><KatalogKategorieId>2</KatalogKategorieId></Katalogwert>
  <Katalogwert><Id>2495</Id><Wert>Solare Strahlungsenergie</Wert><KatalogKategorieId>3</KatalogKategorieId></Katalogwert>
</Katalogwerte>
"""


def _unit(uid, art=852, status=35, lat="48.2637", lon="11.2830",
          hectares="4.5", park="ANUMAR Solarpark Bergkirchen (Feld 1)", state="Bayern"):
    coords = ""
    if lat is not None:
        coords = f"<Breitengrad>{lat}</Breitengrad><Laengengrad>{lon}</Laengengrad>"
    area = f"<GroesseDerInAnspruchGenommenenFlaecheInHektar>{hectares}</GroesseDerInAnspruchGenommenenFlaecheInHektar>" if hectares else ""
    parkname = f"<NameDesSolarparks>{park}</NameDesSolarparks>" if park else ""
    return f"""  <EinheitSolar>
    <EinheitMastrNummer>{uid}</EinheitMastrNummer>
    <ArtDerSolaranlage>{art}</ArtDerSolaranlage>
    <EinheitBetriebsstatus>{status}</EinheitBetriebsstatus>
    <Energietraeger>2495</Energietraeger>
    {coords}
    <Bruttoleistung>7500.5</Bruttoleistung>
    {area}{parkname}
    <NameStromerzeugungseinheit>Einheit {uid}</NameStromerzeugungseinheit>
    <Inbetriebnahmedatum>2021-06-30</Inbetriebnahmedatum>
    <Gemeinde>Bergkirchen</Gemeinde>
    <Landkreis>Dachau</Landkreis>
    <Bundesland>{state}</Bundesland>
  </EinheitSolar>
"""


@pytest.fixture(scope="module")
def built_db(tmp_path_factory):
    """Run the real extractor over a synthetic export archive."""
    import build_mastr

    tmp = tmp_path_factory.mktemp("mastr")
    archive = tmp / "Gesamtdatenexport_test.zip"
    units = "".join([
        _unit("SEE900000000001"),                                  # keep
        _unit("SEE900000000002", lat="48.2588", lon="11.2908",
              park="ANUMAR Solarpark Bergkirchen (Feld 2)"),        # keep
        _unit("SEE900000000003", art=853),                          # rooftop -> drop
        _unit("SEE900000000004", status=31),                        # shut down -> drop
        _unit("SEE900000000005", lat=None, lon=None),               # no coords -> drop
        _unit("SEE900000000006", hectares=None, park=None),         # keep, no area
    ])
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Katalogwerte.xml", CATALOG)
        z.writestr(
            "EinheitenSolar_1.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>\n<EinheitenSolar>\n{units}</EinheitenSolar>\n',
        )
    out = tmp / "mastr_solar.sqlite3"
    build_mastr.build(archive, out)
    return out


class TestExtractor:
    def test_only_ground_mounted_operating_units_with_coords_are_kept(self, built_db):
        conn = sqlite3.connect(built_db)
        ids = {r[0] for r in conn.execute("SELECT mastr_id FROM units")}
        assert ids == {"SEE900000000001", "SEE900000000002", "SEE900000000006"}

    def test_hectares_converted_to_square_metres(self, built_db):
        conn = sqlite3.connect(built_db)
        area = conn.execute(
            "SELECT area_m2 FROM units WHERE mastr_id='SEE900000000001'"
        ).fetchone()[0]
        assert area == pytest.approx(45_000)

    def test_missing_area_stays_null(self, built_db):
        conn = sqlite3.connect(built_db)
        area = conn.execute(
            "SELECT area_m2 FROM units WHERE mastr_id='SEE900000000006'"
        ).fetchone()[0]
        assert area is None

    def test_catalog_resolved_by_label_not_hardcoded_id(self, built_db):
        """Ids come from the catalog, so an id renumbering cannot silently pass."""
        import build_mastr

        with pytest.raises(SystemExit):
            build_mastr.ids_for({1: "Something Else"}, "Freiflächensolaranlage")


class TestStore:
    def test_missing_dataset_is_not_an_error(self, tmp_path):
        store = mastr.MastrStore(tmp_path / "absent.sqlite3")
        assert not store.available()
        assert store.query_bbox(48, 11, 49, 12) == []
        assert store.meta() == {}

    def test_bbox_query(self, built_db):
        store = mastr.MastrStore(built_db)
        assert store.available()
        # All three kept units sit around Bergkirchen.
        assert len(store.query_bbox(48.25, 11.27, 48.27, 11.30)) == 3
        # Only Feld 2 lies in this narrower western box.
        west = store.query_bbox(48.255, 11.285, 48.262, 11.295)
        assert [u.mastr_id for u in west] == ["SEE900000000002"]
        assert store.query_bbox(50.0, 6.0, 51.0, 7.0) == []

    def test_query_tiles_deduplicates_overlaps(self, built_db):
        store = mastr.MastrStore(built_db)
        overlapping = [(48.20, 11.20, 48.30, 11.35), (48.25, 11.25, 48.35, 11.40)]
        assert len(store.query_tiles(overlapping)) == 3

    def test_park_name_preferred_over_unit_name(self, built_db):
        store = mastr.MastrStore(built_db)
        unit = next(u for u in store.query_bbox(48.26, 11.28, 48.27, 11.29))
        assert unit.best_name.startswith("ANUMAR")


class TestCrossSourceMerge:
    """Both sources cluster together, so a shared park is not counted twice."""

    def setup_method(self):
        self.line = [(11.0, 49.0), (11.2, 49.0)]
        self.proj = projection_for(self.line)
        self.index = PolylineIndex([self.line], self.proj)

    def _osm_way(self, wid, lat, lon, d=0.001):
        return {
            "type": "way", "id": wid,
            "tags": {"power": "plant", "plant:source": "solar", "name": "Traced Park"},
            "geometry": [{"lat": lat, "lon": lon}, {"lat": lat, "lon": lon + d},
                         {"lat": lat + d, "lon": lon + d}, {"lat": lat + d, "lon": lon}],
        }

    def _parks(self, elements, min_area_m2=0):
        kept, _ = select_features(elements, self.index, self.proj, 500)
        return build_parks(kept, self.proj, 300, min_area_m2)[0]

    def test_colocated_sources_merge_into_one_park(self):
        lat, lon = 49.0 + 50 / 110_574, 11.05
        units = [mastr.MastrUnit("SEE1", lat, lon, "u", "Registry Park", 5000.0,
                                 40_000.0, "2021-01-01", "Ort")]
        parks = self._parks([self._osm_way(1, lat, lon)] + mastr.to_elements(units))
        assert len(parks) == 1
        assert parks[0].sources == ["mastr", "osm"]

    def test_measured_area_wins_over_declared(self):
        """A declared figure must not be added to a measured one."""
        lat, lon = 49.0 + 50 / 110_574, 11.05
        osm_only = self._parks([self._osm_way(1, lat, lon)])
        units = [mastr.MastrUnit("SEE1", lat, lon, "u", "P", 5000.0, 999_999.0, None, None)]
        merged = self._parks([self._osm_way(1, lat, lon)] + mastr.to_elements(units))
        assert merged[0].area_m2 == pytest.approx(osm_only[0].area_m2)
        assert merged[0].area_is_declared is False

    def test_registry_only_park_is_flagged_and_uses_declared_area(self):
        lat, lon = 49.0 + 50 / 110_574, 11.05
        units = [mastr.MastrUnit("SEE1", lat, lon, "u", "Registry Park", 7500.5,
                                 45_000.0, "2021-06-30", "Bergkirchen")]
        parks = self._parks(mastr.to_elements(units))
        assert len(parks) == 1
        assert parks[0].sources == ["mastr"]
        assert parks[0].area_is_declared is True
        assert parks[0].area_m2 == pytest.approx(45_000)
        assert parks[0].capacity_kw == pytest.approx(7500.5)
        assert parks[0].name == "Registry Park"

    def test_declared_areas_are_not_summed_across_units(self):
        """One park registered as several units must not multiply in size."""
        lat, lon = 49.0 + 50 / 110_574, 11.05
        units = [
            mastr.MastrUnit(f"SEE{i}", lat, lon + i * 0.0002, "u", "Split Park",
                            2500.0, 45_000.0, None, None)
            for i in range(4)
        ]
        parks = self._parks(mastr.to_elements(units))
        assert len(parks) == 1
        assert parks[0].area_m2 == pytest.approx(45_000)  # not 180,000
        assert parks[0].capacity_kw == pytest.approx(10_000)  # capacity does add up

    def test_distant_sources_stay_separate(self):
        units = [mastr.MastrUnit("SEE1", 49.0, 11.16, "u", "Far Park", 100.0,
                                 20_000.0, None, None)]
        parks = self._parks([self._osm_way(1, 49.0, 11.02)] + mastr.to_elements(units))
        assert len(parks) == 2

    def test_registry_units_obey_the_size_filter(self):
        """Registry units must not be exempt from the minimum-area threshold.

        Regression: MaStR elements were given power=plant/plant:source=solar,
        which is the marker that exempts an explicitly declared plant from the
        size filter. Every registry unit tripped it, making the filter inert --
        a 6,000 m² park survived a 10,000 m² threshold.
        """
        lat, lon = 49.0 + 50 / 110_574, 11.05
        small = [mastr.MastrUnit("SEE1", lat, lon, "u", "Small Park", 600.0,
                                 6_000.0, None, None)]
        assert self._parks(mastr.to_elements(small), min_area_m2=10_000) == []
        assert len(self._parks(mastr.to_elements(small), min_area_m2=1_000)) == 1

    def test_osm_declared_plant_is_still_exempt(self):
        """The exemption must survive for what it was actually meant for."""
        lat, lon = 49.0 + 50 / 110_574, 11.05
        tiny = self._osm_way(1, lat, lon, d=0.0002)  # far below the threshold
        parks = self._parks([tiny], min_area_m2=10_000)
        assert len(parks) == 1, "an OSM power=plant must not be size-filtered away"

    def test_registry_unit_without_area_survives_size_filter(self):
        """Unknown size must not be read as zero and filtered away."""
        lat, lon = 49.0 + 50 / 110_574, 11.05
        units = [mastr.MastrUnit("SEE1", lat, lon, "u", "P", 900.0, None, None, None)]
        parks = self._parks(mastr.to_elements(units), min_area_m2=10_000)
        assert len(parks) == 1
        assert parks[0].area_m2 is None
