from datetime import datetime

from planning_solver.mrp import allocate_materials
from planning_solver.models import (
    BOMItem,
    Component,
    DemandLine,
    DemandSource,
    IncomingReceipt,
    OrderPriority,
    PlanningDataset,
    Product,
)

REF = datetime(2026, 1, 1)


def make_product(component_id="C1", qty_per_unit=1.0, scrap_rate=0.0) -> Product:
    return Product(
        id="P1", name="SP1",
        bom=[BOMItem(component_id=component_id, qty_per_unit=qty_per_unit, scrap_rate=scrap_rate)],
    )


def make_demand(id_, qty, priority=OrderPriority.NORMAL, due=datetime(2026, 2, 1)) -> DemandLine:
    return DemandLine(
        id=id_, product_id="P1", qty=qty, due_date=due, priority=priority,
        source=DemandSource.SALES_ORDER, ref_id=id_,
    )


def test_sufficient_stock_ready_immediately():
    comp = Component(id="C1", name="Comp", on_hand_qty=100)
    ds = PlanningDataset(products=[make_product()], components=[comp])
    d = make_demand("D1", 50)
    result = allocate_materials([d], ds, REF)
    assert result["D1"].blocked is False
    assert result["D1"].eta == REF


def test_insufficient_stock_waits_for_receipt():
    receipt_date = datetime(2026, 1, 15)
    comp = Component(
        id="C1", name="Comp", on_hand_qty=30,
        incoming_receipts=[IncomingReceipt(ref="PO1", qty=100, expected_date=receipt_date)],
    )
    ds = PlanningDataset(products=[make_product()], components=[comp])
    d = make_demand("D1", 80)
    result = allocate_materials([d], ds, REF)
    assert result["D1"].blocked is False
    assert result["D1"].eta == receipt_date


def test_safety_stock_not_consumed():
    comp = Component(id="C1", name="Comp", on_hand_qty=100, safety_stock=30)
    ds = PlanningDataset(products=[make_product()], components=[comp])
    # usable = 70, cần 80 -> thiếu, không có receipt -> blocked
    d = make_demand("D1", 80)
    result = allocate_materials([d], ds, REF)
    assert result["D1"].blocked is True
    assert result["D1"].shortages[0].shortfall_qty == 10


def test_priority_order_wins_contested_component():
    comp = Component(id="C1", name="Comp", on_hand_qty=100)
    ds = PlanningDataset(products=[make_product()], components=[comp])
    high = make_demand("HIGH", 80, priority=OrderPriority.HIGH, due=datetime(2026, 3, 1))
    low = make_demand("LOW", 80, priority=OrderPriority.LOW, due=datetime(2026, 1, 5))
    # LOW có due date sớm hơn nhưng priority thấp hơn -> HIGH phải được cấp hàng trước
    result = allocate_materials([low, high], ds, REF)
    assert result["HIGH"].blocked is False
    assert result["LOW"].blocked is True
    assert result["LOW"].shortages[0].shortfall_qty == 60


def test_scrap_rate_increases_requirement():
    comp = Component(id="C1", name="Comp", on_hand_qty=100)
    ds = PlanningDataset(products=[make_product(scrap_rate=0.1)], components=[comp])
    d = make_demand("D1", 100)  # cần 100 * 1.1 = 110 > 100 tồn kho -> thiếu
    result = allocate_materials([d], ds, REF)
    assert result["D1"].blocked is True
    assert abs(result["D1"].shortages[0].shortfall_qty - 10) < 1e-6


def test_missing_component_definition_is_full_shortage():
    ds = PlanningDataset(products=[make_product(component_id="GHOST")], components=[])
    d = make_demand("D1", 10)
    result = allocate_materials([d], ds, REF)
    assert result["D1"].blocked is True
    assert result["D1"].shortages[0].component_id == "GHOST"


def test_product_without_bom_is_immediately_ready():
    ds = PlanningDataset(products=[Product(id="P1", name="SP1")], components=[])
    d = make_demand("D1", 10)
    result = allocate_materials([d], ds, REF)
    assert result["D1"].blocked is False
    assert result["D1"].eta == REF
