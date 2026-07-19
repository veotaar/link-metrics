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
