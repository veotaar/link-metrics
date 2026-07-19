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
from link_metrics.runtime import (
    ContenderRuntimeError,
    database_owner_connection,
    inspect_contender,
    start_contender,
    stop_contender,
)


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
        else:
            output = reset_from_template(
                args.root.resolve(), args.contender_id, args.expected_checksum
            )
    except (
        ConformanceError,
        ContenderDiscoveryError,
        ContenderRuntimeError,
        ContractLintError,
        DatasetError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(json.dumps(output, indent=2, sort_keys=True))
    return 0
