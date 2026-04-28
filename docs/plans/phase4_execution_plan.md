# Phase 4 Execution Plan

## Title
전략 기반 자동매매 End-to-End 실행 계획

## Status
- state: done
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
- `main.py`는 현재 `TradingRuntime`와 `AutoTrader` wiring을 함께 기동한다.
- `execution/runtime.py`는 token refresh, broker polling, fill ingestion, reconciliation, pre-close cancel, healthcheck만 수행하며, broker polling과 pre-close cancel도 `auto_trading.markets`에 설정된 시장만 대상으로 한다.
- `strategy -> resolver -> risk -> sizing -> order submit` runtime 경로는 구현되었고, Phase 4 plan 범위의 live market smoke validation까지 닫혔다.
- `StrategyDataProvider` protocol 기준 KR 자동매매용 provider와 live loader fallback이 구현되었으며, source 품질 검증은 장중 smoke 단계에서 확인한다.
- `P4-01`부터 `P4-05B`까지 scheduler hook, provider, orchestration, submit integration, safeguard/logging, `main.py` wiring, blocker hardening은 준비되었다.
- `P4-06` live smoke validation은 실제 VTS 주문 제출, 체결 반영, reconciliation 정상 유지, 반복 진입 가드 검증까지 완료했다.
- VTS soak 실행을 위해 `config/config.yaml`은 `auto_trading.enabled=true`, `monitor.telegram.enabled=true` 기준으로 정리하고, `scripts/start_auto_trading.ps1`, `scripts/stop_auto_trading.ps1`를 통해 `main.py` runtime을 백그라운드로 기동/종료할 수 있게 한다.
- 장중 soak에서 확인된 반복 진입 리스크를 막기 위해, 같은 `ticker + strategy` 기보유 포지션에 대한 추가 `buy` 진입은 기본적으로 차단한다.
- PRD 5.7 국내 시장 특수 제약은 `execution.market_constraints.MarketConstraintValidator`로 order candidate 생성 직전에 검증한다.

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

### 4.5. Main Wiring Before P4-06
- `P4-06`에 들어가기 전에 `main.py` 기준 실제 auto-trading wiring을 먼저 구현한다.
- 이 작업은 새 phase 확장이 아니라 `P4-06`의 선행 조건으로 취급한다.
- 최소 범위:
  - `AutoTrader` 인스턴스 생성
  - `strategy_cycle_runner`를 `TradingRuntime`에 실제 주입
  - KR 전용 `universe_loader` 연결
  - KR 전용 `price_history_loader` 연결
  - `OperationsRecorder` 재사용
- 제약:
  - `VTS only`
  - `KR only`
  - 신규 외부 서비스 도입 금지
  - canonical universe source가 없으면 최소 고정 universe 또는 명시적 loader로 제한
  - 데이터 부족 시 주문 제출 대신 skip/rejection으로 남긴다
- 완료 기준:
  - `main.py` 실행만으로 장중 scheduler가 실제 strategy cycle을 호출할 수 있다
  - runner는 기존 `AutoTrader.execute_cycle(...)`를 사용한다
  - `system_logs`에 skip/completed/failure가 남는다
  - live broker write는 여전히 `VTS`에서만 허용된다

### 5. Safety Guards
- 아래 중 하나라도 참이면 cycle을 skip한다.
  - `env != vts`
  - `trading_blocked=True`
  - `writer_queue_degraded=True`
  - token invalid or stale
  - polling stale
  - market closed
- 추가 상한:
- cycle당 신규 주문 최대 1건
- 동일 ticker 중복 주문 금지
- open order 존재 ticker 재진입 금지
- 같은 `ticker + strategy` 기보유 포지션에 대한 추가 `buy` 진입 금지
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
| P4-02 | KR StrategyDataProvider 구현 | done | dual/trend 전략이 runtime에서 실행 가능한 입력을 받는다 |
| P4-03 | AutoTrader orchestration 구현 | done | dry orchestration 결과가 `AutoTradeCycleResult`로 반환된다 |
| P4-04 | Order submission integration | done | cycle 1회에서 실제 주문 제출까지 이어진다 |
| P4-05 | Runtime safeguards + logging | done | 위험 상태에서 주문 제출 없이 skip 이유가 기록된다 |
| P4-05A | main.py actual auto-trading wiring | done | `main.py` 실행만으로 KR VTS strategy cycle이 실제 `AutoTrader.execute_cycle(...)`까지 연결된다 |
| P4-05B | P4-06 blocker hardening | done | broker cash fallback, cycle-scoped token reuse, price-context 보강으로 live cycle이 구조적 `data_unavailable` 없이 실행된다 |
| P4-06 | VTS scheduled smoke validation | done | 장중 1회 이상 자동 진입 또는 청산이 기존 체결/원장 경로로 반영된다 |
| P4-07 | KR market constraints | done | 가격제한폭, 동시호가, 공매도 방지, 단기과열/거래정지, T+2 현금 검증이 주문 후보 생성 전 적용된다 |

## Implementation Notes
- `P4-01` 완료
  - `core.settings`에 `auto_trading` 설정 모델을 추가했다.
  - `config/config.yaml`에 KR scheduled 기본 계약과 안전한 기본값(`enabled=false`)을 추가했다.
  - `execution/runtime.py`에 `strategy_cycle_kr` job 등록과 `strategy_cycle_runner` hook을 추가했다.
  - 실제 전략 실행, data provider, order submit 연결은 아직 하지 않았고 이후 `P4-02`, `P4-03`, `P4-04`로 분리한다.
- `P4-02` 완료
  - `strategy.data_provider.KRStrategyDataProvider`를 추가했다.
  - `event_calendar`를 읽어 KR용 `EventFlag`를 생성하고, 가격 이력은 주입 가능한 loader에서 읽도록 구현했다.
  - 현재 저장소에 collector/adjusted_price/event_calendar 모듈 기반의 영속 가격 read path가 없어서, 가격 입력은 pluggable loader로 두고 상위 orchestration이 빈 결과를 `data_unavailable`로 처리하도록 남긴다.
- `P4-03` 완료
  - `execution.auto_trader.AutoTrader`를 추가했다.
  - `universe_loader` 주입형 contract로 `generate_signals -> resolver -> risk -> sizing` dry cycle을 구성했다.
  - 현재 저장소에 canonical universe source가 없어서, universe는 orchestration 입력으로 주입받도록 두고 실제 source 연결은 `P4-04` 이후로 남긴다.
  - 이 단계는 DB write 없이 `AutoTradeCycleResult`, `ResolvedOrderCandidate`, rejection 목록만 반환한다.
- `P4-04` 완료
  - `AutoTrader.execute_cycle(...)`를 추가해 `persist_signal -> create_order_intent -> persist_validated_order -> place_order` 경로를 연결했다.
  - cycle order/notional limit을 submit 직전 단계에서 강제하고, 제출 실패는 rejection 목록에 반영하도록 구현했다.
  - 현재 저장소에 runtime/main에서 사용할 canonical universe source가 없어서, 실제 scheduled runtime wiring은 여전히 injected runner와 universe loader에 의존한다.
- `P4-05` 완료
  - `execution.runtime`에 auto-trading cycle guard를 추가해 `market_closed`, `trading_blocked`, `writer_queue_degraded`, `token_stale`, `polling_stale`, `non_vts_environment` 조건에서 runner 호출을 건너뛰도록 했다.
  - skip/completed/failed 결과는 `monitor.operations.OperationsRecorder`를 통해 `system_logs`에 best-effort로 기록하도록 정리했다.
  - 계획 문서 초안에는 `explicit prod flag`가 있었지만 현재 설정 모델에는 아직 없어서, 이번 단계는 최소 안전 해법으로 `env != vts`이면 auto-trading cycle을 hard skip하도록 고정했다.
- `P4-05A` 완료
  - `data/collector.py`를 추가해 보수적 KR 기본 universe loader와 `pykrx` best-effort price history loader를 제공하도록 정리했다.
  - `main.py`는 이제 `AutoTrader`, `KRStrategyDataProvider`, `OperationsRecorder`를 조립하고 `strategy_cycle_runner`를 `TradingRuntime`에 실제 주입한다.
  - KR 기본 universe loader는 KOSPI200 live source를 우선 사용하고, 실패 시 `data/kospi200_constituents.json` 정적 캐시, 그 다음 최소 fallback universe를 사용한다.
  - `pykrx` price history가 불가한 환경에서는 빈 history를 반환하고, 상위 orchestration은 기존 계약대로 `data_unavailable` rejection/skip으로 처리한다.
- `P4-06` blocker 보강
  - `execution.kis_api`에 KR 일봉 조회와 정규화 표면을 추가했다.
  - `data.collector`는 `pykrx -> KIS` composite KR price history loader를 제공하도록 보강했다.
  - `execution.auto_trader`는 최신 `portfolio_snapshot`이 없을 때 optional broker cash loader를 사용해 `cash_available`을 보강하도록 정리했다.
  - `main.py`의 live runner는 KR 현금 조회를 `cash_available_loader`로 연결해, 빈 `portfolio_snapshots` 상태에서도 entry sizing이 가능하도록 했다.
- `P4-05B` 완료
  - `main.py`는 같은 strategy cycle 안에서 확보한 access token을 KR broker cash 조회, KR KIS 일봉 조회, 주문 제출 경로가 재사용하도록 보강했다.
  - `execution.auto_trader`는 전략 실행 후 `latest_prices`를 계산하도록 순서를 조정해, 같은 cycle에서 이미 읽은 KR price history를 price context에 재사용하도록 정리했다.
  - live read-only validation 기준으로 `cash_available`는 broker cash로 채워지고, 구조적 `latest_price_missing` blocker는 해소되었다. 남은 no-op 가능성은 현재 시점 전략 signal이 sizing 단계에서 걸리는 경우다.
- VTS soak runbook 보강
  - `main.py`는 `TelegramNotifier`를 runtime에 주입해 기존 운영 이벤트(`token_refresh_failure`, `polling_mismatch`, `trading_blocked`, `writer_queue_degraded`, `pre_close_cancel_failure`)를 실제 텔레그램 송신 경로로 연결한다.
  - `scripts/start_auto_trading.ps1`는 `env=vts`, `auto_trading.enabled=true`를 확인한 뒤 `main.py`를 백그라운드로 실행하고 PID/로그 파일을 남긴다.
  - `scripts/stop_auto_trading.ps1`는 PID 파일과 command line을 확인한 뒤 동일 runtime만 안전하게 종료한다.
- 반복 진입 가드 보강
  - VTS scheduled soak 중 확인된 반복 매수 누적을 막기 위해 `execution.auto_trader`는 같은 `ticker + strategy`에 이미 열린 포지션이 있으면 추가 `buy` 후보를 `existing_position_reentry_blocked`로 reject한다.
  - 이 가드는 동일 종목을 다른 전략이 보유하는 경우까지 막지 않으며, PRD의 복수 전략 동시 매수 정책은 유지한다.
- KR-only runtime market scope 보강
  - `execution.runtime`은 `auto_trading.markets=["KR"]` 상태에서 US broker polling과 US pre-close cancel job을 실행하지 않는다.
  - 이에 따라 설정 범위 밖인 US market context의 `polling_mismatch`, `trading_blocked`, `pre_close_cancel_failure` Telegram 이벤트는 발생하지 않아야 한다.
- `P4-06` 완료
  - KR VTS 장중에서 scheduler 기반 strategy cycle이 실제로 주문 제출과 기존 체결/원장 경로로 이어지는 것을 확인했다.
  - `order -> order_executions -> trades -> positions` 반영과 `reconciliation_runs.status=ok`, `mismatch_count=0` 유지가 live로 검증됐다.
  - VTS soak 중 발견된 반복 진입 이슈는 `existing_position_reentry_blocked` 가드 추가 후 runtime 재시작과 후속 cycle 검증으로 재발 방지까지 확인했다.
- `P4-07` 완료
  - `execution.market_constraints`를 추가해 KR 자동매매 주문 후보 생성 전 가격제한폭, 동시호가, 공매도 방지, 단기과열/거래정지, T+2 현금 검증을 수행한다.
  - `AutoTrader.run_cycle()`은 risk/sizing 이후 market constraint를 호출하고, 실패 시 기존 `rejected_signals` 표면으로 reject reason을 남긴다.
  - 새 설정 기본값은 `risk.kr_price_limit_pct=0.30`, 동시호가 차단 enabled, `08:30-09:00`/`15:20-15:30` KST, short-sell block enabled, settlement cash buffer 0%다.

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
1. `Phase 4 VTS soak observation`

## First Recommended Task
- `Phase 4 VTS soak observation`
- 이유:
  - plan 범위의 구현과 smoke validation은 완료되었고, 다음 남은 과제는 장시간 VTS 운용에서 운영 안정성, rejection pattern, 수익률/리스크를 관찰하는 것이다.
  - 구현 미완료가 아니라 soak run 성격의 후속 운영 검증이므로 별도 작업 흐름으로 관리하는 편이 맞다.
