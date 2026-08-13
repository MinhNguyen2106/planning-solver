from datetime import datetime, time

from planning_solver.calendar import CompressedTimeline
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
