from datetime import datetime, time

import pytest
from pydantic import ValidationError

from planning_solver.demand import build_demand_lines, build_mts_replenishment_lines, commitment_etd
from planning_solver.models import (
    Customer,
    DateCountMode,
    DemandSource,
    FinishedGoodsInventory,
    ForecastEntry,
    OrderPriority,
    PlanningDataset,
    PlanningStrategy,
    Product,
    SalesOrder,
    Shift,
    WorkCalendar,
)


def base_product(**kwargs) -> Product:
    return Product(id="P1", name="SP1", **kwargs)


def so_etd(id_, etd, **kwargs) -> SalesOrder:
    return SalesOrder(
        id=id_, customer_id="C1", product_id="P1", qty=kwargs.pop("qty", 100),
        order_date=datetime(2026, 1, 1), requested_etd=etd, **kwargs,
    )


def test_requested_etd_used_directly():
    so = so_etd("SO1", datetime(2026, 2, 5))
    assert commitment_etd(so, {}) == datetime(2026, 2, 5)


def test_requested_eta_converted_using_customer_transit_lead_time():
    so = SalesOrder(
        id="SO1", customer_id="CUST-A", product_id="P1", qty=100,
        order_date=datetime(2026, 1, 1), requested_eta=datetime(2026, 2, 10),
    )
    customers = {"CUST-A": Customer(id="CUST-A", name="A", transit_lead_time_days=3)}
    assert commitment_etd(so, customers) == datetime(2026, 2, 7)


def test_requested_eta_converted_using_calendar_days_ignores_weekend():
    # 2026-02-10 là Thứ 3. Trừ 3 NGÀY LỊCH -> 2026-02-07 (Thứ 7), không quan
    # tâm hôm đó là cuối tuần.
    so = SalesOrder(
        id="SO1", customer_id="CUST-A", product_id="P1", qty=100,
        order_date=datetime(2026, 1, 1), requested_eta=datetime(2026, 2, 10),
    )
    customers = {
        "CUST-A": Customer(
            id="CUST-A", name="A", transit_lead_time_days=3,
            transit_lead_time_mode=DateCountMode.CALENDAR_DAYS,
        )
    }
    assert commitment_etd(so, customers) == datetime(2026, 2, 7)


def test_requested_eta_converted_using_working_days_skips_weekend():
    # 2026-02-10 là Thứ 3. Trừ 3 NGÀY LÀM VIỆC (lịch mặc định T2-T7) phải
    # nhảy qua cuối tuần liền trước -> kết quả 2026-02-06 (Thứ 6), không
    # phải 2026-02-07 như calendar_days.
    so = SalesOrder(
        id="SO1", customer_id="CUST-A", product_id="P1", qty=100,
        order_date=datetime(2026, 1, 1), requested_eta=datetime(2026, 2, 10),
    )
    customers = {
        "CUST-A": Customer(
            id="CUST-A", name="A", transit_lead_time_days=3,
            transit_lead_time_mode=DateCountMode.WORKING_DAYS,
        )
    }
    assert commitment_etd(so, customers) == datetime(2026, 2, 6)


def test_working_days_mode_uses_explicit_logistics_calendar_when_given():
    # Lịch riêng chỉ làm việc Thứ 4 -> lùi 1 ngày làm việc từ Thứ 3 10/2 phải
    # nhảy về tận Thứ 4 tuần trước (2026-02-04).
    so = SalesOrder(
        id="SO1", customer_id="CUST-A", product_id="P1", qty=100,
        order_date=datetime(2026, 1, 1), requested_eta=datetime(2026, 2, 10),
    )
    customers = {
        "CUST-A": Customer(
            id="CUST-A", name="A", transit_lead_time_days=1,
            transit_lead_time_mode=DateCountMode.WORKING_DAYS,
        )
    }
    only_wednesday = WorkCalendar(
        shifts=[Shift(name="W", weekdays=[2], start=time(0, 0), end=time(23, 59))]
    )
    assert commitment_etd(so, customers, only_wednesday) == datetime(2026, 2, 4)


def test_requested_etd_takes_precedence_over_eta():
    so = SalesOrder(
        id="SO1", customer_id="CUST-A", product_id="P1", qty=100,
        order_date=datetime(2026, 1, 1),
        requested_eta=datetime(2026, 2, 10), requested_etd=datetime(2026, 2, 1),
    )
    customers = {"CUST-A": Customer(id="CUST-A", name="A", transit_lead_time_days=3)}
    assert commitment_etd(so, customers) == datetime(2026, 2, 1)


def test_missing_eta_and_etd_is_rejected():
    with pytest.raises(ValidationError):
        SalesOrder(
            id="SO1", customer_id="C1", product_id="P1", qty=100,
            order_date=datetime(2026, 1, 1),
        )


def test_missing_customer_master_data_defaults_to_zero_lead_time():
    so = SalesOrder(
        id="SO1", customer_id="UNKNOWN", product_id="P1", qty=100,
        order_date=datetime(2026, 1, 1), requested_eta=datetime(2026, 2, 10),
    )
    assert commitment_etd(so, {}) == datetime(2026, 2, 10)


def test_finished_goods_netted_against_earliest_due_order_first():
    p = base_product()
    so_early = so_etd("SO-EARLY", datetime(2026, 1, 10))
    so_late = so_etd("SO-LATE", datetime(2026, 1, 20))
    ds = PlanningDataset(
        products=[p],
        sales_orders=[so_late, so_early],
        finished_goods_inventory=[FinishedGoodsInventory(product_id="P1", on_hand_qty=100)],
    )
    lines = build_demand_lines(ds, reference_start=datetime(2026, 1, 1))
    by_ref = {l.ref_id: l for l in lines}
    # Tồn kho 100 phải được trừ hết vào đơn giao sớm nhất (SO-EARLY) -> đơn này biến mất khỏi demand
    assert "SO-EARLY" not in by_ref
    assert by_ref["SO-LATE"].qty == 100


def test_forecast_netted_against_po_in_same_period():
    p = base_product()
    so = so_etd("SO1", datetime(2026, 2, 5), qty=300)
    fc = ForecastEntry(
        id="FC1", product_id="P1", qty=1000,
        period_start=datetime(2026, 2, 1), period_end=datetime(2026, 2, 28),
    )
    ds = PlanningDataset(products=[p], sales_orders=[so], forecasts=[fc])
    lines = build_demand_lines(ds, reference_start=datetime(2026, 1, 1))
    fc_line = next(l for l in lines if l.source == DemandSource.FORECAST)
    # 1000 dự báo - 300 đã có PO trong kỳ = 700 còn lại
    assert fc_line.qty == 700


def test_forecast_fully_absorbed_by_po_disappears():
    p = base_product()
    so = so_etd("SO1", datetime(2026, 2, 5), qty=1000)
    fc = ForecastEntry(
        id="FC1", product_id="P1", qty=500,
        period_start=datetime(2026, 2, 1), period_end=datetime(2026, 2, 28),
    )
    ds = PlanningDataset(products=[p], sales_orders=[so], forecasts=[fc])
    lines = build_demand_lines(ds, reference_start=datetime(2026, 1, 1))
    assert all(l.source != DemandSource.FORECAST for l in lines)


def test_lot_size_rounding_applied():
    p = base_product(min_lot_size=50, lot_size_multiple=50)
    so = so_etd("SO1", datetime(2026, 2, 5), qty=101)
    ds = PlanningDataset(products=[p], sales_orders=[so])
    lines = build_demand_lines(ds, reference_start=datetime(2026, 1, 1))
    assert lines[0].qty == 150


# --- Make-to-Stock (MTS) ---------------------------------------------------


def mts_product(**kwargs) -> Product:
    kwargs.setdefault("reorder_point", 100)
    kwargs.setdefault("target_stock_level", 500)
    return Product(
        id="P1", name="SP1", planning_strategy=PlanningStrategy.MAKE_TO_STOCK, **kwargs
    )


def test_mts_target_must_exceed_reorder_point():
    with pytest.raises(ValidationError):
        Product(
            id="P1", name="SP1", planning_strategy=PlanningStrategy.MAKE_TO_STOCK,
            reorder_point=200, target_stock_level=100,
        )


def test_mts_stock_already_below_reorder_point_triggers_immediate_replenishment():
    p = mts_product()
    ref = datetime(2026, 1, 1)
    lines = build_mts_replenishment_lines(
        p, sales_orders=[], forecasts=[], etd_by_so={}, current_on_hand=50, reference_start=ref
    )
    assert len(lines) == 1
    line = lines[0]
    assert line.source == DemandSource.REPLENISHMENT
    assert line.due_date == ref
    assert line.qty == 450  # 500 (target) - 50 (tồn hiện tại)
    assert line.priority == OrderPriority.NORMAL


def test_mts_stock_above_reorder_point_triggers_nothing():
    p = mts_product()
    lines = build_mts_replenishment_lines(
        p, sales_orders=[], forecasts=[], etd_by_so={},
        current_on_hand=300, reference_start=datetime(2026, 1, 1),
    )
    assert lines == []


def test_mts_consumption_events_trigger_replenishment_at_correct_date():
    p = mts_product()  # reorder_point=100, target=500
    so1 = so_etd("SO1", datetime(2026, 2, 1), qty=50)
    so2 = so_etd("SO2", datetime(2026, 2, 10), qty=30)
    etd_by_so = {"SO1": datetime(2026, 2, 1), "SO2": datetime(2026, 2, 10)}
    # on_hand=120: sau SO1 (120-50=70, vẫn > 100? không, 70 < 100 -> phải trigger ở SO1)
    lines = build_mts_replenishment_lines(
        p, sales_orders=[so1, so2], forecasts=[], etd_by_so=etd_by_so,
        current_on_hand=120, reference_start=datetime(2026, 1, 1),
    )
    assert len(lines) == 1
    assert lines[0].due_date == datetime(2026, 2, 1)
    assert lines[0].qty == 500 - 70  # đưa tồn kho (70) về target (500)


def test_mts_multiple_triggers_over_time():
    p = mts_product(reorder_point=50, target_stock_level=200)
    events = [
        so_etd("SO1", datetime(2026, 2, 1), qty=170),  # 200-170=30 <= 50 -> trigger, stock=200
        so_etd("SO2", datetime(2026, 3, 1), qty=160),  # 200-160=40 <= 50 -> trigger lần 2, stock=200
    ]
    etd_by_so = {"SO1": datetime(2026, 2, 1), "SO2": datetime(2026, 3, 1)}
    lines = build_mts_replenishment_lines(
        p, sales_orders=events, forecasts=[], etd_by_so=etd_by_so,
        current_on_hand=200, reference_start=datetime(2026, 1, 1),
    )
    assert len(lines) == 2
    assert lines[0].due_date == datetime(2026, 2, 1)
    assert lines[0].qty == 200 - 30
    assert lines[1].due_date == datetime(2026, 3, 1)
    assert lines[1].qty == 200 - 40


def test_mts_forecast_netted_against_po_before_consumption():
    p = mts_product(reorder_point=900, target_stock_level=1000)  # dễ trigger
    so = so_etd("SO1", datetime(2026, 2, 5), qty=300)
    fc = ForecastEntry(
        id="FC1", product_id="P1", qty=1000,
        period_start=datetime(2026, 2, 1), period_end=datetime(2026, 2, 28),
    )
    etd_by_so = {"SO1": datetime(2026, 2, 5)}
    lines = build_mts_replenishment_lines(
        p, sales_orders=[so], forecasts=[fc], etd_by_so=etd_by_so,
        current_on_hand=1000, reference_start=datetime(2026, 1, 1),
    )
    # SO tiêu thụ 300 (còn 700, > reorder_point=900? không -> trigger ngay ở SO)
    # rồi forecast net còn 1000-300=700 tiêu thụ tiếp ở period_end
    assert len(lines) == 2
    assert lines[1].due_date == datetime(2026, 2, 28)


def test_build_demand_lines_mts_product_has_no_individual_so_line():
    mts = mts_product()
    mto = Product(id="P2", name="SP2")
    so_mts = SalesOrder(
        id="SO-MTS", customer_id="C1", product_id="P1", qty=10,
        order_date=datetime(2026, 1, 1), requested_etd=datetime(2026, 2, 5),
    )
    so_mto = SalesOrder(
        id="SO-MTO", customer_id="C1", product_id="P2", qty=10,
        order_date=datetime(2026, 1, 1), requested_etd=datetime(2026, 2, 5),
    )
    ds = PlanningDataset(
        products=[mts, mto],
        sales_orders=[so_mts, so_mto],
        finished_goods_inventory=[FinishedGoodsInventory(product_id="P1", on_hand_qty=50)],
    )
    lines = build_demand_lines(ds, reference_start=datetime(2026, 1, 1))
    sources_by_ref = {l.ref_id: l.source for l in lines}
    assert "SO-MTS" not in sources_by_ref  # PO của SP MTS không sinh dòng riêng
    assert sources_by_ref["SO-MTO"] == DemandSource.SALES_ORDER
    assert any(l.source == DemandSource.REPLENISHMENT and l.product_id == "P1" for l in lines)
