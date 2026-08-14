# planning-solver

Hệ thống lập kế hoạch sản xuất nhà máy: nhận **PO (đơn hàng)** và **Forecast
(dự báo)** làm đầu vào, tính toán **ETA/ETD**, và sinh ra **lịch sản xuất**
tôn trọng đồng thời các ràng buộc về:

- **Sản phẩm** (BOM/định mức nguyên vật liệu, lot size, chiến lược **Make-to-Order hoặc Make-to-Stock** - xem bên dưới)
- **Dây chuyền sản xuất** (năng suất theo sản phẩm, thời gian chuyển đổi -
  bao gồm cả **changeover phụ thuộc sản phẩm chạy trước**, sequence-dependent,
  tuỳ chọn theo từng dây chuyền)
- **Thời gian làm việc của dây chuyền** (ca kíp, ngày lễ, tăng ca)
- **Tồn kho & linh kiện** (tồn kho thành phẩm, tồn kho NVL, lịch nhập hàng)
- **Con người** (tổ/nhóm nhân lực dùng chung nhiều dây chuyền, giới hạn headcount)

## Kiến trúc tổng quan

```
PO (ETA hoặc ETD của khách) ──┐
Forecast (period_end)      ──┼─► demand.py ──► DemandLine (due_date = ETD mục tiêu, đã net)
Customer (transit lead time) ┘        (ETA PO ⇒ quy đổi ra ETD mục tiêu bằng transit lead time)
                                                    │
                                                    ▼
                          mrp.py  (nổ BOM, phân bổ tồn kho + lịch nhập
                                    linh kiện theo ưu tiên)  ──► ETA nguyên vật liệu
                                                    │
                                                    ▼
                     scheduler.py  (OR-Tools CP-SAT: gán dây chuyền,
                        tôn trọng lịch làm việc, nhân lực; ETD mục tiêu
                        là ràng buộc MỀM - tối thiểu hoá trễ hạn có trọng số)
                                                    │
                                                    ▼
                        eta_etd.py  (ETD hệ thống tính = hoàn thành SX +
                                      buffer QC/đóng gói; so với ETD mục
                                      tiêu ⇒ on-time / trễ)
                                                    │
                                                    ▼
                              PlanReport (kế hoạch cuối cùng)
```

**Về ETA/ETD**: hệ thống phân biệt rõ 2 cặp — (1) ETA/ETD của PO là cam kết
giao hàng với khách, PO khai báo MỘT trong hai (ETA hàng đến khách, hoặc ETD
hàng rời xưởng), quy đổi qua lại bằng `transit_lead_time_days` của khách hàng
(`Customer`); (2) ETA nguyên vật liệu (đầu ra MRP) và ETD hệ thống tính (đầu
ra scheduler) là hai mốc hệ thống TỰ TÍNH dựa trên tồn kho/lịch dây chuyền,
so sánh với ETD mục tiêu ở bước (1) để biết đúng hạn hay không. Xem chi tiết,
kèm ví dụ, ở mục 2 của [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Phép quy đổi ETA→ETD ở bước (1) hỗ trợ **2 cách đếm ngày, chọn theo từng
khách hàng** (`Customer.transit_lead_time_mode`): `calendar_days` (mặc định,
tính cả cuối tuần/lễ) hoặc `working_days` (bỏ qua ngày không làm việc theo
`PlanningDataset.logistics_calendar`, mặc định T2-T7 nếu không khai báo).

**Về MTO/MTS**: mỗi sản phẩm chọn 1 trong 2 chiến lược qua
`Product.planning_strategy`:
- **Make-to-Order (MTO, mặc định)**: mỗi PO/kỳ Forecast sinh 1 lệnh sản xuất
  riêng như mô tả ở trên.
- **Make-to-Stock (MTS)**: PO/Forecast KHÔNG sinh lệnh riêng - chỉ là sự
  kiện tiêu thụ tồn kho. Hệ thống mô phỏng tồn kho dự kiến theo thời gian và
  tự sinh lệnh **bổ sung tồn kho** mỗi khi chạm `reorder_point`, đủ số lượng
  đưa tồn kho về `target_stock_level`. Lệnh bổ sung này đi qua đúng scheduler
  như PO thường (cùng ràng buộc mềm, cạnh tranh NVL/dây chuyền theo độ ưu
  tiên). Xem ví dụ `P-STOOL` trong dữ liệu mẫu và chi tiết thuật toán ở mục 3
  của [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**Về chia lot**: số lượng ròng của MỖI PO/Forecast/lệnh bổ sung tồn kho
KHÔNG bị làm tròn thành 1 con số - nó được **chia thành nhiều lot sản xuất**
theo `Product.lot_size_multiple` (size chuẩn), mỗi lot là 1 lệnh sản xuất
độc lập (có thể chạy dây chuyền/thời điểm khác nhau), cùng chung hạn giao.
Lot dư cuối cùng giữ nguyên số lẻ, không làm tròn lên/xuống - trừ khi dư quá
nhỏ so với `min_lot_size` thì gộp vào lot liền trước. Không cấu hình
(`lot_size_multiple=None`, mặc định) = không chia. Chi tiết & ví dụ ở mục 4
của [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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

Dữ liệu mẫu ở `data/sample/factory_demo.json`: 3 sản phẩm dùng chung linh
kiện - ghế & bàn nhựa (MTO) và ghế đẩu nhựa (MTS, minh hoạ lệnh bổ sung tồn
kho tự sinh), mỗi sản phẩm đều cấu hình `lot_size_multiple` nên PO/Forecast
sẽ bị chia thành nhiều lot khi in ra (vd. PO 1100 cái chia 3 lot: 400+400+300),
3 dây chuyền (2 dây chuyền ép nhựa dùng chung 1 tổ nhân lực, 1 dây chuyền
lắp ráp riêng), 4 PO + 3 dòng dự báo. `LINE-A1` khai báo
`sequence_changeovers` (đổi khuôn ghế↔bàn tốn 90 phút, cùng sản phẩm liên
tiếp = 0 phút) để minh hoạ changeover phụ thuộc thứ tự; `LINE-A2`/`LINE-B`
vẫn dùng changeover phẳng cũ - cả 2 cách cùng tồn tại trong 1 dataset.

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
  demand.py     Gộp PO + Forecast -> DemandLine (nhánh MTO: 1-1; nhánh MTS:
                mô phỏng tồn kho -> lệnh bổ sung theo reorder point)
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
chia 1 lot ra nhiều dây chuyền chạy song song, ràng buộc nhân lực đa kỹ
năng/đa lịch, changeover tiêu tốn nhân lực (hiện sequence-dependent
changeover đã hỗ trợ - xem mục 8b - nhưng khoảng changeover chưa tính vào
Cumulative), đa cấp BOM, lưu trữ DB thay vì JSON, giao diện Gantt chart.
