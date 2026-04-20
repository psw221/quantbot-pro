# Phase 4 Execution Plan

## Title
전략 기반 자동매매 End-to-End 실행 계획

## Status
- state: proposed
- default_env: vts
- rollout_mode: scheduled
- markets: KR only
- strategies: dual_momentum, trend_following

## Summary
- 목표는 `VTS scheduled` 모드에서 `dual_momentum`과 `trend_following` 전략을 실제 자동 주문 경로까지 연결하는 것이다.
- 기존 주문/체결/폴링/reconciliation 코어는 재설계하지 않고, 그 앞단에 `strategy -> resolver -> risk -> sizing -> order intent -> broker submit` orchestration만 추가한다.
- 1차 범위는 `KR` 시장으로 고정한다. 현재 VTS 실검증과 broker poll/fill ingestion이 KR 기준으로 가장 닫혀 있기 때문이다.
- 성공 기준은 장중 스케줄러가 전략을 주기적으로 실행하고, 설정된 상한 안에서 주문 생성, 제출, 체결 반영, 운영 로그까지 한 경로로 재현되는 것이다.

## Current State
- `main.py`는 현재 `TradingRuntime`만 기동한다.
- `execution/runtime.py`는 token refresh, broker polling, fill ingestion, reconciliation, pre-close cancel, healthcheck만 수행한다.
- 전략 구현은 존재하지만 `strategy -> resolver -> risk -> sizing -> order submit`을 한 번에 연결하는 runtime 경로는 없다.
- `StrategyDataProvider` protocol은 정의되어 있으나, runtime에서 바로 쓰는 KR 자동매매용 provider 구현은 별도 정리가 필요하다.

## Scope
### In Scope
- `VTS scheduled` 기준 KR 자동매매 orchestration 추가
- `dual_momentum`, `trend_following` 전략 실행 경로 연결
- signal resolution, risk, sizing, order intent, broker submit 연계
- runtime scheduler job 추가
- 운영 안전장치, skip 조건, 최소 운영 로그 정리
- 관련 테스트와 VTS smoke validation 절차 정리

### Out of Scope
- 주문 실행 코어 전면 재설계
- US 자동매매
- `factor_investing` 전략 자동매매 연계
- 웹소켓 기반 실시간 전략 트리거
- PROD 자동매매 enable
- 대규모 UI 확장 또는 신규 외부 서비스 도입

## Defaults And Assumptions
- 첫 자동매매 경로는 `KR only`다.
- 실행 환경 기본값은 `env=vts`다.
- 장중 반복 실행은 `APScheduler in-process`를 그대로 사용한다.
- `factor_investing`는 이번 단계에서 제외한다.
- 기존 주문 상태, reconciliation 상태, writer queue, settlement FX 규칙은 그대로 유지한다.
- PROD 활성화는 별도 후속 작업으로 분리한다.

## Implementation Plan
### 1. Runtime Orchestration
- `execution/runtime.py`에 `strategy cycle job`을 추가한다.
- cycle 순서는 아래로 고정한다.
  1. 시장 상태 확인
  2. `trading_blocked`, `writer_queue_degraded`, token invalid, polling stale 확인
  3. universe 로드
  4. 전략별 `generate_signals(...)` 실행
  5. `signal_resolver`로 ticker 충돌 해소
  6. 보유 포지션 기준 `get_exit_signal(...)` 병합
  7. `risk_manager` 적용
  8. `position_sizer` 적용
  9. `OrderIntent` 생성
  10. `order_manager.place_order(...)` 호출
- runtime은 orchestration만 담당하고, 전략 내부 계산이나 raw broker parsing은 담당하지 않는다.

### 2. Auto-Trader Service
- `execution/auto_trader.py` 또는 동등한 단일 서비스 계층을 추가한다.
- 책임은 아래로 제한한다.
  - 전략 인스턴스 생성
  - 전략 실행 순서 관리
  - signal, resolver, risk, sizing 연결
  - 주문 제출 대상 반환
- 금지 사항:
  - DB 직접 write
  - KIS raw payload 처리
  - fill 처리
- 공개 계약:
  - `run_cycle(market: Literal["KR"], as_of: datetime) -> AutoTradeCycleResult`

### 3. Strategy Data Provider
- runtime에서 사용할 `StrategyDataProvider` 구현을 추가한다.
- 1차 데이터 범위:
  - 가격 이력
  - 이벤트 플래그
- `factor_investing` 입력은 이번 범위에서 제외한다.
- 정책:
  - DB read path 우선
  - 데이터 부족 시 주문 실행으로 진행하지 않고 cycle result에 `data_unavailable`를 남긴다.

### 4. Config And Entry Contract
- `config/config.yaml`에 auto-trading 설정을 추가한다.
- 필수 설정:
  - `auto_trading.enabled`
  - `auto_trading.markets`
  - `auto_trading.kr.schedule_cron`
  - `auto_trading.strategies`
  - `auto_trading.max_orders_per_cycle`
  - `auto_trading.max_order_notional_per_cycle`
  - `auto_trading.allow_new_entries`
  - `auto_trading.allow_exits`
- 기본값:
  - `markets=["KR"]`
  - `strategies=["dual_momentum","trend_following"]`
  - `max_orders_per_cycle=1`
  - `allow_new_entries=true`
  - `allow_exits=true`
- `main.py`는 새 서비스를 wiring만 하고 실행 정책 판단은 runtime과 auto trader에서 처리한다.

### 5. Safety Guards
- 아래 중 하나라도 참이면 cycle을 skip한다.
  - `env != vts` and explicit prod flag not set
  - `trading_blocked=True`
  - `writer_queue_degraded=True`
  - token invalid or stale
  - polling stale
  - market closed
- 추가 상한:
  - cycle당 신규 주문 최대 1건
  - 동일 ticker 중복 주문 금지
  - open order 존재 ticker 재진입 금지
  - unresolved signal conflict는 `hold`
- notifier는 새 정책을 추가하지 않고, 이번 단계에서는 `system_logs` 기록 중심으로 제한한다.

## Public Interfaces
- 새 타입:
  - `AutoTradeCycleResult`
  - `ResolvedOrderCandidate`
  - 필요 시 `AutoTradingConfig`
- 새 서비스 인터페이스:
  - `AutoTrader.run_cycle(market, as_of) -> AutoTradeCycleResult`
- runtime 연계:
  - `TradingRuntime._run_strategy_cycle_job()`
- 유지 대상:
  - `BaseStrategy`
  - `OrderManager`
  - `FillProcessor`
  - `ReconciliationService`

## Task Breakdown
| ID | Task | Status | Done Criteria |
| --- | --- | --- | --- |
| P4-01 | Auto-trading config + runtime wiring | done | enabled 상태에서 strategy cycle job이 runtime에 등록된다 |
| P4-02 | KR StrategyDataProvider 구현 | todo | dual/trend 전략이 runtime에서 실행 가능한 입력을 받는다 |
| P4-03 | AutoTrader orchestration 구현 | todo | dry orchestration 결과가 `AutoTradeCycleResult`로 반환된다 |
| P4-04 | Order submission integration | todo | cycle 1회에서 실제 주문 제출까지 이어진다 |
| P4-05 | Runtime safeguards + logging | todo | 위험 상태에서 주문 제출 없이 skip 이유가 기록된다 |
| P4-06 | VTS scheduled smoke validation | todo | 장중 1회 이상 자동 진입 또는 청산이 기존 체결/원장 경로로 반영된다 |

## Implementation Notes
- `P4-01` 완료
  - `core.settings`에 `auto_trading` 설정 모델을 추가했다.
  - `config/config.yaml`에 KR scheduled 기본 계약과 안전한 기본값(`enabled=false`)을 추가했다.
  - `execution/runtime.py`에 `strategy_cycle_kr` job 등록과 `strategy_cycle_runner` hook을 추가했다.
  - 실제 전략 실행, data provider, order submit 연결은 아직 하지 않았고 이후 `P4-02`, `P4-03`, `P4-04`로 분리한다.

## Test Plan
- 단위 테스트
  - `dual_momentum`, `trend_following` 신호가 resolver를 통과하는지
  - risk reject 시 주문이 생성되지 않는지
  - sizing 결과 0이면 주문이 생성되지 않는지
  - open order, blocked, degraded, stale 상태에서 cycle skip 되는지
  - market closed에서 skip 되는지
- orchestration 테스트
  - cycle당 최대 1건만 submit 되는지
  - entry와 exit signal 충돌 시 resolver 결과만 submit 되는지
  - data unavailable 시 submit 없이 reason만 남는지
- runtime 테스트
  - APScheduler에 strategy cycle job이 등록되는지
  - polling, health job과 독립적으로 동작하는지
- integration-style 테스트
  - `generate_signals -> resolver -> risk -> sizing -> place_order` mock 경로
  - submit 성공 시 `orders` 상태가 기존 lifecycle로 이어지는지

## Verification Commands
```bash
python -m pytest tests\test_execution -q
python -m pytest tests\ -q
python -m compileall core execution strategy risk tests main.py
```

## Acceptance Criteria
- VTS KR 장중에서 scheduler 기반 전략 cycle이 실제로 실행된다.
- cycle은 문서화된 skip 조건을 지키며 blocked/degraded/stale 상태에서 주문을 보내지 않는다.
- `dual_momentum`, `trend_following` 전략 신호가 resolver, risk, sizing, order submit까지 한 경로로 이어진다.
- 기존 order lifecycle, fill ingestion, reconciliation, writer queue 제약을 깨지 않는다.
- VTS smoke validation에서 최소 1건의 자동 주문이 기존 원장 반영 경로로 이어진다.

## Docs Update Expectations
- `docs/PRD_v1.4.md`
  - Phase 4 범위와 auto-trading 운영 기준이 확정되면 반영
- `docs/DB_SCHEMA_v1.2.md`
  - 새 저장 필드나 테이블이 생길 경우에만 반영
- `docs/plans/phase4_execution_plan.md`
  - task status, verification scope, implementation notes 갱신

## Recommended Start Order
1. `P4-02 KR StrategyDataProvider 구현`
2. `P4-03 AutoTrader orchestration 구현`
3. `P4-04 Order submission integration`
4. `P4-05 Runtime safeguards + logging`
5. `P4-06 VTS scheduled smoke validation`

## First Recommended Task
- `P4-02 KR StrategyDataProvider 구현`
- 이유:
  - `P4-01`에서 scheduler job과 설정 계약이 고정됐으므로, 다음은 전략이 실제로 consume할 KR 가격/event read path를 먼저 닫아야 한다.
  - provider가 먼저 있어야 `P4-03` orchestration이 mock이 아니라 실제 입력 계약 위에서 구현된다.
