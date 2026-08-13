"""Bộ lập lịch sản xuất (APS) dùng OR-Tools CP-SAT.

Ràng buộc được mô hình hoá:
  - Mỗi dòng nhu cầu (DemandLine) được sản xuất TRỌN VẸN trên ĐÚNG MỘT dây
    chuyền trong tập dây chuyền đủ điều kiện (line có định mức năng suất cho
    sản phẩm đó).
  - Một dây chuyền chỉ chạy một lệnh tại một thời điểm (no-overlap), và chỉ
    chạy trong giờ làm việc theo lịch riêng của dây chuyền đó (xem calendar.py
    - thời gian được nén để lệnh tự "tạm dừng" ngoài giờ làm việc).
  - Lệnh sản xuất không được bắt đầu trước khi nguyên vật liệu sẵn sàng
    (material ETA tính từ mrp.py).
  - Nhân lực dùng chung (WorkforcePool): tổng số nhân công của các lệnh đang
    chạy đồng thời trên các dây chuyền cùng tổ không được vượt quá headcount
    của tổ - áp dụng ràng buộc cứng (Cumulative) khi các dây chuyền trong tổ
    có LỊCH LÀM VIỆC GIỐNG HỆT NHAU (trường hợp phổ biến: cùng nhà máy, cùng
    ca). Nếu lịch khác nhau giữa các line trong cùng tổ, hệ thống báo warning
    và bỏ qua ràng buộc cứng cho tổ đó (xem ARCHITECTURE.md mục "Giới hạn").
  - Mục tiêu: tối thiểu hoá tổng độ trễ giao hàng có trọng số theo độ ưu tiên
    (đơn PO ưu tiên cao > PO thường > Forecast).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ortools.sat.python import cp_model

from .calendar import CompressedTimeline
from .mrp import MaterialReadiness
from .models import DemandLine, OrderPriority, PlanningDataset

_PRIORITY_WEIGHT = {
    OrderPriority.HIGH: 100,
    OrderPriority.NORMAL: 10,
    OrderPriority.LOW: 1,
}


@dataclass
class ScheduledJob:
    demand_line_id: str
    product_id: str
    line_id: str
    qty: float
    start_dt: datetime
    end_dt: datetime  # thời điểm sản xuất hoàn thành (chưa gồm buffer QC/đóng gói)
    material_eta: datetime
    due_date: datetime


@dataclass
class UnscheduledJob:
    demand_line_id: str
    product_id: str
    reason: str
    detail: str = ""


@dataclass
class ScheduleResult:
    scheduled: list[ScheduledJob] = field(default_factory=list)
    unscheduled: list[UnscheduledJob] = field(default_factory=list)
    workforce_warnings: list[str] = field(default_factory=list)
    solver_status: str = ""


def _job_hours(dataset: PlanningDataset, d: DemandLine, line_id: str) -> float | None:
    line = dataset.line_map()[line_id]
    rate = line.rate_for(d.product_id)
    if rate is None:
        return None
    return d.qty / rate.rate_per_hour + rate.changeover_minutes / 60.0


def schedule_production(
    demand_lines: list[DemandLine],
    material_readiness: dict[str, MaterialReadiness],
    dataset: PlanningDataset,
    horizon_start: datetime,
    horizon_end: datetime,
    slot_minutes: int = 60,
    time_limit_s: float = 20.0,
) -> ScheduleResult:
    result = ScheduleResult()
    lines = dataset.line_map()
    pools = dataset.pool_map()

    # --- 0) Cảnh báo tĩnh: nhân lực yêu cầu vượt quá sức chứa của tổ ---
    for line in dataset.lines:
        if not line.labor_pool_id:
            continue
        pool = pools.get(line.labor_pool_id)
        if pool is None:
            result.workforce_warnings.append(
                f"Dây chuyền '{line.id}' tham chiếu tổ nhân lực '{line.labor_pool_id}' không tồn tại."
            )
            continue
        for rate in line.product_rates:
            if rate.required_headcount > pool.headcount:
                result.workforce_warnings.append(
                    f"Dây chuyền '{line.id}' cần {rate.required_headcount} nhân công cho SP "
                    f"'{rate.product_id}' nhưng tổ '{pool.id}' chỉ có {pool.headcount} người."
                )

    # --- 1) Loại bỏ các đơn bị chặn do thiếu nguyên vật liệu ---
    schedulable: list[DemandLine] = []
    for d in demand_lines:
        readiness = material_readiness.get(d.id)
        if readiness is None or readiness.blocked:
            shortages = ", ".join(
                f"{s.component_id} thiếu {s.shortfall_qty:g}" for s in (readiness.shortages if readiness else [])
            )
            result.unscheduled.append(
                UnscheduledJob(d.id, d.product_id, "material_shortage", shortages)
            )
            continue
        schedulable.append(d)

    if not schedulable:
        result.solver_status = "NO_SCHEDULABLE_DEMAND"
        return result

    # --- 2) Xây timeline nén cho từng dây chuyền ---
    timelines: dict[str, CompressedTimeline] = {
        line_id: CompressedTimeline.build(line.calendar, horizon_start, horizon_end, slot_minutes)
        for line_id, line in lines.items()
    }

    # --- 3) Liệt kê cặp (demand, line) khả thi ---
    model = cp_model.CpModel()
    intervals_by_line: dict[str, list[cp_model.IntervalVar]] = {lid: [] for lid in lines}
    demands_by_line: dict[str, list[int]] = {lid: [] for lid in lines}
    assigned_vars: dict[tuple[str, str], cp_model.IntVar] = {}
    start_vars: dict[tuple[str, str], cp_model.IntVar] = {}
    end_vars: dict[tuple[str, str], cp_model.IntVar] = {}
    contribution_vars: list[cp_model.IntVar] = []

    feasible_pairs: dict[str, list[str]] = {d.id: [] for d in schedulable}

    for d in schedulable:
        readiness = material_readiness[d.id]
        eligible_lines = [
            lid for lid, line in lines.items() if line.rate_for(d.product_id) is not None
        ]
        if not eligible_lines:
            result.unscheduled.append(
                UnscheduledJob(d.id, d.product_id, "no_eligible_line", "Không có dây chuyền nào sản xuất được SP này.")
            )
            continue

        any_fit = False
        for lid in eligible_lines:
            tl = timelines[lid]
            hours = _job_hours(dataset, d, lid)
            if hours is None:
                continue
            duration_slots = tl.duration_in_slots(hours)
            ready_from = max(readiness.eta, horizon_start)
            earliest_slot = tl.earliest_slot_at_or_after(ready_from)
            if not tl.fits_within_horizon(earliest_slot, duration_slots):
                continue
            any_fit = True

            key = (d.id, lid)
            latest_start = tl.num_slots() - duration_slots
            start = model.NewIntVar(earliest_slot, latest_start, f"start_{d.id}_{lid}")
            end = model.NewIntVar(earliest_slot + duration_slots, tl.num_slots(), f"end_{d.id}_{lid}")
            assigned = model.NewBoolVar(f"assigned_{d.id}_{lid}")
            interval = model.NewOptionalIntervalVar(
                start, duration_slots, end, assigned, f"iv_{d.id}_{lid}"
            )
            model.Add(end == start + duration_slots)

            due_slot = tl.working_slot_count_before(d.due_date)
            tardiness = model.NewIntVar(0, tl.num_slots(), f"tard_{d.id}_{lid}")
            model.Add(tardiness >= end - due_slot)
            model.Add(tardiness >= 0)
            contribution = model.NewIntVar(0, tl.num_slots(), f"contrib_{d.id}_{lid}")
            model.Add(contribution == tardiness).OnlyEnforceIf(assigned)
            model.Add(contribution == 0).OnlyEnforceIf(assigned.Not())

            assigned_vars[key] = assigned
            start_vars[key] = start
            end_vars[key] = end
            weight = _PRIORITY_WEIGHT[d.priority]
            weighted = model.NewIntVar(0, tl.num_slots() * weight, f"w_{d.id}_{lid}")
            model.Add(weighted == contribution * weight)
            contribution_vars.append(weighted)

            intervals_by_line[lid].append(interval)
            line = lines[lid]
            rate = line.rate_for(d.product_id)
            demands_by_line[lid].append(rate.required_headcount if rate else 0)
            feasible_pairs[d.id].append(lid)

        if not any_fit:
            result.unscheduled.append(
                UnscheduledJob(
                    d.id, d.product_id, "no_capacity_in_horizon",
                    "Không đủ chỗ trống trong horizon lập kế hoạch trên bất kỳ dây chuyền nào.",
                )
            )

    # --- 4) Ràng buộc: mỗi đơn khả thi được gán đúng 1 dây chuyền ---
    for d_id, lids in feasible_pairs.items():
        if not lids:
            continue
        model.Add(sum(assigned_vars[(d_id, lid)] for lid in lids) == 1)

    # --- 5) No-overlap trên mỗi dây chuyền ---
    for lid, ivs in intervals_by_line.items():
        if ivs:
            model.AddNoOverlap(ivs)

    # --- 6) Ràng buộc nhân lực dùng chung (Cumulative) khi lịch giống hệt nhau ---
    pool_to_lines: dict[str, list[str]] = {}
    for line in dataset.lines:
        if line.labor_pool_id:
            pool_to_lines.setdefault(line.labor_pool_id, []).append(line.id)

    for pool_id, member_line_ids in pool_to_lines.items():
        pool = pools.get(pool_id)
        if pool is None:
            continue
        signatures = {lines[lid].calendar.signature() for lid in member_line_ids}
        if len(signatures) > 1:
            result.workforce_warnings.append(
                f"Tổ nhân lực '{pool_id}' dùng chung cho các dây chuyền có LỊCH LÀM VIỆC "
                f"KHÁC NHAU ({member_line_ids}) - hệ thống bỏ qua ràng buộc cứng về nhân lực "
                "cho tổ này (v1 giới hạn), cần kiểm tra thủ công."
            )
            continue
        pooled_intervals: list[cp_model.IntervalVar] = []
        pooled_demands: list[int] = []
        for lid in member_line_ids:
            pooled_intervals.extend(intervals_by_line[lid])
            pooled_demands.extend(demands_by_line[lid])
        if pooled_intervals:
            model.AddCumulative(pooled_intervals, pooled_demands, pool.headcount)

    # --- 7) Mục tiêu: tối thiểu tổng trễ hạn có trọng số ---
    if contribution_vars:
        model.Minimize(sum(contribution_vars))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    result.solver_status = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for d_id, lids in feasible_pairs.items():
            if lids:
                d = next(x for x in schedulable if x.id == d_id)
                result.unscheduled.append(
                    UnscheduledJob(d_id, d.product_id, "solver_infeasible_or_timeout", result.solver_status)
                )
        return result

    for d in schedulable:
        lids = feasible_pairs.get(d.id, [])
        chosen = next((lid for lid in lids if solver.Value(assigned_vars[(d.id, lid)])), None)
        if chosen is None:
            continue
        tl = timelines[chosen]
        start_slot = solver.Value(start_vars[(d.id, chosen)])
        end_slot = solver.Value(end_vars[(d.id, chosen)])
        readiness = material_readiness[d.id]
        result.scheduled.append(
            ScheduledJob(
                demand_line_id=d.id,
                product_id=d.product_id,
                line_id=chosen,
                qty=d.qty,
                start_dt=tl.slot_start_dt(start_slot),
                end_dt=tl.completion_dt(end_slot),
                material_eta=readiness.eta,
                due_date=d.due_date,
            )
        )

    return result
