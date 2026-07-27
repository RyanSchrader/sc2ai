import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sc2.position import Point2

from studio.models import (
    ActionType,
    Condition,
    ConditionKind,
    MetricName,
    StrategyAction,
    StrategyDocument,
    blank_strategy,
)
from studio.catalog import RaceName
from studio.runtime import (
    DeclarativeBot,
    compare,
    is_surrender_message,
    least_recently_scouted_position,
    stalemate_expired,
)
from studio.models import Comparator


FIXTURES = Path(__file__).resolve().parents[1] / "strategies" / "builtin_bots.json"


def test_all_eight_builtin_strategies_validate():
    records = json.loads(FIXTURES.read_text())

    assert len(records) == 8
    assert {record["slug"] for record in records} == {
        "terran-basic",
        "terran-bunker-rush",
        "protoss-basic",
        "protoss-intermediate",
        "zerg-basic-1",
        "zerg-basic-2",
        "protoss-tower-rush",
        "ryan-zealot-rush",
    }
    for record in records:
        strategy = StrategyDocument.model_validate(record["strategy"])
        assert strategy.phases
        assert sum(len(phase.rules) for phase in strategy.phases) >= 5


def test_zerg_basic_swarm_fixture_ships_roach_transition():
    records = json.loads(FIXTURES.read_text())
    record = next(record for record in records if record["slug"] == "zerg-basic-1")
    strategy = StrategyDocument.model_validate(record["strategy"])
    rules = {
        rule.id: rule
        for phase in strategy.phases
        for rule in phase.rules
    }

    assert rules["workers"].actions[0].amount == 48
    assert rules["third-base"].trigger.value == 32
    assert rules["warren"].trigger.subject == "ZERGLING"
    assert rules["warren"].trigger.value == 24
    assert rules["roaches"].actions[0].unit.value == "ROACH"
    assert rules["roaches"].actions[0].fallback_units[0].value == "ZERGLING"
    assert rules["attack"].actions[0].units == ["ROACH", "ZERGLING"]
    assert rules["attack"].actions[0].min_size == 24


def test_cross_race_action_is_rejected():
    strategy = blank_strategy(RaceName.PROTOSS)
    strategy.phases[0].rules[0].actions = [
        StrategyAction(type=ActionType.TRAIN_UNITS, unit="MARINE")
    ]

    with pytest.raises(ValidationError):
        StrategyDocument.model_validate(strategy.model_dump())


def test_unknown_condition_subject_is_rejected_before_runtime():
    with pytest.raises(ValidationError):
        Condition(
            kind=ConditionKind.METRIC,
            metric=MetricName.UNIT_COUNT,
            subject="NOT_A_REAL_UNIT",
            value=1,
        )


@pytest.mark.parametrize(
    ("comparator", "expected"),
    [
        (Comparator.LT, False),
        (Comparator.LTE, True),
        (Comparator.EQ, True),
        (Comparator.GTE, True),
        (Comparator.GT, False),
    ],
)
def test_comparators(comparator, expected):
    assert compare(10, comparator, 10) is expected


@pytest.mark.parametrize("message", ["gg", "GG!", "gg wp", "Good game.", "I concede"])
def test_surrender_messages_are_recognized(message):
    assert is_surrender_message(message)


@pytest.mark.parametrize("message", ["gl hf", "good game plan", "gg, ready?", "attack"])
def test_non_surrender_chat_is_ignored(message):
    assert not is_surrender_message(message)


@pytest.mark.asyncio
async def test_declarative_bot_accepts_opponent_surrender():
    class FakeClient:
        def __init__(self):
            self.requests = []

        async def _execute(self, **request):
            self.requests.append(request)

    bot = DeclarativeBot(
        blank_strategy(RaceName.ZERG),
        accept_computer_surrender=True,
    )
    bot.player_id = 1
    bot.client = FakeClient()
    bot.state = SimpleNamespace(
        chat=[SimpleNamespace(player_id=2, message="gg")]
    )

    assert await bot._accept_opponent_surrender()
    assert bot.accepted_opponent_surrender
    end_game = bot.client.requests[0]["debug"].debug[0].end_game
    assert end_game.end_result == end_game.DeclareVictory


@pytest.mark.asyncio
async def test_declarative_bot_ignores_gg_without_computer_opponent_flag():
    bot = DeclarativeBot(blank_strategy(RaceName.ZERG))
    bot.player_id = 1
    bot.state = SimpleNamespace(
        chat=[SimpleNamespace(player_id=2, message="gg")]
    )

    assert not await bot._accept_opponent_surrender()
    assert not bot.accepted_opponent_surrender


def test_army_scouts_unvisited_expansion_before_recent_location():
    recent = Point2((20, 20))
    unvisited = Point2((80, 80))

    target = least_recently_scouted_position(
        [recent, unvisited],
        {(20.0, 20.0): 500},
        Point2((10, 10)),
    )

    assert target == unvisited


def test_stalemate_requires_grace_period_and_full_inactivity_timeout():
    assert not stalemate_expired(599, 100, 600, 180)
    assert not stalemate_expired(700, 600, 600, 180)
    assert stalemate_expired(780, 600, 600, 180)
