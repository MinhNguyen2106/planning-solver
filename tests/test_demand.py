from datetime import datetime, time

import pytest
from pydantic import ValidationError

from planning_solver.demand import build_demand_lines, commitment_etd
from planning_solver.models import (
    Customer,
    DateCountMode,
    DemandSource,
    FinishedGoodsInventory,
    ForecastEntry,
    PlanningDataset,
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
    lines = build_demand_lines(ds)
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
    lines = build_demand_lines(ds)
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
    lines = build_demand_lines(ds)
    assert all(l.source != DemandSource.FORECAST for l in lines)


def test_lot_size_rounding_applied():
    p = base_product(min_lot_size=50, lot_size_multiple=50)
    so = so_etd("SO1", datetime(2026, 2, 5), qty=101)
    ds = PlanningDataset(products=[p], sales_orders=[so])
    lines = build_demand_lines(ds)
    assert lines[0].qty == 150
