from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from studio.app import create_app
from studio.catalog import RaceName
from studio.models import blank_strategy
from studio.repository import StudioRepository


def _strategy_payload() -> dict:
    return blank_strategy(RaceName.PROTOSS).model_dump(mode="json")


def test_validation_api_rejects_semantically_invalid_action(tmp_path: Path):
    payload = _strategy_payload()
    payload["phases"][0]["rules"][0]["actions"] = [
        {"type": "expand", "structure": "PYLON", "amount": 2}
    ]
    app = create_app(StudioRepository(tmp_path / "contract.db"))

    with TestClient(app) as client:
        response = client.post("/api/strategies/validate", json=payload)

    assert response.status_code == 422
    assert "townhall" in response.text


def test_validation_api_rejects_unknown_strategy_fields(tmp_path: Path):
    payload = _strategy_payload()
    payload["phases"][0]["rules"][0]["actions"][0]["ignored"] = True
    app = create_app(StudioRepository(tmp_path / "contract.db"))

    with TestClient(app) as client:
        response = client.post("/api/strategies/validate", json=payload)

    assert response.status_code == 422
    assert "Extra inputs are not permitted" in response.text


def test_validation_api_accepts_new_tactical_action(tmp_path: Path):
    payload = _strategy_payload()
    payload["phases"][0]["rules"][0]["actions"] = [
        {"type": "scout", "unit": "PROBE", "target": "enemy_start"}
    ]
    app = create_app(StudioRepository(tmp_path / "contract.db"))

    with TestClient(app) as client:
        response = client.post("/api/strategies/validate", json=payload)

    assert response.status_code == 200
    assert response.json()["valid"] is True
