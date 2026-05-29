# Fraud Detection Logic — Streaming / No Look-Back Design

Reference sources:
- `rules.md`
- `schema.sql`
- `rule2-detection-analysis.md`

Reference tag format:
- `[R1]` = Rule 1
- `[R2]` = Rule 2
- `[SCH]` = schema.sql
- `[EV]` = evidence fields
- `[TL]` = timeline fields

---

# 1. Global Detection Architecture

## 1.1 Processing Model

The fraud engine processes records in strict event-time order:

```text
ORDER BY event_timestamp ASC
```

The engine NEVER queries historical rows again.

Detection must therefore use:

- Sliding windows
- Streaming aggregation
- Stateful caches
- Event correlation
- Temporal joins in memory
- Incremental statistics

---

# 2. Recommended Streaming Engine Design

## 2.1 Core Event Bus

Normalize all source tables into a unified event stream:

| Source Table | Event Type |
|---|---|
| dbo_bkjournal | POS transaction |
| dbo_bkjournal_payment | payment event |
| oil_transactions | fuel sale |
| retail_transactions | retail sale |
| retail_returns | refund |
| inventory_events | stock movement |
| iot_events | IoT sensor event |
| loyalty_events | loyalty usage |

Unified event model:

```json
{
  "event_time": "2026-05-12T10:00:00",
  "event_type": "PAYMENT",
  "table": "dbo_bkjournal_payment",
  "record_id": "JOURNAL_ID",
  "station_id": "POS_ID",
  "employee_id": "USERNAME",
  "payload": {}
}
```

---

# 3. Recommended Cache Design

## 3.1 Required Stateful Caches

| Cache Key | Purpose | TTL |
|---|---|---|
| `card_window:{card_no}` | Fleet repeat detection | 60m |
| `pump_window:{pump_id}` | Split / double-click analysis | 5m |
| `employee_session:{emp}` | Phantom employee | shift |
| `tank_balance:{tank_id}` | Tank stock state | persistent |
| `delivery_batch:{batch}` | Manifest reconciliation | 7d |
| `price_snapshot:{pump}` | Price mismatch | 1d |
| `loyalty_usage:{card}` | Loyalty abuse | 1d |
| `return_window:{receipt}` | Phantom return | 24h |
| `offline_state:{station}` | Offline fraud | shift |
| `sensor_baseline:{tank}` | Sensor tampering | persistent |

---

# 4. Confidence Strategy

Each rule should support:

| Signal Type | Example |
|---|---|
| Base condition | rule predicate matched |
| Time escalation | shorter gap => higher confidence |
| Cross-rule escalation | Rule 2 + Rule 8 together |
| Volume escalation | high THB / liters |
| Repeat escalation | repeated occurrences |
| Manual override escalation | manual EDC / manual release |

Recommended formula:

```text
final_confidence =
  base_confidence
  + temporal_bonus
  + repeat_bonus
  + manual_action_bonus
  + cross_rule_bonus
```

---

# 5. Rule-by-Rule Detection Logic

---

# [R1] Change Payment Type to Fleet Card

## Detection Goal
Cash sale later converted into Fleet payment.

## Primary Tables

| Table | Fields |
|---|---|
| dbo_bkjournal_payment | PAYMENT_TYPE, APPROVE_DATE, VOID_DATE, VALUE |
| dbo_bkjournal | JOURNAL_ID, CREATEDATE, USERNAME, POS_ID |
| dbo_lkpayment_type | PAYMENT_TYPE_GROUP_ID |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| CREATEDATE | fuel/payment started |
| APPROVE_DATE | final Fleet approval |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| PAYMENT_TYPE | changed payment type |
| USERNAME | cashier |
| VALUE | transaction amount |
| APPROVE_DATE - CREATEDATE | suspicious delay |

## Streaming Logic

```text
1. Receive transaction open event
2. Store pending transaction state
3. Wait for payment completion
4. If payment type becomes Fleet after long delay:
      trigger alert
```

## Required Cache

```text
pending_txn:{journal_id}
```

Store:
- create timestamp
- initial payment type
- employee
- amount

TTL: 30m

---

# [R2] Repeat Fleet Card Usage

Reference: `rule2-detection-analysis.md`

## Primary Tables

| Table | Fields |
|---|---|
| dbo_bkjournal_payment | CARD_NO, APPROVE_DATE, PAYMENT_TYPE, VALUE |
| dbo_bkjournal | POS_ID, USERNAME |
| dbo_lkpayment_type | PAYMENT_TYPE_GROUP_ID |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| APPROVE_DATE | fleet approval time |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| CARD_NO | repeated card |
| POS_ID | same station |
| USERNAME | involved cashier |
| VALUE | total amount |

## Streaming Logic

```text
Maintain 60m sliding window per CARD_NO + POS_ID.

IF count(card usage) > 1 within 60m:
   trigger alert

IF gap < 10m:
   increase confidence
```

## Required Cache

```text
card_window:{card_no}:{pos_id}
```

Store:
- approve timestamps
- journal ids
- cashier ids
- values

TTL: 60m

---

# [R3] Fleet Split Transactions

## Primary Tables

| Table | Fields |
|---|---|
| oil_transactions | Pump_ID, Card_ID, Transaction_Time, Volume |
| dbo_bkjournal_payment | CARD_NO, VALUE |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| Transaction_Time | fueling/payment sequence |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| Pump_ID | same pump |
| Card_ID | grouped cards |
| VALUE | split value |
| Volume | split fuel amount |

## Streaming Logic

```text
Window: 5 minutes per Pump_ID.

Count transactions from same pump.
If multiple Fleet payments appear rapidly:
   detect split behavior.
```

## Cache

```text
pump_txn_window:{pump_id}
```

TTL: 5m

---

# [R4] Bundle Payment

## Primary Tables

| Table | Fields |
|---|---|
| oil_transactions | Volume, Total_Amount, Payment_Method |
| dbo_bkjournal_payment | CARD_NO, VALUE |
| dbo_bkjournal | USERNAME, SHIFT_ID |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| CREATEDATE | transaction order |
| APPROVE_DATE | Fleet payment timing |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| VALUE | Fleet amount |
| Total_Amount | grouped cash amount |
| SHIFT_ID | same shift |
| USERNAME | same cashier |

## Streaming Logic

```text
Accumulate recent cash transactions per cashier/shift.

If later Fleet payment ~= sum(cash txns):
   trigger bundle alert.
```

## Cache

```text
cash_pool:{employee}:{shift}
```

Store:
- pending cash amounts
- timestamps
- journal ids

TTL: shift duration

---

# [R5] Off-System Fuel Sale

## Primary Tables

| Table | Fields |
|---|---|
| iot_events | nozzle flow, flow_start, flow_end |
| oil_transactions | Transaction_ID |
| inventory_events | stock balance |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| flow timestamp | nozzle activity |
| transaction timestamp | POS linkage |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| nozzle volume | actual fuel |
| Transaction_ID | missing POS linkage |
| stock difference | unexplained loss |

## Streaming Logic

```text
When nozzle flow occurs:
   open temporary nozzle session.

If no POS txn linked within 2m:
   suspicious.

Correlate with stock shrinkage.
```

## Cache

```text
active_nozzle:{pump}
```

Store:
- start/end time
- liters
- linked transaction

TTL: 5m

---

# [R6] Delayed Invoice

## Primary Tables

| Table | Fields |
|---|---|
| oil_transactions | Fueling_End |
| dbo_bkjournal_payment | APPROVE_DATE |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| Fueling_End | fueling completed |
| APPROVE_DATE | payment completed |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| time gap | delayed close |
| employee | responsible cashier |

## Streaming Logic

```text
gap = APPROVE_DATE - Fueling_End

IF gap > 15m:
   alert
```

## Cache

Reuse:

```text
pending_txn:{journal_id}
```

---

# [R7] Void Fleet/Credit Card

## Primary Tables

| Table | Fields |
|---|---|
| dbo_bkjournal_payment | VOID_DATE, APPROVE_DATE, APPROVE_CODE |
| dbo_bkjournal | USERNAME |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| APPROVE_DATE | original payment |
| VOID_DATE | reversal time |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| VOID_DATE | suspicious reversal |
| APPROVE_CODE | EDC reference |
| USERNAME | cashier |

## Streaming Logic

```text
If VOID_DATE exists after payment success:
   alert

Increase confidence when void delay > 15m.
```

## Cache

```text
payment_state:{journal_id}
```

TTL: 24h

---

# [R8] Manual Entry Payment

## Primary Tables

| Table | Fields |
|---|---|
| dbo_bkjournal_payment | EDC_ISAUTO, CARD_NO, PAYMENT_TYPE |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| APPROVE_DATE | payment time |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| EDC_ISAUTO=FALSE | manual entry |
| CARD_NO | entered card |

## Streaming Logic

```text
If EDC_ISAUTO = FALSE:
   alert immediately
```

## Cache

No large cache required.

---

# [R9] Pump Price Mismatch

## Primary Tables

| Table | Fields |
|---|---|
| oil_transactions | POS_Unit_Price |
| fuel_prices | official price |
| inventory_events | tank drop |
| iot_events | dispenser price |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| price change timestamp | override timing |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| FCC_Unit_Price | dispenser price |
| POS_Unit_Price | POS price |
| tank drop | actual volume |

## Streaming Logic

```text
Compare dispenser price against official POS price.

Also reconcile:
  revenue ≈ volume × official price
```

## Cache

```text
price_snapshot:{pump}
```

TTL: 1d

---

# [R10] Offline Sales

## Primary Tables

| Table | Fields |
|---|---|
| iot_events | heartbeat, network status |
| oil_transactions | sales during offline |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| offline start/end | outage window |
| heartbeat timestamp | network proof |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| OFFLINE status | suspicious mode |
| heartbeat alive | contradiction |
| cash sales | hidden activity |

## Streaming Logic

```text
If POS status=OFFLINE but heartbeat alive:
   open suspicious offline window.

Aggregate transactions during window.
```

## Cache

```text
offline_state:{station}
```

TTL: shift

---

# [R11] Double Click / Hanging Nozzle

## Primary Tables

| Table | Fields |
|---|---|
| iot_events | Nozzle_Hang_Up, flow cycle |
| oil_transactions | linked transaction |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| flow sequence | customer boundary |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| Nozzle_Hang_Up | nozzle not reset |
| flow sessions | hidden carry-over |

## Streaming Logic

```text
Track nozzle flow sessions.

If second flow begins without hang/reset:
   suspicious.
```

## Cache

```text
pump_flow_state:{pump}
```

TTL: 10m

---

# [R12] Loyalty Point Theft

## Primary Tables

| Table | Fields |
|---|---|
| loyalty_events | Blue Card ID, Station_ID |
| retail_transactions | cash sale |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| scan timestamp | loyalty usage |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| loyalty card | reused card |
| station count | multi-station abuse |
| usage count | abnormal reuse |

## Streaming Logic

```text
Count usage per loyalty card/day.

If count > threshold OR multiple stations:
   alert
```

## Cache

```text
loyalty_usage:{card}:{date}
```

TTL: 24h

---

# [R13] Phantom Return

## Primary Tables

| Table | Fields |
|---|---|
| retail_returns | refund amount, receipt |
| retail_transactions | original sale |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| sale timestamp | original sale |
| refund timestamp | suspicious refund |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| refund amount | fraud value |
| original receipt | missing linkage |
| elapsed hours | suspicious delay |

## Streaming Logic

```text
When refund arrives:
   lookup cached original sale.

If no original sale OR refund too late:
   alert
```

## Cache

```text
sale_receipt_cache:{receipt}
```

TTL: 7d

---

# [R14] Fuel Siphoning

## Primary Tables

| Table | Fields |
|---|---|
| inventory_events | manifest volume, tank increase |
| iot_events | GPS, ATG |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| dispatch time | transport start |
| delivery time | ATG increase |
| GPS stop time | route deviation |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| manifest liters | expected |
| ATG increase | actual |
| GPS stop | suspicious detour |

## Streaming Logic

```text
Create delivery batch state.

Compare:
  manifest volume
  vs ATG increase

Track off-route stops.
```

## Cache

```text
delivery_batch:{batch}
```

TTL: delivery lifecycle

---

# [R15] Fuel Adulteration

## Primary Tables

| Table | Fields |
|---|---|
| inventory_events | stock increase |
| iot_events | density reading |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| batch delivery time | before/after density |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| density change | contamination |
| stock excess | diluted fuel |

## Streaming Logic

```text
After delivery:
   compare pre/post density.

If density changes > threshold:
   alert
```

## Cache

```text
tank_density_state:{tank}
```

Persistent cache.

---

# [R16] Cross-Contamination

## Primary Tables

| Table | Fields |
|---|---|
| inventory_events | Product_Type, Tank_ID, PO Number |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| receiving timestamp | unloading sequence |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| Product_Type | expected product |
| Tank_ID | actual target tank |
| PO Number | missing authorization |

## Streaming Logic

```text
Validate:
  product type matches tank type.

If mismatch:
   immediate alert.
```

## Cache

```text
tank_product_map:{tank}
```

Persistent.

---

# [R17] Pump Calibration Fraud

## Primary Tables

| Table | Fields |
|---|---|
| iot_events | dispenser totalizer |
| inventory_events | tank drop |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| cumulative daily readings | variance trend |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| totalizer liters | displayed liters |
| tank drop liters | actual liters |
| calibration seal status | tampering |

## Streaming Logic

```text
Maintain cumulative variance:
  dispenser total
  vs tank depletion.

If persistent divergence:
   alert
```

## Cache

```text
pump_variance:{pump}
```

Persistent rolling statistics.

---

# [R18] Phantom Employee

## Primary Tables

| Table | Fields |
|---|---|
| dbo_bkjournal | USERNAME, POS_ID, CREATEDATE |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| login timestamp | concurrent session detection |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| USERNAME | duplicated employee |
| POS_ID | simultaneous terminals |

## Streaming Logic

```text
Track active employee sessions.

If same employee active at 2 locations simultaneously:
   alert
```

## Cache

```text
employee_session:{employee}
```

TTL: shift

---

# [R19] Tank Sensor Tampering

## Primary Tables

| Table | Fields |
|---|---|
| iot_events | ATG reading, sensor diagnostic |
| inventory_events | manual dip reading |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| sensor timestamp | ATG reading |
| dip timestamp | manual measurement |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| ATG reading | sensor output |
| dip reading | physical verification |
| self-diagnostic | sensor fault |

## Streaming Logic

```text
Compare manual dip against ATG reading.

If variance exceeds threshold:
   alert
```

## Cache

```text
sensor_baseline:{tank}
```

Persistent.

---

# [R20] Customer Collusion

## Primary Tables

| Table | Fields |
|---|---|
| oil_transactions | Customer_ID, override price |
| retail_transactions | discount events |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| transaction date | weekly frequency |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| Customer_ID | repeated customer |
| discount rate | suspicious benefit |
| frequency | repeated overrides |

## Streaming Logic

```text
Aggregate overrides per customer/week.

If repeated discounts exceed threshold:
   alert
```

## Cache

```text
customer_discount_window:{customer}
```

TTL: 7d

---

# [R21] Evaporation Overstatement

## Primary Tables

| Table | Fields |
|---|---|
| inventory_events | claimed evaporation |
| iot_events | temperature, humidity |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| environmental timestamps | expected evaporation model |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| claimed loss | declared loss |
| expected loss | modeled loss |
| weather conditions | environmental context |

## Streaming Logic

```text
Calculate expected evaporation using weather model.

If claimed loss >> expected:
   alert
```

## Cache

```text
evaporation_model:{station}:{day}
```

TTL: 30d

---

# [R22] Nozzle Rapid Cycle

## Primary Tables

| Table | Fields |
|---|---|
| iot_events | start/stop cycles, flow rate |

## Timeline Fields [TL]

| Field | Usage |
|---|---|
| flow cycle timestamps | rapid toggling |

## Evidence Fields [EV]

| Field | Meaning |
|---|---|
| cycle count | rapid trigger abuse |
| flow rate SD | unstable flow |

## Streaming Logic

```text
Track start/stop count during one fueling session.

If cycles > threshold:
   alert
```

## Cache

```text
fuel_session:{pump}
```

TTL: session lifecycle

---

# 6. Cross-Rule Correlation

The strongest detections occur when multiple rules correlate.

| Combined Rules | Meaning |
|---|---|
| R1 + R2 | delayed Fleet conversion + repeat card |
| R2 + R8 | repeated Fleet card with manual entry |
| R5 + R17 | off-system sale + calibration fraud |
| R14 + R15 | siphoning + adulteration |
| R10 + R5 | offline mode hiding off-system sales |
| R11 + R22 | hanging nozzle + rapid cycle manipulation |

Recommended architecture:

```text
Rule Engine
    ↓
Correlation Engine
    ↓
Case Management
```

---

# 7. Recommended Technologies

| Component | Recommendation |
|---|---|
| Streaming | Kafka / Redpanda |
| Stateful stream processing | Flink / Kafka Streams |
| Cache | Redis |
| OLAP analytics | DuckDB / ClickHouse |
| Long-term ML features | Iceberg / Delta Lake |
| Real-time dashboard | Grafana |
| Correlation engine | custom CEP layer |

---

# 8. Recommended Detection Output Schema

```json
{
  "event_id": "EVT-R2-20260512001",
  "rule_id": 2,
  "confidence": 85,
  "station_id": "P01",
  "employee_id": "EMP001",
  "timeline": [],
  "evidence": {},
  "linked_records": [],
  "tags": [
    "rule2",
    "fleet",
    "repeat-card",
    "high-risk"
  ]
}
```

---

# 9. Recommended Tags Per Rule

| Rule | Tags |
|---|---|
| R1 | `payment-change`, `fleet-conversion` |
| R2 | `repeat-card`, `fleet-abuse` |
| R3 | `split-payment`, `limit-evasion` |
| R4 | `bundle-payment`, `cash-conversion` |
| R5 | `off-system-sale`, `manual-release` |
| R6 | `delayed-close`, `invoice-delay` |
| R7 | `void-fraud`, `post-payment-void` |
| R8 | `manual-entry`, `edc-risk` |
| R9 | `price-mismatch`, `price-tamper` |
| R10 | `offline-mode`, `network-abuse` |
| R11 | `double-click`, `hanging-nozzle` |
| R12 | `loyalty-abuse`, `point-theft` |
| R13 | `phantom-return`, `refund-fraud` |
| R14 | `siphoning`, `transport-loss` |
| R15 | `fuel-adulteration`, `density-change` |
| R16 | `cross-contamination`, `wrong-tank` |
| R17 | `pump-calibration`, `meter-tamper` |
| R18 | `phantom-employee`, `shared-account` |
| R19 | `sensor-tamper`, `ATG-mismatch` |
| R20 | `customer-collusion`, `discount-abuse` |
| R21 | `evaporation-fraud`, `inventory-loss` |
| R22 | `rapid-cycle`, `flow-manipulation` |

---

# 10. Case-by-Case Business Fraud Summary

---

# Case 1.2 — Credit Card Manual Fraud

## Fraud Type

Manual Credit Card Payment Manipulation

---

## Victims

| Victim | Impact |
|---|---|
| Station owner | direct financial loss |
| Bank/acquirer | chargeback/fake authorization |
| Customer | stolen card usage |
| Accounting team | reconciliation mismatch |

---

## Fraudsters

| Actor | Role |
|---|---|
| Cashier | manually enters fake card/payment |
| Weak supervisor/accounting | fails to validate settlement |
| Possible external accomplice | provides stolen card data |

---

## Fraud Workflow

```text
Customer fuels vehicle
    ↓
Cashier changes payment to Manual Credit Card
    ↓
Fake/manual approval entered
    ↓
POS shows successful payment
    ↓
No real settlement received
    ↓
Cash difference hidden or stolen
```

---

## Damage Value

From slide:

```text
~400,000 THB / month
```

---

## Existing Detection Rules

| Rule | Coverage |
|---|---|
| R8 | Manual card entry |
| R7 | void/reversal correlation |

---

## Detection Gaps

| Gap | Missing Capability |
|---|---|
| bank authorization verification | no real issuer validation |
| cross-station card velocity | stolen card reuse |
| approval-code integrity | fake authorization code |
| settlement reconciliation | fake payment detection |

---

## Recommended New Detection

| New Rule | Description |
|---|---|
| NR-CC-01 | issuer/acquirer approval verification |
| NR-CC-02 | global card velocity detection |
| NR-CC-03 | settlement mismatch detection |
| NR-CC-04 | approval-code format anomaly |

---

# Case 1.3 — Fleet Card Fraud

## Fraud Type

Fleet Card Misuse / Fake Fleet Billing

---

## Victims

| Victim | Impact |
|---|---|
| Fleet company | fraudulent billing |
| Station owner | cash theft |
| Logistics company | incorrect fuel expense |
| Accounting | reconciliation gap |

---

## Fraudsters

| Actor | Role |
|---|---|
| Cashier | changes payment type |
| Driver | provides Fleet card |
| Internal collusion | hides reconciliation gaps |

---

## Fraud Workflow

```text
Customer pays cash
    ↓
Cashier later applies Fleet card
    ↓
Fleet company charged instead
    ↓
Cash removed outside system
    ↓
Fraud hidden via weak reconciliation
```

---

## Damage Value

From slide:

```text
~1,000,000 THB / month
```

---

## Existing Detection Rules

| Rule | Coverage |
|---|---|
| R1 | delayed Fleet conversion |
| R2 | repeated Fleet card |
| R3 | split Fleet transactions |
| R4 | bundled transactions |
| R8 | manual entry |
| R20 | customer collusion |

---

## Detection Gaps

| Gap | Missing Capability |
|---|---|
| vehicle verification | vehicle != authorized Fleet vehicle |
| route/fuel efficiency | abnormal consumption |
| GPS validation | vehicle not present |
| camera verification | wrong vehicle at pump |
| enterprise-wide card analytics | same card used across regions |

---

## Recommended New Detection

| New Rule | Description |
|---|---|
| NR-FLT-01 | ANPR vs Fleet registry matching |
| NR-FLT-02 | GPS/vehicle proximity validation |
| NR-FLT-03 | abnormal fuel efficiency model |
| NR-FLT-04 | cross-region Fleet card usage |
| NR-FLT-05 | vehicle-camera mismatch |

---

# Case 2.1 — Fake Tax Invoice Using Another Customer

## Fraud Type

Invoice Resale / Fake Tax Invoice

---

## Victims

| Victim | Impact |
|---|---|
| Station owner | financial/legal exposure |
| Tax authority | fraudulent tax deduction |
| Transport company | accounting fraud |
| Real customer | invoice misuse |

---

## Fraudsters

| Actor | Role |
|---|---|
| Cashier | reuses another customer transaction |
| Truck driver | purchases fake invoice |
| Accounting collusion | weak review |

---

## Fraud Workflow

```text
Real customer fuels normally
    ↓
Customer leaves without invoice
    ↓
Cashier reprints/reuses invoice
    ↓
Invoice sold to another company
    ↓
Fake expense/tax deduction claimed
```

---

## Damage Value

From slide:

```text
10 transactions/month
Potentially 1,000–5,000 fake transactions
```

---

## Existing Detection Rules

| Rule | Coverage |
|---|---|
| invoice reuse | duplicated invoice |
| delayed issuance | invoice timing anomaly |
| transaction mismatch | invoice != original sale |

---

## Detection Gaps

| Gap | Missing Capability |
|---|---|
| invoice buyer profiling | unrelated company behavior |
| invoice laundering graph | repeated invoice buyers |
| vehicle identity validation | invoice vehicle mismatch |
| invoice marketplace detection | broker network |
| customer relationship graph | repeated suspicious pairings |

---

## Recommended New Detection

| New Rule | Description |
|---|---|
| NR-INV-01 | invoice buyer anomaly profiling |
| NR-INV-02 | invoice resale network graph |
| NR-INV-03 | ANPR vs invoice vehicle validation |
| NR-INV-04 | invoice issuance after vehicle departure |
| NR-INV-05 | repeated invoice broker relationship |

---

# Case 2.2 — Modified Tax Invoice

## Fraud Type

Invoice Tampering / Invoice Forgery

---

## Victims

| Victim | Impact |
|---|---|
| Tax authority | fake tax deduction |
| Station owner | legal risk |
| Accounting/auditors | false financial records |
| Customer/company | manipulated invoice data |

---

## Fraudsters

| Actor | Role |
|---|---|
| Internal staff | edits invoice content |
| External collaborator | uses fake invoice |
| Weak auditing | fails to detect modification |

---

## Fraud Workflow

```text
Original invoice created
    ↓
Invoice copied/exported
    ↓
Amount/date/customer edited
    ↓
Modified invoice redistributed
    ↓
Fake tax/expense claim submitted
```

---

## Existing Detection Rules

| Rule | Coverage |
|---|---|
| invoice hash validation | detects modification |

---

## Detection Gaps

| Gap | Missing Capability |
|---|---|
| invoice lineage | who changed what |
| PDF/image tampering | edited files |
| version tracking | modification sequence |
| print/export audit | suspicious regeneration |
| immutable event chain | forensic audit trail |

---

## Recommended New Detection

| New Rule | Description |
|---|---|
| NR-INVT-01 | append-only invoice event ledger |
| NR-INVT-02 | OCR + PDF signature validation |
| NR-INVT-03 | suspicious invoice regeneration |
| NR-INVT-04 | invoice modification lineage |
| NR-INVT-05 | exported file integrity validation |

---

# Case 3 — POS Oil System Fraud / Off-System Fueling

## Fraud Type

Fuel Dispensing Outside POS Control

---

## Victims

| Victim | Impact |
|---|---|
| Station owner | direct fuel theft |
| Accounting | unexplained inventory loss |
| Fuel supplier | inventory discrepancy |
| Franchise operator | compliance failure |

---

## Fraudsters

| Actor | Role |
|---|---|
| Pump attendant | manipulates dispenser |
| Technician | modifies connection/system |
| Internal collusion | hides inventory mismatch |

---

## Fraud Workflow

```text
Pump/POS connection interrupted
    ↓
Fuel dispensed manually
    ↓
No official POS transaction
    ↓
Inventory decreases silently
    ↓
Cash collected outside system
```

---

## Existing Detection Rules

| Rule | Coverage |
|---|---|
| R5 | off-system sales |
| R10 | offline mode abuse |
| R11 | hanging nozzle |
| R17 | calibration mismatch |
| R19 | sensor tampering |
| R22 | rapid nozzle cycling |

---

## Detection Gaps

| Gap | Missing Capability |
|---|---|
| intentional network sabotage | deliberate disconnect |
| firmware tampering | modified dispenser software |
| physical theft detection | hidden containers/manual siphon |
| maintenance abuse | fake maintenance mode |
| CCTV correlation | physical fueling verification |
| hardware integrity monitoring | unauthorized changes |

---

## Recommended New Detection

| New Rule | Description |
|---|---|
| NR-POS-01 | network sabotage detection |
| NR-POS-02 | dispenser firmware checksum monitoring |
| NR-POS-03 | CCTV vs POS transaction correlation |
| NR-POS-04 | maintenance-mode anomaly |
| NR-POS-05 | after-hours fueling detection |
| NR-POS-06 | unauthorized hardware/configuration change |

---

# 11. Final Recommendations

## Avoid Historical Queries

Do NOT repeatedly query:

```sql
SELECT *
FROM transactions
WHERE ...
```

for every event.

This will not scale.

Use:

- streaming joins
- state stores
- Redis windows
- incremental aggregation
- event sourcing

---

## Strongly Recommended Architecture

```text
Kafka
  → Flink/Kafka Streams
      → Redis state cache
          → Fraud Rule Engine
              → Correlation Engine
                  → Alert Store
                      → Dashboard/API
```

---

## Best Long-Term Enhancement

Add:

- graph analysis
- employee relationship graphs
- customer collusion graphs
- anomaly ensemble scoring
- temporal sequence ML
- unsupervised behavior clustering

This allows detection beyond deterministic rules.

