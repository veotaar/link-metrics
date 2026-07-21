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
from link_metrics.dataset_runtime import build_template, inspect_template, reset_from_template
from link_metrics.reporting import write_reports
from link_metrics.results import ResultError, run_capacity_sweep
from link_metrics.runtime import (
    ContenderRuntimeError,
    database_owner_connection,
    inspect_contender,
    start_contender,
    stop_contender,
)
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

    report = groups.add_parser("report", help="regenerate Result Series reports")
    report_commands = report.add_subparsers(dest="command", required=True)
    generate = report_commands.add_parser(
        "generate",
        help="generate compact JSON, Markdown, and HTML from raw bundles",
    )
    generate.add_argument("raw_bundles", nargs="+", type=Path)
    generate.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        if args.group == "contenders" and args.command == "discover":
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
            output = build_template(args.root.resolve(), args.contender_id)
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
                scenario="registration",
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
        ResultError,
        StartupError,
        TrialError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(json.dumps(output, indent=2, sort_keys=True))
    return 0
