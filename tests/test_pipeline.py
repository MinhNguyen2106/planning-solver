from datetime import datetime
from pathlib import Path

from planning_solver.io_utils import load_dataset
from planning_solver.pipeline import run_planning

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample" / "factory_demo.json"


def test_sample_dataset_produces_a_plan():
    dataset = load_dataset(SAMPLE)
    report = run_planning(
        dataset, planning_start=datetime(2026, 8, 13, 6, 0), horizon_days=60, time_limit_s=10
    )
    assert report.solver_status in ("OPTIMAL", "FEASIBLE")
    assert len(report.plan_lines) > 0
    # Mọi dòng ETD phải sau (hoặc bằng) thời điểm sản xuất hoàn thành
    for p in report.plan_lines:
        assert p.etd >= p.production_end
        assert p.production_start >= p.eta
        assert p.production_end >= p.production_start


def test_eta_never_before_planning_start():
    dataset = load_dataset(SAMPLE)
    start = datetime(2026, 8, 13, 6, 0)
    report = run_planning(dataset, planning_start=start, horizon_days=60, time_limit_s=10)
    for p in report.plan_lines:
        assert p.eta >= start
