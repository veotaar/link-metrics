"""Command interface for the Link Metrics benchmark control plane."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from link_metrics.contenders import ContenderDiscoveryError, discover_contenders
from link_metrics.conformance import ConformanceError, conform_contender
from link_metrics.contract import ContractLintError, lint_contract
from link_metrics.dataset import (
    DatasetError,
    describe_dataset,
    sample_workload,
    write_reference_tokens,
)
from link_metrics.dataset_runtime import (
    build_template_runtime,
    inspect_template,
    reset_from_template,
)
from link_metrics.environment import (
    LOCAL_RESOURCE_PROFILE,
    assess_host_preflight,
    capture_host_observation,
)
from link_metrics.lite import (
    DEFAULT_LITE_SCENARIOS,
    LiteError,
    format_lite_results,
    run_lite_exploration,
)
from link_metrics.progress import ExecutionBudget
from link_metrics.reporting import write_reports
from link_metrics.results import ResultError, run_capacity_sweep
from link_metrics.runtime import (
    ContenderRuntimeError,
    database_owner_connection,
    inspect_contender,
    start_contender,
    stop_contender,
)
from link_metrics.series import SeriesError, run_result_series, verify_result_series
from link_metrics.startup import StartupError, run_cold_startup
from link_metrics.trial import SCENARIOS, TrialError, run_scenario_trial


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="link-metrics")
    groups = parser.add_subparsers(dest="group", required=True)
    contenders = groups.add_parser("contenders", help="operate on Contender manifests")
    contender_commands = contenders.add_subparsers(dest="command", required=True)
    discover = contender_commands.add_parser("discover", help="discover local Contenders")
    discover.add_argument("--root", type=Path, default=Path.cwd())
    for command, help_text in (
        ("start", "build and start a Contender with PostgreSQL"),
        ("inspect", "inspect a running Contender"),
        ("database-url", "print the ephemeral control-plane database URL"),
        ("stop", "stop a Contender and PostgreSQL"),
        ("conform", "run the mandatory API conformance gate"),
    ):
        runtime = contender_commands.add_parser(command, help=help_text)
        runtime.add_argument("contender_id")
        runtime.add_argument("--root", type=Path, default=Path.cwd())

    contract = groups.add_parser("contract", help="operate on the API Contract")
    contract_commands = contract.add_subparsers(dest="command", required=True)
    lint = contract_commands.add_parser("lint", help="lint the OpenAPI authority")
    default_document = Path(__file__).resolve().parents[3] / "contracts" / "http" / "openapi.yaml"
    lint.add_argument("--document", type=Path, default=default_document)

    host = groups.add_parser("host", help="inspect the benchmark host")
    host_commands = host.add_subparsers(dest="command", required=True)
    host_commands.add_parser(
        "preflight",
        help="report whether the host satisfies the official resource profile",
    )

    dataset = groups.add_parser("dataset", help="operate on the Benchmark Dataset")
    dataset_commands = dataset.add_subparsers(dest="command", required=True)
    describe = dataset_commands.add_parser("describe", help="describe Dataset inputs")
    describe.add_argument("--root", type=Path, default=Path.cwd())
    tokens = dataset_commands.add_parser("tokens", help="write fresh reference JWTs")
    tokens.add_argument("--repetition", type=int, required=True)
    tokens.add_argument("--issued-at", type=lambda value: int(value.replace("_", "")))
    tokens.add_argument("--output", type=Path, required=True)
    tokens.add_argument("--root", type=Path, default=Path.cwd())
    sample = dataset_commands.add_parser("sample", help="show deterministic workload samples")
    sample.add_argument("--repetition", type=int, required=True)
    sample.add_argument("--count", type=int, default=10)
    sample.add_argument("--root", type=Path, default=Path.cwd())
    for command, help_text in (
        ("build", "build the immutable Dataset template"),
        ("inspect", "inspect Dataset template provenance"),
        ("reset", "reset and prewarm the Trial database"),
    ):
        template_command = dataset_commands.add_parser(command, help=help_text)
        template_command.add_argument("contender_id")
        template_command.add_argument("--root", type=Path, default=Path.cwd())
        if command == "reset":
            template_command.add_argument("--expected-checksum")

    trial = groups.add_parser("trial", help="run performance Trials")
    trial_commands = trial.add_subparsers(dest="command", required=True)
    smoke = trial_commands.add_parser(
        "smoke",
        help="run a short nonofficial registration smoke Trial",
    )
    smoke.add_argument("contender_id")
    smoke.add_argument("--scenario", choices=SCENARIOS, default="registration")
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--repetition", type=int, default=1)
    smoke.add_argument("--root", type=Path, default=Path.cwd())
    run = trial_commands.add_parser(
        "run",
        help="run one official registration Trial at an offered rate",
    )
    run.add_argument("contender_id")
    run.add_argument("--scenario", required=True, choices=SCENARIOS)
    run.add_argument("--rate", type=float, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--repetition", type=int, default=1)
    run.add_argument("--root", type=Path, default=Path.cwd())

    startup = groups.add_parser("startup", help="measure cold Contender startup")
    startup_commands = startup.add_subparsers(dest="command", required=True)
    startup_run = startup_commands.add_parser(
        "run",
        help="run the official 20-repetition cold-start measurement",
    )
    startup_run.add_argument("contender_id")
    startup_run.add_argument("--output", type=Path, required=True)
    startup_run.add_argument("--root", type=Path, default=Path.cwd())

    capacity = groups.add_parser("capacity", help="discover and measure Scenario capacity")
    capacity_commands = capacity.add_subparsers(dest="command", required=True)
    capacity_run = capacity_commands.add_parser(
        "run",
        help="calibrate and run the standardized official rate sweep",
    )
    capacity_run.add_argument("contender_ids", nargs="+")
    capacity_run.add_argument("--scenario", required=True, choices=SCENARIOS)
    capacity_run.add_argument("--output", type=Path, required=True)
    capacity_run.add_argument("--root", type=Path, default=Path.cwd())

    lite = groups.add_parser("lite", help="run fast non-publishable exploration")
    lite_commands = lite.add_subparsers(dest="command", required=True)
    lite_run = lite_commands.add_parser(
        "run",
        help="estimate capacity without producing a Result Series",
    )
    lite_run.add_argument(
        "contender_ids",
        nargs="*",
        help="Contenders to explore; defaults to every discovered Contender",
    )
    lite_run.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIOS,
        help=(
            "Scenario to explore; repeat the option for more than one "
            f"(default: {', '.join(DEFAULT_LITE_SCENARIOS)})"
        ),
    )
    lite_run.add_argument(
        "--max-hours",
        type=float,
        default=2,
        help="stop starting Trials after this wall-clock budget (maximum: 2)",
    )
    lite_run.add_argument("--root", type=Path, default=Path.cwd())

    report = groups.add_parser("report", help="regenerate Result Series reports")
    report_commands = report.add_subparsers(dest="command", required=True)
    generate = report_commands.add_parser(
        "generate",
        help="generate compact JSON, Markdown, and HTML from raw bundles",
    )
    generate.add_argument("raw_bundles", nargs="+", type=Path)
    generate.add_argument("--output-dir", type=Path, required=True)

    series = groups.add_parser("series", help="operate the complete local Result Series")
    series_commands = series.add_subparsers(dest="command", required=True)
    series_run = series_commands.add_parser(
        "run",
        help="advance the complete cohort within a daily wall-clock budget",
    )
    series_run.add_argument("--time-budget-hours", type=float, required=True)
    series_run.add_argument("--output-dir", type=Path, required=True)
    series_run.add_argument("--root", type=Path, default=Path.cwd())
    series_verify = series_commands.add_parser(
        "verify",
        help="verify raw and generated Result Series checksums",
    )
    series_verify.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        if args.group == "host":
            output = assess_host_preflight(
                LOCAL_RESOURCE_PROFILE,
                capture_host_observation(LOCAL_RESOURCE_PROFILE),
            )
            print(json.dumps(output, indent=2, sort_keys=True))
            return 0 if output["valid"] else 2
        if args.group == "series" and args.command == "verify":
            output = verify_result_series(args.output_dir.resolve())
        elif args.group == "series":
            contenders = tuple(
                item["id"] for item in discover_contenders(args.root.resolve())
            )
            try:
                budget = ExecutionBudget.for_hours(args.time_budget_hours)
            except ValueError as error:
                raise SeriesError(str(error)) from error
            output = run_result_series(
                args.root.resolve(),
                output_dir=args.output_dir.resolve(),
                contenders=contenders,
                budget=budget,
            )
        elif args.group == "contenders" and args.command == "discover":
            output = discover_contenders(args.root.resolve())
        elif args.group == "contenders" and args.command == "start":
            output = start_contender(args.root.resolve(), args.contender_id)
        elif args.group == "contenders" and args.command == "inspect":
            output = inspect_contender(args.root.resolve(), args.contender_id)
        elif args.group == "contenders" and args.command == "database-url":
            output = database_owner_connection(args.root.resolve(), args.contender_id)
        elif args.group == "contenders" and args.command == "stop":
            output = stop_contender(args.root.resolve(), args.contender_id)
        elif args.group == "contenders" and args.command == "conform":
            output = conform_contender(args.root.resolve(), args.contender_id)
        elif args.group == "contract":
            output = lint_contract(args.document.resolve())
        elif args.command == "describe":
            output = describe_dataset(args.root.resolve())
        elif args.group == "dataset" and args.command == "tokens":
            output = write_reference_tokens(
                args.root.resolve(),
                args.repetition,
                args.output.resolve(),
                args.issued_at,
            )
        elif args.group == "dataset" and args.command == "sample":
            output = sample_workload(args.root.resolve(), args.repetition, args.count)
        elif args.group == "dataset" and args.command == "build":
            output = build_template_runtime(args.root.resolve(), args.contender_id)
        elif args.group == "dataset" and args.command == "inspect":
            output = inspect_template(args.root.resolve(), args.contender_id)
        elif args.group == "dataset" and args.command == "reset":
            output = reset_from_template(
                args.root.resolve(), args.contender_id, args.expected_checksum
            )
        elif args.group == "capacity":
            output = run_capacity_sweep(
                args.root.resolve(),
                args.contender_ids,
                scenario=args.scenario,
                output=args.output.resolve(),
            )
        elif args.group == "lite":
            root = args.root.resolve()
            contenders = args.contender_ids or [
                item["id"] for item in discover_contenders(root)
            ]
            output = run_lite_exploration(
                root,
                contenders,
                scenarios=args.scenario or DEFAULT_LITE_SCENARIOS,
                max_hours=args.max_hours,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        elif args.group == "report":
            output = write_reports(
                [path.resolve() for path in args.raw_bundles],
                args.output_dir.resolve(),
            )
        elif args.group == "startup":
            output = run_cold_startup(
                args.root.resolve(),
                args.contender_id,
                output=args.output.resolve(),
            )
        elif args.group == "trial" and args.command == "smoke":
            output = run_scenario_trial(
                args.root.resolve(),
                args.contender_id,
                scenario=args.scenario,
                output=args.output.resolve(),
                mode="smoke",
                repetition=args.repetition,
            )
        else:
            output = run_scenario_trial(
                args.root.resolve(),
                args.contender_id,
                scenario=args.scenario,
                output=args.output.resolve(),
                mode="trial",
                repetition=args.repetition,
                offered_rate=args.rate,
            )
    except (
        ConformanceError,
        ContenderDiscoveryError,
        ContenderRuntimeError,
        ContractLintError,
        DatasetError,
        LiteError,
        ResultError,
        SeriesError,
        StartupError,
        TrialError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.group == "lite":
        print(format_lite_results(output))
    else:
        print(json.dumps(output, indent=2, sort_keys=True))
    return 0
