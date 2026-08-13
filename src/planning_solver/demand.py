"""Gộp PO (đơn hàng khách - nhu cầu chốt) và Forecast (dự báo - nhu cầu chưa
chốt) thành danh sách DemandLine thống nhất để đưa vào MRP + lập lịch.

Mỗi sản phẩm đi theo MỘT trong hai chiến lược (`Product.planning_strategy`,
xem `models.PlanningStrategy`):

  MAKE_TO_ORDER (mặc định) - mỗi PO/kỳ Forecast sinh ra MỘT lệnh sản xuất
    riêng, hạn giao = ETD mục tiêu của chính PO/Forecast đó. Đây là logic
    gốc của hệ thống (mục "1) MTO" bên dưới).

  MAKE_TO_STOCK - PO/Forecast của sản phẩm này KHÔNG sinh lệnh sản xuất
    riêng lẻ; chúng chỉ là các sự kiện TIÊU THỤ tồn kho thành phẩm. Hệ
    thống mô phỏng tồn kho dự kiến theo thời gian (time-phased) và tự sinh
    lệnh BỔ SUNG TỒN KHO (`DemandSource.REPLENISHMENT`) mỗi khi tồn kho dự
    kiến chạm `reorder_point`, đủ số lượng để đưa tồn kho về
    `target_stock_level` (mục "2) MTS" bên dưới). Cách này phù hợp với sản
    phẩm sản xuất sẵn để bán ngay từ kho, không gắn với một đơn hàng cụ thể.

Quy đổi cam kết giao hàng của PO ra ETD (commitment_etd) - áp dụng như nhau
cho PO của cả sản phẩm MTO lẫn MTS (khách hàng luôn cần biết ETA/ETD dù nhà
máy sản xuất theo kiểu gì):
  PO khai báo MỘT trong hai mốc (xem models.SalesOrder):
    - `requested_etd` (hàng RỜI XƯỞNG) -> dùng thẳng.
    - `requested_eta` (hàng ĐẾN TAY khách) -> trừ đi `transit_lead_time_days`
      tra từ `Customer` (master data khách hàng/tuyến giao hàng) để ra ETD.
      Phép trừ này có THỂ CHỌN 1 trong 2 cách đếm ngày, theo
      `Customer.transit_lead_time_mode` (xem models.DateCountMode):
        - `calendar_days` (mặc định): trừ thẳng theo ngày lịch, tính cả
          cuối tuần/ngày lễ.
        - `working_days`: trừ theo NGÀY LÀM VIỆC, bỏ qua ngày không làm
          việc (cuối tuần + ngày lễ) theo `PlanningDataset.logistics_calendar`
          (hoặc lịch mặc định T2-T7 nếu không khai báo) - xem
          `calendar.subtract_working_days`.

Quy tắc netting chung (tránh đếm trùng nhu cầu):
  - Forecast của một sản phẩm trong một giai đoạn được "net" trừ đi tổng số
    lượng SalesOrder rơi vào đúng giai đoạn đó của cùng sản phẩm - phần dự
    báo đã được PO "hiện thực hoá" thì không tính thêm nữa.
    forecast_còn_lại = max(0, forecast_qty - tổng PO cùng sản phẩm trong kỳ)
  - Số lượng cuối cùng được làm tròn theo min_lot_size / lot_size_multiple
    của sản phẩm.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .calendar import default_business_calendar, subtract_working_days
from .models import (
    PRIORITY_RANK,
    Customer,
    DateCountMode,
    DemandLine,
    DemandSource,
    ForecastEntry,
    OrderPriority,
    PlanningDataset,
    PlanningStrategy,
    Product,
    SalesOrder,
    WorkCalendar,
)


def commitment_etd(
    so: SalesOrder,
    customers: dict[str, Customer],
    logistics_calendar: WorkCalendar | None = None,
) -> datetime:
    """Quy đổi cam kết giao hàng của một PO ra ETD tại xưởng (thời điểm sản
    xuất phải hoàn thành hàng để kịp giao). `logistics_calendar` chỉ được
    dùng khi khách hàng có `transit_lead_time_mode = working_days`; nếu
    không truyền, dùng lịch mặc định T2-T7 (`default_business_calendar`)."""
    if so.requested_etd is not None:
        return so.requested_etd

    customer = customers.get(so.customer_id)
    lead_days = customer.transit_lead_time_days if customer else 0.0
    mode = customer.transit_lead_time_mode if customer else DateCountMode.CALENDAR_DAYS
    # so.requested_eta chắc chắn khác None ở đây (đã validate ở SalesOrder).
    eta = so.requested_eta
    assert eta is not None

    if mode == DateCountMode.WORKING_DAYS:
        calendar = logistics_calendar or default_business_calendar()
        return subtract_working_days(eta, lead_days, calendar)
    return eta - timedelta(days=lead_days)


def _net_forecast_qty(
    fc_product_id: str,
    period_start: datetime,
    period_end: datetime,
    fc_qty: float,
    sales_orders: list[SalesOrder],
    etd_by_so: dict[str, datetime],
) -> float:
    """Số lượng dự báo còn lại sau khi trừ đi các PO cùng sản phẩm rơi vào
    đúng giai đoạn của dự báo (tránh đếm trùng PO + Forecast)."""
    po_qty_in_period = sum(
        so.qty
        for so in sales_orders
        if so.product_id == fc_product_id and period_start <= etd_by_so[so.id] < period_end
    )
    return max(0.0, fc_qty - po_qty_in_period)


def _apply_finished_goods(
    sales_orders: list[SalesOrder],
    etd_by_so: dict[str, datetime],
    fg_on_hand: dict[str, float],
) -> list[tuple[SalesOrder, float]]:
    """[MTO] Trừ tồn kho thành phẩm vào các đơn hàng theo thứ tự ETD sớm
    nhất. Trả về list (order, qty_to_produce) - qty_to_produce có thể = 0
    nếu tồn kho đủ cover toàn bộ đơn."""
    remaining = dict(fg_on_hand)
    result: list[tuple[SalesOrder, float]] = []
    for so in sorted(sales_orders, key=lambda o: (etd_by_so[o.id], PRIORITY_RANK[o.priority])):
        avail = remaining.get(so.product_id, 0.0)
        covered = min(avail, so.qty)
        remaining[so.product_id] = avail - covered
        qty_to_produce = so.qty - covered
        result.append((so, qty_to_produce))
    return result


def build_mts_replenishment_lines(
    product: Product,
    sales_orders: list[SalesOrder],
    forecasts: list[ForecastEntry],
    etd_by_so: dict[str, datetime],
    current_on_hand: float,
    reference_start: datetime,
) -> list[DemandLine]:
    """[MTS] Mô phỏng tồn kho dự kiến theo thời gian cho MỘT sản phẩm
    MAKE_TO_STOCK: `sales_orders`/`forecasts` của sản phẩm này chỉ được coi
    là các SỰ KIỆN TIÊU THỤ (không sinh lệnh sản xuất riêng từng cái). Mỗi
    khi tồn kho dự kiến chạm `reorder_point`, sinh 1 lệnh bổ sung
    (`DemandSource.REPLENISHMENT`) đủ để đưa tồn kho về `target_stock_level`,
    với `due_date` = thời điểm PHẢI CÓ hàng bổ sung (ngày tồn kho dự kiến
    chạm ngưỡng).

    Giả định đơn giản hoá: lệnh bổ sung được coi là "về kho ngay" trong lúc
    mô phỏng (chỉ để tính ĐÚNG SỐ LƯỢNG và THỜI ĐIỂM cần bổ sung); thời gian
    sản xuất thực tế của lệnh đó do scheduler.py quyết định sau, dựa trên
    due_date này như một mục tiêu MỀM giống hệt PO/Forecast của MTO.
    """
    # Gộp mọi sự kiện tiêu thụ (PO + Forecast đã net) thành 1 dòng thời gian.
    events: list[tuple[datetime, float]] = [
        (etd_by_so[so.id], so.qty) for so in sales_orders if so.product_id == product.id
    ]
    for fc in forecasts:
        if fc.product_id != product.id:
            continue
        remaining = _net_forecast_qty(
            fc.product_id, fc.period_start, fc.period_end, fc.qty, sales_orders, etd_by_so
        )
        if remaining > 0:
            events.append((fc.period_end, remaining))
    events.sort(key=lambda e: e[0])

    lines: list[DemandLine] = []
    stock = current_on_hand
    seq = 0

    def trigger(at: datetime) -> None:
        nonlocal stock, seq
        qty = product.round_up_lot(product.target_stock_level - stock)
        if qty <= 0:
            return
        seq += 1
        lines.append(
            DemandLine(
                id=f"MTS-{product.id}-{seq}",
                product_id=product.id,
                qty=qty,
                due_date=at,
                priority=product.replenishment_priority,
                source=DemandSource.REPLENISHMENT,
                ref_id=f"{product.id}#{seq}",
            )
        )
        stock += qty

    # Tồn kho HIỆN TẠI đã chạm/dưới ngưỡng -> cần bổ sung ngay từ bây giờ.
    if stock <= product.reorder_point:
        trigger(reference_start)

    for event_date, qty in events:
        stock -= qty
        if stock <= product.reorder_point:
            trigger(event_date)

    return lines


def build_demand_lines(dataset: PlanningDataset, reference_start: datetime) -> list[DemandLine]:
    """Sinh danh sách DemandLine từ SalesOrder + Forecast, tách theo chiến
    lược MTO/MTS của từng sản phẩm (`Product.planning_strategy`).
    `reference_start` là mốc "hiện tại" dùng để mô phỏng tồn kho cho các sản
    phẩm MTS (thường trùng `planning_start` truyền vào `pipeline.run_planning`)."""
    products = dataset.product_map()
    customers = dataset.customer_map()
    fg_on_hand = {i.product_id: i.on_hand_qty for i in dataset.finished_goods_inventory}

    etd_by_so = {
        so.id: commitment_etd(so, customers, dataset.logistics_calendar)
        for so in dataset.sales_orders
    }

    mts_product_ids = {
        p.id for p in dataset.products if p.planning_strategy == PlanningStrategy.MAKE_TO_STOCK
    }
    # Sản phẩm không có định nghĩa trong `products` (không rõ chiến lược)
    # mặc định đi theo nhánh MTO, giữ nguyên hành vi gốc của hệ thống.
    mto_sales_orders = [so for so in dataset.sales_orders if so.product_id not in mts_product_ids]
    mto_forecasts = [fc for fc in dataset.forecasts if fc.product_id not in mts_product_ids]
    mts_sales_orders = [so for so in dataset.sales_orders if so.product_id in mts_product_ids]
    mts_forecasts = [fc for fc in dataset.forecasts if fc.product_id in mts_product_ids]

    demand_lines: list[DemandLine] = []

    # ============================== 1) MTO ==============================
    # PO - nhu cầu chốt, ưu tiên trừ tồn kho trước
    so_net = _apply_finished_goods(mto_sales_orders, etd_by_so, fg_on_hand)
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

    # Forecast - net trừ đi PO cùng sản phẩm rơi vào cùng giai đoạn
    for fc in mto_forecasts:
        remaining_forecast = _net_forecast_qty(
            fc.product_id, fc.period_start, fc.period_end, fc.qty, mto_sales_orders, etd_by_so
        )
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

    # ============================== 2) MTS ==============================
    # PO/Forecast của sản phẩm MTS KHÔNG sinh lệnh riêng - chỉ là sự kiện
    # tiêu thụ dùng để mô phỏng tồn kho và tự sinh lệnh bổ sung.
    for product_id in mts_product_ids:
        product = products[product_id]
        demand_lines.extend(
            build_mts_replenishment_lines(
                product,
                mts_sales_orders,
                mts_forecasts,
                etd_by_so,
                current_on_hand=fg_on_hand.get(product_id, 0.0),
                reference_start=reference_start,
            )
        )

    return demand_lines
