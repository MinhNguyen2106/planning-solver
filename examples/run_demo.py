#!/usr/bin/env python3
"""Demo end-to-end: đọc dữ liệu mẫu, chạy pipeline, in báo cáo kế hoạch.

Chạy:
    python examples/run_demo.py
    python examples/run_demo.py --data data/sample/factory_demo.json --out /tmp/plan.json
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from planning_solver.io_utils import load_dataset, save_plan_report
from planning_solver.pipeline import run_planning

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data", default=str(REPO_ROOT / "data" / "sample" / "factory_demo.json")
    )
    parser.add_argument("--out", default=None, help="Đường dẫn file JSON kết quả (tuỳ chọn)")
    parser.add_argument(
        "--start", default=None, help="Thời điểm bắt đầu lập kế hoạch, ISO format. Mặc định = bây giờ."
    )
    parser.add_argument("--horizon-days", type=int, default=60)
    args = parser.parse_args()

    dataset = load_dataset(args.data)
    planning_start = (
        datetime.fromisoformat(args.start) if args.start else datetime(2026, 8, 13, 6, 0)
    )

    report = run_planning(dataset, planning_start=planning_start, horizon_days=args.horizon_days)

    print(f"Solver status: {report.solver_status}")
    print(f"On-time rate: {report.on_time_rate():.0%}\n")

    print(f"{'Demand':<22}{'SP':<10}{'SL':>8}  {'Line':<10}{'ETA NVL':<18}{'Bắt đầu SX':<18}{'ETD (giao)':<18}{'Hạn':<18}{'Đúng hạn?'}")
    for p in report.plan_lines:
        print(
            f"{p.demand_line_id:<22}{p.product_id:<10}{p.qty:>8.0f}  {p.line_id:<10}"
            f"{p.eta.strftime('%m-%d %H:%M'):<18}{p.production_start.strftime('%m-%d %H:%M'):<18}"
            f"{p.etd.strftime('%m-%d %H:%M'):<18}{p.due_date.strftime('%m-%d %H:%M'):<18}"
            f"{'✅' if p.on_time else f'❌ trễ {p.delay_hours:.0f}h'}"
        )

    if report.unscheduled:
        print("\n⚠️  Các dòng nhu cầu KHÔNG xếp được lịch:")
        for u in report.unscheduled:
            print(f"  - {u.demand_line_id} ({u.product_id}): {u.reason} — {u.detail}")

    if report.workforce_warnings:
        print("\n⚠️  Cảnh báo ràng buộc nhân lực:")
        for w in report.workforce_warnings:
            print(f"  - {w}")

    if args.out:
        save_plan_report(report, args.out)
        print(f"\nĐã lưu báo cáo đầy đủ: {args.out}")


if __name__ == "__main__":
    main()
