"""Per-state ground-mount statistics."""

import sqlite3

import pytest

from app import analysis
from app.analysis import STATE_AREA_KM2, state_statistics
from app.mastr import MastrStore


def _store(tmp_path, rows, with_commercial=False):
    """Build a throwaway extract in the same shape build_mastr.py writes."""
    path = tmp_path / "mastr.sqlite3"
    conn = sqlite3.connect(path)
    extra = ", usage TEXT, commercial INTEGER" if with_commercial else ""
    conn.execute(
        "CREATE TABLE units (mastr_id TEXT PRIMARY KEY, lat REAL, lon REAL, name TEXT,"
        f" park_name TEXT, capacity_kw REAL, area_m2 REAL, commissioned TEXT,"
        f" municipality TEXT, district TEXT, state TEXT{extra})"
    )
    n = 11 + (2 if with_commercial else 0)
    conn.executemany(f"INSERT INTO units VALUES ({','.join('?' * n)})", rows)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('source','test.zip')")
    conn.commit()
    conn.close()
    return MastrStore(path)


def _unit(uid, lat, lon, kw, state, commercial=None, usage=None):
    row = [uid, lat, lon, "u", None, kw, None, "2021-01-01", "Ort", "Kreis", state]
    if commercial is not None:
        row.extend([usage, commercial])
    return tuple(row)


class TestStateAreas:
    def test_all_sixteen_states_present(self):
        assert len(STATE_AREA_KM2) == 16

    def test_areas_sum_to_germany(self):
        """A wrong area silently skews one row's density, so pin the total."""
        assert sum(STATE_AREA_KM2.values()) == pytest.approx(357_600, abs=500)

    def test_a_few_known_areas(self):
        assert STATE_AREA_KM2["Bayern"] == 70_542
        assert STATE_AREA_KM2["Berlin"] == 891
        assert STATE_AREA_KM2["Nordrhein-Westfalen"] == 34_113


class TestStatistics:
    def test_missing_dataset_is_not_an_error(self, tmp_path):
        result = state_statistics(MastrStore(tmp_path / "absent.sqlite3"))
        assert result["available"] is False
        assert result["states"] == []

    def test_colocated_units_count_as_one_park(self, tmp_path):
        rows = [_unit(f"S{i}", 48.5, 11.5 + i * 0.0005, 500, "Bayern") for i in range(4)]
        r = state_statistics(_store(tmp_path, rows))
        bayern = next(s for s in r["states"] if s["state"] == "Bayern")
        assert bayern["units"] == 4
        assert bayern["sites"] == 1
        assert bayern["capacity_mw"] == pytest.approx(2.0)

    def test_distant_units_stay_separate(self, tmp_path):
        rows = [_unit("A", 48.5, 11.5, 500, "Bayern"), _unit("B", 48.9, 12.4, 500, "Bayern")]
        r = state_statistics(_store(tmp_path, rows))
        assert next(s for s in r["states"] if s["state"] == "Bayern")["sites"] == 2

    def test_small_units_are_excluded_as_private(self, tmp_path):
        rows = [_unit("A", 48.5, 11.5, 50, "Bayern"), _unit("B", 49.5, 11.9, 900, "Bayern")]
        r = state_statistics(_store(tmp_path, rows), min_kw=100)
        assert next(s for s in r["states"] if s["state"] == "Bayern")["sites"] == 1

    def test_density_is_per_thousand_square_km(self, tmp_path):
        # 100 sites in Bayern -> 100 * 1000 / 70,542 km2. Enough of them that the
        # 2-decimal rounding in the response does not swamp the assertion.
        rows = [_unit(f"S{i}", 48.0 + i * 0.05, 11.0 + i * 0.05, 1000, "Bayern")
                for i in range(100)]
        r = state_statistics(_store(tmp_path, rows))
        bayern = next(s for s in r["states"] if s["state"] == "Bayern")
        assert bayern["sites"] == 100
        assert bayern["sites_per_1000km2"] == pytest.approx(100 * 1000 / 70_542, abs=0.01)
        assert bayern["mw_per_1000km2"] == pytest.approx(100 * 1000 / 70_542, abs=0.1)

    def test_every_state_appears_even_with_no_parks(self, tmp_path):
        r = state_statistics(_store(tmp_path, [_unit("A", 48.5, 11.5, 900, "Bayern")]))
        assert len(r["states"]) == 16
        bremen = next(s for s in r["states"] if s["state"] == "Bremen")
        assert bremen["sites"] == 0
        assert bremen["median_site_mw"] is None  # not zero: there is nothing to average

    def test_shares_add_up(self, tmp_path):
        rows = [
            _unit("A", 48.5, 11.5, 1000, "Bayern"),
            _unit("B", 52.4, 13.0, 3000, "Brandenburg"),
            _unit("C", 51.0, 7.0, 500, "Nordrhein-Westfalen"),
        ]
        r = state_statistics(_store(tmp_path, rows))
        assert sum(s["share_sites_pct"] for s in r["states"]) == pytest.approx(100, abs=0.3)
        assert sum(s["share_mw_pct"] for s in r["states"]) == pytest.approx(100, abs=0.3)

    def test_unknown_state_is_dropped_not_guessed(self, tmp_path):
        rows = [_unit("A", 48.5, 11.5, 900, "Bayern"), _unit("B", 48.6, 11.6, 900, None)]
        r = state_statistics(_store(tmp_path, rows))
        assert r["totals"]["units"] == 1

    def test_median_and_largest_reflect_site_totals(self, tmp_path):
        rows = [
            _unit("A", 48.5, 11.5, 1000, "Bayern"),
            _unit("B", 48.5, 11.5002, 1000, "Bayern"),   # same site as A -> 2 MW
            _unit("C", 49.5, 12.5, 500, "Bayern"),
        ]
        r = state_statistics(_store(tmp_path, rows))
        bayern = next(s for s in r["states"] if s["state"] == "Bayern")
        assert bayern["sites"] == 2
        assert bayern["largest_site_mw"] == pytest.approx(2.0)
        assert bayern["median_site_mw"] == pytest.approx(1.25)

    def test_sorted_by_power_density(self, tmp_path):
        rows = [
            _unit("A", 48.5, 11.5, 1000, "Bayern"),
            _unit("B", 52.4, 13.0, 9000, "Brandenburg"),
        ]
        r = state_statistics(_store(tmp_path, rows))
        densities = [s["mw_per_1000km2"] for s in r["states"]]
        assert densities == sorted(densities, reverse=True)


class TestCommercialFlag:
    def test_flag_absent_means_no_filtering(self, tmp_path):
        """Before the re-extract, nothing should be silently dropped."""
        rows = [_unit("A", 48.5, 11.5, 900, "Bayern")]
        r = state_statistics(_store(tmp_path, rows))
        assert r["commercial_filter"] is False
        assert r["totals"]["units"] == 1

    def test_flag_present_filters_private_units(self, tmp_path):
        rows = [
            _unit("A", 48.5, 11.5, 900, "Bayern", commercial=1, usage="Industrie"),
            _unit("B", 49.5, 12.5, 900, "Bayern", commercial=0, usage="Haushalt"),
        ]
        store = _store(tmp_path, rows, with_commercial=True)
        r = state_statistics(store)
        assert r["commercial_filter"] is True
        assert r["totals"]["units"] == 1

    def test_sparsely_populated_usage_field_is_not_claimed_as_a_filter(self, tmp_path):
        """The real register sets this field for 0.19% of ground-mount units.

        Reporting the count as "classified commercial by the register" would be
        false when almost nothing was classified.
        """
        rows = [_unit(f"S{i}", 48.0 + i * 0.05, 11.0 + i * 0.05, 900, "Bayern",
                      commercial=1, usage="Industrie" if i == 0 else None)
                for i in range(100)]
        r = state_statistics(_store(tmp_path, rows, with_commercial=True))
        assert r["usage_coverage_pct"] == pytest.approx(1.0)
        assert r["commercial_filter"] is False, "must not claim a filter that did not run"
        assert r["totals"]["units"] == 100  # nothing dropped on an unusable field
