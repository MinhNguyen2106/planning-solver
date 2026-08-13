# Kiến trúc hệ thống lập kế hoạch sản xuất

## 1. Bài toán

Đầu vào:

- **PO** (`SalesOrder`): đơn hàng khách đã chốt — sản phẩm, số lượng, độ ưu
  tiên, và cam kết giao hàng khai báo bằng **ETA hoặc ETD của chính PO đó**
  (xem mục 2 bên dưới).
- **Forecast** (`ForecastEntry`): dự báo nhu cầu theo giai đoạn, chưa chốt —
  dùng `period_end` làm hạn (dự báo chưa có khách/tuyến giao hàng cụ thể nên
  không có ETA/ETD riêng).
- Dữ liệu nền: sản phẩm + BOM, tồn kho (thành phẩm & linh kiện), lịch nhập
  hàng linh kiện, dây chuyền sản xuất + lịch làm việc + năng suất, tổ nhân
  lực, khách hàng (`Customer` — transit lead time để quy đổi ETA↔ETD).

Đầu ra: **kế hoạch sản xuất** — mỗi nhu cầu được gán vào dây chuyền nào, chạy
từ lúc nào đến lúc nào, kèm **ETA nguyên vật liệu** (đủ NVL từ lúc nào —
mrp.py) và **ETD hệ thống tính toán** (thành phẩm thực tế sẵn sàng xuất dựa
trên lịch chạy — eta_etd.py), so với **ETD mục tiêu** (quy đổi từ PO/Forecast)
để biết có đúng hạn hay không.

## 2. Hai khái niệm ETA/ETD KHÔNG được nhầm lẫn

Hệ thống có **hai cặp ETA/ETD hoàn toàn khác nhau**, cố tình đặt tên riêng để
không lẫn:

| | Ý nghĩa | Nguồn | Field |
|---|---|---|---|
| **ETA/ETD của PO** | Cam kết giao hàng với KHÁCH. PO khai báo ETA (hàng đến tay khách) HOẶC ETD (hàng rời xưởng) — chỉ cần 1 trong 2. | Input (`SalesOrder.requested_eta` / `requested_etd`) | Quy đổi thành `DemandLine.due_date` — **ETD mục tiêu** |
| **ETA của nguyên vật liệu** | Khi nào linh kiện/NVL đủ để BẮT ĐẦU sản xuất đơn đó | Tính toán bởi MRP (mrp.py) | `PlanLine.eta` |
| **ETD hệ thống tính** | Khi nào thành phẩm THỰC SỰ sẵn sàng xuất, dựa trên lịch chạy dây chuyền đã giải + buffer QC/đóng gói | Tính toán bởi scheduler + eta_etd.py | `PlanLine.etd` |

Quy đổi ETA(PO) → ETD mục tiêu (`demand.commitment_etd`):

```
nếu PO có requested_etd:            ETD mục tiêu = requested_etd  (dùng thẳng)
nếu PO chỉ có requested_eta:        ETD mục tiêu = requested_eta − transit_lead_time_days(khách hàng)
```

`transit_lead_time_days` tra từ `Customer` (master data khách hàng/tuyến giao
hàng) — mỗi PO chỉ tham chiếu `customer_id`, không tự khai báo lead time
riêng, để chuẩn hoá dữ liệu vận chuyển tập trung một chỗ.

**Phép trừ ngày ở trên có 2 cách tính, chọn qua `Customer.transit_lead_time_mode`
(`DateCountMode`)** — vì "3 ngày" có thể hiểu là 3 ngày lịch hoặc 3 ngày làm
việc của bên vận chuyển/hải quan, và hai cách hiểu cho kết quả khác nhau bất
cứ khi nào lead time "vắt" qua cuối tuần/ngày lễ:

| Mode | Cách tính | Khi nào dùng |
|---|---|---|
| `calendar_days` (mặc định) | Trừ thẳng `timedelta(days=N)` — tính cả T7/CN/lễ | Lead time đã bao gồm buffer cho cuối tuần, hoặc bên vận chuyển chạy cả cuối tuần |
| `working_days` | Trừ N **ngày làm việc**, tự động nhảy qua ngày không làm việc — xem `calendar.subtract_working_days` | Lead time do hãng vận chuyển/hải quan công bố theo "ngày làm việc" (vd. "3 ngày làm việc" thực ra mất hơn 3 ngày lịch nếu rơi vào cuối tuần) |

Ngày nào là "ngày làm việc" cho mục đích này được xác định bởi
`PlanningDataset.logistics_calendar` (một `WorkCalendar` dùng CHUNG cho toàn
bộ việc tính ETA/ETD — độc lập với lịch của từng dây chuyền sản xuất). Nếu
không khai báo, hệ thống dùng lịch mặc định T2-T7 (`calendar.default_business_calendar`).
Ví dụ trong `data/sample/factory_demo.json`: `CUST-CAFE` dùng `working_days`
nên PO có `requested_eta` rơi vào Thứ 2 sẽ bị lùi ETD mục tiêu về Thứ 7 tuần
trước (bỏ qua Chủ nhật), trong khi `CUST-HP` dùng `calendar_days` thì trừ
thẳng theo ngày lịch.

`DemandLine.due_date` = ETD mục tiêu này (PO) hoặc `period_end` (Forecast).
Đây là mục tiêu cho scheduler: **số lượng hàng phải hoàn thành sản xuất
(+ buffer QC/đóng gói) trước ETD mục tiêu** — cụ thể là ràng buộc **MỀM**
(xem mục 6): scheduler luôn cố xếp lịch cho mọi đơn khả thi (đủ NVL, có dây
chuyền), tối thiểu hoá tổng số giờ trễ so với ETD mục tiêu theo trọng số ưu
tiên, thay vì loại hẳn đơn ra khỏi kế hoạch nếu không kịp — vì trong thực tế
kế hoạch viên vẫn cần thấy TOÀN BỘ đơn (kể cả đơn trễ) để biết mà xử lý
(thương lượng lại khách, tăng ca, đặt gia công ngoài...), không phải để hệ
thống âm thầm bỏ qua.

So sánh cuối cùng: `PlanLine.etd (ETD hệ thống tính) <= PlanLine.due_date
(ETD mục tiêu)` ⇒ `on_time`. Vì `etd` đã cộng thêm buffer QC/đóng gói, điều
kiện này thực chất đòi hỏi sản xuất phải hoàn thành SỚM HƠN ETD mục tiêu ít
nhất bằng khoảng buffer đó — đúng với thực tế cần thời gian đóng gói/QC
trước khi hàng có thể rời xưởng.

Đây là bài toán MRP (Material Requirements Planning) nối với APS (Advanced
Planning & Scheduling) — chuẩn công nghiệp là 2 giai đoạn tách rời nhưng liên
kết chặt: MRP quyết định "khi nào có NVL để bắt đầu", APS quyết định "bắt đầu
ở đâu, chạy đến khi nào".

## 3. Chiến lược lập kế hoạch: Make-to-Order (MTO) vs Make-to-Stock (MTS)

Mọi thứ ở mục 1-2 mô tả nhu cầu theo kiểu **Make-to-Order (MTO)**: mỗi PO/kỳ
Forecast sinh ra MỘT lệnh sản xuất riêng, hạn giao = ETD mục tiêu của chính
PO/Forecast đó. Đây là mặc định, phù hợp khi sản xuất gắn trực tiếp với đơn
hàng cụ thể.

Nhiều sản phẩm thực tế lại sản xuất **để tồn kho (Make-to-Stock, MTS)** — sản
xuất theo lô lớn, đều đặn, để LUÔN CÓ SẴN hàng trong kho, khách đặt là xuất
ngay, không đợi sản xuất. Với các sản phẩm này, gắn 1 lệnh sản xuất cho từng
PO là sai bản chất: PO chỉ nên **rút hàng từ kho có sẵn**, còn việc "khi nào
sản xuất, sản xuất bao nhiêu" phải dựa vào MỨC TỒN KHO, không phải từng đơn.

Chọn chiến lược qua `Product.planning_strategy` (`models.PlanningStrategy`,
mặc định `make_to_order`). Khi đặt `make_to_stock`, sản phẩm cần thêm 2
tham số chính sách tồn kho:

| Field | Ý nghĩa |
|---|---|
| `reorder_point` | Ngưỡng tồn kho dự kiến — chạm ngưỡng này là phải có lệnh sản xuất bổ sung |
| `target_stock_level` | Mức tồn kho mục tiêu sau khi bổ sung (up-to-level); phải > `reorder_point` (validate ở `Product`) |
| `replenishment_priority` | Độ ưu tiên gán cho các lệnh bổ sung tự sinh (mặc định NORMAL) — cùng thang với PO nên vẫn cạnh tranh nguyên vật liệu/dây chuyền công bằng với các đơn MTO khác theo đúng độ ưu tiên |

### Thuật toán (demand.build_mts_replenishment_lines)

Với sản phẩm MTS, PO/Forecast **không** sinh `DemandLine` riêng — chúng được
gộp thành một dòng thời gian các **sự kiện tiêu thụ tồn kho** (PO tại ETD mục
tiêu của nó, Forecast tại `period_end`, đã net PO-trong-kỳ như MTO). Sau đó
mô phỏng tồn kho dự kiến (time-phased) đi qua các sự kiện này theo thứ tự
thời gian:

```
tồn kho = tồn kho hiện tại (FinishedGoodsInventory)
nếu tồn kho <= reorder_point ngay từ đầu: sinh lệnh bổ sung NGAY (due_date = "hiện tại")

với mỗi sự kiện tiêu thụ (theo thời gian tăng dần):
    tồn kho -= qty tiêu thụ
    nếu tồn kho <= reorder_point:
        sinh 1 DemandLine (source=REPLENISHMENT) qty = target_stock_level - tồn kho,
        due_date = thời điểm sự kiện này (hạn PHẢI CÓ hàng bổ sung)
        tồn kho += qty vừa sinh   # coi như bổ sung "về kho" ngay để mô phỏng tiếp
```

Đây chính là phương pháp reorder-point/up-to-level (chính sách `(s, S)`) kinh
điển trong quản lý tồn kho, dùng chung kỹ thuật "đường cung tích luỹ theo thời
gian" như MRP linh kiện ở mục 6 — chỉ khác là áp dụng cho TỒN KHO THÀNH PHẨM
thay vì linh kiện đầu vào.

`DemandLine` sinh ra từ nhánh MTS có `due_date` = "hạn phải có hàng bổ sung"
và đi qua **CHÍNH XÁC CÙNG một scheduler** như đơn MTO (cùng là ràng buộc MỀM,
mục 7) — nghĩa là lệnh bổ sung tồn kho cạnh tranh sòng phẳng về nguyên vật
liệu và dây chuyền với các PO khác theo đúng độ ưu tiên, không có đối xử đặc
biệt. Ví dụ trong `data/sample/factory_demo.json`: `P-STOOL` (MTS) có tồn
kho ban đầu dưới `reorder_point` nên cần bổ sung ngay, nhưng vì linh kiện
dùng chung (`C-LEG`) đã bị 2 PO ưu tiên HIGH giành hết tồn kho sẵn có, lệnh
bổ sung này phải đợi đến lô nhập linh kiện tiếp theo mới có NVL để sản xuất —
kết quả là bị trễ so với hạn "bổ sung ngay", đúng như hành vi mong đợi của
ràng buộc mềm.

## 4. Luồng xử lý (pipeline.py)

```
demand.build_demand_lines()
  -> quy đổi ETA/ETD của từng PO ra ETD mục tiêu (commitment_etd, mục 2)
  -> net PO/Forecast, trừ tồn kho thành phẩm, quy đổi lot size
  -> list[DemandLine]  (due_date = ETD mục tiêu)

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

## 5. Mô hình lịch làm việc (calendar.py) — "trục thời gian nén"

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

## 6. MRP — phân bổ tồn kho/linh kiện theo thứ tự ưu tiên (mrp.py)

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

## 7. Scheduler — CP-SAT (scheduler.py)

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
| Ưu tiên đơn gấp/quan trọng, **ETD mục tiêu là ràng buộc MỀM** | mục tiêu tối thiểu hoá **tổng trễ hạn có trọng số** so với `due_date` (= ETD mục tiêu, mục 2) theo priority (HIGH=100, NORMAL=10, LOW=1). Đơn không kịp ETD mục tiêu vẫn được xếp lịch (trễ), KHÔNG bị loại khỏi kế hoạch — chỉ những đơn thiếu NVL (xem mục 6) hoặc hết chỗ trong horizon lập kế hoạch mới bị loại (`unscheduled`). |

## 8. ETA / ETD — tổng hợp lại 3 mốc trong `PlanLine` (eta_etd.py)

`eta_etd.build_plan_report()` gộp 3 mốc thời gian (đã giải thích ở mục 2)
thành từng dòng `PlanLine`:

- `eta`: **ETA nguyên vật liệu** — đầu ra của MRP (mục 6), khi nào đủ NVL để
  bắt đầu sản xuất đơn này.
- `etd`: **ETD hệ thống tính** = thời điểm sản xuất hoàn thành (đầu ra
  scheduler, mục 7) + thời gian QC/đóng gói (`Product.post_production_buffer_hours`).
- `due_date`: **ETD mục tiêu** — quy đổi từ `SalesOrder.requested_eta`/
  `requested_etd` (PO) hoặc `period_end` (Forecast), xem mục 2.
- `on_time = (etd <= due_date)`, `delay_hours = max(0, etd − due_date)`.

## 9. Giới hạn & hướng mở rộng (v1)

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
7. **Transit lead time ở cấp khách hàng, chưa phải tuyến/phương thức vận
   chuyển** — `Customer.transit_lead_time_days` là MỘT con số cố định cho
   mỗi khách hàng. Nếu một khách có nhiều kho nhận hàng/nhiều phương thức
   vận chuyển (đường bộ nhanh vs. đường biển chậm) với lead time khác nhau,
   cần tách thành entity `ShippingLane` riêng (khách hàng × phương thức/kho
   nhận → lead time), PO tham chiếu `shipping_lane_id` thay vì `customer_id`
   trực tiếp.
8. **Mô phỏng tồn kho MTS (mục 3) là ĐIỂM (point simulation), không phải MRP
   time-phased đầy đủ** — bỏ qua các lệnh bổ sung ĐÃ ĐANG SẢN XUẤT (open
   replenishment orders chưa hoàn thành) khi tính tồn kho dự kiến ở lần chạy
   kế hoạch tiếp theo; mỗi lần `run_planning()` chạy lại từ đầu dựa trên
   `FinishedGoodsInventory.on_hand_qty` hiện tại, không "nhớ" các lệnh bổ
   sung đã sinh ở lần chạy trước. Phù hợp cho lập kế hoạch định kỳ (chạy lại
   mỗi ngày/tuần với tồn kho mới nhất) nhưng chưa tối ưu cho theo dõi liên
   tục trong ngày. Hướng mở rộng: thêm khái niệm "lệnh sản xuất đang mở"
   (open production order, có qty + expected completion date) như một nguồn
   cung bổ sung trong mô phỏng, tương tự `Component.incoming_receipts`.
9. **1 chính sách tồn kho duy nhất cho mỗi sản phẩm MTS** — không hỗ trợ
   tồn kho mục tiêu thay đổi theo mùa vụ (vd. tăng target_stock_level trước
   mùa cao điểm). Có thể mở rộng bằng cách cho `reorder_point`/
   `target_stock_level` biến thiên theo khoảng thời gian thay vì hằng số.
