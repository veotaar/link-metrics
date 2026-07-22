from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_readiness_is_not_a_scored_scenario() -> None:
    protocol = yaml.safe_load(
        (REPOSITORY_ROOT / "benchmark/protocol/scenarios.yaml").read_text(encoding="utf-8")
    )

    scenario_ids = [scenario["id"] for scenario in protocol["scenarios"]]
    operation_ids = [scenario["operationId"] for scenario in protocol["scenarios"]]

    assert "health" not in operation_ids
    assert scenario_ids == [
        "registration",
        "login",
        "short-link-creation",
        "uniform-resolution",
        "viral-resolution",
        "statistics",
    ]


def test_complete_scenario_methodology_starts_a_new_result_series() -> None:
    version = (
        REPOSITORY_ROOT / "benchmark/protocol/VERSION"
    ).read_text(encoding="utf-8").strip()

    assert version == "4.0.0"
