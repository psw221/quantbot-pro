# AGENTS.md — QuantBot Pro 프로젝트 가이드 (최종본)

> 이 파일은 Codex와 개발자가 **동일한 구현 기준**으로 작업하기 위한 최상위 실행 가이드입니다.  
> 목적은 설명이 아니라 **일관된 책임 분리, 저장 규칙, 검증 규칙, 안전 장치**를 강제하는 것입니다.

---

## 1. 문서 우선순위

이 프로젝트의 문서 우선순위는 아래와 같습니다.

1. `docs/PRD_v1.4.md` — 제품 요구사항, 운영 정책, 리스크 정책의 기준
2. `docs/DB_SCHEMA_v1.2.md` — 저장 구조, 정합성, 원장 모델의 기준
3. `AGENTS.md` — 구현 규칙, 디렉터리 책임, 코딩/검증 규칙의 기준

문서 간 충돌 시 원칙:

- 기능·운영 정책 충돌 → `docs/PRD_v1.4.md` 우선
- 저장 구조·정합성 충돌 → `docs/DB_SCHEMA_v1.2.md` 우선
- 코드 위치·스타일·실행 규칙 충돌 → `AGENTS.md` 우선

---

## 2. 프로젝트 개요

**QuantBot Pro**는 한국투자증권 Open Trading API(KIS API)를 통해 국내(KRX) 및 미국(NYSE/NASDAQ) 주식을 자동 매매하는 퀀트 트레이딩 시스템입니다.

핵심 설계 원칙은 아래와 같습니다.

1. **VTS와 PROD를 절대 혼용하지 않는다.**
2. **토큰은 메모리에만 보관하고, DB에는 토큰 자체를 저장하지 않는다.**
3. **주문(Order)과 체결(Execution)을 분리하여 부분체결을 정확히 표현한다.**
4. **브로커 계좌 잔고와 내부 전략 포지션을 분리해 관리한다.**
5. **웹소켓은 1차 소스, 브로커 폴링은 2차 검증·보정 소스로 사용한다.**
6. **SQLite는 WAL 모드와 단일 Writer 큐를 전제로 사용한다.**
7. **모든 변경은 테스트 또는 검증 스크립트로 재현 가능해야 한다.**

---

## 3. 기술 스택 및 버전

```text
Python              3.11.x
pandas              2.2.x
numpy               1.26.x
scipy               1.13.x
requests            2.31.x
websockets          12.x
APScheduler         3.10.x
SQLAlchemy          2.0.x
influxdb-client     1.40.x
vectorbt            0.26.x
streamlit           1.35.x
python-telegram-bot 21.x
pykrx               1.0.x
yfinance            0.2.x
python-dotenv       1.0.x
pydantic            2.x
pytest              8.x
```

추가 원칙:

- `vectorbt`를 기본 백테스팅 엔진으로 사용합니다.
- `backtrader`는 선택 의존성입니다. 필요 시 사용자 합의 후 추가합니다.
- 기존 스택으로 구현 가능한 기능이면 새 라이브러리를 추가하지 않습니다.

---

## 4. 실제 프로젝트 폴더 구조

아래 구조를 기준으로 파일명과 경로를 맞춥니다.

```text
quantbot-pro/
├── AGENTS.md
├── main.py
├── requirements.txt
├── docs/
│   ├── PRD_v1.4.md
│   ├── DB_SCHEMA_v1.2.md
│   └── plans/
│       └── phase2_execution_plan.md
├── config/
│   ├── config.yaml
│   └── .env
├── core/
│   ├── settings.py
│   ├── models.py
│   ├── exceptions.py
│   └── logging.py
├── auth/
│   └── token_manager.py
├── data/
│   ├── database.py
│   ├── influx.py
│   ├── collector.py
│   ├── realtime.py
│   ├── adjusted_price.py
│   └── event_calendar.py
├── strategy/
│   ├── base.py
│   ├── dual_momentum.py
│   ├── trend_following.py
│   ├── factor_investing.py
│   └── signal_resolver.py
├── risk/
│   ├── risk_manager.py
│   ├── position_sizer.py
│   ├── exit_manager.py
│   └── event_filter.py
├── execution/
│   ├── kis_api.py
│   ├── market_constraints.py
│   ├── order_manager.py
│   ├── fill_processor.py
│   └── writer_queue.py
├── tax/
│   └── tax_calculator.py
├── backtest/
│   └── backtest_runner.py
├── monitor/
│   ├── dashboard.py
│   ├── telegram_bot.py
│   └── healthcheck.py
├── scripts/
│   ├── init_db.py
│   ├── validate_config.py
│   ├── restore_portfolio.py
│   └── migrate_vYYYYMMDD.py
└── tests/
    ├── test_strategy/
    ├── test_risk/
    ├── test_execution/
    └── conftest.py
```

### 책임 분리 규칙

- `core/settings.py`만 설정을 로딩합니다.
- `data/database.py`는 DB 연결, ORM, 세션, 초기화만 담당합니다.
- `execution/order_manager.py`는 주문 생성, 주문 전송, 브로커 상태 조회, 주문 상태 재동기화의 1차 책임을 가집니다.
- `execution/fill_processor.py`는 체결 반영, 포지션/원장 갱신, FIFO 차감 처리만 담당합니다.
- `execution/writer_queue.py`는 **모든 SQLite write의 단일 진입점**입니다.
- 하나의 파일에 설정 로딩, DB 세션, 외부 API 호출, 비즈니스 규칙을 혼합하지 않습니다.

---

## 5. 핵심 도메인 모델

## 5.1 Signal

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

@dataclass
class Signal:
    ticker: str
    market: Literal["KR", "US"]
    action: Literal["buy", "sell", "hold"]
    strategy: Literal["dual_momentum", "trend_following", "factor_investing"]
    strength: float
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)
```

## 5.2 OrderIntent

```python
@dataclass
class OrderIntent:
    client_order_id: str
    signal_id: int
    ticker: str
    market: Literal["KR", "US"]
    strategy: str
    side: Literal["buy", "sell"]
    quantity: int
    order_type: Literal["limit", "market"]
    price: float | None
```

## 5.3 ExecutionFill

```python
@dataclass
class ExecutionFill:
    order_id: int
    execution_no: str
    fill_seq: int
    filled_quantity: int
    filled_price: float
    fee: float
    tax: float
    executed_at: datetime
```

## 5.4 Position

```python
@dataclass
class Position:
    ticker: str
    market: Literal["KR", "US"]
    strategy: str
    quantity: int
    avg_cost: float
    current_price: float
    highest_price: float
    entry_date: datetime
```

## 5.5 BrokerPositionSnapshot

```python
@dataclass
class BrokerPositionSnapshot:
    ticker: str
    market: Literal["KR", "US"]
    quantity: int
    avg_cost: float
    currency: Literal["KRW", "USD"]
    snapshot_at: datetime
    source_env: Literal["vts", "prod"]
```

---

## 6. 설정 로딩 규칙

모든 파라미터는 `config/config.yaml`에서, 인증 정보는 `config/.env`에서 로드합니다.

```python
from core.settings import get_settings

settings = get_settings()
stop_loss = settings.risk.stop_loss_domestic
```

금지 사항:

```python
import yaml
with open("config/config.yaml") as f:
    cfg = yaml.safe_load(f)

STOP_LOSS = -0.07
API_KEY = "hardcoded"
```

원칙:

- `get_settings()`는 싱글턴 캐시를 사용합니다.
- 모듈 import 시점에 `.env`를 직접 읽지 않습니다.
- 실전/모의 환경 전환은 `settings.env`만을 통해 수행합니다.

---

## 7. SQLite 사용 규칙

이 프로젝트는 SQLite를 사용하지만, **잠금 경합을 피하기 위한 강제 규칙**이 있습니다.

### 7.1 필수 PRAGMA

DB 초기화 시 아래 설정을 반드시 적용합니다.

```python
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

### 7.2 단일 Writer 큐 강제

모든 SQLite 쓰기 작업은 `execution/writer_queue.py` 또는 `data/database.py`가 제공하는 전용 write API를 통해서만 수행합니다.

허용 예시:

- 주문 상태 업데이트
- 체결 이벤트 반영
- 포지션/로트 갱신
- 토큰 메타데이터 갱신
- 로그/리포트 메타데이터 기록

금지 예시:

- 비즈니스 로직에서 직접 `session.commit()` 호출
- 여러 스레드/코루틴에서 같은 DB 파일에 동시 쓰기
- 웹소켓 콜백 내부에서 즉시 DB write 수행

### 7.3 Writer 큐 구현 규칙

- Writer 큐는 FIFO 순서를 보장해야 합니다.
- Writer 큐는 단일 worker로 동작해야 합니다.
- 큐 입력 단위는 “작업 함수 + 파라미터” 또는 “명령 객체”로 통일합니다.
- 체결 반영은 **원자적 트랜잭션**으로 처리합니다.
- 실패한 write는 재시도 정책을 명시하고, 재시도 후에도 실패하면 신규 주문을 중단합니다.

---

## 8. KIS API 사용 규칙

### 8.1 환경 분리

- `env: vts | prod` 외 직접 도메인 하드코딩 금지
- 토큰은 환경별로 분리 관리
- VTS 로그와 PROD 로그는 저장 경로를 분리

### 8.2 Rate Limit

- KIS API는 초당 20건 제한을 전제로 설계합니다.
- 모든 브로커 API 호출은 rate limiter를 통과해야 합니다.

### 8.3 토큰

- 토큰 값은 메모리에만 저장
- DB에는 `issued_at`, `expires_at`, `env`, `is_valid`만 저장
- 토큰 갱신 실패 시 즉시 알림 + 재시도 + Emergency Stop

---

## 9. 오더 관리자 책임 범위

`execution/order_manager.py`는 아래 책임을 가집니다.

1. 신호 기반 주문 의도 생성
2. 브로커 주문 전송
3. 브로커 주문번호 매핑 관리
4. 미체결/부분체결 상태 추적
5. 웹소켓 이벤트 수신 상태와 브로커 상태의 불일치 감지
6. **10분 주기 브로커 폴링 기반 재동기화**
7. 불일치 탐지 시 신규 주문 중단 및 reconciliation 트리거

### 9.1 웹소켓과 폴링의 역할

- 웹소켓: 1차 이벤트 소스
- 폴링: 누락 보정, 정합성 검증, 브로커 최종 상태 확인용 2차 소스

### 9.2 폴링 규칙

- 시장 운영 시간 중 최소 10분 주기로 브로커 상태를 폴링합니다.
- 확인 대상은 아래와 같습니다.
  - 미체결 주문 상태
  - 부분체결 누락 여부
  - 보유 수량
  - 가용 현금
- 폴링 결과가 내부 상태와 다르면 아래 순서로 처리합니다.
  1. 해당 종목 신규 주문 중단
  2. mismatch 이벤트 로그 기록
  3. 브로커 스냅샷 저장
  4. fill re-sync 또는 복구 절차 실행

### 9.3 체결 반영 규칙

- 체결 반영은 `fill_processor.py`가 담당합니다.
- 하나의 체결 이벤트 반영은 아래를 하나의 트랜잭션으로 처리합니다.
  - `order_executions` insert
  - `orders` 상태/누적 수량 갱신
  - `trades` insert
  - `position_lots` 갱신
  - `positions` 갱신
  - `tax_events` 갱신 필요 시 반영

---

## 10. 전략 모듈 규칙

모든 전략은 `strategy/base.py`의 `BaseStrategy`를 상속해야 합니다.

```python
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

class BaseStrategy(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.name: str = ""

    @abstractmethod
    def generate_signals(
        self,
        universe: list[str],
        market: Literal["KR", "US"],
        as_of: datetime,
    ) -> list[Signal]:
        ...

    @abstractmethod
    def get_exit_signal(
        self,
        position: Position,
        current_price: float,
    ) -> Signal | None:
        ...
```

금지 사항:

- 문자열만 반환하는 전략 인터페이스 구현
- BaseStrategy 미상속 전략 작성
- 전략 내부에서 직접 주문 실행

---

## 11. 테스트 및 검증 규칙

### 11.1 기본 검증 명령

```bash
pytest tests/ -v
pytest tests/ -v --cov=. --cov-report=term-missing
python scripts/validate_config.py
```

### 11.2 필수 테스트 범주

- 전략 신호 생성 테스트
- 리스크 차단 테스트
- 주문/체결 상태 전이 테스트
- 부분체결 반영 테스트
- 브로커 폴링 기반 mismatch 탐지 테스트
- writer queue 직렬화 테스트
- WAL 모드 초기화 테스트

### 11.3 목표 커버리지

- 전체 단위 테스트 커버리지 **70% 이상**
- 주문/체결/정합성 관련 핵심 경로 **80% 이상 권장**

---

## 12. 절대 금지 사항

```text
🚫 토큰 값을 DB/파일/로그에 저장 금지
🚫 계좌번호, API Key, 원본 인증 헤더 로그 출력 금지
🚫 get_settings() 우회하여 설정 직접 파싱 금지
🚫 비즈니스 모듈에서 직접 sqlite write/commit 금지
🚫 WAL 미적용 상태로 운영 금지
🚫 writer queue 우회 금지
🚫 웹소켓 콜백에서 직접 포지션/원장 수정 금지
🚫 order_manager가 fill 처리 로직까지 직접 구현 금지
🚫 실전 환경에서 테스트 코드 실행 금지
🚫 브로커 상태 불일치 탐지 후 신규 주문 지속 금지
```

---

## 13. 현재 개발 우선순위

### Phase 1

- `core/settings.py`
- `core/exceptions.py`
- `data/database.py`
- `execution/writer_queue.py`
- `auth/token_manager.py`
- `execution/kis_api.py`
- `scripts/init_db.py`

### Phase 2

- `execution/order_manager.py`
- `execution/fill_processor.py`
- `strategy/*`
- `risk/*`
- `tests/test_execution/*`

### Phase 3

- `monitor/*`
- `tax/tax_calculator.py`
- `scripts/restore_portfolio.py`
- `backtest/backtest_runner.py`

---

## 14. 참고 문서

- 제품 요구사항: `docs/PRD_v1.4.md`
- DB 설계: `docs/DB_SCHEMA_v1.2.md`
- Phase 2 실행 계획: `docs/plans/phase2_execution_plan.md`
- 설정 파일: `config/config.yaml`
- 민감 정보: `config/.env`

## 문서 참조 규칙

Before implementing any feature that changes domain logic, strategy behavior,
order lifecycle, reconciliation, polling, persistence, or tax calculation,
read these documents first:

- `docs/PRD_v1.4.md`
- `docs/DB_SCHEMA_v1.2.md`

When the task is part of an ongoing staged implementation plan, also check:

- `docs/plans/phase2_execution_plan.md`

Follow these rules:
- Treat `AGENTS.md` as the always-on repository rulebook.
- Treat `docs/PRD_v1.4.md` as the source of truth for product behavior and operational policy.
- Treat `docs/DB_SCHEMA_v1.2.md` as the source of truth for persistence models, order/execution/trade relationships, reconciliation records, settlement FX fields, and SQLite architecture constraints.
- If these documents appear inconsistent, do not guess. Report the conflict first, propose the smallest safe resolution, and wait for approval before expanding scope.
- Do not implement DB schema changes, order state changes, polling/reconciliation logic, or tax-related calculations without checking the two docs above first.

## 문서 업데이트 규칙

When a code change affects product behavior, operational policy, domain models,
DB schema, order lifecycle, reconciliation, polling behavior, tax logic, or runbooks,
update the relevant documentation in the same task.

Documentation update rules:
- Update `docs/PRD_v1.4.md` when product behavior, operational rules, priorities, or phase scope changes.
- Update `docs/DB_SCHEMA_v1.2.md` when tables, fields, indexes, state relationships, settlement FX fields, or SQLite architecture rules change.
- Update `docs/plans/phase2_execution_plan.md` when Phase 2 task status, recommended next step, verification scope, or execution notes change.
- Update `AGENTS.md` when repository-wide implementation rules, coding constraints, workflow rules, or document reference/update policies change.
- If a change affects code but does not require a doc update, explicitly state why no documentation change is needed.
- Do not leave schema, state-transition, or operational behavior changes undocumented.
- If documentation and code disagree, report the mismatch first and propose the smallest safe correction.
- In every substantial task result, include a “Docs updated” summary listing which files were changed or why no doc change was required.

## 작업 전 점검 규칙

Before implementing any non-trivial change:
- identify affected modules and state boundaries first
- list assumptions that are not explicitly stated in docs
- report doc/code mismatches before editing
- for multi-step work, produce a short plan before making changes

## 검증 규칙

After every substantial change:
- run the smallest relevant test first
- then run the broader project verification command
- if tests are skipped, explain exactly why
- include the executed verification commands in the final summary

## 범위 통제 규칙

Do not expand scope without explicit approval.
Prefer the smallest safe implementation that satisfies the current task.
If a better larger refactor is possible, propose it separately instead of bundling it in.
