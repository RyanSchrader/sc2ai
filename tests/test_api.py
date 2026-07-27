from pathlib import Path

from fastapi.testclient import TestClient

from studio.app import create_app
from studio.catalog import RaceName
from studio.models import StrategyProposal, blank_strategy
from studio.repository import StudioRepository


def test_bot_api_round_trip(tmp_path: Path):
    repository = StudioRepository(tmp_path / "api.db")
    app = create_app(repository)

    with TestClient(app) as client:
        response = client.get("/api/bots")
        assert response.status_code == 200
        assert len(response.json()) == 8

        bot_id = response.json()[0]["id"]
        detail = client.get(f"/api/bots/{bot_id}")
        assert detail.status_code == 200
        assert "strategy" in detail.json()

        fork = client.post(f"/api/bots/{bot_id}/fork", json={})
        assert fork.status_code == 201
        assert fork.json()["forkedFrom"] == bot_id

        deleted = client.delete(f"/api/bots/{bot_id}")
        assert deleted.status_code == 200
        assert deleted.json()["deletedAt"] is not None

        restored = client.post(f"/api/bots/{bot_id}/restore")
        assert restored.status_code == 200
        assert restored.json()["deletedAt"] is None


def test_catalog_and_installed_maps(tmp_path: Path):
    app = create_app(StudioRepository(tmp_path / "api.db"))
    with TestClient(app) as client:
        catalog = client.get("/api/catalog")
        assert catalog.status_code == 200
        assert set(catalog.json()["races"]) == {"terran", "protoss", "zerg"}

        maps = client.get("/api/runtime/maps")
        assert maps.status_code == 200
        assert "maps" in maps.json()


def test_assistant_proposal_requires_explicit_apply(tmp_path: Path):
    app = create_app(StudioRepository(tmp_path / "assistant.db"))

    async def fake_propose(**_arguments):
        return StrategyProposal(
            summary="Created a safe test strategy",
            suggested_name="Assistant Test",
            suggested_slug="assistant-test",
            description="Generated only after explicit apply.",
            strategy=blank_strategy(RaceName.PROTOSS),
        )

    app.state.assistant.propose = fake_propose
    with TestClient(app) as client:
        proposal = client.post(
            "/api/assistant/proposals",
            json={"prompt": "Make a simple Protoss bot", "requested_race": "protoss"},
        )
        assert proposal.status_code == 201
        assert len(client.get("/api/bots").json()) == 8

        applied = client.post(
            f"/api/assistant/proposals/{proposal.json()['id']}/apply",
            json={},
        )
        assert applied.status_code == 200
        assert applied.json()["slug"] == "assistant-test"
        assert len(client.get("/api/bots").json()) == 9


def test_analytics_and_benchmark_api(tmp_path: Path, monkeypatch):
    repository = StudioRepository(tmp_path / "analytics.db")
    app = create_app(repository)
    monkeypatch.setattr("studio.app.discover_maps", lambda: ["TestMap"])

    with TestClient(app) as client:
        bots = client.get("/api/bots").json()
        bot = bots[0]
        opponent = next(item for item in bots if item["id"] != bot["id"])

        stats = client.get(f"/api/bots/{bot['id']}/stats")
        assert stats.status_code == 200
        assert stats.json()["winRate"] is None

        suite = client.post(
            "/api/benchmarks",
            json={
                "name": "API benchmark",
                "scenarios": [
                    {
                        "name": "Pinned opponent",
                        "map_name": "TestMap",
                        "opponent_type": "bot",
                        "opponent_bot_id": opponent["id"],
                        "opponent_revision": opponent["currentRevision"],
                    }
                ],
            },
        )
        assert suite.status_code == 201
        assert suite.json()["scenarios"][0]["opponentRevision"] == 1
        assert client.get("/api/benchmarks").json()[0]["name"] == "API benchmark"
