"""The pure parts of ``bench_cold_start.py`` (design D14): percentiles,
summaries, the cost estimate and budget refusal, the plan and batch order,
the Markdown tables, the JSON schema keys and the watchdog rule. No AWS,
no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bench_cold_start as bench


def parse(*argv: str) -> Any:
    return bench.parse_args(["--template", "rayito-base", *argv])


def launch_sample(
    index: int, kernel_ready_s: float, error: str | None = None
) -> dict[str, Any]:
    sample = bench.LaunchSample(
        phase="a", variant="full", mode="sdk", batch=0, index=index
    )
    sample.microvm_id = None if error else f"microvm-{index}"
    sample.api_s = 0.5
    sample.token_s = 0.1
    sample.agent_ready_s = kernel_ready_s - 1.0
    sample.kernel_ready_s = kernel_ready_s
    sample.first_cell_s = 0.2
    sample.vm_alive_s = kernel_ready_s + 2.0
    sample.error = error
    return bench.asdict(sample)


def test_percentile_is_nearest_rank() -> None:
    assert bench.percentile([3.0], 0.95) == 3.0
    ten = [float(i) for i in range(1, 11)]
    assert bench.percentile(ten, 0.95) == 10.0
    assert bench.percentile(ten, 0.5) == 5.0
    twenty = [float(i) for i in range(1, 21)]
    assert bench.percentile(twenty, 0.95) == 19.0
    with pytest.raises(ValueError, match="empty"):
        bench.percentile([], 0.95)


def test_summarize_skips_error_samples_and_counts_n() -> None:
    samples = [launch_sample(i, float(i + 1)) for i in range(20)]
    samples.append(launch_sample(20, 99.0, error="ThrottlingException"))
    summary = bench.summarize(samples)
    kernel = summary["kernel_ready_s"]
    assert kernel["n"] == 20
    assert kernel["p50"] == 10.5
    assert kernel["p95"] == 19.0
    assert (kernel["min"], kernel["max"]) == (1.0, 20.0)
    assert "index" not in summary and "batch" not in summary
    assert "over_budget" not in summary
    assert summary["stack_cell_s"] == {
        "n": 0,
        "p50": None,
        "p95": None,
        "min": None,
        "max": None,
    }


def test_summarize_of_nothing_is_empty() -> None:
    assert bench.summarize([]) == {}


def test_estimate_cost_defaults_and_budget() -> None:
    estimate = bench.estimate_cost(112, 20)
    assert 1.3 < estimate < 1.6
    storage_only = bench.estimate_cost(0, 0)
    assert storage_only == pytest.approx(2.0 * 0.08 * 7 / 30)
    assert bench.main(["--template", "t", "--sequential", "2000", "--dry-run"]) == 2
    assert bench.main(["--template", "t", "--template-slim", "s", "--dry-run"]) == 0


def test_plan_from_args_batch_order_and_counts() -> None:
    plan = bench.plan_from_args(parse("--template-slim", "rayito-base-slim"))
    assert [(b.mode, b.size) for b in plan.batches] == [
        ("sdk", 5),
        ("raw", 5),
        ("sdk", 10),
        ("raw", 10),
        ("sdk", 20),
        ("raw", 20),
    ]
    assert plan.phases == ("a", "b", "c", "e")
    assert plan.launches == 112
    assert plan.cycles == 20
    without_slim = bench.plan_from_args(parse())
    assert without_slim.phases == ("a", "b", "c")
    assert without_slim.launches == 92
    subset = bench.plan_from_args(parse("--phases", "a,c", "--sequential", "3"))
    assert subset.phases == ("a", "c")
    assert subset.launches == 5
    assert subset.cycles == 20
    sdk_only = bench.plan_from_args(parse("--burst-modes", "sdk", "--bursts", "5"))
    assert [(b.mode, b.size) for b in sdk_only.batches] == [("sdk", 5)]


def synthetic_run() -> dict[str, Any]:
    sequential = [launch_sample(i, 5.0 + i * 0.1) for i in range(20)]
    burst = [launch_sample(i, 6.0 + i * 0.1) for i in range(5)]
    for sample in burst:
        sample.update(
            {
                "phase": "b",
                "batch": 5,
                "kernel_ready_from_batch_s": 7.0 + sample["index"],
            }
        )
    cycles = [
        bench.asdict(
            bench.CycleSample(
                vm="explicit",
                cycle=i,
                pause_s=1.4,
                resume_api_s=0.3,
                resume_s=0.6 + i * 0.01,
                first_cell_after_resume_s=0.1,
                resume_generation=i + 1,
                kernel_alive=True,
            )
        )
        for i in range(10)
    ]
    phases = {
        "sequential": {
            "variant": "full",
            "samples": sequential,
            "summary": bench.summarize(sequential),
        },
        "bursts": [
            bench.batch_record(
                bench.Batch("sdk", 5), "2026-09-16T00:00:00.000000Z", burst
            )
        ],
        "resume": {
            "explicit": {
                "launch": launch_sample(0, 5.0),
                "samples": cycles,
                "summary": bench.summarize(cycles),
            },
            "auto": {
                "launch": launch_sample(1, 5.0),
                "samples": [],
                "summary": bench.summarize([]),
            },
        },
    }
    images = {
        "full": {
            "arn": "arn:full",
            "version": "10.0",
            "memorySnapshotSizeInBytes": 919146496,
        },
        "slim": None,
    }
    return {
        "schema": bench.SCHEMA,
        "meta": {
            "generated_at": "2026-09-16T00:00:00.000000Z",
            "run_id": "20260916T000000Z",
            "aborted": False,
            "region": "us-east-1",
            "client_rtt_ms": 93.0,
            "sdk_version": "0.0.5",
            "boto3_version": "1.43.94",
            "python": "3.12.0",
            "rayd_version": "10.0",
            "args": {},
            "images": images,
        },
        "phases": phases,
        "cost": bench.cost_block(phases, images),
    }


def test_json_round_trip_keeps_the_schema_keys(tmp_path: Path) -> None:
    run = synthetic_run()
    path = tmp_path / "run.json"
    bench.write_run(path, run)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema"] == "rayito.bench.cold-start/1"
    assert set(loaded["meta"]) >= {
        "generated_at",
        "run_id",
        "aborted",
        "region",
        "client_rtt_ms",
        "sdk_version",
        "boto3_version",
        "python",
        "rayd_version",
        "args",
        "images",
    }
    assert set(loaded["cost"]) >= {
        "launches",
        "throttled_launches",
        "suspends",
        "resumes",
        "vm_seconds",
        "snapshot_read_gb_expected",
        "snapshot_write_gb_expected",
        "estimated_usd",
    }
    assert loaded["cost"]["launches"] == 27
    assert loaded["cost"]["suspends"] == 10
    assert loaded["cost"]["resumes"] == 10
    assert loaded["cost"]["snapshot_write_gb_expected"] == pytest.approx(
        10 * 0.919146496, abs=1e-3
    )
    assert loaded["phases"]["bursts"][0]["batch_wall_s"] == 11.0
    assert loaded["phases"]["bursts"][0]["throttled"] == 0


def test_render_tables_has_a_row_per_metric_and_batch() -> None:
    text = bench.render_tables(synthetic_run())
    assert "### (a) sequential, full" in text
    assert "| `kernel_ready_s` | 20 |" in text
    assert "| sdk | 5 | 5 | 0 | 0 | 11.000 |" in text
    assert "### (c) resume, explicit (kernel_alive 10/10)" in text
    assert "| `resume_s` | 10 |" in text
    assert '"estimated_usd"' in text


def test_report_flag_renders_without_aws(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "run.json"
    bench.write_run(path, synthetic_run())
    assert bench.main(["--report", str(path)]) == 0
    assert "kernel_ready_s" in capsys.readouterr().out


def test_watchdog_selects_only_vms_over_the_budget() -> None:
    ages = {"microvm-a": 301.0, "microvm-b": 299.0, "microvm-c": 300.0}
    assert bench.select_over_budget(ages) == ["microvm-a"]
    assert bench.select_over_budget({}) == []


def test_launch_error_name_from_client_error() -> None:
    from botocore.exceptions import ClientError

    exc = ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "slow down"},
            "retryAfterSeconds": 2,
        },
        "RunMicrovm",
    )
    assert bench.launch_error_name(exc) == ("ThrottlingException", 2.0)
    assert bench.launch_error_name(RuntimeError("x")) == ("RuntimeError", None)


def test_cells_are_constants_and_noisy_loggers_are_quiet() -> None:
    import logging

    assert bench.FIRST_CELL == "1+1"
    assert "savefig" in bench.STACK_CELL and "len(buf.getvalue())" in bench.STACK_CELL
    bench.quiet_loggers()
    for name in ("boto3", "botocore", "grpc", "rayito"):
        assert logging.getLogger(name).level == logging.WARNING
