"""Gộp PO (đơn hàng khách - nhu cầu chốt) và Forecast (dự báo - nhu cầu chưa
chốt) thành danh sách DemandLine thống nhất để đưa vào MRP + lập lịch.

Quy đổi cam kết giao hàng của PO ra ETD (commitment_etd):
  PO khai báo MỘT trong hai mốc (xem models.SalesOrder):
    - `requested_etd` (hàng RỜI XƯỞNG) -> dùng thẳng.
    - `requested_eta` (hàng ĐẾN TAY khách) -> trừ đi `transit_lead_time_days`
      tra từ `Customer` (master data khách hàng/tuyến giao hàng) để ra ETD.
  ETD này chính là `DemandLine.due_date` - mục tiêu MỀM mà sản xuất phải
  hoàn thành hàng trước đó (đưa vào hàm mục tiêu của scheduler, không phải
  ràng buộc cứng loại đơn khỏi kế hoạch).

  Forecast không có ETA/ETD riêng (dự báo, chưa chốt khách/tuyến giao hàng)
  nên dùng thẳng `period_end` làm due_date.

Quy tắc netting (tránh đếm trùng nhu cầu):
  1. Tồn kho thành phẩm (finished goods on-hand) được trừ trước vào các
     SalesOrder có ETD SỚM NHẤT trước (FIFO theo due date), vì hàng tồn
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

from datetime import datetime, timedelta

from .models import (
    PRIORITY_RANK,
    Customer,
    DemandLine,
    DemandSource,
    OrderPriority,
    PlanningDataset,
    SalesOrder,
)


def commitment_etd(so: SalesOrder, customers: dict[str, Customer]) -> datetime:
    """Quy đổi cam kết giao hàng của một PO ra ETD tại xưởng (thời điểm sản
    xuất phải hoàn thành hàng để kịp giao)."""
    if so.requested_etd is not None:
        return so.requested_etd
    customer = customers.get(so.customer_id)
    lead_days = customer.transit_lead_time_days if customer else 0.0
    # so.requested_eta chắc chắn khác None ở đây (đã validate ở SalesOrder).
    return so.requested_eta - timedelta(days=lead_days)  # type: ignore[operator]


def _apply_finished_goods(
    sales_orders: list[SalesOrder],
    etd_by_so: dict[str, datetime],
    fg_on_hand: dict[str, float],
) -> list[tuple[SalesOrder, float]]:
    """Trừ tồn kho thành phẩm vào các đơn hàng theo thứ tự ETD sớm nhất.
    Trả về list (order, qty_to_produce) - qty_to_produce có thể = 0 nếu tồn
    kho đủ cover toàn bộ đơn."""
    remaining = dict(fg_on_hand)
    result: list[tuple[SalesOrder, float]] = []
    for so in sorted(sales_orders, key=lambda o: (etd_by_so[o.id], PRIORITY_RANK[o.priority])):
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
    customers = dataset.customer_map()
    fg_on_hand = {i.product_id: i.on_hand_qty for i in dataset.finished_goods_inventory}

    etd_by_so = {so.id: commitment_etd(so, customers) for so in dataset.sales_orders}

    demand_lines: list[DemandLine] = []

    # 1) PO (Sales Orders) - nhu cầu chốt, ưu tiên trừ tồn kho trước
    so_net = _apply_finished_goods(dataset.sales_orders, etd_by_so, fg_on_hand)
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
                due_date=etd_by_so[so.id],
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
            and fc.period_start <= etd_by_so[so.id] < fc.period_end
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
