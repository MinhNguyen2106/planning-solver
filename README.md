# planning-solver

Hệ thống lập kế hoạch sản xuất nhà máy: nhận **PO (đơn hàng)** và **Forecast
(dự báo)** làm đầu vào, tính toán **ETA/ETD**, và sinh ra **lịch sản xuất**
tôn trọng đồng thời các ràng buộc về:

- **Sản phẩm** (BOM/định mức nguyên vật liệu, lot size)
- **Dây chuyền sản xuất** (năng suất theo sản phẩm, thời gian chuyển đổi)
- **Thời gian làm việc của dây chuyền** (ca kíp, ngày lễ, tăng ca)
- **Tồn kho & linh kiện** (tồn kho thành phẩm, tồn kho NVL, lịch nhập hàng)
- **Con người** (tổ/nhóm nhân lực dùng chung nhiều dây chuyền, giới hạn headcount)

## Kiến trúc tổng quan

```
PO (SalesOrder) + Forecast ──► demand.py ──► DemandLine (nhu cầu đã net)
                                                    │
                                                    ▼
                          mrp.py  (nổ BOM, phân bổ tồn kho + lịch nhập
                                    linh kiện theo ưu tiên)  ──► ETA nguyên vật liệu
                                                    │
                                                    ▼
                     scheduler.py  (OR-Tools CP-SAT: gán dây chuyền,
                        tôn trọng lịch làm việc, nhân lực, hạn giao)
                                                    │
                                                    ▼
                        eta_etd.py  (ETD = hoàn thành SX + buffer QC/đóng gói,
                                      so sánh với due date ⇒ on-time / trễ)
                                                    │
                                                    ▼
                              PlanReport (kế hoạch cuối cùng)
```

Chi tiết thuật toán & các giả định đơn giản hoá: xem [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Cài đặt

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Chạy thử (demo với dữ liệu mẫu)

```bash
python examples/run_demo.py
```

Dữ liệu mẫu ở `data/sample/factory_demo.json`: 2 sản phẩm (ghế, bàn nhựa)
dùng chung linh kiện, 3 dây chuyền (2 dây chuyền ép nhựa dùng chung 1 tổ nhân
lực, 1 dây chuyền lắp ráp riêng), 4 đơn hàng + 2 dòng dự báo.

## Chạy API

```bash
uvicorn api.main:app --reload --port 8000
# rồi: POST /plan/run  với body {"dataset": {...}, "planning_start": "2026-08-13T06:00:00"}
```

Swagger UI: http://localhost:8000/docs

## Chạy test

```bash
pytest -q
```

## Cấu trúc thư mục

```
src/planning_solver/
  models.py     Toàn bộ domain model (Pydantic): Product, BOM, Component,
                ProductionLine, WorkCalendar, WorkforcePool, SalesOrder,
                ForecastEntry, PlanningDataset...
  calendar.py   Nén thời gian làm việc của từng dây chuyền thành slot liên tục
  demand.py     Gộp PO + Forecast, trừ tồn kho thành phẩm, tránh đếm trùng
  mrp.py        Nổ BOM, phân bổ tồn kho/linh kiện theo ưu tiên -> ETA
  scheduler.py  CP-SAT: xếp lịch dây chuyền + nhân lực + hạn giao
  eta_etd.py    Tính ETD, so sánh due date, gộp thành PlanReport
  pipeline.py   run_planning() - chạy toàn bộ luồng trên
  io_utils.py   Đọc/ghi dataset & báo cáo dạng JSON
api/main.py     FastAPI expose pipeline qua HTTP
examples/       Script demo
data/sample/    Bộ dữ liệu mẫu
tests/          pytest cho từng module + end-to-end
```

## Mở rộng tiếp theo (đề xuất)

Xem mục "Giới hạn & hướng mở rộng" trong `docs/ARCHITECTURE.md` — bao gồm:
chia nhỏ lô hàng trên nhiều dây chuyền, ràng buộc nhân lực đa kỹ năng/đa lịch,
sequence-dependent changeover, đa cấp BOM, lưu trữ DB thay vì JSON, giao diện
Gantt chart.
