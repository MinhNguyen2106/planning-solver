"""Mô hình dữ liệu (domain models) cho hệ thống lập kế hoạch sản xuất.

Toàn bộ dữ liệu đầu vào của bài toán được mô tả ở đây bằng Pydantic:

- Sản phẩm (Product) + Định mức nguyên vật liệu (BOM)
- Linh kiện / nguyên vật liệu (Component) + tồn kho + lịch nhập hàng (PO mua hàng)
- Dây chuyền sản xuất (ProductionLine) + lịch làm việc (WorkCalendar)
- Tổ/nhóm nhân lực (WorkforcePool) gắn với dây chuyền
- Đơn hàng khách (SalesOrder / PO) và Dự báo (ForecastEntry)
- Tồn kho thành phẩm (FinishedGoodsInventory)

Các module khác (demand, mrp, scheduler, eta_etd, pipeline) chỉ thao tác trên
các model này, không phụ thuộc vào nguồn dữ liệu cụ thể (JSON, DB, API...).
"""
from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Lịch làm việc (working calendar)
# ---------------------------------------------------------------------------

class Shift(BaseModel):
    """Một ca làm việc lặp lại theo tuần, ví dụ: Ca 1, T2-T7, 06:00-14:00."""

    name: str
    weekdays: list[int] = Field(
        description="Các thứ trong tuần áp dụng ca này, 0=Thứ 2 ... 6=Chủ nhật"
    )
    start: time
    end: time

    @model_validator(mode="after")
    def _check_same_day(self) -> "Shift":
        if self.end <= self.start:
            raise ValueError(
                f"Ca '{self.name}': giờ kết thúc phải sau giờ bắt đầu trong cùng ngày "
                "(ca qua đêm chưa được hỗ trợ ở v1, hãy tách thành 2 ca)."
            )
        if not self.weekdays:
            raise ValueError(f"Ca '{self.name}': phải có ít nhất 1 ngày trong tuần")
        return self


class CalendarException(BaseModel):
    """Ngoại lệ lịch cho một ngày cụ thể: nghỉ lễ, tăng ca cuối tuần, bảo trì..."""

    day: date
    working: bool = Field(description="False = nghỉ (lễ/bảo trì), True = ngày làm thêm")
    shifts: Optional[list[Shift]] = Field(
        default=None,
        description="Nếu working=True và có set, dùng ca này thay cho ca mặc định của ngày đó",
    )


class WorkCalendar(BaseModel):
    """Lịch làm việc: tập ca lặp lại theo tuần + danh sách ngoại lệ theo ngày."""

    shifts: list[Shift] = Field(default_factory=list)
    exceptions: list[CalendarException] = Field(default_factory=list)

    def signature(self) -> str:
        """Chuỗi định danh nội dung lịch, dùng để gộp các dây chuyền có lịch giống hệt
        nhau (phục vụ ràng buộc nhân lực dùng chung - xem scheduler.py)."""
        parts = sorted(
            f"{s.name}:{sorted(s.weekdays)}:{s.start}:{s.end}" for s in self.shifts
        )
        exc_parts = sorted(
            f"{e.day}:{e.working}:{sorted(sh.name for sh in (e.shifts or []))}"
            for e in self.exceptions
        )
        return "|".join(parts) + "##" + "|".join(exc_parts)


# ---------------------------------------------------------------------------
# Linh kiện / nguyên vật liệu & tồn kho
# ---------------------------------------------------------------------------

class IncomingReceipt(BaseModel):
    """Một lô hàng linh kiện sắp về (PO mua hàng đã đặt, hoặc lịch sản xuất nội bộ)."""

    ref: str = Field(description="Số PO mua hàng / mã lô")
    qty: float = Field(gt=0)
    expected_date: datetime


class Component(BaseModel):
    """Linh kiện / nguyên vật liệu đầu vào cho sản xuất."""

    id: str
    name: str
    uom: str = "pcs"
    on_hand_qty: float = Field(default=0, ge=0, description="Tồn kho hiện tại")
    safety_stock: float = Field(default=0, ge=0, description="Tồn kho an toàn, không được đụng vào khi tính ETA")
    lead_time_days: float = Field(default=0, ge=0, description="Lead time đặt hàng mới nếu thiếu hàng")
    incoming_receipts: list[IncomingReceipt] = Field(default_factory=list)

    def usable_on_hand(self) -> float:
        return max(0.0, self.on_hand_qty - self.safety_stock)


class BOMItem(BaseModel):
    """Một dòng định mức nguyên vật liệu: cần bao nhiêu component cho 1 đơn vị sản phẩm."""

    component_id: str
    qty_per_unit: float = Field(gt=0)
    scrap_rate: float = Field(default=0.0, ge=0, lt=1, description="Tỷ lệ hao hụt, 0.02 = 2%")


# ---------------------------------------------------------------------------
# Sản phẩm & dây chuyền
# ---------------------------------------------------------------------------

class LineProductRate(BaseModel):
    """Năng suất của một dây chuyền khi sản xuất một sản phẩm cụ thể."""

    product_id: str
    rate_per_hour: float = Field(gt=0, description="Số lượng sản phẩm/giờ khi dây chuyền chạy")
    changeover_minutes: float = Field(
        default=0, ge=0, description="Thời gian chuyển đổi (setup) khi bắt đầu lệnh sản xuất này"
    )
    required_headcount: int = Field(default=0, ge=0, description="Số nhân công cần để vận hành dây chuyền cho SP này")


class Product(BaseModel):
    id: str
    name: str
    uom: str = "pcs"
    bom: list[BOMItem] = Field(default_factory=list)
    min_lot_size: float = Field(default=1, gt=0)
    lot_size_multiple: float = Field(default=1, gt=0)
    post_production_buffer_hours: float = Field(
        default=0, ge=0, description="Thời gian QC/đóng gói sau khi sản xuất xong trước khi có thể xuất (ETD)"
    )

    def round_up_lot(self, qty: float) -> float:
        """Làm tròn số lượng theo min lot size & bội số lô."""
        if qty <= 0:
            return 0.0
        qty = max(qty, self.min_lot_size)
        n = -(-qty // self.lot_size_multiple)  # ceil
        return n * self.lot_size_multiple


class ProductionLine(BaseModel):
    id: str
    name: str
    calendar: WorkCalendar
    product_rates: list[LineProductRate] = Field(default_factory=list)
    labor_pool_id: Optional[str] = None

    def rate_for(self, product_id: str) -> Optional[LineProductRate]:
        return next((r for r in self.product_rates if r.product_id == product_id), None)

    def eligible_products(self) -> set[str]:
        return {r.product_id for r in self.product_rates}


class WorkforcePool(BaseModel):
    """Tổ/nhóm nhân lực dùng chung cho một hoặc nhiều dây chuyền có cùng lịch làm việc."""

    id: str
    name: str
    headcount: int = Field(gt=0)


# ---------------------------------------------------------------------------
# Nhu cầu: PO (đơn hàng bán) & Forecast
# ---------------------------------------------------------------------------

class OrderPriority(str, Enum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


PRIORITY_RANK: dict["OrderPriority", int] = {
    OrderPriority.HIGH: 0,
    OrderPriority.NORMAL: 1,
    OrderPriority.LOW: 2,
}
"""Thứ hạng ưu tiên dùng để sắp xếp/so sánh (số nhỏ hơn = ưu tiên cao hơn).
OrderPriority là string Enum (để dữ liệu JSON dễ đọc: "HIGH"/"NORMAL"/"LOW"),
nên mọi so sánh thứ tự phải tra qua bảng này thay vì dùng .value trực tiếp."""


class Customer(BaseModel):
    """Dữ liệu chủ (master data) khách hàng / tuyến giao hàng - dùng để quy
    đổi ETA (hàng đến tay khách) <-> ETD (hàng rời xưởng) cho các PO chỉ khai
    báo ETA."""

    id: str
    name: str
    transit_lead_time_days: float = Field(
        default=0,
        ge=0,
        description="Thời gian vận chuyển tiêu chuẩn từ xưởng đến khách hàng/tuyến này (ngày)",
    )


class SalesOrder(BaseModel):
    """Đơn hàng bán / PO của khách hàng - nhu cầu đã CHỐT (firm demand).

    Cam kết giao hàng của một PO được khai báo bằng MỘT trong hai mốc:
      - `requested_etd`: khách/hợp đồng đã CHỐT SẴN ngày hàng phải RỜI XƯỞNG
        (Estimated Time of Departure) -> dùng thẳng, không cần quy đổi.
      - `requested_eta`: khách chỉ yêu cầu ngày hàng phải ĐẾN NƠI (Estimated
        Time of Arrival tại khách hàng) -> hệ thống quy đổi ra ETD bằng cách
        trừ đi `transit_lead_time_days` tra từ `Customer` (xem
        `demand.commitment_etd()`).
    Nếu cả hai cùng được khai báo, `requested_etd` được ưu tiên dùng trực tiếp.
    """

    id: str
    customer_id: str
    product_id: str
    qty: float = Field(gt=0)
    order_date: datetime
    requested_eta: Optional[datetime] = Field(
        default=None, description="Ngày khách yêu cầu HÀNG ĐẾN NƠI (tại khách hàng)"
    )
    requested_etd: Optional[datetime] = Field(
        default=None, description="Ngày khách/hợp đồng chốt HÀNG RỜI XƯỞNG - nếu có, dùng trực tiếp"
    )
    priority: OrderPriority = OrderPriority.NORMAL

    @model_validator(mode="after")
    def _check_eta_or_etd(self) -> "SalesOrder":
        if self.requested_eta is None and self.requested_etd is None:
            raise ValueError(
                f"SalesOrder '{self.id}': phải khai báo requested_eta hoặc requested_etd (hoặc cả hai)."
            )
        return self


class ForecastEntry(BaseModel):
    """Dự báo nhu cầu theo giai đoạn - nhu cầu CHƯA CHỐT, sẽ được net trừ đi phần
    đã có PO tương ứng để tránh tính trùng (double counting)."""

    id: str
    product_id: str
    period_start: datetime
    period_end: datetime
    qty: float = Field(gt=0)


class FinishedGoodsInventory(BaseModel):
    product_id: str
    on_hand_qty: float = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Nhu cầu sản xuất đã net (đầu ra của demand.py, đầu vào của mrp.py/scheduler.py)
# ---------------------------------------------------------------------------

class DemandSource(str, Enum):
    SALES_ORDER = "sales_order"
    FORECAST = "forecast"


class DemandLine(BaseModel):
    """Một dòng nhu cầu sản xuất, đã net tồn kho thành phẩm và quy đổi lot size.

    `due_date` là **ETD mục tiêu** (commitment ETD) mà sản xuất phải hoàn
    thành trước đó:
      - Với PO: quy đổi từ `requested_etd` (dùng trực tiếp) hoặc từ
        `requested_eta` trừ `transit_lead_time_days` của khách hàng
        (xem `demand.commitment_etd()`).
      - Với Forecast: dùng `period_end` (dự báo không có ETA/ETD riêng).
    Đây là mục tiêu MỀM cho scheduler (được đưa vào hàm mục tiêu tối thiểu
    hoá trễ hạn có trọng số, không phải ràng buộc cứng loại đơn khỏi kế
    hoạch) - xem scheduler.py.
    """

    id: str
    product_id: str
    qty: float
    due_date: datetime
    priority: OrderPriority
    source: DemandSource
    ref_id: str = Field(description="ID của SalesOrder hoặc ForecastEntry gốc")


# ---------------------------------------------------------------------------
# Bộ dữ liệu đầu vào tổng hợp
# ---------------------------------------------------------------------------

class PlanningDataset(BaseModel):
    products: list[Product] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    lines: list[ProductionLine] = Field(default_factory=list)
    workforce_pools: list[WorkforcePool] = Field(default_factory=list)
    customers: list[Customer] = Field(default_factory=list)
    sales_orders: list[SalesOrder] = Field(default_factory=list)
    forecasts: list[ForecastEntry] = Field(default_factory=list)
    finished_goods_inventory: list[FinishedGoodsInventory] = Field(default_factory=list)

    def product_map(self) -> dict[str, Product]:
        return {p.id: p for p in self.products}

    def component_map(self) -> dict[str, Component]:
        return {c.id: c for c in self.components}

    def customer_map(self) -> dict[str, Customer]:
        return {c.id: c for c in self.customers}

    def line_map(self) -> dict[str, ProductionLine]:
        return {l.id: l for l in self.lines}

    def pool_map(self) -> dict[str, WorkforcePool]:
        return {w.id: w for w in self.workforce_pools}
