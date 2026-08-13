"""Gộp PO (đơn hàng khách - nhu cầu chốt) và Forecast (dự báo - nhu cầu chưa
chốt) thành danh sách DemandLine thống nhất để đưa vào MRP + lập lịch.

Quy tắc netting (tránh đếm trùng nhu cầu):
  1. Tồn kho thành phẩm (finished goods on-hand) được trừ trước vào các
     SalesOrder có ngày giao SỚM NHẤT trước (FIFO theo due date), vì hàng tồn
     có thể xuất ngay không cần sản xuất.
  2. Forecast của một sản phẩm trong một giai đoạn được "net" trừ đi tổng số
     lượng SalesOrder rơi vào đúng giai đoạn đó của cùng sản phẩm - phần dự
     báo đã được PO "hiện thực hoá" thì không tính thêm nữa.
     forecast_còn_lại = max(0, forecast_qty - tổng PO cùng sản phẩm trong kỳ)
  3. Số lượng cuối cùng được làm tròn theo min_lot_size / lot_size_multiple
     của sản phẩm (không áp dụng lot-size khi netting, chỉ áp dụng trước khi
     đưa vào lịch sản xuất, để giữ đúng số cần cho khách trong báo cáo demand).
"""
from __future__ import annotations

from .models import (
    PRIORITY_RANK,
    DemandLine,
    DemandSource,
    OrderPriority,
    PlanningDataset,
    SalesOrder,
)


def _apply_finished_goods(
    sales_orders: list[SalesOrder], fg_on_hand: dict[str, float]
) -> list[tuple[SalesOrder, float]]:
    """Trừ tồn kho thành phẩm vào các đơn hàng theo thứ tự due date sớm nhất.
    Trả về list (order, qty_to_produce) - qty_to_produce có thể = 0 nếu tồn
    kho đủ cover toàn bộ đơn."""
    remaining = dict(fg_on_hand)
    result: list[tuple[SalesOrder, float]] = []
    for so in sorted(sales_orders, key=lambda o: (o.requested_ship_date, PRIORITY_RANK[o.priority])):
        avail = remaining.get(so.product_id, 0.0)
        covered = min(avail, so.qty)
        remaining[so.product_id] = avail - covered
        qty_to_produce = so.qty - covered
        result.append((so, qty_to_produce))
    return result


def build_demand_lines(dataset: PlanningDataset) -> list[DemandLine]:
    """Sinh danh sách DemandLine từ SalesOrder + Forecast, đã net tồn kho
    thành phẩm và tránh double-count PO/Forecast."""
    products = dataset.product_map()
    fg_on_hand = {i.product_id: i.on_hand_qty for i in dataset.finished_goods_inventory}

    demand_lines: list[DemandLine] = []

    # 1) PO (Sales Orders) - nhu cầu chốt, ưu tiên trừ tồn kho trước
    so_net = _apply_finished_goods(dataset.sales_orders, fg_on_hand)
    for so, qty in so_net:
        if qty <= 0:
            continue
        product = products.get(so.product_id)
        final_qty = product.round_up_lot(qty) if product else qty
        demand_lines.append(
            DemandLine(
                id=f"SO-{so.id}",
                product_id=so.product_id,
                qty=final_qty,
                due_date=so.requested_ship_date,
                priority=so.priority,
                source=DemandSource.SALES_ORDER,
                ref_id=so.id,
            )
        )

    # 2) Forecast - net trừ đi PO cùng sản phẩm rơi vào cùng giai đoạn
    for fc in dataset.forecasts:
        po_qty_in_period = sum(
            so.qty
            for so in dataset.sales_orders
            if so.product_id == fc.product_id
            and fc.period_start <= so.requested_ship_date < fc.period_end
        )
        remaining_forecast = max(0.0, fc.qty - po_qty_in_period)
        if remaining_forecast <= 0:
            continue
        product = products.get(fc.product_id)
        final_qty = product.round_up_lot(remaining_forecast) if product else remaining_forecast
        demand_lines.append(
            DemandLine(
                id=f"FC-{fc.id}",
                product_id=fc.product_id,
                qty=final_qty,
                due_date=fc.period_end,
                priority=OrderPriority.LOW,
                source=DemandSource.FORECAST,
                ref_id=fc.id,
            )
        )

    return demand_lines
