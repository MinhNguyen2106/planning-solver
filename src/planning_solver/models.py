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
# Độ ưu tiên (dùng chung bởi PO, Forecast, và lệnh bổ sung tồn kho MTS)
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


class PlanningStrategy(str, Enum):
    """Chiến lược lập kế hoạch sản xuất cho một sản phẩm - xem demand.py."""

    MAKE_TO_ORDER = "make_to_order"
    """MTO (mặc định): mỗi PO/kỳ Forecast của sản phẩm này sinh ra MỘT lệnh
    sản xuất riêng, hạn giao (due_date) = ETD mục tiêu của chính PO/Forecast
    đó. Đây là hành vi gốc của hệ thống (xem demand.build_demand_lines)."""

    MAKE_TO_STOCK = "make_to_stock"
    """MTS: PO/Forecast của sản phẩm này KHÔNG sinh lệnh sản xuất riêng từng
    cái - chúng chỉ là các sự kiện TIÊU THỤ tồn kho thành phẩm. Hệ thống mô
    phỏng tồn kho dự kiến theo thời gian và tự sinh lệnh BỔ SUNG TỒN KHO
    (`DemandSource.REPLENISHMENT`) mỗi khi tồn kho dự kiến chạm
    `reorder_point`, với số lượng đủ đưa tồn kho về `target_stock_level`
    (xem demand.build_mts_replenishment_lines)."""


class Product(BaseModel):
    id: str
    name: str
    uom: str = "pcs"
    bom: list[BOMItem] = Field(default_factory=list)
    lot_size_multiple: Optional[float] = Field(
        default=None, gt=0,
        description=(
            "Size CHUẨN của 1 lot sản xuất. Nếu khai báo, mỗi PO/Forecast/lệnh "
            "bổ sung (MTS) được CHIA thành nhiều lot bằng giá trị này (xem "
            "demand.split_into_demand_lines). None (mặc định) = không chia, "
            "toàn bộ nhu cầu là 1 lot duy nhất."
        ),
    )
    min_lot_size: Optional[float] = Field(
        default=None, ge=0,
        description=(
            "Ngưỡng gộp lot dư: nếu lot cuối (phần dư sau khi chia hết cho "
            "lot_size_multiple) NHỎ HƠN giá trị này, gộp phần dư đó vào lot "
            "liền trước (lot cuối lớn hơn size chuẩn một chút) thay vì đứng "
            "thành 1 lot riêng quá nhỏ. Không bao giờ làm tròn LÊN - phần dư "
            "đủ lớn (>= min_lot_size) vẫn đứng riêng ĐÚNG bằng giá trị dư đó. "
            "Bắt buộc khai báo cùng lot_size_multiple (cả hai cùng None hoặc "
            "cùng có giá trị)."
        ),
    )
    post_production_buffer_hours: float = Field(
        default=0, ge=0, description="Thời gian QC/đóng gói sau khi sản xuất xong trước khi có thể xuất (ETD)"
    )
    planning_strategy: PlanningStrategy = PlanningStrategy.MAKE_TO_ORDER

    # --- Chỉ áp dụng khi planning_strategy = MAKE_TO_STOCK ---
    reorder_point: float = Field(
        default=0, ge=0,
        description="[MTS] Ngưỡng tồn kho dự kiến kích hoạt lệnh bổ sung tồn kho",
    )
    target_stock_level: float = Field(
        default=0, ge=0,
        description="[MTS] Mức tồn kho mục tiêu sau khi bổ sung (up-to-level)",
    )
    replenishment_priority: OrderPriority = Field(
        default=OrderPriority.NORMAL,
        description="[MTS] Độ ưu tiên gán cho các lệnh bổ sung tồn kho tự sinh",
    )

    @model_validator(mode="after")
    def _check_mts_policy(self) -> "Product":
        if self.planning_strategy == PlanningStrategy.MAKE_TO_STOCK and (
            self.target_stock_level <= self.reorder_point
        ):
            raise ValueError(
                f"Sản phẩm '{self.id}': MAKE_TO_STOCK yêu cầu target_stock_level "
                f"({self.target_stock_level}) > reorder_point ({self.reorder_point})."
            )
        return self

    @model_validator(mode="after")
    def _check_lot_fields_consistency(self) -> "Product":
        has_multiple = self.lot_size_multiple is not None
        has_min = self.min_lot_size is not None
        if has_multiple != has_min:
            raise ValueError(
                f"Sản phẩm '{self.id}': min_lot_size và lot_size_multiple phải cùng được "
                "khai báo, hoặc cùng để trống (None = không chia lot)."
            )
        if has_multiple and self.min_lot_size > self.lot_size_multiple:
            raise ValueError(
                f"Sản phẩm '{self.id}': min_lot_size ({self.min_lot_size}) không được lớn "
                f"hơn lot_size_multiple ({self.lot_size_multiple}) - nếu không, phần dư sẽ "
                "LUÔN bị gộp vào lot trước, size chuẩn coi như vô nghĩa."
            )
        return self

    def split_into_lots(self, qty: float) -> list[float]:
        """Chia số lượng nhu cầu ròng `qty` thành các LOT sản xuất.

        - `lot_size_multiple` không khai báo (None): KHÔNG chia, trả về đúng
          1 lot = toàn bộ `qty` (không làm tròn).
        - Có khai báo: chia thành các lot đầy đủ = `lot_size_multiple`. Phần
          dư (nếu có) đứng thành 1 lot riêng ĐÚNG BẰNG phần dư đó (không làm
          tròn lên/xuống) - TRỪ KHI phần dư nhỏ hơn `min_lot_size`, khi đó
          phần dư được GỘP vào lot liền trước (lot cuối lớn hơn size chuẩn).
          Tổng các lot trả về luôn bằng đúng `qty` (bảo toàn số lượng).
        """
        if qty <= 0:
            return []
        if self.lot_size_multiple is None:
            return [qty]

        qty = round(qty, 6)
        lot = self.lot_size_multiple
        n_full = int(qty // lot)
        remainder = round(qty - n_full * lot, 6)

        if remainder == 0:
            return [lot] * n_full

        min_lot = self.min_lot_size or 0.0
        if remainder < min_lot and n_full > 0:
            return [lot] * (n_full - 1) + [lot + remainder]
        # Phần dư đủ lớn (>= min_lot_size), HOẶC không có lot chuẩn nào để
        # gộp vào (n_full == 0, toàn bộ qty nhỏ hơn cả 1 lot chuẩn) -> đứng
        # thành 1 lot riêng, giữ nguyên giá trị dư (không làm tròn).
        return [lot] * n_full + [remainder]


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

class DateCountMode(str, Enum):
    """Cách đếm ngày khi cộng/trừ ngày tháng cho ETA/ETD (transit lead time,
    và mọi phép quy đổi ngày tương tự trong tương lai)."""

    CALENDAR_DAYS = "calendar_days"
    """Đếm NGÀY LỊCH thông thường - bao gồm cả cuối tuần/ngày lễ (vd. 3 ngày
    lịch = cộng/trừ thẳng timedelta(days=3), không quan tâm hôm đó có phải
    ngày nghỉ hay không). Mặc định - đơn giản, khớp cách hiểu "N ngày" phổ
    biến khi không có yêu cầu đặc biệt."""

    WORKING_DAYS = "working_days"
    """Đếm NGÀY LÀM VIỆC - bỏ qua ngày không làm việc (cuối tuần + ngày lễ)
    theo một lịch cụ thể (`PlanningDataset.logistics_calendar`, hoặc lịch
    mặc định T2-T7 nếu không khai báo). Dùng khi transit lead time được tính
    theo "ngày làm việc" của hãng vận chuyển/hải quan, vd. "3 ngày làm việc"
    sẽ tự động cộng thêm cho đủ nếu rơi vào cuối tuần/lễ."""


class Customer(BaseModel):
    """Dữ liệu chủ (master data) khách hàng / tuyến giao hàng - dùng để quy
    đổi ETA (hàng đến tay khách) <-> ETD (hàng rời xưởng) cho các PO chỉ khai
    báo ETA."""

    id: str
    name: str
    transit_lead_time_days: float = Field(
        default=0,
        ge=0,
        description="Thời gian vận chuyển tiêu chuẩn từ xưởng đến khách hàng/tuyến này",
    )
    transit_lead_time_mode: DateCountMode = Field(
        default=DateCountMode.CALENDAR_DAYS,
        description="transit_lead_time_days tính theo ngày lịch hay ngày làm việc - xem DateCountMode",
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
    REPLENISHMENT = "replenishment"
    """Lệnh bổ sung tồn kho tự sinh cho sản phẩm MAKE_TO_STOCK - không gắn
    với một PO/Forecast cụ thể, mà với thời điểm tồn kho dự kiến chạm
    reorder_point (xem demand.build_mts_replenishment_lines). `ref_id` của
    DemandLine loại này có dạng "{product_id}#{số thứ tự}", không phải ID
    của một SalesOrder/ForecastEntry."""


class DemandLine(BaseModel):
    """Một dòng nhu cầu sản xuất, đã net tồn kho thành phẩm và quy đổi lot size.

    `due_date` là **ETD mục tiêu** (commitment ETD) mà sản xuất phải hoàn
    thành trước đó:
      - Với PO (sản phẩm MAKE_TO_ORDER): quy đổi từ `requested_etd` (dùng
        trực tiếp) hoặc từ `requested_eta` trừ `transit_lead_time_days` của
        khách hàng (xem `demand.commitment_etd()`).
      - Với Forecast (MAKE_TO_ORDER): dùng `period_end`.
      - Với lệnh bổ sung tồn kho (MAKE_TO_STOCK, `source=REPLENISHMENT`):
        ngày tồn kho dự kiến CHẠM `reorder_point` - tức hạn chót phải có
        hàng bổ sung để tránh hụt dưới ngưỡng an toàn (xem
        `demand.build_mts_replenishment_lines`).
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
    ref_id: str = Field(
        description="ID của SalesOrder/ForecastEntry gốc, hoặc '{product_id}#{n}' nếu source=REPLENISHMENT"
    )


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
    logistics_calendar: Optional[WorkCalendar] = Field(
        default=None,
        description=(
            "Lịch ngày làm việc dùng khi quy đổi ETA/ETD theo NGÀY LÀM VIỆC "
            "(Customer.transit_lead_time_mode = working_days). Không set thì "
            "dùng lịch mặc định T2-T7 (calendar.default_business_calendar)."
        ),
    )

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
