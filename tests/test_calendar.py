from datetime import datetime, time

from planning_solver.calendar import (
    CompressedTimeline,
    add_working_days,
    default_business_calendar,
    is_working_day,
    subtract_working_days,
)
from planning_solver.models import CalendarException, Shift, WorkCalendar


def make_calendar() -> WorkCalendar:
    # Thứ 2 - Thứ 6, 08:00-12:00 và 13:00-17:00 (nghỉ trưa 1h không tính là giờ làm)
    return WorkCalendar(
        shifts=[
            Shift(name="Sáng", weekdays=[0, 1, 2, 3, 4], start=time(8, 0), end=time(12, 0)),
            Shift(name="Chiều", weekdays=[0, 1, 2, 3, 4], start=time(13, 0), end=time(17, 0)),
        ]
    )


def test_weekend_excluded():
    cal = make_calendar()
    # 2026-08-15 là Thứ 7, 2026-08-16 là Chủ nhật -> không có slot nào trong khoảng này
    tl = CompressedTimeline.build(
        cal, datetime(2026, 8, 15, 0, 0), datetime(2026, 8, 17, 0, 0), slot_minutes=60
    )
    assert tl.num_slots() == 0


def test_daily_working_hours_count():
    cal = make_calendar()
    # 2026-08-10 là Thứ 2 -> 8 giờ làm (4h sáng + 4h chiều)
    tl = CompressedTimeline.build(
        cal, datetime(2026, 8, 10, 0, 0), datetime(2026, 8, 11, 0, 0), slot_minutes=60
    )
    assert tl.num_slots() == 8
    assert tl.slot_start_dt(0) == datetime(2026, 8, 10, 8, 0)
    # slot thứ 4 (index 3) là 11:00-12:00, slot thứ 5 (index 4) nhảy sang 13:00 (bỏ giờ nghỉ trưa)
    assert tl.slot_start_dt(3) == datetime(2026, 8, 10, 11, 0)
    assert tl.slot_start_dt(4) == datetime(2026, 8, 10, 13, 0)


def test_holiday_exception_removes_day():
    cal = make_calendar()
    cal.exceptions.append(CalendarException(day=datetime(2026, 8, 10).date(), working=False))
    tl = CompressedTimeline.build(
        cal, datetime(2026, 8, 10, 0, 0), datetime(2026, 8, 11, 0, 0), slot_minutes=60
    )
    assert tl.num_slots() == 0


def test_earliest_slot_at_or_after_skips_to_next_working_instant():
    cal = make_calendar()
    tl = CompressedTimeline.build(
        cal, datetime(2026, 8, 10, 0, 0), datetime(2026, 8, 12, 0, 0), slot_minutes=60
    )
    # 12:30 rơi vào giờ nghỉ trưa -> slot sớm nhất phải là 13:00
    idx = tl.earliest_slot_at_or_after(datetime(2026, 8, 10, 12, 30))
    assert tl.slot_start_dt(idx) == datetime(2026, 8, 10, 13, 0)


def test_duration_in_slots_rounds_up():
    cal = make_calendar()
    tl = CompressedTimeline.build(
        cal, datetime(2026, 8, 10, 0, 0), datetime(2026, 8, 11, 0, 0), slot_minutes=60
    )
    assert tl.duration_in_slots(2.0) == 2
    assert tl.duration_in_slots(2.1) == 3
    assert tl.duration_in_slots(0.1) == 1


def test_fits_within_horizon():
    cal = make_calendar()
    tl = CompressedTimeline.build(
        cal, datetime(2026, 8, 10, 0, 0), datetime(2026, 8, 11, 0, 0), slot_minutes=60
    )
    assert tl.fits_within_horizon(0, 8) is True
    assert tl.fits_within_horizon(1, 8) is False


# --- Quy đổi ngày làm việc (dùng cho ETA/ETD, xem demand.commitment_etd) ---


def test_is_working_day():
    cal = make_calendar()  # Thứ 2 - Thứ 6
    assert is_working_day(cal, datetime(2026, 8, 10).date()) is True  # Thứ 2
    assert is_working_day(cal, datetime(2026, 8, 15).date()) is False  # Thứ 7
    assert is_working_day(cal, datetime(2026, 8, 16).date()) is False  # CN


def test_subtract_working_days_skips_weekend():
    cal = make_calendar()  # Thứ 2 - Thứ 6
    # 2026-08-17 là Thứ 2. Lùi 3 ngày làm việc phải nhảy qua T7+CN (15,16)
    # -> đếm ngược 14 (T6), 13 (T5), 12 (T4) -> kết quả 2026-08-12.
    result = subtract_working_days(datetime(2026, 8, 17, 0, 0), 3, cal)
    assert result == datetime(2026, 8, 12, 0, 0)


def test_add_working_days_is_inverse_of_subtract():
    cal = make_calendar()
    start = datetime(2026, 8, 12, 0, 0)
    forward = add_working_days(start, 3, cal)
    assert forward == datetime(2026, 8, 17, 0, 0)
    assert subtract_working_days(forward, 3, cal) == start


def test_subtract_working_days_fractional_part():
    cal = make_calendar()
    result = subtract_working_days(datetime(2026, 8, 17, 0, 0), 1.5, cal)
    # 1 ngày làm việc nguyên -> 2026-08-14 00:00, rồi trừ thêm 0.5 ngày thường
    assert result == datetime(2026, 8, 13, 12, 0)


def test_calendar_exception_excluded_from_working_days():
    cal = make_calendar()
    cal.exceptions.append(CalendarException(day=datetime(2026, 8, 14).date(), working=False))
    # Giờ 2026-08-14 (T6) cũng bị coi là nghỉ -> lùi 3 ngày làm việc từ T2 17/8
    # phải nhảy qua 16,15,14 -> đếm 13 (T5), 12 (T4), 11 (T3) -> kết quả 11/8.
    result = subtract_working_days(datetime(2026, 8, 17, 0, 0), 3, cal)
    assert result == datetime(2026, 8, 11, 0, 0)


def test_default_business_calendar_is_monday_to_saturday():
    cal = default_business_calendar()
    assert is_working_day(cal, datetime(2026, 8, 15).date()) is True  # Thứ 7
    assert is_working_day(cal, datetime(2026, 8, 16).date()) is False  # CN
