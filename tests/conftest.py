import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def osrm_response() -> dict:
    return json.loads((FIXTURES / "osrm_munich_nuremberg.json").read_text())


@pytest.fixture(scope="session")
def overpass_elements() -> list[dict]:
    return json.loads((FIXTURES / "overpass_munich_nuremberg.json").read_text())["elements"]


@pytest.fixture(scope="session")
def route(osrm_response):
    from app.routing import extract_runs

    return extract_runs(osrm_response["routes"][0]["legs"][0]["steps"])
