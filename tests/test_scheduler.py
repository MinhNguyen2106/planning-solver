from datetime import datetime, time, timedelta

from planning_solver.mrp import ComponentShortage, MaterialReadiness
from planning_solver.models import (
    ChangeoverRule,
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


# --- Sequence-dependent changeover (AddCircuit) ----------------------------


def _short_calendar() -> WorkCalendar:
    """Ca 4 tiếng (08:00-12:00), dùng để ép khít công suất trong ngày - làm
    cho khoảng changeover (nếu có) trở thành yếu tố QUYẾT ĐỊNH việc có kịp
    hạn hay không, thay vì bị "nhoè" bởi dư thừa thời gian trong horizon dài."""
    return WorkCalendar(
        shifts=[Shift(name="Ca ngắn", weekdays=[0, 1, 2, 3, 4], start=time(8, 0), end=time(12, 0))]
    )


_ONE_DAY_START = datetime(2026, 8, 10, 8, 0)  # Thứ 2, đúng giờ mở ca
_ONE_DAY_END = datetime(2026, 8, 11, 0, 0)  # horizon chỉ đúng 1 ngày - không có ngày làm việc kế tiếp để "trốn" trễ hạn


def test_sequence_changeover_zero_between_same_product_allows_tight_fit():
    # 2 lệnh cùng SP1, mỗi lệnh 2 tiếng (qty=20, rate=10/h) = vừa khít 4 tiếng
    # ca làm việc NẾU không có khoảng trống nào giữa 2 lệnh. changeover phẳng
    # (LineProductRate) cố tình đặt 60 phút (khác 0) để chứng minh luật
    # sequence_changeovers (P1,P1)=0 phút mới là luật thực sự được áp dụng -
    # nếu lỡ dùng nhầm giá trị phẳng, 2 lệnh sẽ không kịp lọt vào ca 4 tiếng.
    line = ProductionLine(
        id="L1", name="Line1", calendar=_short_calendar(),
        product_rates=[LineProductRate(product_id="P1", rate_per_hour=10, changeover_minutes=60)],
        sequence_changeovers=[
            ChangeoverRule(to_product_id="P1", minutes=0),  # lệnh đầu tiên trong ca: 0 phút
            ChangeoverRule(from_product_id="P1", to_product_id="P1", minutes=0),
        ],
    )
    ds = PlanningDataset(products=[Product(id="P1", name="SP1")], lines=[line])
    due = datetime(2026, 8, 10, 12, 0)
    demands = [
        DemandLine(
            id=f"D{i}", product_id="P1", qty=20, due_date=due,
            priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id=f"D{i}",
        )
        for i in range(1, 3)
    ]
    readiness = {d.id: ready_now(d.id, _ONE_DAY_START) for d in demands}
    result = schedule_production(demands, readiness, ds, _ONE_DAY_START, _ONE_DAY_END, slot_minutes=60)

    assert result.solver_status == "OPTIMAL"
    assert len(result.scheduled) == 2
    j1, j2 = sorted(result.scheduled, key=lambda j: j.start_dt)
    assert j1.start_dt == _ONE_DAY_START
    assert j1.end_dt == j2.start_dt  # khít hoàn toàn, không khoảng trống
    assert j2.end_dt <= due  # kịp hạn nhờ không mất thời gian changeover thừa


def test_sequence_changeover_nonzero_forces_measurable_gap_and_tardiness():
    # 2 SẢN PHẨM KHÁC NHAU, changeover đối xứng 60 phút mỗi chiều. Ca làm
    # việc 6 tiếng (08:00-14:00) - ĐỦ CHỖ (6h) để chứa 2h+1h(gap)+2h=5h dù
    # thứ tự nào (không bị chặn cứng bởi domain biến `start`), nhưng HẠN
    # GIAO chỉ đặt ở mốc 4 tiếng (12:00, bằng khung ca ngắn ở test trên) ->
    # solver vẫn xếp được lịch nhưng chắc chắn trễ đúng 1 tiếng (không có
    # thứ tự nào tránh được khoảng trống changeover bắt buộc).
    six_hour_calendar = WorkCalendar(
        shifts=[Shift(name="Ca dài hơn", weekdays=[0, 1, 2, 3, 4], start=time(8, 0), end=time(14, 0))]
    )
    line = ProductionLine(
        id="L1", name="Line1", calendar=six_hour_calendar,
        product_rates=[
            LineProductRate(product_id="PA", rate_per_hour=10),
            LineProductRate(product_id="PB", rate_per_hour=10),
        ],
        sequence_changeovers=[
            ChangeoverRule(to_product_id="PA", minutes=0),
            ChangeoverRule(to_product_id="PB", minutes=0),
            ChangeoverRule(from_product_id="PA", to_product_id="PB", minutes=60),
            ChangeoverRule(from_product_id="PB", to_product_id="PA", minutes=60),
        ],
    )
    ds = PlanningDataset(products=[Product(id="PA", name="A"), Product(id="PB", name="B")], lines=[line])
    due = datetime(2026, 8, 10, 12, 0)
    demands = [
        DemandLine(
            id="DA", product_id="PA", qty=20, due_date=due,
            priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="DA",
        ),
        DemandLine(
            id="DB", product_id="PB", qty=20, due_date=due,
            priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="DB",
        ),
    ]
    readiness = {d.id: ready_now(d.id, _ONE_DAY_START) for d in demands}
    result = schedule_production(demands, readiness, ds, _ONE_DAY_START, _ONE_DAY_END, slot_minutes=60)

    assert result.solver_status == "OPTIMAL"
    assert len(result.scheduled) == 2
    j1, j2 = sorted(result.scheduled, key=lambda j: j.start_dt)
    assert j1.start_dt == _ONE_DAY_START
    assert j2.start_dt - j1.end_dt == timedelta(minutes=60)  # khoảng trống bắt buộc, không thể tránh
    assert j2.end_dt == datetime(2026, 8, 10, 13, 0)
    assert j2.end_dt > due  # trễ đúng 1 tiếng - không thể tránh khỏi với 2 SP khác nhau


def test_sequence_changeover_drives_solver_ordering_choice():
    # Luật BẤT ĐỐI XỨNG: PA->PB tốn 300 phút (rất đắt), PB->PA chỉ tốn 10
    # phút. Với dung lượng ca 12 tiếng (720 phút) và mỗi lệnh tốn 300 phút
    # sản xuất, chỉ có thứ tự "PB trước, PA sau" mới đủ chỗ trong ca (300 +
    # 10(làm tròn 30) + 300 = 630 <= 720); thứ tự ngược lại (300 + 300 + 300
    # = 900 > 720) KHÔNG THỂ nào lọt vào 1 ngày -> solver buộc phải chọn thứ
    # tự PB trước. Đây là hành vi KHÔNG THỂ đạt được với AddNoOverlap cũ
    # (đối xứng, không phân biệt thứ tự sản phẩm).
    cal = WorkCalendar(
        shifts=[Shift(name="Ca dài", weekdays=[0, 1, 2, 3, 4], start=time(8, 0), end=time(20, 0))]
    )
    line = ProductionLine(
        id="L1", name="Line1", calendar=cal,
        product_rates=[
            LineProductRate(product_id="PA", rate_per_hour=10),
            LineProductRate(product_id="PB", rate_per_hour=10),
        ],
        sequence_changeovers=[
            ChangeoverRule(to_product_id="PA", minutes=0),
            ChangeoverRule(to_product_id="PB", minutes=0),
            ChangeoverRule(from_product_id="PA", to_product_id="PB", minutes=300),
            ChangeoverRule(from_product_id="PB", to_product_id="PA", minutes=10),
        ],
    )
    ds = PlanningDataset(products=[Product(id="PA", name="A"), Product(id="PB", name="B")], lines=[line])
    due = datetime(2026, 8, 10, 20, 0)
    demands = [
        DemandLine(
            id="DA", product_id="PA", qty=50, due_date=due,
            priority=OrderPriority.HIGH, source=DemandSource.SALES_ORDER, ref_id="DA",
        ),
        DemandLine(
            id="DB", product_id="PB", qty=50, due_date=due,
            priority=OrderPriority.HIGH, source=DemandSource.SALES_ORDER, ref_id="DB",
        ),
    ]
    readiness = {d.id: ready_now(d.id, _ONE_DAY_START) for d in demands}
    result = schedule_production(demands, readiness, ds, _ONE_DAY_START, _ONE_DAY_END, slot_minutes=30)

    assert result.solver_status == "OPTIMAL"
    assert len(result.scheduled) == 2
    job_pa = next(j for j in result.scheduled if j.product_id == "PA")
    job_pb = next(j for j in result.scheduled if j.product_id == "PB")
    assert job_pb.start_dt < job_pa.start_dt  # buộc phải chạy PB trước PA
    assert job_pa.start_dt - job_pb.end_dt >= timedelta(minutes=30)  # 10 phút làm tròn lên 1 slot(30ph)
    assert job_pa.end_dt <= due and job_pb.end_dt <= due  # cả 2 vẫn kịp hạn


def test_line_without_sequence_changeovers_uses_flat_changeover_for_every_job():
    # Dây chuyền KHÔNG khai báo sequence_changeovers -> mỗi lệnh vẫn dùng
    # đúng công thức cũ (qty/rate + changeover_minutes/60), thời lượng CỦA
    # MỖI LỆNH không phụ thuộc lệnh nào chạy trước nó - chứng minh code path
    # cũ hoàn toàn không bị đụng tới.
    line = ProductionLine(
        id="L1", name="Line1", calendar=daily_calendar(),
        product_rates=[
            LineProductRate(product_id="P1", rate_per_hour=10, changeover_minutes=15),
            LineProductRate(product_id="P2", rate_per_hour=10, changeover_minutes=25),
        ],
    )
    ds = PlanningDataset(products=[Product(id="P1", name="A"), Product(id="P2", name="B")], lines=[line])
    # qty nhỏ để CẢ 2 lệnh gọn trong 1 ca 8 tiếng (daily_calendar 08:00-16:00)
    # dù thứ tự nào - tránh tràn sang ngày kế (nếu tràn, end_dt-start_dt sẽ
    # cộng luôn khoảng nghỉ qua đêm, làm sai lệch phép so sánh thời lượng).
    demands = [
        DemandLine(
            id="D1", product_id="P1", qty=25, due_date=datetime(2026, 8, 20),
            priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="D1",
        ),
        DemandLine(
            id="D2", product_id="P2", qty=15, due_date=datetime(2026, 8, 20),
            priority=OrderPriority.NORMAL, source=DemandSource.SALES_ORDER, ref_id="D2",
        ),
    ]
    readiness = {d.id: ready_now(d.id) for d in demands}
    result = schedule_production(demands, readiness, ds, HORIZON_START, HORIZON_END)

    assert len(result.scheduled) == 2
    job_p1 = next(j for j in result.scheduled if j.product_id == "P1")
    job_p2 = next(j for j in result.scheduled if j.product_id == "P2")
    # hours = 25/10 + 15/60 = 2.75 -> ceil lên 3 tiếng (slot 60 phút)
    assert job_p1.end_dt - job_p1.start_dt == timedelta(hours=3)
    # hours = 15/10 + 25/60 = 1.9167 -> ceil lên 2 tiếng
    assert job_p2.end_dt - job_p2.start_dt == timedelta(hours=2)


def test_workforce_cumulative_still_works_with_sequenced_line():
    cal = daily_calendar()
    line_a = ProductionLine(
        id="A", name="Line A", calendar=cal,
        product_rates=[LineProductRate(product_id="PA", rate_per_hour=10, required_headcount=2)],
        labor_pool_id="POOL",
        sequence_changeovers=[ChangeoverRule(to_product_id="PA", minutes=0)],  # opt-in vào AddCircuit
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
    overlap = job_a.start_dt < job_b.end_dt and job_b.start_dt < job_a.end_dt
    assert not overlap
