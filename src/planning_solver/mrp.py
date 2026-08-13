"""MRP (Material Requirements Planning): nổ BOM và phân bổ tồn kho + lịch
nhập hàng linh kiện theo thứ tự ưu tiên để tính ETA nguyên vật liệu cho từng
dòng nhu cầu sản xuất (DemandLine).

Thuật toán phân bổ (time-phased available-to-promise, kiểu "pegging" đơn giản):

  Với mỗi linh kiện, ta dựng "đường cung" (supply timeline) tích luỹ:
    mốc 0  : tồn kho khả dụng hiện tại (on_hand - safety_stock)
    mốc t1 : + lô nhập hàng gần nhất
    mốc t2 : + lô nhập hàng tiếp theo
    ...
  rồi CỘNG DỒN. Mỗi DemandLine, xử lý THEO THỨ TỰ ƯU TIÊN (priority, rồi due
  date), "đặt chỗ" (reserve) đúng số lượng linh kiện nó cần trên đường cung
  này. Thời điểm cộng dồn vừa đủ để thoả nhu cầu chính là ETA của linh kiện
  cho đơn đó. Đơn xử lý sau sẽ tiếp tục cộng dồn từ điểm đơn trước đã đặt chỗ
  -> đảm bảo không có 2 đơn cùng "giành" một đơn vị tồn kho/lô hàng.

  ETA nguyên vật liệu của một DemandLine = MAX(ETA của mọi linh kiện trong BOM)
  (đơn chỉ có thể bắt đầu sản xuất khi TẤT CẢ linh kiện đã sẵn sàng).

  Nếu một linh kiện không đủ trong toàn bộ các lô đã biết -> đơn đó bị đánh
  dấu "blocked" kèm danh sách thiếu hụt (shortage) để bộ phận mua hàng xử lý.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime

from .models import PRIORITY_RANK, Component, DemandLine, PlanningDataset


@dataclass
class ComponentShortage:
    component_id: str
    shortfall_qty: float


@dataclass
class MaterialReadiness:
    demand_line_id: str
    eta: datetime | None
    blocked: bool
    shortages: list[ComponentShortage] = field(default_factory=list)
    component_eta: dict[str, datetime | None] = field(default_factory=dict)


class ComponentLedger:
    """Sổ cái cộng dồn cung ứng cho một linh kiện, dùng để phân bổ tuần tự."""

    def __init__(self, component: Component, reference_start: datetime):
        events: list[tuple[datetime, float]] = [(reference_start, component.usable_on_hand())]
        for r in component.incoming_receipts:
            events.append((r.expected_date, r.qty))
        events.sort(key=lambda e: e[0])

        self._dates: list[datetime] = []
        self._cumulative: list[float] = []
        running = 0.0
        for dt, qty in events:
            running += qty
            self._dates.append(dt)
            self._cumulative.append(running)

        self.consumed_cumulative: float = 0.0
        self.total_supply: float = running

    def reserve(self, qty: float) -> tuple[datetime | None, float]:
        """Đặt chỗ `qty` đơn vị. Trả về (ready_dt, shortfall).
        ready_dt=None và shortfall>0 nếu không đủ nguồn cung."""
        if qty <= 0:
            return self._dates[0] if self._dates else None, 0.0

        target = self.consumed_cumulative + qty
        idx = bisect.bisect_left(self._cumulative, target)
        if idx < len(self._cumulative):
            self.consumed_cumulative = target
            return self._dates[idx], 0.0

        shortfall = target - self.total_supply
        self.consumed_cumulative = self.total_supply
        return None, shortfall


def allocate_materials(
    demand_lines: list[DemandLine],
    dataset: PlanningDataset,
    reference_start: datetime,
) -> dict[str, MaterialReadiness]:
    """Tính ETA nguyên vật liệu cho từng DemandLine, xử lý theo thứ tự ưu tiên
    rồi đến due date sớm nhất trước (đơn quan trọng/gấp hơn được ưu tiên
    giành nguồn cung trước)."""
    products = dataset.product_map()

    ledgers: dict[str, ComponentLedger] = {
        c.id: ComponentLedger(c, reference_start) for c in dataset.components
    }

    ordered = sorted(demand_lines, key=lambda d: (PRIORITY_RANK[d.priority], d.due_date))

    results: dict[str, MaterialReadiness] = {}
    for d in ordered:
        product = products.get(d.product_id)
        if product is None or not product.bom:
            # Không có BOM (vd. sản phẩm mua sẵn / không cấu thành từ linh kiện
            # theo dõi trong hệ thống) -> coi như nguyên vật liệu sẵn sàng ngay.
            results[d.id] = MaterialReadiness(
                demand_line_id=d.id, eta=reference_start, blocked=False
            )
            continue

        component_eta: dict[str, datetime | None] = {}
        shortages: list[ComponentShortage] = []
        for item in product.bom:
            ledger = ledgers.get(item.component_id)
            qty_needed = d.qty * item.qty_per_unit * (1 + item.scrap_rate)
            if ledger is None:
                # Linh kiện không tồn tại trong hệ thống -> coi là thiếu hoàn toàn.
                shortages.append(ComponentShortage(item.component_id, qty_needed))
                component_eta[item.component_id] = None
                continue
            ready_dt, shortfall = ledger.reserve(qty_needed)
            component_eta[item.component_id] = ready_dt
            if shortfall > 1e-9:
                shortages.append(ComponentShortage(item.component_id, shortfall))

        if shortages:
            results[d.id] = MaterialReadiness(
                demand_line_id=d.id,
                eta=None,
                blocked=True,
                shortages=shortages,
                component_eta=component_eta,
            )
        else:
            eta = max(component_eta.values()) if component_eta else reference_start
            results[d.id] = MaterialReadiness(
                demand_line_id=d.id, eta=eta, blocked=False, component_eta=component_eta
            )

    return results
