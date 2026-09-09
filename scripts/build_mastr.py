"""Build a compact ground-mount solar dataset from the MaStR full export.

The Marktstammdatenregister is the Bundesnetzagentur's register of every
generating unit in Germany. Registration is legally mandatory, which makes it
far more complete than OpenStreetMap for solar farms -- OSM only contains what a
volunteer has traced, and commercial solar parks are frequently never drawn.

Usage:
    python scripts/build_mastr.py path/to/Gesamtdatenexport_*.zip

Writes `data/mastr_solar.sqlite3`, a few MB, which the app queries at runtime.
The 3 GB source archive is not needed afterwards.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "mastr_solar.sqlite3"

# Catalog entries are referenced by numeric id, and those ids are not stable
# enough to hardcode. Resolve them by their German label instead.
#
# Note the label really is "Freiflächensolaranlage": a bare "Freifläche" also
# exists in the catalog but belongs to a different category, and filtering on it
# silently matches nothing.
GROUND_MOUNTED = "Freiflächensolaranlage"
IN_OPERATION = "In Betrieb"
SOLAR_ENERGY = "Solare Strahlungsenergie"


def _text(elem: ET.Element, tag: str) -> str | None:
    child = elem.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _int(elem: ET.Element, tag: str) -> int | None:
    raw = _text(elem, tag)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _float(elem: ET.Element, tag: str) -> float | None:
    raw = _text(elem, tag)
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def load_catalog(archive: zipfile.ZipFile) -> dict[int, str]:
    """Map catalog id -> label from Katalogwerte.xml."""
    name = next(
        (n for n in archive.namelist() if Path(n).name.lower().startswith("katalogwerte")),
        None,
    )
    if name is None:
        raise SystemExit("Katalogwerte.xml not found in the archive")

    catalog: dict[int, str] = {}
    with archive.open(name) as stream:
        for _, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag != "Katalogwert":
                continue
            ident = _text(elem, "Id")
            value = _text(elem, "Wert")
            if ident and value:
                catalog[int(ident)] = value
            elem.clear()
    return catalog


def ids_for(catalog: dict[int, str], label: str) -> set[str]:
    """All catalog ids carrying a given label, as strings for direct comparison."""
    found = {str(k) for k, v in catalog.items() if v == label}
    if not found:
        raise SystemExit(
            f"catalog label {label!r} not found -- the export format may have changed"
        )
    return found


def solar_unit_files(archive: zipfile.ZipFile) -> list[str]:
    return sorted(
        n for n in archive.namelist()
        if Path(n).name.lower().startswith("einheitensolar") and n.lower().endswith(".xml")
    )


def build(zip_path: Path, out_path: Path, states: set[str] | None = None) -> None:
    archive = zipfile.ZipFile(zip_path)

    print("reading catalog…", flush=True)
    catalog = load_catalog(archive)
    ground = ids_for(catalog, GROUND_MOUNTED)
    operating = ids_for(catalog, IN_OPERATION)
    solar = ids_for(catalog, SOLAR_ENERGY)
    print(f"  Freifläche ids={sorted(ground)} InBetrieb ids={sorted(operating)}")

    files = solar_unit_files(archive)
    print(f"streaming {len(files)} solar unit file(s)…", flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(out_path)
    conn.execute(
        """CREATE TABLE units (
             mastr_id TEXT PRIMARY KEY,
             lat REAL NOT NULL,
             lon REAL NOT NULL,
             name TEXT,
             park_name TEXT,
             capacity_kw REAL,
             area_m2 REAL,
             commissioned TEXT,
             municipality TEXT,
             district TEXT,
             state TEXT
           )"""
    )

    seen = kept = no_coords = 0
    batch: list[tuple] = []
    for filename in files:
        with archive.open(filename) as stream:
            for _, elem in ET.iterparse(stream, events=("end",)):
                if elem.tag != "EinheitSolar":
                    continue
                seen += 1
                if seen % 500_000 == 0:
                    print(f"  scanned {seen:,} units, kept {kept:,}", flush=True)

                art = _text(elem, "ArtDerSolaranlage")
                status = _text(elem, "EinheitBetriebsstatus")
                energy = _text(elem, "Energietraeger")
                if art not in ground or status not in operating or (
                    energy is not None and energy not in solar
                ):
                    elem.clear()
                    continue

                lat = _float(elem, "Breitengrad")
                lon = _float(elem, "Laengengrad")
                if lat is None or lon is None:
                    # Small units may withhold coordinates; without a position
                    # they cannot be placed along a route, so they are counted
                    # and reported rather than silently ignored.
                    no_coords += 1
                    elem.clear()
                    continue

                # Bundesland is a catalog id (e.g. 1409), not a label.
                state = catalog.get(_int(elem, "Bundesland"))
                if states and state not in states:
                    elem.clear()
                    continue

                hectares = _float(elem, "GroesseDerInAnspruchGenommenenFlaecheInHektar")
                batch.append(
                    (
                        _text(elem, "EinheitMastrNummer"),
                        lat,
                        lon,
                        _text(elem, "NameStromerzeugungseinheit"),
                        _text(elem, "NameDesSolarparks"),
                        _float(elem, "Bruttoleistung"),
                        hectares * 10_000 if hectares else None,
                        _text(elem, "Inbetriebnahmedatum"),
                        _text(elem, "Gemeinde"),
                        _text(elem, "Landkreis"),
                        state,
                    )
                )
                kept += 1
                elem.clear()

                if len(batch) >= 5_000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO units VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                    )
                    batch.clear()

    if batch:
        conn.executemany("INSERT OR REPLACE INTO units VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)

    if kept == 0:
        conn.close()
        out_path.unlink(missing_ok=True)
        raise SystemExit(
            f"no units matched after scanning {seen:,} records — the filter labels are "
            "probably wrong for this export. Refusing to write an empty dataset."
        )

    conn.execute("CREATE INDEX idx_lat ON units(lat)")
    conn.execute("CREATE INDEX idx_lon ON units(lon)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [("source", zip_path.name), ("units", str(kept)), ("scanned", str(seen))],
    )
    conn.commit()

    with_area = conn.execute(
        "SELECT COUNT(*) FROM units WHERE area_m2 IS NOT NULL"
    ).fetchone()[0]
    conn.close()

    print(
        f"\nscanned {seen:,} solar units\n"
        f"  kept {kept:,} ground-mounted, in operation, with coordinates\n"
        f"  skipped {no_coords:,} ground-mounted units that withhold coordinates\n"
        f"  {with_area:,} of the kept units declare a land area\n"
        f"wrote {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Gesamtdatenexport_*.zip")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--state", action="append", default=None,
        help="restrict to a Bundesland (repeatable); default is all of Germany",
    )
    args = parser.parse_args()
    if not args.archive.exists():
        sys.exit(f"archive not found: {args.archive}")
    build(args.archive, args.out, set(args.state) if args.state else None)


if __name__ == "__main__":
    main()
