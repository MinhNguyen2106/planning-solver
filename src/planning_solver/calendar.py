"""Bộ máy xử lý lịch làm việc (working calendar) của từng dây chuyền.

Ý tưởng cốt lõi: mỗi dây chuyền có "đồng hồ" thời gian làm việc riêng (chỉ đếm
thời gian trong ca, bỏ qua thời gian nghỉ/ngoài ca/ngày lễ). Ta "nén" trục thời
gian thực thành một dãy các *slot* làm việc liên tiếp (CompressedTimeline).
CP-SAT scheduler chỉ làm việc trên chỉ số slot (số nguyên), sau đó ta map
ngược slot -> thời gian thực để ra lịch cuối cùng.

Ưu điểm: một lệnh sản xuất kéo dài nhiều ca/nhiều ngày sẽ tự động "tạm dừng"
đúng vào giờ nghỉ và "chạy tiếp" vào ca kế tiếp, mà không cần khai báo tường
minh khoảng nghỉ trong solver.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .models import Shift, WorkCalendar


def _day_windows(calendar: WorkCalendar, day: date) -> list[tuple[datetime, datetime]]:
    """Trả về danh sách khoảng thời gian làm việc (start, end) trong ngày `day`."""
    exc = next((e for e in calendar.exceptions if e.day == day), None)
    if exc is not None:
        if not exc.working:
            return []
        shifts = exc.shifts if exc.shifts is not None else calendar.shifts
    else:
        shifts = calendar.shifts

    weekday = day.weekday()
    windows = [
        (datetime.combine(day, s.start), datetime.combine(day, s.end))
        for s in shifts
        if weekday in s.weekdays
    ]
    windows.sort()
    return windows


def working_windows_between(
    calendar: WorkCalendar, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Liệt kê mọi khoảng làm việc (start, end) cắt với [start, end)."""
    if end <= start:
        return []
    windows: list[tuple[datetime, datetime]] = []
    d = start.date()
    while d <= end.date():
        for ws, we in _day_windows(calendar, d):
            cs, ce = max(ws, start), min(we, end)
            if cs < ce:
                windows.append((cs, ce))
        d += timedelta(days=1)
    return windows


def is_working_day(calendar: WorkCalendar, day: date) -> bool:
    """True nếu `day` có ít nhất 1 ca làm việc theo lịch. Dùng cho các phép
    tính Ở CẤP ĐỘ NGÀY (không quan tâm giờ giấc cụ thể trong ca) - ví dụ quy
    đổi ETA/ETD theo "ngày làm việc"."""
    return len(_day_windows(calendar, day)) > 0


def default_business_calendar() -> WorkCalendar:
    """Lịch ngày làm việc mặc định dùng khi cần tính theo NGÀY LÀM VIỆC (vd.
    quy đổi ETA/ETD ở demand.py) nhưng không có lịch cụ thể nào được khai báo
    (`PlanningDataset.logistics_calendar`): Thứ 2 - Thứ 7 là ngày làm việc,
    không có ngoại lệ ngày lễ. Muốn tính đúng ngày nghỉ lễ thực tế, hãy khai
    báo `logistics_calendar` tường minh thay vì dùng mặc định này."""
    return WorkCalendar(
        shifts=[Shift(name="_business_day", weekdays=[0, 1, 2, 3, 4, 5], start=time(0, 0), end=time(23, 59))]
    )


def shift_by_working_days(
    dt: datetime, days: float, calendar: WorkCalendar, direction: int = -1
) -> datetime:
    """Dịch `dt` đi `days` NGÀY LÀM VIỆC theo `calendar`, bỏ qua ngày không
    làm việc (cuối tuần theo lịch + ngoại lệ nghỉ lễ/tăng ca).

    `direction=-1` lùi về trước (dùng để quy đổi ETA -> ETD: hàng phải rời
    xưởng sớm hơn ngày cần đến bao nhiêu NGÀY LÀM VIỆC của bên vận chuyển),
    `direction=+1` tiến lên (chiều ngược lại, nếu cần trong tương lai).

    Hỗ trợ `days` lẻ (vd 1.5): phần nguyên được lùi/tiến từng NGÀY LÀM VIỆC
    trọn vẹn, phần lẻ được cộng/trừ thêm như một khoảng thời gian thông
    thường (không xét ngày nghỉ) - đủ dùng cho lead time thực tế (thường là
    số ngày nguyên)."""
    if direction not in (-1, 1):
        raise ValueError("direction phải là -1 (lùi) hoặc 1 (tiến)")

    whole = int(days)
    frac = days - whole
    step = timedelta(days=direction)

    cur = dt
    remaining = whole
    while remaining > 0:
        cur += step
        if is_working_day(calendar, cur.date()):
            remaining -= 1
    if frac:
        cur += timedelta(days=frac) * direction
    return cur


def subtract_working_days(dt: datetime, days: float, calendar: WorkCalendar) -> datetime:
    """Lùi `dt` về trước `days` ngày làm việc - xem `shift_by_working_days`."""
    return shift_by_working_days(dt, days, calendar, direction=-1)


def add_working_days(dt: datetime, days: float, calendar: WorkCalendar) -> datetime:
    """Tiến `dt` lên `days` ngày làm việc - xem `shift_by_working_days`."""
    return shift_by_working_days(dt, days, calendar, direction=1)


@dataclass
class CompressedTimeline:
    """Nén thời gian làm việc của MỘT dây chuyền trong khoảng [horizon_start,
    horizon_end) thành các slot có độ dài `slot_minutes` phút.

    `slot_starts[i]` = thời điểm thực bắt đầu của slot làm việc thứ i.
    Chỉ những slot NẰM TRỌN trong một ca làm việc mới được đưa vào (slot lẻ
    cuối ca bị bỏ qua để đơn giản hoá mô hình - có thể giảm slot_minutes nếu
    cần độ chính xác cao hơn).
    """

    slot_minutes: int
    horizon_start: datetime
    horizon_end: datetime
    slot_starts: list[datetime] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        calendar: WorkCalendar,
        horizon_start: datetime,
        horizon_end: datetime,
        slot_minutes: int = 60,
    ) -> "CompressedTimeline":
        slots: list[datetime] = []
        step = timedelta(minutes=slot_minutes)
        for ws, we in working_windows_between(calendar, horizon_start, horizon_end):
            t = ws
            while t + step <= we:
                slots.append(t)
                t += step
        return cls(
            slot_minutes=slot_minutes,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            slot_starts=slots,
        )

    def num_slots(self) -> int:
        return len(self.slot_starts)

    def slot_start_dt(self, slot: int) -> datetime:
        return self.slot_starts[slot]

    def slot_end_dt(self, slot: int) -> datetime:
        """Thời điểm kết thúc thực của slot (slot_start + slot_minutes)."""
        return self.slot_starts[slot] + timedelta(minutes=self.slot_minutes)

    def completion_dt(self, end_slot_exclusive: int) -> datetime:
        """`end_slot_exclusive` là chỉ số slot NGAY SAU slot làm việc cuối cùng
        (quy ước kiểu Python range: [start, end)). Trả về thời điểm hoàn thành thực."""
        if end_slot_exclusive <= 0:
            return self.horizon_start
        return self.slot_end_dt(end_slot_exclusive - 1)

    def earliest_slot_at_or_after(self, dt: datetime) -> int:
        """Chỉ số slot làm việc sớm nhất bắt đầu tại-hoặc-sau `dt`.
        Nếu `dt` <= horizon_start, slot 0 luôn hợp lệ.
        Trả về num_slots() nếu không còn slot nào trong horizon (không đủ chỗ)."""
        return bisect.bisect_left(self.slot_starts, dt)

    def working_slot_count_before(self, dt: datetime) -> int:
        """Số slot làm việc nằm hoàn toàn trước `dt` - dùng làm 'toạ độ' đơn điệu
        không giảm của `dt` trên trục nén, để so sánh hạn giao hàng (due date)
        với thời điểm hoàn thành (end slot) một cách nhất quán, kể cả khi `dt`
        rơi vào giờ nghỉ."""
        return bisect.bisect_left(self.slot_starts, dt)

    def duration_in_slots(self, hours_needed: float) -> int:
        """Số slot cần để hoàn thành một khối lượng công việc dài `hours_needed`
        giờ làm việc thuần (đã trừ thời gian nghỉ). Luôn >= 1 - dùng cho thời
        gian SẢN XUẤT thật (không dùng cho khoảng changeover - xem
        `changeover_slots`, khoảng đó được phép = 0)."""
        slot_hours = self.slot_minutes / 60.0
        return max(1, math.ceil(hours_needed / slot_hours - 1e-9))

    def changeover_slots(self, minutes: float) -> int:
        """Số slot cho khoảng GAP changeover giữa 2 lệnh liên tiếp trên cùng
        dây chuyền (scheduler.py, mô hình sequence-dependent changeover).
        KHÁC `duration_in_slots()` ở chỗ KHÔNG có sàn tối thiểu 1 slot: một
        luật changeover 0 phút (vd. 2 lệnh cùng sản phẩm chạy liên tiếp)
        phải cho phép nối liền, không bị ép có khoảng trống nào cả."""
        if minutes <= 0:
            return 0
        return self.duration_in_slots(minutes / 60.0)

    def fits_within_horizon(self, earliest_slot: int, duration_slots: int) -> bool:
        return earliest_slot + duration_slots <= self.num_slots()
