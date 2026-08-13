"""Điểm vào (entry point) chạy toàn bộ pipeline lập kế hoạch sản xuất:

    PO + Forecast
        -> demand.build_demand_lines()          (net nhu cầu, trừ tồn kho TP)
        -> mrp.allocate_materials()              (tính ETA nguyên vật liệu)
        -> scheduler.schedule_production()       (xếp lịch dây chuyền, CP-SAT)
        -> eta_etd.build_plan_report()           (tính ETD, so sánh due date)

Dùng `run_planning()` như một hàm duy nhất; các bước con vẫn export riêng để
test độc lập hoặc thay thế (vd. đổi thuật toán scheduler mà không đụng vào MRP).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .demand import build_demand_lines
from .eta_etd import PlanReport, build_plan_report
from .models import PlanningDataset
from .mrp import allocate_materials
from .scheduler import schedule_production


def run_planning(
    dataset: PlanningDataset,
    planning_start: datetime,
    horizon_days: int = 90,
    slot_minutes: int = 60,
    time_limit_s: float = 20.0,
) -> PlanReport:
    """Chạy trọn vẹn 1 lượt lập kế hoạch, trả về PlanReport gồm:
      - plan_lines: kế hoạch sản xuất từng dòng nhu cầu (line, thời gian, ETA/ETD)
      - unscheduled: các dòng không xếp được lịch (thiếu NVL / hết chỗ / vô nghiệm)
      - workforce_warnings: cảnh báo về ràng buộc nhân lực chưa thể enforce cứng
    """
    horizon_end = planning_start + timedelta(days=horizon_days)

    demand_lines = build_demand_lines(dataset)
    material_readiness = allocate_materials(demand_lines, dataset, planning_start)
    schedule_result = schedule_production(
        demand_lines,
        material_readiness,
        dataset,
        horizon_start=planning_start,
        horizon_end=horizon_end,
        slot_minutes=slot_minutes,
        time_limit_s=time_limit_s,
    )
    return build_plan_report(
        demand_lines,
        schedule_result.scheduled,
        schedule_result.unscheduled,
        schedule_result.workforce_warnings,
        schedule_result.solver_status,
        dataset,
    )
