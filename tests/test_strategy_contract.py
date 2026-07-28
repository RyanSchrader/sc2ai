from __future__ import annotations

import pytest
from pydantic import ValidationError
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from studio.catalog import (
    ACTION_SPECS,
    GAS_BY_RACE,
    PRODUCER_BY_UPGRADE,
    TOWNHALL_BY_RACE,
    UPGRADE_SPECS,
    ActionType,
    RaceName,
    public_catalog,
)
from studio.models import (
    Condition,
    ConditionKind,
    MetricName,
    StrategyAction,
    StrategyDocument,
    StrategyRule,
    blank_strategy,
    validation_result,
)


def _document_with_action(
    race: RaceName,
    action: StrategyAction,
) -> StrategyDocument:
    strategy = blank_strategy(race)
    strategy.phases[0].rules[0].actions = [action]
    return StrategyDocument.model_validate(strategy.model_dump(mode="json"))


def test_every_catalog_default_is_valid_for_its_advertised_race():
    for action_type, spec in ACTION_SPECS.items():
        for race, payload in spec.defaults_by_race.items():
            action = StrategyAction.model_validate(payload)
            document = _document_with_action(race, action)

            assert document.phases[0].rules[0].actions[0].type == action_type


@pytest.mark.parametrize("race", list(RaceName))
def test_role_aware_defaults_use_the_correct_gas_and_townhall(race):
    gas = ACTION_SPECS[ActionType.MAINTAIN_GAS].defaults_by_race[race]
    expansion = ACTION_SPECS[ActionType.EXPAND].defaults_by_race[race]

    assert gas["structure"] == GAS_BY_RACE[race]
    assert expansion["structure"] == TOWNHALL_BY_RACE[race]


def test_public_catalog_exposes_the_authoritative_contract():
    catalog = public_catalog()

    assert catalog["schemaVersions"] == [1]
    assert set(catalog["actionTypes"]) == {
        action_type.value for action_type in ACTION_SPECS
    }
    assert set(catalog["actionSpecs"]) == set(catalog["actionTypes"])
    assert (
        catalog["actionSpecs"]["maintain_gas"]["defaultsByRace"]["terran"][
            "structure"
        ]
        == "REFINERY"
    )
    assert catalog["actionSpecs"]["build_forward"]["races"] == [
        "terran",
        "protoss",
    ]
    assert catalog["upgradeMetadata"]["WARPGATERESEARCH"]["producer"] == (
        "CYBERNETICSCORE"
    )
    assert catalog["unitMetadata"]["STALKER"]["techRequirement"] == (
        "CYBERNETICSCORE"
    )


def test_runtime_consumed_optional_location_fields_have_per_race_defaults():
    for spec in ACTION_SPECS.values():
        operational_defaults = {
            field.name
            for field in spec.fields
            if field.name in {"distance", "target"}
        }
        for defaults in spec.defaults_by_race.values():
            assert operational_defaults <= defaults.keys()


def test_supported_upgrades_and_producers_exist_in_burnysc2_ids():
    for upgrade, spec in UPGRADE_SPECS.items():
        assert upgrade.value in UpgradeId.__members__
        assert spec.producer == PRODUCER_BY_UPGRADE[upgrade]
        assert spec.producer.value in UnitTypeId.__members__


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "expand",
            "structure": "PYLON",
            "amount": 2,
        },
        {
            "type": "maintain_gas",
            "structure": "GATEWAY",
            "amount": 1,
        },
        {
            "type": "train_units",
            "unit": "PROBE",
        },
        {
            "type": "attack",
            "units": ["ZEALOT"],
            "min_size": 5,
            "required_unit": "ZEALOT",
        },
        {
            "type": "train_units",
            "unit": "ZEALOT",
            "units": ["STALKER"],
        },
        {
            "type": "scout",
            "unit": "PROBE",
            "target": "main",
        },
        {
            "type": "distribute_workers",
            "amount": 1,
        },
    ],
)
def test_semantically_invalid_action_shapes_are_rejected(payload):
    with pytest.raises(ValidationError):
        StrategyAction.model_validate(payload)


def test_unknown_action_fields_are_rejected():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StrategyAction.model_validate(
            {"type": "distribute_workers", "python": "do_something()"}
        )


def test_unknown_document_and_condition_fields_are_rejected():
    document = blank_strategy(RaceName.PROTOSS).model_dump(mode="json")
    document["arbitrary_code"] = "ignored?"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StrategyDocument.model_validate(document)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Condition.model_validate({"kind": "always", "mystery": True})


def test_unsupported_schema_version_is_rejected():
    payload = blank_strategy(RaceName.PROTOSS).model_dump(mode="json")
    payload["schema_version"] = 999

    with pytest.raises(ValidationError):
        StrategyDocument.model_validate(payload)


def test_condition_nesting_depth_is_bounded():
    nested: dict[str, object] = {"kind": "always"}
    for _ in range(9):
        nested = {"kind": "not", "children": [nested]}
    payload = blank_strategy(RaceName.PROTOSS).model_dump(mode="json")
    payload["phases"][0]["rules"][0]["trigger"] = nested

    with pytest.raises(
        ValidationError,
        match="cannot be nested more than 8 levels",
    ):
        StrategyDocument.model_validate(payload)


def test_enemy_unit_conditions_can_reference_another_race():
    strategy = blank_strategy(RaceName.PROTOSS)
    strategy.phases[0].rules[0].trigger = Condition(
        kind=ConditionKind.METRIC,
        metric=MetricName.ENEMY_UNIT_COUNT,
        subject="MARINE",
        value=1,
    )

    assert StrategyDocument.model_validate(strategy.model_dump(mode="json"))


def test_contract_serialization_emits_only_operational_action_fields():
    action = StrategyAction(
        type=ActionType.BUILD_STRUCTURE,
        structure="GATEWAY",
        amount=2,
        distance=8,
    )

    assert action.model_dump(mode="json") == {
        "type": "build_structure",
        "structure": "GATEWAY",
        "amount": 2,
        "distance": 8.0,
    }


def test_exact_legacy_expanded_action_shape_loads_and_normalizes():
    legacy = {
        "type": "expand",
        "unit": None,
        "units": [],
        "fallback_units": [],
        "structure": "NEXUS",
        "amount": 2,
        "buffer": None,
        "distance": 7.0,
        "placement": "main",
        "min_size": None,
        "required_unit": None,
        "required_amount": None,
        "target": "enemy_start",
    }

    action = StrategyAction.model_validate(legacy)

    assert action.model_dump(mode="json") == {
        "type": "expand",
        "structure": "NEXUS",
        "amount": 2,
    }


@pytest.mark.parametrize(
    ("action_type", "field"),
    [
        ("train_workers", "amount"),
        ("maintain_supply", "buffer"),
    ],
)
def test_zero_valued_legacy_action_setting_is_preserved(action_type, field):
    legacy = {
        "type": action_type,
        "unit": None,
        "units": [],
        "fallback_units": [],
        "structure": None,
        "amount": None,
        "buffer": None,
        "distance": 7.0,
        "placement": "main",
        "min_size": None,
        "required_unit": None,
        "required_amount": None,
        "target": "enemy_start",
    }
    legacy[field] = 0

    action = StrategyAction.model_validate(legacy)

    assert action.model_dump(mode="json") == {
        "type": action_type,
        field: 0,
    }


def test_partial_legacy_shape_does_not_silently_ignore_target():
    with pytest.raises(ValidationError, match="does not support fields: target"):
        StrategyAction.model_validate(
            {
                "type": "expand",
                "structure": "NEXUS",
                "amount": 2,
                "target": "enemy_start",
            }
        )


def test_conditions_serialize_only_fields_with_runtime_meaning():
    always = Condition(
        kind="always",
    )
    structure_count = Condition(
        kind="metric",
        metric="structure_count",
        subject="GATEWAY",
        status="ready",
        comparator="gte",
        value=2,
    )

    assert always.model_dump(mode="json") == {"kind": "always"}
    assert structure_count.model_dump(mode="json") == {
        "kind": "metric",
        "metric": "structure_count",
        "comparator": "gte",
        "value": 2.0,
        "subject": "GATEWAY",
        "status": "ready",
    }


def test_irrelevant_condition_field_is_rejected_outside_legacy_shape():
    with pytest.raises(ValidationError, match="do not support fields: status"):
        Condition.model_validate({"kind": "always", "status": "total"})


def test_cooldown_is_only_serialized_for_cooldown_rules():
    continuous = StrategyRule(
        id="continuous",
        name="Continuous",
        actions=[StrategyAction(type="distribute_workers")],
    )
    cooldown = StrategyRule(
        id="cooldown",
        name="Cooldown",
        execution="cooldown",
        cooldown_seconds=3,
        actions=[StrategyAction(type="distribute_workers")],
    )

    assert "cooldown_seconds" not in continuous.model_dump(mode="json")
    assert cooldown.model_dump(mode="json")["cooldown_seconds"] == 3


def test_partial_non_cooldown_rule_cannot_carry_ignored_cooldown():
    with pytest.raises(
        ValidationError,
        match="continuous rules do not support cooldown_seconds",
    ):
        StrategyRule(
            id="continuous",
            name="Continuous",
            cooldown_seconds=1,
            actions=[StrategyAction(type="distribute_workers")],
        )


def test_validation_diagnostics_warn_about_missing_production_dependency():
    strategy = blank_strategy(RaceName.PROTOSS)
    strategy.phases[0].rules[0].actions = [
        StrategyAction(type="train_units", unit="STALKER")
    ]

    result = validation_result(strategy)

    assert result["valid"]
    assert any(
        "STALKER needs CYBERNETICSCORE" in warning
        for warning in result["warnings"]
    )
