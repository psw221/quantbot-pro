# DB_SCHEMA_v1.2.md — QuantBot Pro 데이터베이스 설계서 (최종본)

> Version: `v1.2` | 작성일: 2026년 4월  
> 연계 문서: `docs/PRD_v1.4.md`, `AGENTS.md`

---

## 목차

1. [개정 목적](#1-개정-목적)
2. [DB 구성 개요](#2-db-구성-개요)
3. [핵심 설계 원칙](#3-핵심-설계-원칙)
4. [SQLite 아키텍처 메타데이터](#4-sqlite-아키텍처-메타데이터)
5. [SQLite 테이블 설계](#5-sqlite-테이블-설계)
6. [InfluxDB 측정값 설계](#6-influxdb-측정값-설계)
7. [ERD 및 데이터 흐름](#7-erd-및-데이터-흐름)
8. [인덱스 전략](#8-인덱스-전략)
9. [SQLAlchemy 구현 원칙](#9-sqlalchemy-구현-원칙)
10. [초기화 및 마이그레이션](#10-초기화-및-마이그레이션)
11. [변경 이력](#11-변경-이력)

---

## 1. 개정 목적

이번 최종본은 아래 보강 사항을 반영합니다.

- 주문과 체결 분리
- 부분체결 1:N 구조 명확화
- 전략 포지션과 브로커 계좌 잔고 분리
- **매수/매도 결제일 환율 저장 구조 추가**
- **SQLite WAL 모드와 단일 Writer 큐를 시스템 수준 제약으로 명시**
- SQL 예시, 인덱스 전략, ORM 기준 통일

---

## 2. DB 구성 개요

| 저장소 | 종류 | 위치 | 저장 데이터 |
|--------|------|------|-------------|
| 운용 DB | SQLite | `data/quantbot.db` | 전략 포지션, 주문, 체결, 거래 원장, 세금, 동기화 이력 |
| 시계열 DB | InfluxDB | `localhost:8086` | 실시간 시세, 포트폴리오 가치, DB/Writer 큐 메트릭 |

### SQLite 선택 이유

- 파일 기반 운용으로 초기 장애 포인트 최소화
- 복구 및 백업 절차가 단순함
- 주문/체결 원장 보관에 적합함

### InfluxDB 선택 이유

- 틱/분봉/헬스체크성 시계열 저장에 적합함
- SQLite의 과도한 쓰기 부하와 파일 팽창을 방지함

---

## 3. 핵심 설계 원칙

### 3.1 원장 분리

이 시스템은 아래 두 원장을 함께 관리합니다.

1. **전략 기준 원장**
   - 전략별 보유 수량
   - 손절/익절/리밸런싱 판단 근거
2. **브로커 기준 원장**
   - 실제 계좌 총 보유 수량
   - DR 및 정합성 복구 기준

### 3.2 주문과 체결의 분리

- `orders`: 주문 의도와 주문 상태
- `order_executions`: 개별 체결 이벤트
- `trades`: 회계/세금/성과 계산을 위한 거래 원장

### 3.3 환율 추적성

미국 주식 손익과 세금 추산의 추적 가능성을 위해 아래를 저장합니다.

- 체결 시점 환율
- 결제일
- 결제일 환율
- 환율 소스 식별자

### 3.4 민감정보 보호

- 토큰 값 저장 금지
- 원본 인증 헤더 저장 금지
- 브로커 API 응답 전문 저장 금지
- `system_logs.extra_json`에도 민감정보 저장 금지

---

## 4. SQLite 아키텍처 메타데이터

아래 항목은 스키마의 일부이자 시스템 제약입니다.

### 4.1 SQLite 운영 모드

```text
journal_mode = WAL
synchronous = NORMAL
busy_timeout_ms = 5000
foreign_keys = ON
```

### 4.2 쓰기 제약

```text
write_model = single_writer_queue
writer_queue = required
concurrent_direct_write = forbidden
```

### 4.3 메타데이터 기록 원칙

- WAL 모드와 writer queue는 **문서화된 시스템 제약**입니다.
- 이 메타데이터는 DB 내부 테이블에 고빈도로 기록하지 않습니다.
- 런타임 관측값은 InfluxDB `system_runtime` 측정값에 저장합니다.

---

## 5. SQLite 테이블 설계

## 5.1 `positions`

전략 기준 현재 보유 포지션 요약입니다.

```sql
CREATE TABLE positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    market          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    quantity        INTEGER NOT NULL,
    avg_cost        REAL    NOT NULL,
    current_price   REAL    NOT NULL DEFAULT 0,
    highest_price   REAL    NOT NULL DEFAULT 0,
    entry_date      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (ticker, market, strategy)
);
```

## 5.2 `position_lots`

FIFO 취득가 계산과 전략별 오픈 로트 관리용입니다.

```sql
CREATE TABLE position_lots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id         INTEGER NOT NULL,
    strategy            TEXT    NOT NULL,
    ticker              TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    open_quantity       INTEGER NOT NULL,
    remaining_quantity  INTEGER NOT NULL,
    open_price          REAL    NOT NULL,
    open_trade_fx_rate  REAL,
    open_settlement_date TEXT,
    open_settlement_fx_rate REAL,
    opened_at           TEXT    NOT NULL,
    source_trade_id     INTEGER NOT NULL,
    updated_at          TEXT    NOT NULL,
    FOREIGN KEY (position_id) REFERENCES positions(id),
    FOREIGN KEY (source_trade_id) REFERENCES trades(id)
);
```

> KR 거래는 환율 필드를 `NULL`로 둘 수 있고, US 거래는 값 저장이 권장됩니다.

## 5.3 `broker_positions`

브로커 계좌 기준 보유 스냅샷입니다.

```sql
CREATE TABLE broker_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    market          TEXT    NOT NULL,
    quantity        INTEGER NOT NULL,
    avg_cost        REAL    NOT NULL,
    currency        TEXT    NOT NULL DEFAULT 'KRW',
    snapshot_at     TEXT    NOT NULL,
    source_env      TEXT    NOT NULL,
    UNIQUE (ticker, market, source_env, snapshot_at)
);
```

## 5.4 `signals`

```sql
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    market          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    strength        REAL    NOT NULL DEFAULT 1.0,
    reason          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending',
    reject_reason   TEXT,
    generated_at    TEXT    NOT NULL,
    processed_at    TEXT
);
```

Canonical `signals.status` 값:

- `pending`
- `resolved`
- `rejected`
- `ordered`

## 5.5 `orders`

```sql
CREATE TABLE orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id     TEXT    NOT NULL UNIQUE,
    kis_order_no        TEXT,
    signal_id           INTEGER NOT NULL,
    ticker              TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    strategy            TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    order_type          TEXT    NOT NULL,
    quantity            INTEGER NOT NULL,
    price               REAL,
    status              TEXT    NOT NULL DEFAULT 'pending',
    retry_count         INTEGER NOT NULL DEFAULT 0,
    submitted_at        TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    error_code          TEXT,
    error_message       TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
```

Canonical `orders.status` 값:

- `pending`
- `validated`
- `submitted`
- `partially_filled`
- `filled`
- `cancel_pending`
- `cancelled`
- `rejected`
- `reconcile_hold`
- `failed`

`orders.error_code`, `orders.error_message` 사용 원칙:

- 브로커 제출 실패의 정규화된 결과만 저장합니다.
- `retryable`, `terminal`, `auth`, `reconcile_hold` 분류 판단에 필요한 최소 오류 정보만 남깁니다.
- 브로커 raw payload 전체나 인증 헤더는 저장하지 않습니다.
- `reconcile_hold` 전환 시에도 `error_code`, `error_message`에는 hold 원인을 추적할 수 있는 최소 문자열만 저장합니다.

## 5.6 `order_executions`

부분체결을 표현하는 개별 체결 이벤트 테이블입니다.

```sql
CREATE TABLE order_executions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER NOT NULL,
    execution_no        TEXT    NOT NULL UNIQUE,
    fill_seq            INTEGER NOT NULL,
    filled_quantity     INTEGER NOT NULL,
    filled_price        REAL    NOT NULL,
    fee                 REAL    NOT NULL DEFAULT 0,
    tax                 REAL    NOT NULL DEFAULT 0,
    currency            TEXT    NOT NULL DEFAULT 'KRW',
    trade_fx_rate       REAL,
    settlement_date     TEXT,
    settlement_fx_rate  REAL,
    fx_rate_source      TEXT,
    executed_at         TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);
```

### 환율 필드 의미

- `trade_fx_rate`: 체결 시점 환율
- `settlement_date`: 해당 체결의 결제일
- `settlement_fx_rate`: 결제일 환율
- `fx_rate_source`: 환율 제공 소스 식별자

## 5.7 `trades`

회계/세금/성과 계산용 거래 원장입니다.

```sql
CREATE TABLE trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER NOT NULL,
    execution_id        INTEGER NOT NULL UNIQUE,
    ticker              TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    strategy            TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    quantity            INTEGER NOT NULL,
    price               REAL    NOT NULL,
    amount              REAL    NOT NULL,
    fee                 REAL    NOT NULL DEFAULT 0,
    tax                 REAL    NOT NULL DEFAULT 0,
    net_amount          REAL    NOT NULL,
    currency            TEXT    NOT NULL DEFAULT 'KRW',
    trade_fx_rate       REAL,
    settlement_date     TEXT,
    settlement_fx_rate  REAL,
    fx_rate_source      TEXT,
    signal_id           INTEGER,
    executed_at         TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (execution_id) REFERENCES order_executions(id),
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
```

## 5.8 `portfolio_snapshots`

```sql
CREATE TABLE portfolio_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date       TEXT    NOT NULL UNIQUE,
    total_value_krw     REAL    NOT NULL,
    cash_krw            REAL    NOT NULL,
    domestic_value_krw  REAL    NOT NULL,
    overseas_value_krw  REAL    NOT NULL,
    usd_krw_rate        REAL    NOT NULL,
    daily_return        REAL    NOT NULL DEFAULT 0,
    cumulative_return   REAL    NOT NULL DEFAULT 0,
    drawdown            REAL    NOT NULL DEFAULT 0,
    max_drawdown        REAL    NOT NULL DEFAULT 0,
    position_count      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL
);
```

## 5.9 `token_store`

```sql
CREATE TABLE token_store (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    env         TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    is_valid    INTEGER NOT NULL DEFAULT 1
);
```

## 5.10 `event_calendar`

```sql
CREATE TABLE event_calendar (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date      TEXT NOT NULL,
    event_time      TEXT,
    event_type      TEXT NOT NULL,
    market          TEXT NOT NULL,
    ticker          TEXT,
    title           TEXT NOT NULL,
    impact          TEXT NOT NULL DEFAULT 'medium',
    action          TEXT NOT NULL,
    is_processed    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
```

## 5.11 `tax_events`

세금 계산을 위한 재가공 원장입니다.

```sql
CREATE TABLE tax_events (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                    INTEGER NOT NULL UNIQUE,
    ticker                      TEXT    NOT NULL,
    market                      TEXT    NOT NULL,
    sell_date                   TEXT    NOT NULL,
    quantity                    INTEGER NOT NULL,
    sell_price                  REAL    NOT NULL,
    cost_basis                  REAL    NOT NULL,
    gain_loss_usd               REAL,
    gain_loss_krw               REAL    NOT NULL,
    buy_trade_fx_rate           REAL,
    buy_settlement_date         TEXT,
    buy_settlement_fx_rate      REAL,
    sell_trade_fx_rate          REAL,
    sell_settlement_date        TEXT,
    sell_settlement_fx_rate     REAL,
    fx_rate_source              TEXT,
    taxable_gain                REAL    NOT NULL DEFAULT 0,
    tax_year                    INTEGER NOT NULL,
    is_included_in_report       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);
```

Phase 2 구현 규칙:

- `tax_events`는 미국 주식 매도 체결에서 FIFO 취득원가와 settlement FX 추적을 잃지 않기 위한 hook 테이블로 우선 사용합니다.
- 최종 신고/리포트 계산 로직은 Phase 3 이상에서 확장할 수 있습니다.

## 5.12 `backtest_results`

```sql
CREATE TABLE backtest_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT    NOT NULL,
    market          TEXT    NOT NULL,
    start_date      TEXT    NOT NULL,
    end_date        TEXT    NOT NULL,
    params_json     TEXT    NOT NULL,
    annual_return   REAL    NOT NULL,
    sharpe_ratio    REAL    NOT NULL,
    max_drawdown    REAL    NOT NULL,
    win_rate        REAL    NOT NULL,
    total_trades    INTEGER NOT NULL,
    profit_factor   REAL    NOT NULL DEFAULT 0,
    notes           TEXT,
    created_at      TEXT    NOT NULL
);
```

운영 메모:

- `backtest_results.notes`에는 민감정보 없이 실행 엔진 메타데이터 같은 최소 운영 메모만 저장합니다.
- 현재 저장소 기준 기본 메모 형식은 `engine=<vectorbt|fallback>`입니다.

## 5.13 `system_logs`

```sql
CREATE TABLE system_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT NOT NULL,
    module      TEXT NOT NULL,
    message     TEXT NOT NULL,
    extra_json  TEXT,
    created_at  TEXT NOT NULL
);
```

운영 메모:

- `system_logs.extra_json`은 운영 추적용 최소 메타데이터만 저장합니다.
- 계좌번호, API Key, raw broker payload, 인증 헤더 등 민감정보는 저장하지 않습니다.
- restore/backtest/monitoring 흐름에서 사용하는 `extra_json`도 writer queue 경유 recorder가 sanitize한 값만 기록합니다.

## 5.14 `reconciliation_runs`

웹소켓과 브로커 폴링 결과를 대조한 정합성 검증 이력입니다.

```sql
CREATE TABLE reconciliation_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type            TEXT    NOT NULL,      -- 'scheduled_poll' | 'manual_restore' | 'startup_check'
    source_env          TEXT    NOT NULL,
    started_at          TEXT    NOT NULL,
    completed_at        TEXT,
    mismatch_count      INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL,      -- 'ok' | 'warning' | 'failed'
    summary_json        TEXT,
    created_at          TEXT    NOT NULL
);
```

Canonical `reconciliation_runs.status` 값:

- `ok`
- `warning`
- `failed`

서비스 계층 canonical reconciliation 상태:

- `idle`
- `scheduled_polling`
- `mismatch_detected`
- `reconciling`
- `reconciled`
- `failed`

실패 분류와 정합성 기록 원칙:

- `auth` 오류 자체는 `reconciliation_runs`를 만들지 않습니다.
- `reconcile_hold` 전환은 정합성 복구가 필요하다는 의미이므로 `reconciliation_runs` 기록을 남길 수 있습니다.
- `summary_json`에는 민감정보 없이 mismatch 원인, 분류, 관련 `order_id` 수준의 최소 정보만 저장합니다.

---

## 6. InfluxDB 측정값 설계

## 6.1 `realtime_quotes`

```text
measurement: realtime_quotes
tags: ticker, market
fields: price, volume, ask, bid
timestamp: websocket receive time
```

## 6.2 `portfolio_value`

```text
measurement: portfolio_value
tags: env
fields: total_krw, domestic, overseas, cash, drawdown
timestamp: 1-minute interval
```

## 6.3 `system_runtime`

SQLite/WAL/Writer Queue 메타데이터의 런타임 관측값입니다.

```text
measurement: system_runtime
tags: env, component
fields:
  - writer_queue_depth (integer)
  - writer_queue_lag_ms (float)
  - db_write_latency_ms (float)
  - wal_checkpoint_age_sec (float)
  - poll_last_success_ts (integer)
  - reconciliation_mismatch_count (integer)
```

---

## 7. ERD 및 데이터 흐름

```text
signals (1) ──────────────── (N) orders
                               │
                               │
                               ▼
                         order_executions (N)
                               │
                               ▼
                            trades (1)
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
         position_lots                    tax_events
                │
                ▼
            positions

broker_positions  ← 브로커 계좌 기준 스냅샷
reconciliation_runs ← 폴링/복구 정합성 검증 이력
```

핵심 규칙:

- `orders`는 주문 상태 원장
- `order_executions`는 체결 이벤트 원장
- `trades`는 세금/성과 계산 원장
- `positions`는 전략 기준 요약 상태
- `broker_positions`는 브로커 기준 실제 상태

---

## 8. 인덱스 전략

```sql
CREATE INDEX idx_positions_strategy             ON positions (strategy, market);
CREATE INDEX idx_position_lots_lookup           ON position_lots (ticker, market, strategy, opened_at);
CREATE INDEX idx_broker_positions_snapshot      ON broker_positions (ticker, market, source_env, snapshot_at);
CREATE INDEX idx_signals_status                 ON signals (status, generated_at);
CREATE INDEX idx_orders_status                  ON orders (status, updated_at);
CREATE INDEX idx_orders_kis_order_no            ON orders (kis_order_no);
CREATE INDEX idx_order_executions_order         ON order_executions (order_id, executed_at);
CREATE INDEX idx_trades_ticker_date             ON trades (ticker, executed_at);
CREATE INDEX idx_trades_strategy                ON trades (strategy, executed_at);
CREATE INDEX idx_tax_events_year_market         ON tax_events (tax_year, market);
CREATE INDEX idx_snapshots_date                 ON portfolio_snapshots (snapshot_date);
CREATE INDEX idx_event_calendar_date            ON event_calendar (event_date, market);
CREATE INDEX idx_system_logs_level_date         ON system_logs (level, created_at);
CREATE INDEX idx_reconciliation_runs_started_at ON reconciliation_runs (started_at, status);
```

---

## 9. SQLAlchemy 구현 원칙

- SQLAlchemy ORM 모델을 구현 기준으로 사용합니다.
- 인덱스는 ORM의 `Index()` 또는 명시적 migration으로 관리합니다.
- SQLite connection 생성 시 WAL PRAGMA를 강제 적용합니다.
- 모든 write는 writer queue를 통과합니다.
- `order_executions` 반영은 하나의 트랜잭션으로 처리합니다.

예시 원칙:

```python
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

with engine.begin() as conn:
    conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
    conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
    conn.exec_driver_sql("PRAGMA busy_timeout=5000;")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON;")
```

---

## 10. 초기화 및 마이그레이션

### 초기화 원칙

1. DB 생성 직후 WAL PRAGMA 적용
2. 모든 테이블 생성
3. 필수 인덱스 생성
4. 초기 검증 스크립트 실행

현재 저장소 기준:

- 초기화 엔트리포인트는 `scripts/init_db.py`를 사용합니다.
- `scripts/init_db.py`는 문서상 SQLite 스키마 전체를 멱등적으로 생성하는 용도로 유지합니다.

### 마이그레이션 원칙

1. `docs/DB_SCHEMA_v1.2.md` 수정
2. ORM 모델 수정
3. `scripts/migrate_vYYYYMMDD.py` 작성
4. VTS 환경 검증
5. 실전 반영 전 백업 및 정합성 점검

현재 저장소 기준:

- migration placeholder는 `scripts/migrate_vYYYYMMDD.py` 경로 규칙으로 관리합니다.
- 운영 중 스키마 변경은 `init_db.py` 재실행으로 대체하지 않고, dedicated migration 스크립트로 처리합니다.

---

## 11. 변경 이력

| 버전 | 날짜 | 변경 내용 |
|------|------|-----------|
| v1.0 | 2025-04 | 최초 작성 |
| v1.1 | 2026-04 | 주문/체결 분리, 브로커 원장 분리 |
| v1.2 | 2026-04 | 결제일 환율 필드 추가, WAL/Writer Queue 메타데이터 명시, reconciliation 이력 추가 |
