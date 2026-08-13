from datetime import datetime

from planning_solver.demand import build_demand_lines
from planning_solver.models import (
    DemandSource,
    FinishedGoodsInventory,
    ForecastEntry,
    PlanningDataset,
    Product,
    SalesOrder,
)


def base_product(**kwargs) -> Product:
    return Product(id="P1", name="SP1", **kwargs)


def test_finished_goods_netted_against_earliest_due_order_first():
    p = base_product()
    so_early = SalesOrder(
        id="SO-EARLY", customer="A", product_id="P1", qty=100,
        order_date=datetime(2026, 1, 1), requested_ship_date=datetime(2026, 1, 10),
    )
    so_late = SalesOrder(
        id="SO-LATE", customer="B", product_id="P1", qty=100,
        order_date=datetime(2026, 1, 1), requested_ship_date=datetime(2026, 1, 20),
    )
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
    so = SalesOrder(
        id="SO1", customer="A", product_id="P1", qty=300,
        order_date=datetime(2026, 1, 1), requested_ship_date=datetime(2026, 2, 5),
    )
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
    so = SalesOrder(
        id="SO1", customer="A", product_id="P1", qty=1000,
        order_date=datetime(2026, 1, 1), requested_ship_date=datetime(2026, 2, 5),
    )
    fc = ForecastEntry(
        id="FC1", product_id="P1", qty=500,
        period_start=datetime(2026, 2, 1), period_end=datetime(2026, 2, 28),
    )
    ds = PlanningDataset(products=[p], sales_orders=[so], forecasts=[fc])
    lines = build_demand_lines(ds)
    assert all(l.source != DemandSource.FORECAST for l in lines)


def test_lot_size_rounding_applied():
    p = base_product(min_lot_size=50, lot_size_multiple=50)
    so = SalesOrder(
        id="SO1", customer="A", product_id="P1", qty=101,
        order_date=datetime(2026, 1, 1), requested_ship_date=datetime(2026, 2, 5),
    )
    ds = PlanningDataset(products=[p], sales_orders=[so])
    lines = build_demand_lines(ds)
    assert lines[0].qty == 150
