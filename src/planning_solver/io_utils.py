"""Tiện ích đọc/ghi PlanningDataset và PlanReport dạng JSON."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .eta_etd import PlanReport
from .models import PlanningDataset


def load_dataset(path: str | Path) -> PlanningDataset:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return PlanningDataset.model_validate(data)


def save_dataset(dataset: PlanningDataset, path: str | Path) -> None:
    Path(path).write_text(dataset.model_dump_json(indent=2), encoding="utf-8")


def _default(o: Any):
    if isinstance(o, datetime):
        return o.isoformat()
    if is_dataclass(o):
        return asdict(o)
    return str(o)


def plan_report_to_dict(report: PlanReport) -> dict:
    return {
        "plan_lines": [_default(p) for p in report.plan_lines],
        "unscheduled": [_default(u) for u in report.unscheduled],
        "workforce_warnings": report.workforce_warnings,
        "solver_status": report.solver_status,
        "on_time_rate": report.on_time_rate(),
    }


def save_plan_report(report: PlanReport, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(plan_report_to_dict(report), indent=2, ensure_ascii=False, default=_default),
        encoding="utf-8",
    )
