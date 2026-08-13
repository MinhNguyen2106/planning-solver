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
from datetime import date, datetime, timedelta

from .models import WorkCalendar


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
        giờ làm việc thuần (đã trừ thời gian nghỉ)."""
        slot_hours = self.slot_minutes / 60.0
        return max(1, math.ceil(hours_needed / slot_hours - 1e-9))

    def fits_within_horizon(self, earliest_slot: int, duration_slots: int) -> bool:
        return earliest_slot + duration_slots <= self.num_slots()
