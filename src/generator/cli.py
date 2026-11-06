import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml
from dotenv import load_dotenv

from generator.adapter import discover, export_tasks, prepare_repository
from generator.filters.fail_to_pass import EvaluationError, HarborEvaluator


def main():
    parser = argparse.ArgumentParser(description="Export and run the Nim Harbor pilot")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--metadata", type=Path)
    export.add_argument("--limit", type=int, default=3)
    export.add_argument("--scan-limit", type=int, default=200)
    export.add_argument(
        "--no-cache",
        action="store_true",
        help="Recheck candidates without reading or writing the export cache",
    )
    export.add_argument("--calibration-concurrency", type=int, default=16)
    run = sub.add_parser("run")
    run.add_argument("agent", choices=["oracle", "nop", "model"])
    run.add_argument(
        "--config", type=Path, default=Path("src/generator/configs/job.yaml")
    )
    run.add_argument(
        "--env-file",
        type=Path,
        help="Load credentials from this file, overriding inherited environment values",
    )
    args = parser.parse_args()
    if args.command == "export":
        if args.limit < 1 or args.scan_limit < 1 or args.calibration_concurrency < 1:
            parser.error(
                "--limit, --scan-limit and --calibration-concurrency must be positive"
            )
        if args.metadata:
            data = json.loads(args.metadata.read_text())
            prs = data.get("pull_requests", data) if isinstance(data, dict) else data
        else:
            prs = discover(args.scan_limit)
        try:
            repo = prepare_repository()
        except (ValueError, subprocess.CalledProcessError) as error:
            raise SystemExit(f"Repository setup failed: {error}") from error
        output = Path("datasets/nim")
        output.mkdir(parents=True, exist_ok=True)
        evaluator = HarborEvaluator(concurrency=args.calibration_concurrency)
        try:
            result = export_tasks(
                repo,
                prs,
                output,
                limit=args.limit,
                evaluator=evaluator,
                cache_dir=None if args.no_cache else Path(".cache/exports"),
            )
        except EvaluationError as error:
            raise SystemExit(f"Calibration stopped: {error}") from error
        exported, rejected, scanned = (
            len(result["exported"]),
            len(result["rejected"]),
            result["scanned"],
        )
        print(
            f"Exported {exported} calibrated tasks from {scanned} PRs; {rejected} ineligible."
        )
        hits = result.get("cache_hits", {"accepted": 0, "rejected": 0})
        print(
            f"Cache hits: {hits['accepted']} accepted, {hits['rejected']} rejected; {result.get('calibrated', 0)} candidates calibrated this run."
        )
        if exported < args.limit:
            raise SystemExit(
                f"Only {exported}/{args.limit} tasks qualified; increase --scan-limit or supply more metadata"
            )
    else:
        if args.env_file is not None and not args.env_file.is_file():
            parser.error(f"Environment file not found: {args.env_file}")
        load_dotenv(args.env_file or ".env", override=args.env_file is not None)
        if os.environ.get("FIREWORKS_API_KEY"):
            os.environ.setdefault(
                "FIREWORKS_AI_API_KEY", os.environ["FIREWORKS_API_KEY"]
            )
        config = yaml.safe_load(args.config.read_text())
        config.pop("job_name", None)
        if args.agent != "model":
            config["agents"] = [{"name": args.agent}]
        directory = Path(".cache/job-configs")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", dir=directory, delete=False
        ) as stream:
            stream.write(yaml.safe_dump(config, sort_keys=False))
            path = Path(stream.name)
        raise SystemExit(subprocess.call(["harbor", "run", "-c", str(path)]))
