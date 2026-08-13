# Kiến trúc hệ thống lập kế hoạch sản xuất

## 1. Bài toán

Đầu vào:

- **PO** (`SalesOrder`): đơn hàng khách đã chốt — sản phẩm, số lượng, ngày
  cần giao (`requested_ship_date`), độ ưu tiên.
- **Forecast** (`ForecastEntry`): dự báo nhu cầu theo giai đoạn, chưa chốt.
- Dữ liệu nền: sản phẩm + BOM, tồn kho (thành phẩm & linh kiện), lịch nhập
  hàng linh kiện, dây chuyền sản xuất + lịch làm việc + năng suất, tổ nhân lực.

Đầu ra: **kế hoạch sản xuất** — mỗi nhu cầu được gán vào dây chuyền nào, chạy
từ lúc nào đến lúc nào, kèm **ETA** (nguyên vật liệu sẵn sàng) và **ETD**
(thành phẩm sẵn sàng xuất), có đúng hạn hay không.

Đây là bài toán MRP (Material Requirements Planning) nối với APS (Advanced
Planning & Scheduling) — chuẩn công nghiệp là 2 giai đoạn tách rời nhưng liên
kết chặt: MRP quyết định "khi nào có NVL để bắt đầu", APS quyết định "bắt đầu
ở đâu, chạy đến khi nào".

## 2. Luồng xử lý (pipeline.py)

```
demand.build_demand_lines()
  -> net PO/Forecast, trừ tồn kho thành phẩm, quy đổi lot size
  -> list[DemandLine]

mrp.allocate_materials()
  -> nổ BOM từng DemandLine, phân bổ tồn kho + lô nhập hàng linh kiện
     THEO THỨ TỰ ƯU TIÊN (priority, rồi due date sớm nhất)
  -> dict[demand_line_id -> MaterialReadiness{eta, blocked, shortages}]

scheduler.schedule_production()  (OR-Tools CP-SAT)
  -> loại các đơn bị blocked (thiếu NVL)
  -> với các đơn còn lại: chọn 1 dây chuyền đủ điều kiện, xếp thời gian
     không chồng lấn, không sớm hơn ETA NVL, không vượt lịch làm việc,
     tôn trọng ràng buộc nhân lực dùng chung, tối thiểu hoá trễ hạn có
     trọng số ưu tiên
  -> ScheduleResult{scheduled, unscheduled, workforce_warnings}

eta_etd.build_plan_report()
  -> ETD = thời điểm hoàn thành sản xuất + buffer QC/đóng gói
  -> so sánh ETD với due date -> on_time / delay_hours
  -> PlanReport
```

## 3. Mô hình lịch làm việc (calendar.py) — "trục thời gian nén"

Mỗi dây chuyền có lịch làm việc riêng (ca kíp theo tuần + ngoại lệ theo
ngày: nghỉ lễ / tăng ca). Thay vì lập lịch trên trục thời gian thực (dễ phải
xử lý hàng trăm khoảng nghỉ rời rạc trong solver), ta **nén** trục thời gian:
chỉ số hoá các *slot* làm việc liên tiếp của riêng dây chuyền đó
(`CompressedTimeline`). Một lệnh sản xuất kéo dài 3 ca sẽ chiếm 3 khối liên
tiếp trên trục nén — khi map ngược lại trục thời gian thực, nó tự động "nghỉ"
đúng lúc ngoài ca và "chạy tiếp" đúng lúc vào ca, mà không cần khai báo tường
minh từng khoảng nghỉ cho CP-SAT.

Hạn giao (`due_date`) được quy đổi sang cùng toạ độ nén của dây chuyền đang
xét bằng số slot làm việc *trước* thời điểm đó — hàm đơn điệu không giảm nên
việc so sánh "hoàn thành trước/sau hạn" vẫn đúng dù `due_date` rơi đúng vào
giờ nghỉ.

## 4. MRP — phân bổ tồn kho/linh kiện theo thứ tự ưu tiên (mrp.py)

Với mỗi linh kiện, dựng "đường cung tích luỹ":

```
mốc 0 (reference_start): tồn kho khả dụng = on_hand - safety_stock
mốc t1: + lô nhập hàng gần nhất (incoming_receipts)
mốc t2: + lô tiếp theo
...
```

Xử lý các `DemandLine` **theo thứ tự ưu tiên** (PO ưu tiên cao > PO thường >
Forecast, rồi đến due date sớm nhất), mỗi đơn "đặt chỗ" (reserve) đúng lượng
cần trên đường cung — đây chính là kỹ thuật *pegging* đơn giản hoá trong MRP
cổ điển, đảm bảo hai đơn không cùng "giành" một đơn vị tồn kho ảo. Thời điểm
lượng đặt chỗ được thoả chính là ETA của linh kiện đó cho đơn hàng.

ETA nguyên vật liệu của một đơn = **max** ETA của mọi linh kiện trong BOM
(đơn chỉ bắt đầu được khi TẤT CẢ linh kiện đã sẵn sàng). Nếu không đủ nguồn
cung trong toàn bộ các lô đã biết, đơn bị đánh dấu `blocked` kèm danh sách
thiếu hụt để bộ phận mua hàng xử lý — các đơn này **không** được đưa vào
scheduler (không thể lập lịch sản xuất khi chưa biết bao giờ có NVL).

## 5. Scheduler — CP-SAT (scheduler.py)

Biến quyết định: với mỗi cặp (đơn, dây chuyền đủ điều kiện) khả thi trong
horizon, có `assigned` (bool) + `start`/`end` (interval trên trục nén của
dây chuyền đó).

Ràng buộc:

| Ràng buộc | Cách mô hình hoá |
|---|---|
| Mỗi đơn dùng đúng 1 dây chuyền | `sum(assigned[d, *]) == 1` |
| Dây chuyền chạy 1 lệnh/lúc, đúng giờ làm việc | `AddNoOverlap` trên interval (trục đã nén theo lịch riêng từng line) |
| Không sản xuất trước khi có NVL | domain của `start` bắt đầu từ slot sớm nhất ≥ ETA |
| Nhân lực dùng chung nhiều dây chuyền | `AddCumulative` gộp interval của các line cùng tổ **nếu các line đó có lịch làm việc giống hệt nhau** (cùng toạ độ trục nén) |
| Ưu tiên đơn gấp/quan trọng | mục tiêu tối thiểu hoá **tổng trễ hạn có trọng số** theo priority (HIGH=100, NORMAL=10, LOW=1) |

## 6. ETA / ETD (eta_etd.py)

- **ETA** (Estimated Time of Arrival): thời điểm nguyên vật liệu/linh kiện
  sẵn sàng đầy đủ để bắt đầu sản xuất — đầu ra của MRP.
- **ETD** (Estimated Time of Departure): thời điểm thành phẩm sẵn sàng xuất
  xưởng = thời điểm sản xuất hoàn thành (đầu ra scheduler) + thời gian
  QC/đóng gói (`Product.post_production_buffer_hours`).
- So sánh `ETD` với `due_date` (hạn PO/kỳ Forecast) ⇒ `on_time` / `delay_hours`.

## 7. Giới hạn & hướng mở rộng (v1)

Đây là một MVP có kiến trúc đúng đắn cho bài toán thật, nhưng có vài đơn giản
hoá được ghi nhận rõ ràng để dễ mở rộng:

1. **Không chia nhỏ lô** — mỗi `DemandLine` chạy trọn vẹn trên 1 dây chuyền.
   Muốn chia lô song song nhiều dây chuyền: tách `DemandLine` thành nhiều
   sub-lot trước khi đưa vào scheduler (demand.py), hoặc mở rộng scheduler
   cho phép `assigned` > 1 dây chuyền với biến `qty_on_line`.
2. **Nhân lực dùng chung khác lịch làm việc** — nếu các dây chuyền trong
   cùng tổ nhân lực có lịch làm việc KHÁC NHAU, hệ thống chỉ cảnh báo
   (`workforce_warnings`) chứ chưa enforce ràng buộc cứng (vì 2 trục nén khác
   nhau không thể gộp trực tiếp vào 1 `Cumulative`). Hướng mở rộng: dựng một
   "trục thời gian chủ" (master calendar) dùng chung, tách mỗi lệnh sản xuất
   thành các đoạn theo từng ca thực tế (segmentation), rồi cộng dồn nhân lực
   theo ngày/ca trên trục chủ đó — đúng như cách các hệ APS thương mại
   (SAP PP/DS, Preactor…) xử lý.
3. **Changeover đơn giản hoá** — thời gian chuyển đổi (`changeover_minutes`)
   được cộng cố định vào mọi lệnh, chưa phụ thuộc vào sản phẩm chạy TRƯỚC đó
   trên cùng dây chuyền (sequence-dependent changeover). Có thể mở rộng bằng
   `AddCircuit`/`ArcCost` theo cặp sản phẩm liên tiếp.
4. **BOM 1 cấp** — chưa hỗ trợ bán thành phẩm (linh kiện được sản xuất nội bộ
   từ linh kiện khác, đa cấp). Có thể mở rộng bằng cách đệ quy `mrp.py`.
5. **Không có persistence** — dữ liệu đọc/ghi qua JSON (`io_utils.py`). Cho
   production thật nên chuyển sang DB (Postgres) + migration, và API cần auth.
6. **Không có UI trực quan (Gantt)** — `PlanReport` hiện là dữ liệu thô; có
   thể build thêm dashboard (vd. React + timeline chart) đọc từ `/plan/run`.
