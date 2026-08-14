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
(xem mục 8): scheduler luôn cố xếp lịch cho mọi đơn khả thi (đủ NVL, có dây
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
gian" như MRP linh kiện ở mục 7 — chỉ khác là áp dụng cho TỒN KHO THÀNH PHẨM
thay vì linh kiện đầu vào.

`DemandLine` sinh ra từ nhánh MTS có `due_date` = "hạn phải có hàng bổ sung"
và đi qua **CHÍNH XÁC CÙNG một scheduler** như đơn MTO (cùng là ràng buộc MỀM,
mục 8) — nghĩa là lệnh bổ sung tồn kho cạnh tranh sòng phẳng về nguyên vật
liệu và dây chuyền với các PO khác theo đúng độ ưu tiên, không có đối xử đặc
biệt. Ví dụ trong `data/sample/factory_demo.json`: `P-STOOL` (MTS) có tồn
kho ban đầu dưới `reorder_point` nên cần bổ sung ngay, nhưng vì linh kiện
dùng chung (`C-LEG`) đã bị 2 PO ưu tiên HIGH giành hết tồn kho sẵn có, lệnh
bổ sung này phải đợi đến lô nhập linh kiện tiếp theo mới có NVL để sản xuất —
kết quả là bị trễ so với hạn "bổ sung ngay", đúng như hành vi mong đợi của
ràng buộc mềm.

## 4. Chia lot sản xuất (`Product.split_into_lots`, `demand.split_into_demand_lines`)

Số lượng ròng cuối cùng của MỘT PO/Forecast/lệnh bổ sung tồn kho (mục 1-3)
**KHÔNG được làm tròn thành 1 con số duy nhất** - nó được **CHIA thành nhiều
lot sản xuất** (nhiều `DemandLine`), mỗi lot đi qua scheduler NHƯ MỘT LỆNH
SẢN XUẤT ĐỘC LẬP (có thể chạy trên dây chuyền khác nhau, thời điểm khác
nhau). Đây là hành vi thực tế của một nhà máy: một đơn 1100 cái không chạy
liền một mạch 1100 cái, mà chia thành nhiều lô đúc/ép theo khuôn, theo pallet
đóng gói, v.v.

Cấu hình trên `Product`:

| Field | Ý nghĩa |
|---|---|
| `lot_size_multiple` | Size CHUẨN của 1 lot. `None` (mặc định) = KHÔNG chia, cả nhu cầu là 1 lot duy nhất |
| `min_lot_size` | Ngưỡng gộp: nếu lot dư cuối < giá trị này, gộp vào lot liền trước thay vì đứng riêng. **Bắt buộc khai báo cùng `lot_size_multiple`** (validate ở `Product`) — cả hai cùng `None` hoặc cùng có giá trị, và `min_lot_size <= lot_size_multiple` |

Thuật toán `Product.split_into_lots(qty)`:

```
nếu lot_size_multiple = None: trả về [qty]                          # không chia
n_full = số lot ĐẦY ĐỦ = floor(qty / lot_size_multiple)
dư = qty - n_full × lot_size_multiple

nếu dư == 0:            trả về [lot_size_multiple] × n_full          # chia hết, không lot lẻ
nếu dư < min_lot_size và n_full > 0:
                         trả về [lot_size_multiple]×(n_full-1) + [lot_size_multiple + dư]
                         # gộp phần dư vào lot cuối (lot cuối > size chuẩn)
ngược lại:               trả về [lot_size_multiple]×n_full + [dư]
                         # dư đủ lớn (hoặc không có lot nào để gộp vào) -> đứng riêng, GIỮ NGUYÊN
```

**Điểm mấu chốt: KHÔNG bao giờ làm tròn LÊN hay XUỐNG.** Tổng các lot trả về
luôn bằng đúng `qty` đưa vào — khác hẳn hành vi "làm tròn lên bội số gần
nhất" ở các phiên bản trước. Ví dụ: `qty=101, lot_size_multiple=50,
min_lot_size=50` → `[50, 51]` (không phải `150`); `qty=5` (nhỏ hơn cả 1 lot
chuẩn) → `[5]` (không phải `50`).

`demand.split_into_demand_lines()` bọc kết quả trên thành các `DemandLine`
riêng biệt: tất cả lot con của CÙNG một PO/Forecast/lệnh bổ sung dùng
**CHUNG 1 `due_date`** (ETD mục tiêu gốc - mọi lot đều phải xong trước cùng
1 hạn, vì bản chất vẫn là 1 đơn hàng, chỉ chia nhỏ để sản xuất song song/nối
tiếp) và **CHUNG `ref_id`** (truy vết về đúng 1 PO/Forecast/lệnh bổ sung gốc
dù có bao nhiêu lot), chỉ khác nhau `id` (hậu tố `-L{n}`, vd `SO-SO-1001-L1`,
`SO-SO-1001-L2`, ...) và `qty`. Áp dụng thống nhất cho cả 3 nguồn: PO
(MTO), Forecast (MTO), và lệnh bổ sung tồn kho (MTS).

Ví dụ trong `data/sample/factory_demo.json`: `P-CHAIR` có
`lot_size_multiple=400` → PO `SO-1001` (net 1100 cái) chia thành 3 lot:
400 + 400 + 300, cả 3 cùng hạn giao, có thể được scheduler xếp trên các dây
chuyền/thời điểm khác nhau để rút ngắn thời gian hoàn thành tổng thể.

## 5. Luồng xử lý (pipeline.py)

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

## 6. Mô hình lịch làm việc (calendar.py) — "trục thời gian nén"

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

## 7. MRP — phân bổ tồn kho/linh kiện theo thứ tự ưu tiên (mrp.py)

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

## 8. Scheduler — CP-SAT (scheduler.py)

Biến quyết định: với mỗi cặp (đơn, dây chuyền đủ điều kiện) khả thi trong
horizon, có `assigned` (bool) + `start`/`end` (interval trên trục nén của
dây chuyền đó).

Ràng buộc:

| Ràng buộc | Cách mô hình hoá |
|---|---|
| Mỗi đơn dùng đúng 1 dây chuyền | `sum(assigned[d, *]) == 1` |
| Dây chuyền chạy 1 lệnh/lúc, đúng giờ làm việc | `AddNoOverlap` trên interval (trục đã nén theo lịch riêng từng line) - **hoặc** `AddCircuit` nếu dây chuyền có `sequence_changeovers` (xem mục 8b) |
| Không sản xuất trước khi có NVL | domain của `start` bắt đầu từ slot sớm nhất ≥ ETA |
| Nhân lực dùng chung nhiều dây chuyền | `AddCumulative` gộp interval của các line cùng tổ **nếu các line đó có lịch làm việc giống hệt nhau** (cùng toạ độ trục nén) |
| Ưu tiên đơn gấp/quan trọng, **ETD mục tiêu là ràng buộc MỀM** | mục tiêu tối thiểu hoá **tổng trễ hạn có trọng số** so với `due_date` (= ETD mục tiêu, mục 2) theo priority (HIGH=100, NORMAL=10, LOW=1). Đơn không kịp ETD mục tiêu vẫn được xếp lịch (trễ), KHÔNG bị loại khỏi kế hoạch — chỉ những đơn thiếu NVL (xem mục 7) hoặc hết chỗ trong horizon lập kế hoạch mới bị loại (`unscheduled`). |

### 8b. Changeover phụ thuộc thứ tự sản phẩm (sequence-dependent changeover)

Mặc định, `LineProductRate.changeover_minutes` là một hằng số PHẲNG cộng
thẳng vào thời lượng của MỌI lệnh trên dây chuyền, không phân biệt sản phẩm
nào chạy ngay trước nó (`_job_hours`). Đây là hành vi giữ nguyên cho MỌI dây
chuyền KHÔNG khai báo `ProductionLine.sequence_changeovers`.

Khi một dây chuyền khai báo `sequence_changeovers` (khác rỗng), nó chuyển
sang mô hình **phụ thuộc sản phẩm chạy TRƯỚC** (`_production_hours` +
`_add_sequenced_ordering`, dùng `AddCircuit` của CP-SAT thay cho
`AddNoOverlap`):

- Thời lượng interval của mỗi lệnh CHỈ còn thời gian sản xuất thuần
  (`qty / rate_per_hour`) - changeover không còn cộng vào duration.
- Dây chuyền được mô hình hoá như một **vòng khép kín (circuit)** đi qua 1
  node "depot" (đại diện đầu/cuối ca) và mỗi lệnh khả thi là 1 node. Lệnh
  nào KHÔNG thực sự được gán cho dây chuyền này thì "tự vòng" (self-loop
  = `assigned.Not()`) để bị loại khỏi circuit - đúng cơ chế chuẩn của
  `AddCircuit` cho node tuỳ chọn.
- Mỗi cạnh THẬT trong circuit (depot→lệnh đầu tiên, hoặc lệnh này→lệnh kế
  tiếp) mang theo 1 ràng buộc: nếu cạnh đó được chọn, lệnh phía sau phải bắt
  đầu đủ trễ để chừa đúng khoảng changeover tương ứng
  (`ProductionLine.changeover_minutes_for(from_product, to_product)`, quy
  đổi ra slot qua `CompressedTimeline.changeover_slots` - **không** có sàn
  tối thiểu 1 slot như `duration_in_slots`, cho phép changeover = 0 khi 2
  lệnh cùng sản phẩm chạy liên tiếp).
- `changeover_minutes_for` tra theo 3 tầng: (1) luật khớp CHÍNH XÁC cặp
  (from, to); (2) nếu from là 1 sản phẩm thật, fallback về luật "wildcard"
  (from=None, to) làm mặc định dùng chung; (3) fallback cuối:
  `changeover_minutes` phẳng của `LineProductRate` - đảm bảo dây chuyền
  không khai báo `sequence_changeovers` LUÔN rơi vào tầng 3, tái hiện đúng
  hành vi cũ.

**2 điều cần lưu ý minh bạch** khi dùng cơ chế này:
1. `PlanLine.production_start`/`ScheduledJob.start_dt` đổi ý nghĩa trên dây
   chuyền đã opt-in: giờ là lúc **BẮT ĐẦU SẢN XUẤT thực sự** (sau khi đã trừ
   khoảng changeover), không còn gộp changeover vào đầu block như trước.
   `end_dt` (sản xuất xong) và mọi thứ hạ nguồn (`eta_etd.py`) không đổi ý
   nghĩa.
2. Khoảng changeover (gap) hiện **KHÔNG tiêu tốn nhân lực** trong
   `AddCumulative` - nó nằm ngoài mọi `IntervalVar`, khác với dây chuyền cũ
   (changeover nằm trong interval nên có tính vào Cumulative). Xem thêm mục
   Giới hạn.

Chọn `AddCircuit` (thay vì cố gắn thêm vào `AddNoOverlap`) theo đúng gợi ý
đã ghi trong bản thiết kế ban đầu của tài liệu này: `AddNoOverlap` không
lộ ra cấu trúc "lệnh nào đứng ngay trước lệnh nào" để gắn chi phí/khoảng
trống theo cặp sản phẩm liên tiếp; `AddCircuit` (mô hình vòng Hamilton có
node tuỳ chọn) là cơ chế chuẩn của CP-SAT cho đúng bài toán này.

## 9. ETA / ETD — tổng hợp lại 3 mốc trong `PlanLine` (eta_etd.py)

`eta_etd.build_plan_report()` gộp 3 mốc thời gian (đã giải thích ở mục 2)
thành từng dòng `PlanLine`:

- `eta`: **ETA nguyên vật liệu** — đầu ra của MRP (mục 7), khi nào đủ NVL để
  bắt đầu sản xuất đơn này.
- `etd`: **ETD hệ thống tính** = thời điểm sản xuất hoàn thành (đầu ra
  scheduler, mục 8) + thời gian QC/đóng gói (`Product.post_production_buffer_hours`).
- `due_date`: **ETD mục tiêu** — quy đổi từ `SalesOrder.requested_eta`/
  `requested_etd` (PO) hoặc `period_end` (Forecast), xem mục 2.
- `on_time = (etd <= due_date)`, `delay_hours = max(0, etd − due_date)`.

## 10. Giới hạn & hướng mở rộng (v1)

Đây là một MVP có kiến trúc đúng đắn cho bài toán thật, nhưng có vài đơn giản
hoá được ghi nhận rõ ràng để dễ mở rộng:

1. **Mỗi LOT (sau khi đã chia theo mục 4) vẫn chạy trọn vẹn trên 1 dây
   chuyền** — không hỗ trợ chia tiếp MỘT lot ra nhiều dây chuyền chạy song
   song. Đã đạt hiệu ứng "chia nhỏ để chạy song song" ở mức PO/Forecast
   thông qua `lot_size_multiple` (nhiều lot, mỗi lot tự do được rải trên
   dây chuyền khác nhau) — nhưng đó là do NGƯỜI DÙNG chọn trước kích thước
   lot cố định, không phải solver tự động chia động theo tải hiện tại của
   từng dây chuyền.
2. **Kích thước lot ảnh hưởng trực tiếp đến độ lớn mô hình CP-SAT** —
   `lot_size_multiple` càng nhỏ, số `DemandLine` (biến quyết định trong
   solver) càng nhiều. Với đơn hàng lớn + lot nhỏ (vd. PO 10.000 cái, lot
   50 cái → 200 lot), thời gian giải có thể tăng đáng kể; cân nhắc đặt
   `lot_size_multiple` đủ lớn để phản ánh đúng quy mô lô sản xuất thực tế
   (khuôn/pallet/ca máy), không nên đặt quá nhỏ chỉ để "chia mịn".
3. **Nhân lực dùng chung khác lịch làm việc** — nếu các dây chuyền trong
   cùng tổ nhân lực có lịch làm việc KHÁC NHAU, hệ thống chỉ cảnh báo
   (`workforce_warnings`) chứ chưa enforce ràng buộc cứng (vì 2 trục nén khác
   nhau không thể gộp trực tiếp vào 1 `Cumulative`). Hướng mở rộng: dựng một
   "trục thời gian chủ" (master calendar) dùng chung, tách mỗi lệnh sản xuất
   thành các đoạn theo từng ca thực tế (segmentation), rồi cộng dồn nhân lực
   theo ngày/ca trên trục chủ đó — đúng như cách các hệ APS thương mại
   (SAP PP/DS, Preactor…) xử lý.
4. ~~Changeover đơn giản hoá~~ **ĐÃ GIẢI QUYẾT** — sequence-dependent
   changeover (phụ thuộc sản phẩm chạy TRƯỚC) đã hỗ trợ qua
   `ProductionLine.sequence_changeovers` + `AddCircuit` (xem mục 8b), opt-in
   theo từng dây chuyền. Hạn chế còn lại của cơ chế này: (a) khoảng
   changeover chưa tiêu tốn nhân lực trong `AddCumulative` (nằm ngoài mọi
   `IntervalVar` - modeling đúng đòi hỏi thêm 1 tầng interval động theo cạnh
   được chọn, phức tạp hơn nhiều, để dành cho v2); (b) `AddCircuit` tốn
   O(n²) biến theo số lệnh khả thi trên 1 dây chuyền - không nên bật cho
   dây chuyền có hàng trăm lệnh (xem giới hạn tiếp theo về kích thước lot).
5. **BOM 1 cấp** — chưa hỗ trợ bán thành phẩm (linh kiện được sản xuất nội bộ
   từ linh kiện khác, đa cấp). Có thể mở rộng bằng cách đệ quy `mrp.py`.
6. **Không có persistence** — dữ liệu đọc/ghi qua JSON (`io_utils.py`). Cho
   production thật nên chuyển sang DB (Postgres) + migration, và API cần auth.
7. **Không có UI trực quan (Gantt)** — `PlanReport` hiện là dữ liệu thô; có
   thể build thêm dashboard (vd. React + timeline chart) đọc từ `/plan/run`.
8. **Transit lead time ở cấp khách hàng, chưa phải tuyến/phương thức vận
   chuyển** — `Customer.transit_lead_time_days` là MỘT con số cố định cho
   mỗi khách hàng. Nếu một khách có nhiều kho nhận hàng/nhiều phương thức
   vận chuyển (đường bộ nhanh vs. đường biển chậm) với lead time khác nhau,
   cần tách thành entity `ShippingLane` riêng (khách hàng × phương thức/kho
   nhận → lead time), PO tham chiếu `shipping_lane_id` thay vì `customer_id`
   trực tiếp.
9. **Mô phỏng tồn kho MTS (mục 3) là ĐIỂM (point simulation), không phải MRP
   time-phased đầy đủ** — bỏ qua các lệnh bổ sung ĐÃ ĐANG SẢN XUẤT (open
   replenishment orders chưa hoàn thành) khi tính tồn kho dự kiến ở lần chạy
   kế hoạch tiếp theo; mỗi lần `run_planning()` chạy lại từ đầu dựa trên
   `FinishedGoodsInventory.on_hand_qty` hiện tại, không "nhớ" các lệnh bổ
   sung đã sinh ở lần chạy trước. Phù hợp cho lập kế hoạch định kỳ (chạy lại
   mỗi ngày/tuần với tồn kho mới nhất) nhưng chưa tối ưu cho theo dõi liên
   tục trong ngày. Hướng mở rộng: thêm khái niệm "lệnh sản xuất đang mở"
   (open production order, có qty + expected completion date) như một nguồn
   cung bổ sung trong mô phỏng, tương tự `Component.incoming_receipts`.
10. **1 chính sách tồn kho duy nhất cho mỗi sản phẩm MTS** — không hỗ trợ
   tồn kho mục tiêu thay đổi theo mùa vụ (vd. tăng target_stock_level trước
   mùa cao điểm). Có thể mở rộng bằng cách cho `reorder_point`/
   `target_stock_level` biến thiên theo khoảng thời gian thay vì hằng số.
