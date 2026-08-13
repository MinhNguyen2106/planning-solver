from datetime import datetime, time

from planning_solver.mrp import ComponentShortage, MaterialReadiness
from planning_solver.models import (
    DemandLine,
    DemandSource,
    LineProductRate,
    OrderPriority,
    PlanningDataset,
    Product,
    ProductionLine,
    Shift,
    WorkCalendar,
    WorkforcePool,
)
from planning_solver.scheduler import schedule_production

HORIZON_START = datetime(2026, 8, 10, 0, 0)  # Thứ 2
HORIZON_END = datetime(2026, 8, 24, 0, 0)


def daily_calendar() -> WorkCalendar:
    return WorkCalendar(
        shifts=[Shift(name="Hành chính", weekdays=[0, 1, 2, 3, 4], start=time(8, 0), end=time(16, 0))]
    )


def ready_now(demand_id: str, ref: datetime = HORIZON_START) -> MaterialReadiness:
    return MaterialReadiness(demand_line_id=demand_id, eta=ref, blocked=False)


def test_single_order_schedules_within_calendar():
    line = ProductionLine(
        id="L1", name="Line1", calendar=daily_calendar(),
        product_rates=[LineProductRate(product_id="P1", rate_per_hour=10)],
    )
    ds = PlanningDataset(products=[Product(id="P1", name="SP1")], lines=[line])
    d = DemandLine(
        id="D1", product_id="P1", qty=40, due_date=datetime(2026, 8, 15),
        priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="D1",
    )
    result = schedule_production([d], {"D1": ready_now("D1")}, ds, HORIZON_START, HORIZON_END)
    assert len(result.scheduled) == 1
    job = result.scheduled[0]
    assert job.start_dt.hour >= 8 and job.end_dt.hour <= 16
    assert job.start_dt.weekday() < 5


def test_two_orders_same_line_do_not_overlap():
    line = ProductionLine(
        id="L1", name="Line1", calendar=daily_calendar(),
        product_rates=[LineProductRate(product_id="P1", rate_per_hour=10)],
    )
    ds = PlanningDataset(products=[Product(id="P1", name="SP1")], lines=[line])
    demands = [
        DemandLine(
            id=f"D{i}", product_id="P1", qty=40, due_date=datetime(2026, 8, 20),
            priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id=f"D{i}",
        )
        for i in range(1, 3)
    ]
    readiness = {d.id: ready_now(d.id) for d in demands}
    result = schedule_production(demands, readiness, ds, HORIZON_START, HORIZON_END)
    assert len(result.scheduled) == 2
    j1, j2 = sorted(result.scheduled, key=lambda j: j.start_dt)
    assert j1.end_dt <= j2.start_dt


def test_workforce_pool_serializes_jobs_across_lines_with_same_calendar():
    cal = daily_calendar()
    line_a = ProductionLine(
        id="A", name="Line A", calendar=cal,
        product_rates=[LineProductRate(product_id="PA", rate_per_hour=10, required_headcount=2)],
        labor_pool_id="POOL",
    )
    line_b = ProductionLine(
        id="B", name="Line B", calendar=cal,
        product_rates=[LineProductRate(product_id="PB", rate_per_hour=10, required_headcount=2)],
        labor_pool_id="POOL",
    )
    pool = WorkforcePool(id="POOL", name="Tổ chung", headcount=3)  # 2+2=4 > 3 -> không thể chạy song song
    ds = PlanningDataset(
        products=[Product(id="PA", name="A"), Product(id="PB", name="B")],
        lines=[line_a, line_b],
        workforce_pools=[pool],
    )
    d_a = DemandLine(
        id="DA", product_id="PA", qty=40, due_date=datetime(2026, 8, 20),
        priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="DA",
    )
    d_b = DemandLine(
        id="DB", product_id="PB", qty=40, due_date=datetime(2026, 8, 20),
        priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="DB",
    )
    readiness = {"DA": ready_now("DA"), "DB": ready_now("DB")}
    result = schedule_production([d_a, d_b], readiness, ds, HORIZON_START, HORIZON_END)
    assert len(result.scheduled) == 2
    job_a = next(j for j in result.scheduled if j.demand_line_id == "DA")
    job_b = next(j for j in result.scheduled if j.demand_line_id == "DB")
    # Ràng buộc Cumulative phải buộc 2 lệnh không chạy chồng thời gian
    overlap = job_a.start_dt < job_b.end_dt and job_b.start_dt < job_a.end_dt
    assert not overlap


def test_material_shortage_excludes_order_from_schedule():
    line = ProductionLine(
        id="L1", name="Line1", calendar=daily_calendar(),
        product_rates=[LineProductRate(product_id="P1", rate_per_hour=10)],
    )
    ds = PlanningDataset(products=[Product(id="P1", name="SP1")], lines=[line])
    d = DemandLine(
        id="D1", product_id="P1", qty=40, due_date=datetime(2026, 8, 20),
        priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="D1",
    )
    blocked = MaterialReadiness(
        demand_line_id="D1", eta=None, blocked=True,
        shortages=[ComponentShortage("C1", 5)],
    )
    result = schedule_production([d], {"D1": blocked}, ds, HORIZON_START, HORIZON_END)
    assert result.scheduled == []
    assert result.unscheduled[0].reason == "material_shortage"


def test_no_eligible_line_reports_reason():
    ds = PlanningDataset(products=[Product(id="P1", name="SP1")], lines=[])
    d = DemandLine(
        id="D1", product_id="P1", qty=40, due_date=datetime(2026, 8, 20),
        priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="D1",
    )
    result = schedule_production([d], {"D1": ready_now("D1")}, ds, HORIZON_START, HORIZON_END)
    assert result.unscheduled[0].reason == "no_eligible_line"
