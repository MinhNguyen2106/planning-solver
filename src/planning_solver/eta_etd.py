"""Tính ETA / ETD cho từng dòng kế hoạch và tổng hợp thành báo cáo kế hoạch
cuối cùng - đây là bước "chốt" nối MRP (nguyên vật liệu) và Scheduler (dây
chuyền) lại thành thông tin có ý nghĩa với khách hàng / bộ phận kế hoạch:

  ETA (Estimated Time of Arrival)  = thời điểm NGUYÊN VẬT LIỆU/LINH KIỆN sẵn
                                      sàng đầy đủ để bắt đầu sản xuất đơn hàng
                                      (đầu ra của mrp.allocate_materials).
  ETD (Estimated Time of Departure) = thời điểm THÀNH PHẨM sẵn sàng xuất
                                      xưởng/giao hàng = thời điểm sản xuất
                                      hoàn thành (đầu ra của scheduler) +
                                      thời gian QC/đóng gói (post_production_buffer_hours).

  on_time = ETD <= due_date (hạn giao yêu cầu của PO/Forecast)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import DemandLine, PlanningDataset
from .scheduler import ScheduledJob, UnscheduledJob


@dataclass
class PlanLine:
    demand_line_id: str
    product_id: str
    qty: float
    source: str
    ref_id: str
    line_id: str
    eta: datetime  # nguyên vật liệu sẵn sàng
    production_start: datetime
    production_end: datetime
    etd: datetime  # thành phẩm sẵn sàng xuất
    due_date: datetime
    on_time: bool
    delay_hours: float


@dataclass
class PlanReport:
    plan_lines: list[PlanLine]
    unscheduled: list[UnscheduledJob]
    workforce_warnings: list[str]
    solver_status: str

    def on_time_rate(self) -> float:
        if not self.plan_lines:
            return 0.0
        return sum(1 for p in self.plan_lines if p.on_time) / len(self.plan_lines)


def build_plan_report(
    demand_lines: list[DemandLine],
    scheduled_jobs: list[ScheduledJob],
    unscheduled_jobs: list[UnscheduledJob],
    workforce_warnings: list[str],
    solver_status: str,
    dataset: PlanningDataset,
) -> PlanReport:
    demand_by_id = {d.id: d for d in demand_lines}
    products = dataset.product_map()

    plan_lines: list[PlanLine] = []
    for job in scheduled_jobs:
        d = demand_by_id[job.demand_line_id]
        product = products.get(job.product_id)
        buffer_hours = product.post_production_buffer_hours if product else 0.0
        etd = job.end_dt + timedelta(hours=buffer_hours)
        delay = (etd - job.due_date).total_seconds() / 3600.0
        plan_lines.append(
            PlanLine(
                demand_line_id=d.id,
                product_id=d.product_id,
                qty=job.qty,
                source=d.source.value,
                ref_id=d.ref_id,
                line_id=job.line_id,
                eta=job.material_eta,
                production_start=job.start_dt,
                production_end=job.end_dt,
                etd=etd,
                due_date=job.due_date,
                on_time=etd <= job.due_date,
                delay_hours=max(0.0, delay),
            )
        )

    plan_lines.sort(key=lambda p: p.production_start)

    return PlanReport(
        plan_lines=plan_lines,
        unscheduled=unscheduled_jobs,
        workforce_warnings=workforce_warnings,
        solver_status=solver_status,
    )
