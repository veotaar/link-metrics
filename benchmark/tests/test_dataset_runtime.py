import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTENDER_ID = "express-node"
SECOND_CONTENDER_ID = "nest-node"
FULL_DATASET_ENABLED = os.environ.get("LINK_METRICS_TEST_FULL_DATASET") == "1"


def run_control_plane(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "link_metrics", *arguments, "--root", str(REPOSITORY_ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )


def run_psql(
    container: str, sql: str, *, database: str = "link_metrics"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "--username",
            "link_metrics_control",
            "--dbname",
            database,
            "--tuples-only",
            "--no-align",
            "--command",
            sql,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    not FULL_DATASET_ENABLED,
    reason="set LINK_METRICS_TEST_FULL_DATASET=1 for expensive Dataset acceptance",
)
def test_full_dataset_build_clone_and_reset_fidelity() -> None:
    """Exercise the expensive one-time Dataset lifecycle through its public seam."""
    run_control_plane("contenders", "stop", CONTENDER_ID)

    try:
        started = run_control_plane("contenders", "start", CONTENDER_ID)
        assert started.returncode == 0, started.stderr
        database_container = json.loads(started.stdout)["database"]["container"]

        built = run_control_plane("dataset", "build", CONTENDER_ID)
        assert built.returncode == 0, built.stderr
        provenance = json.loads(built.stdout)
        assert provenance["status"] == "built"
        assert provenance["datasetVersion"] == "1.2.0"
        assert len(provenance["templateChecksum"]) == 64
        assert provenance["fingerprint"]["users"] == 100_000
        assert provenance["fingerprint"]["shortLinks"] == 1_000_000
        assert provenance["userSeedCache"]["status"] in {"built", "reused"}
        assert provenance["userSeedCache"]["users"] == 100_000
        assert len(provenance["userSeedCache"]["sha256"]) == 64
        assert provenance["fingerprint"]["ownership"] == {
            "maximumClicked": 5,
            "maximumNeverClicked": 5,
            "maximumShortLinks": 10,
            "minimumClicked": 5,
            "minimumNeverClicked": 5,
            "minimumShortLinks": 10,
        }

        inspected = run_control_plane("dataset", "inspect", CONTENDER_ID)
        assert inspected.returncode == 0, inspected.stderr
        assert json.loads(inspected.stdout)["templateChecksum"] == provenance["templateChecksum"]

        mismatch = run_control_plane(
            "dataset",
            "reset",
            CONTENDER_ID,
            "--expected-checksum",
            "0" * 64,
        )
        assert mismatch.returncode == 2
        assert "template checksum mismatch" in mismatch.stderr

        mutation = run_psql(
            database_container,
            "UPDATE public.links SET click_count = 99, last_clicked_at = clock_timestamp() "
            "WHERE short_code = '00000001';",
        )
        assert mutation.returncode == 0, mutation.stderr

        reset = run_control_plane(
            "dataset",
            "reset",
            CONTENDER_ID,
            "--expected-checksum",
            provenance["templateChecksum"],
        )
        assert reset.returncode == 0, reset.stderr
        reset_summary = json.loads(reset.stdout)
        assert set(reset_summary["prewarmed"]) == {
            "idx_links_user_id",
            "idx_users_email",
            "links",
            "links_pkey",
            "users",
            "users_pkey",
        }

        restored = run_psql(
            database_container,
            "SELECT click_count, last_clicked_at IS NULL FROM public.links "
            "WHERE short_code = '00000001';",
        )
        assert restored.returncode == 0, restored.stderr
        assert restored.stdout.strip() == "0|t"

        template_flags = run_psql(
            database_container,
            "SELECT datistemplate, datallowconn FROM pg_catalog.pg_database "
            "WHERE datname = 'link_metrics_template_1_2_0';",
            database="postgres",
        )
        assert template_flags.returncode == 0, template_flags.stderr
        assert template_flags.stdout.strip() == "t|f"

        state = run_control_plane("contenders", "inspect", CONTENDER_ID)
        assert state.returncode == 0, state.stderr
        assert json.loads(state.stdout)["contender"]["readiness"] == 204
    finally:
        stopped = run_control_plane("contenders", "stop", CONTENDER_ID)
        assert stopped.returncode == 0, stopped.stderr

    try:
        started = run_control_plane("contenders", "start", SECOND_CONTENDER_ID)
        assert started.returncode == 0, started.stderr

        reused = run_control_plane("dataset", "build", SECOND_CONTENDER_ID)
        assert reused.returncode == 0, reused.stderr
        second_provenance = json.loads(reused.stdout)
        assert second_provenance["userSeedCache"] == {
            **provenance["userSeedCache"],
            "status": "reused",
        }
        assert second_provenance["templateChecksum"] == provenance["templateChecksum"]
    finally:
        stopped = run_control_plane("contenders", "stop", SECOND_CONTENDER_ID)
        assert stopped.returncode == 0, stopped.stderr
