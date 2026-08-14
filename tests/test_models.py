from datetime import time

import pytest
from pydantic import ValidationError

from planning_solver.models import ChangeoverRule, LineProductRate, Product, ProductionLine, Shift, WorkCalendar


def lot_product(**kwargs) -> Product:
    return Product(id="P1", name="SP1", **kwargs)


def line_with_rates(sequence_changeovers=None, **flat_changeovers) -> ProductionLine:
    """Dây chuyền tối giản với lịch bất kỳ, năng suất P1/P2 tuỳ ý, chỉ dùng để
    test tra cứu changeover_minutes_for() (không cần chính xác lịch làm việc)."""
    cal = WorkCalendar(shifts=[Shift(name="Ca", weekdays=[0, 1, 2, 3, 4], start=time(8, 0), end=time(16, 0))])
    return ProductionLine(
        id="L1", name="Line1", calendar=cal,
        product_rates=[
            LineProductRate(product_id="P1", rate_per_hour=10, changeover_minutes=flat_changeovers.get("P1", 20)),
            LineProductRate(product_id="P2", rate_per_hour=10, changeover_minutes=flat_changeovers.get("P2", 25)),
        ],
        sequence_changeovers=sequence_changeovers or [],
    )


# --- Validation: min_lot_size và lot_size_multiple phải đi cùng nhau ---


def test_both_none_is_valid_default():
    p = lot_product()
    assert p.min_lot_size is None
    assert p.lot_size_multiple is None


def test_only_lot_size_multiple_set_is_rejected():
    with pytest.raises(ValidationError):
        lot_product(lot_size_multiple=50)


def test_only_min_lot_size_set_is_rejected():
    with pytest.raises(ValidationError):
        lot_product(min_lot_size=50)


def test_min_lot_size_greater_than_multiple_is_rejected():
    with pytest.raises(ValidationError):
        lot_product(min_lot_size=100, lot_size_multiple=50)


def test_min_lot_size_equal_multiple_is_allowed():
    p = lot_product(min_lot_size=50, lot_size_multiple=50)
    assert p.min_lot_size == 50


# --- split_into_lots: không cấu hình -> không chia ---


def test_no_lot_config_returns_single_lot_unrounded():
    p = lot_product()
    assert p.split_into_lots(45.5) == [45.5]
    assert p.split_into_lots(1) == [1]


def test_zero_or_negative_qty_returns_no_lots():
    p = lot_product(min_lot_size=50, lot_size_multiple=50)
    assert p.split_into_lots(0) == []
    assert p.split_into_lots(-10) == []


# --- split_into_lots: có cấu hình -> chia theo lot_size_multiple ---


def test_exact_multiple_splits_into_equal_lots_no_remainder():
    p = lot_product(min_lot_size=50, lot_size_multiple=50)
    assert p.split_into_lots(150) == [50, 50, 50]


def test_remainder_at_or_above_min_lot_size_stands_alone_unrounded():
    p = lot_product(min_lot_size=5, lot_size_multiple=50)
    # 205 = 4x50 + 5 dư; 5 >= min_lot_size(5) -> đứng riêng, KHÔNG làm tròn lên 250
    assert p.split_into_lots(205) == [50, 50, 50, 50, 5]


def test_remainder_below_min_lot_size_merges_into_last_lot():
    p = lot_product(min_lot_size=50, lot_size_multiple=50)
    # 101 = 2x50 + 1 dư; 1 < min_lot_size(50) -> gộp vào lot cuối: [50, 51]
    assert p.split_into_lots(101) == [50, 51]
    assert sum(p.split_into_lots(101)) == 101


def test_qty_smaller_than_one_full_lot_stays_as_single_leftover_lot():
    p = lot_product(min_lot_size=50, lot_size_multiple=50)
    # Không có lot chuẩn nào để gộp vào (n_full=0) -> giữ nguyên, KHÔNG làm
    # tròn lên 50 dù nhỏ hơn cả min_lot_size lẫn lot_size_multiple.
    assert p.split_into_lots(5) == [5]


def test_total_quantity_always_conserved_across_lots():
    p = lot_product(min_lot_size=30, lot_size_multiple=200)
    for qty in (1, 50, 199, 200, 201, 999, 1100.5):
        lots = p.split_into_lots(qty)
        assert sum(lots) == pytest.approx(qty)


def test_large_order_produces_many_full_lots():
    p = lot_product(min_lot_size=50, lot_size_multiple=50)
    lots = p.split_into_lots(1100)
    assert lots == [50] * 22
    assert len(lots) == 22


# --- ProductionLine.changeover_minutes_for: sequence-dependent changeover ---


def test_empty_sequence_changeovers_always_falls_back_to_flat_regardless_of_predecessor():
    line = line_with_rates(sequence_changeovers=[])
    # Không khai báo sequence_changeovers -> LUÔN dùng changeover_minutes
    # phẳng của LineProductRate, bất kể from_product_id là gì (kể cả None) -
    # chứng minh hành vi cũ là 1 trường hợp đặc biệt của cơ chế mới.
    assert line.changeover_minutes_for(None, "P1") == 20
    assert line.changeover_minutes_for("P2", "P1") == 20
    assert line.changeover_minutes_for("P1", "P1") == 20
    assert line.changeover_minutes_for(None, "P2") == 25


def test_exact_pair_match_wins_over_wildcard_and_flat():
    line = line_with_rates(
        sequence_changeovers=[
            ChangeoverRule(to_product_id="P1", minutes=15),  # wildcard (None, P1)
            ChangeoverRule(from_product_id="P2", to_product_id="P1", minutes=5),  # khớp chính xác
        ]
    )
    assert line.changeover_minutes_for("P2", "P1") == 5  # tier 1: khớp chính xác


def test_wildcard_from_none_wins_over_flat_when_no_exact_pair():
    line = line_with_rates(
        sequence_changeovers=[
            ChangeoverRule(to_product_id="P1", minutes=15),  # wildcard (None, P1)
        ]
    )
    # Không có luật riêng cho (P2, P1) -> fallback tier 2: wildcard (None, P1)
    assert line.changeover_minutes_for("P2", "P1") == 15
    # Job đầu tiên trên dây chuyền (from=None) cũng khớp chính luật này (tier 1)
    assert line.changeover_minutes_for(None, "P1") == 15


def test_flat_fallback_used_when_no_matching_rule_at_all():
    line = line_with_rates(
        sequence_changeovers=[
            ChangeoverRule(from_product_id="P1", to_product_id="P1", minutes=0),
        ]
    )
    # Không có luật nào khớp (P2, P1) lẫn wildcard (None, P1) -> tier 3: phẳng
    assert line.changeover_minutes_for("P2", "P1") == 20
    assert line.changeover_minutes_for(None, "P1") == 20
    # Nhưng luật riêng cho (P1, P1) vẫn được ưu tiên đúng
    assert line.changeover_minutes_for("P1", "P1") == 0


def test_flat_fallback_for_unknown_product_with_no_rate_is_zero():
    line = line_with_rates(sequence_changeovers=[])
    assert line.changeover_minutes_for("P1", "P_UNKNOWN") == 0.0
