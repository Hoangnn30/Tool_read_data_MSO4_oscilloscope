from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import TestResult, TestStep


def load_profile(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_sequence(path: str | Path) -> list[TestStep]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = raw["steps"] if isinstance(raw, dict) else raw
    return [TestStep(**step) for step in steps]


def save_results(
    results: list[TestResult],
    directory: str | Path,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = directory / f"test_result_{stamp}.json"
    csv_path = directory / f"test_result_{stamp}.csv"

    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "metadata": metadata or {},
        "passed": all(item.passed for item in results),
        "results": [asdict(item) for item in results],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(["step", "passed", "value", "error", "elapsed_s"])
        for item in results:
            writer.writerow(
                [
                    item.step,
                    item.passed,
                    item.value,
                    item.error,
                    f"{item.elapsed_s:.6f}",
                ]
            )

    return json_path, csv_path
