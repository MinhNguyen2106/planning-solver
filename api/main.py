"""FastAPI phơi bày pipeline lập kế hoạch qua HTTP.

Chạy:
    uvicorn api.main:app --reload --port 8000

Endpoints:
    POST /plan/run   Nhận toàn bộ PlanningDataset (JSON) + tham số horizon,
                      trả về PlanReport (kế hoạch, ETA/ETD, cảnh báo).
    GET  /health      Kiểm tra sống.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from planning_solver.models import PlanningDataset
from planning_solver.pipeline import run_planning

app = FastAPI(
    title="Planning Solver API",
    description="Hệ thống lập kế hoạch sản xuất nhà máy: MRP + APS (CP-SAT) + ETA/ETD",
    version="0.1.0",
)


class PlanRunRequest(BaseModel):
    dataset: PlanningDataset
    planning_start: datetime
    horizon_days: int = Field(default=90, gt=0, le=365)
    slot_minutes: int = Field(default=60, gt=0)
    time_limit_s: float = Field(default=20.0, gt=0, le=120)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/plan/run")
def plan_run(req: PlanRunRequest) -> dict:
    try:
        report = run_planning(
            req.dataset,
            planning_start=req.planning_start,
            horizon_days=req.horizon_days,
            slot_minutes=req.slot_minutes,
            time_limit_s=req.time_limit_s,
        )
    except Exception as exc:  # noqa: BLE001 - trả lỗi rõ ràng cho client
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "solver_status": report.solver_status,
        "on_time_rate": report.on_time_rate(),
        "plan_lines": [
            {
                "demand_line_id": p.demand_line_id,
                "product_id": p.product_id,
                "qty": p.qty,
                "source": p.source,
                "ref_id": p.ref_id,
                "line_id": p.line_id,
                "eta": p.eta,
                "production_start": p.production_start,
                "production_end": p.production_end,
                "etd": p.etd,
                "due_date": p.due_date,
                "on_time": p.on_time,
                "delay_hours": p.delay_hours,
            }
            for p in report.plan_lines
        ],
        "unscheduled": [
            {
                "demand_line_id": u.demand_line_id,
                "product_id": u.product_id,
                "reason": u.reason,
                "detail": u.detail,
            }
            for u in report.unscheduled
        ],
        "workforce_warnings": report.workforce_warnings,
    }
