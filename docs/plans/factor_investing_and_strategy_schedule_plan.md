# Factor Investing And Strategy Schedule Plan

## Summary

현재 저장소 기준으로 `factor_investing`은 전략 클래스와 단위 테스트는 존재하지만 실제 auto-trading 경로에는 연결되어 있지 않습니다. 또한 KR auto-trading은 단일 `15분 cycle` 안에서 활성 전략을 모두 평가하는 구조라, 리밸런싱 전략인 `dual_momentum`과 향후 `factor_investing`까지 동일 주기로 계속 호출됩니다.

이번 계획의 목표는 아래 두 가지입니다.

1. `factor_investing`을 실제 auto-trading 경로와 VTS 검증 범위에 편입한다.
2. 전략별 실행 주기를 분리해 `trend_following`과 리밸런싱 전략의 운영/진단 노이즈를 줄인다.

핵심 결론:

- `factor_investing`은 지금 바로 VTS 검증이 불가능하다.
- 먼저 `factor input read path`, `auto-trading 연계`, `전략별 스케줄 분리`가 필요하다.
- 구현 후 `run-once smoke -> scheduled VTS smoke -> 짧은 soak` 순서로 검증한다.

## Current State

- `strategy/factor_investing.py`는 이미 존재한다.
- `tests/test_strategy/test_f4_strategies.py`에는 factor strategy 단위 테스트가 있다.
- 하지만 현재 auto-trading 경로는 `dual_momentum`, `trend_following`만 허용한다.
- `strategy/data_provider.py`의 `KRStrategyDataProvider.get_factor_inputs()`는 현재 `{}`만 반환한다.
- `execution/runtime.py`는 KR용 단일 strategy-cycle job만 등록한다.
- `execution/auto_trader.py`의 기본 strategy builder는 `factor_investing`을 포함하지 않는다.

문서/코드 기준 충돌:

- `docs/plans/phase4_execution_plan.md`는 `factor_investing`을 명시적으로 제외한 완료 문서다.
- 현재 `config/config.yaml`도 단일 `auto_trading.kr.schedule_cron`만 가진다.

이번 문서는 기존 Phase 4 완료 상태를 덮어쓰지 않고, `factor_investing + 전략별 주기 분리`를 위한 후속 계획으로 취급한다.

## Key Changes

### 1. Factor Investing auto-trading 연계

- `core.settings.AutoTradingSettings`
  - `factor_investing`을 auto-trading 허용 전략에 추가
  - 기존 `KR only` 제약은 유지
- `execution.auto_trader.AutoTrader`
  - 기본 strategy builder에 `factor_investing` 추가
  - factor strategy 후보도 기존 `signal_resolver -> risk_manager -> position_sizer -> order_manager` 경로를 그대로 사용
  - 동일 `ticker + strategy` 재진입 금지 규칙은 factor strategy에도 동일 적용
- `strategy.data_provider.KRStrategyDataProvider`
  - `get_factor_inputs()` 실제 구현
  - 1차 범위는 `KR only`
  - factor input source는 loader 주입형으로 구현
  - input source가 없으면 factor strategy 전체를 명시적 skip/diagnostics 상태로 남긴다
- `main.py`
  - factor input loader wiring 추가
  - loader 부재 시에도 runtime은 뜨되 factor strategy는 `factor_input_unavailable` 성격의 진단 상태를 남긴다

### 2. 전략별 주기 분리

- `execution/runtime.TradingRuntime`
  - 현재 단일 `strategy_cycle_kr` job을 전략별 job으로 분리
  - 추천 job:
    - `strategy_cycle_kr_trend_following`
    - `strategy_cycle_kr_dual_momentum`
    - `strategy_cycle_kr_factor_investing`
- `execution.auto_trader.AutoTrader`
  - 특정 전략 subset만 실행할 수 있는 계약 추가
  - 예: `execute_cycle(market, as_of, strategies=[...])`
- `core.settings`
  - 전략별 KR cron 설정 추가
  - 기존 `auto_trading.kr.schedule_cron`은 backward-compatible fallback으로만 유지
- 추천 기본값:
  - `trend_following`: `*/15 9-15 * * 1-5`
  - `dual_momentum`: `0 9 1 * *`
  - `factor_investing`: `5 9 1 1,4,7,10 *`

### 3. 운영 로그 / 대시보드 정합성

- `system_logs.extra_json`
  - strategy 단위 cycle 결과를 남기도록 확장
  - 최소 필드:
    - `strategy_name`
    - `strategy_cycle_status`
    - `strategy_skip_reason`
    - `factor_input_available`
- `monitor/dashboard.py`, `monitor/dashboard_app.py`
  - auto-trading diagnostics가 strategy별 상태를 보여주도록 확장
  - 예:
    - `trend_following`: completed
    - `factor_investing`: skipped (factor_input_unavailable)

### 4. 문서 동기화

- `docs/plans/phase4_execution_plan.md`
  - Phase 4 완료 상태는 유지하되, 후속 계획 문서 참조만 추가
- `docs/PRD_v1.4.md`
  - KR auto-trading이 전략별 주기를 가질 수 있도록 운영 기준 보강
- `docs/layer5_usage_runbook.md`
  - diagnostics 해석 문구 필요 시 추가

## Task Breakdown

아래 순서는 blocker를 먼저 제거하고, 이후 설정/런타임/진단 계층이 다시 흔들리지 않도록 최소 단위로 자른 구현 순서입니다.

### Task 1. Factor input contract 고정

#### Task 1.1 `KRStrategyDataProvider.get_factor_inputs()` 반환 계약 정의

- 상태:
  - done
  - `KRStrategyDataProvider`가 loader 주입형 `dict[str, FactorSnapshot]` 정규화 계약을 구현했다.
  - raw dict / `FactorSnapshot` 입력 허용, 요청 ticker 필터링, invalid payload 예외 경로 테스트를 반영했다.
- 목표:
  - factor input loader의 입력/출력 shape를 고정한다.
  - `factor_investing`이 기대하는 최소 필드와 누락 처리 규칙을 문서/코드로 맞춘다.
- 대상:
  - `strategy/data_provider.py`
  - 필요 시 `strategy/factor_investing.py`
- 완료 기준:
  - loader가 반환해야 하는 factor input 구조가 명시된다.
  - `get_factor_inputs()`가 `dict[str, FactorSnapshot]` 표면으로 실제 정규화 동작을 수행한다.
  - loader 부재 시 unavailable diagnostics 표면은 `Task 1.2`에서 다루고, `Task 1.1`은 provider contract 고정까지만 담당한다.

세부 계획:

- 범위 경계:
  - 이번 task는 provider contract만 고정한다.
  - `main.py` wiring, runtime skip reason, dashboard diagnostics는 포함하지 않는다.
  - `factor_input_unavailable` 같은 운영 표면은 `Task 1.2`로 미룬다.
- 해결된 blocker / 반영 내용:
  - `strategy/data_provider.py`의 `get_factor_inputs()` stub을 제거하고 loader 기반 정규화 경로를 반영했다.
  - `strategy/base.py`와 `strategy/factor_investing.py`가 기대하던 `dict[str, FactorSnapshot]` 소비 계약은 그대로 유지했다.
  - `tests/test_execution/test_strategy_data_provider.py`의 empty-only 기대를 contract 테스트로 교체했다.
- 구현 내용:
  - `strategy/data_provider.py`에 `FactorInputLoader` 타입 alias를 추가했다.
  - `KRStrategyDataProvider.__init__`에 `factor_input_loader` 주입 지점을 추가했다.
  - raw mapping 또는 `FactorSnapshot`을 `FactorSnapshot`으로 통일하는 `_coerce_factor_snapshot()` helper를 추가했다.
  - helper가 `ticker`, `market`, `value_score`, `quality_score`, `momentum_score`, `low_vol_score`를 정규화하도록 반영했다.
  - raw mapping에 `ticker`/`market`이 없으면 호출 문맥의 ticker key와 `market` 인자로 보정하도록 구현했다.
  - 필수 score 누락, ticker mismatch, market mismatch는 조용히 skip하지 않고 명시적 예외로 드러나게 했다.
  - `get_factor_inputs()`에 `KR only`, 요청 ticker만 반환, 중복 ticker 제거 규칙을 적용했다.
  - loader 미주입 시에는 현 단계에서 `{}`를 유지하고, 이것을 unavailable diagnostics로 승격하는 일은 `Task 1.2`에서 처리하도록 범위를 유지했다.
- 고정할 반환 계약:
  - loader 입력: `tickers`, `market`, `as_of`
  - loader 출력: `Mapping[str, FactorSnapshot | Mapping[str, Any]]`
  - provider 반환: `dict[str, FactorSnapshot]`
  - 반환 key는 요청 ticker와 동일해야 하며, provider는 요청하지 않은 ticker를 외부에 노출하지 않는다.
- 테스트 반영:
  - 기존 empty-only provider 테스트를 contract 테스트로 교체했다.
  - `FactorSnapshot` 직접 반환과 raw dict 반환을 모두 허용하는 테스트를 추가했다.
  - 요청 ticker 필터링, score 정규화, invalid payload 예외 경로를 검증했다.
  - `tests/test_strategy/test_f4_strategies.py`의 factor ranking 테스트와 `tests/test_strategy`, `tests/test_execution` 범위 회귀를 함께 확인했다.
- 비목표:
  - skip reason 문자열 고정
  - runtime diagnostics 확장
  - auto-trading wiring
  - 전략별 스케줄 분리

#### Task 1.2 loader 부재 시 skip 규칙 고정

- 목표:
  - runtime은 기동하되 factor strategy만 안전하게 skip되도록 계약을 고정한다.
- 대상:
  - `strategy/data_provider.py`
  - `execution/auto_trader.py`
  - `main.py`
- 완료 기준:
  - `factor_input_unavailable` 성격의 skip reason이 단일 규칙으로 정해진다.
  - hard failure와 intentional skip이 구분된다.

#### Task 1.3 factor input contract 테스트 추가

- 목표:
  - input 존재/부재 모두 재현 가능한 테스트를 먼저 만든다.
- 대상:
  - `tests/test_strategy/*`
  - 필요 시 `tests/test_execution/*`
- 완료 기준:
  - factor input 존재 시 ranking 기반 signal 생성 테스트가 통과한다.
  - factor input 부재 시 skip 경로 테스트가 통과한다.

### Task 2. Factor investing auto-trading 경로 연결

#### Task 2.1 settings 허용 전략 확장

- 목표:
  - `factor_investing`을 auto-trading 허용 전략으로 추가한다.
- 대상:
  - `core/settings.py`
  - `config/config.yaml`
- 완료 기준:
  - `auto_trading.strategies=["factor_investing"]`가 validation을 통과한다.
  - 1차 범위가 `KR only`라는 제약은 유지된다.

#### Task 2.2 AutoTrader 기본 strategy builder 확장

- 목표:
  - factor strategy를 기존 주문 파이프라인에 연결한다.
- 대상:
  - `execution/auto_trader.py`
- 완료 기준:
  - `factor_investing`이 `signal_resolver -> risk_manager -> position_sizer -> order_manager` 경로를 그대로 사용한다.
  - 동일 `ticker + strategy` 재진입 금지 규칙이 factor strategy에도 그대로 적용된다.

#### Task 2.3 bootstrap wiring 추가

- 목표:
  - main bootstrap에서 factor input loader를 주입한다.
- 대상:
  - `main.py`
- 완료 기준:
  - loader가 있으면 factor strategy가 실행 가능 상태가 된다.
  - loader가 없어도 runtime 기동은 유지된다.

### Task 3. 전략별 스케줄 분리

#### Task 3.1 전략 subset 실행 계약 추가

- 목표:
  - 특정 전략만 실행할 수 있는 AutoTrader 표면을 추가한다.
- 대상:
  - `execution/auto_trader.py`
- 완료 기준:
  - 예: `execute_cycle(market, as_of, strategies=[...])`
  - trend job이 factor strategy를 호출하지 않는다.

#### Task 3.2 전략별 KR cron 설정 추가

- 목표:
  - 기존 단일 KR cron에서 전략별 cron으로 확장한다.
- 대상:
  - `core/settings.py`
  - `config/config.yaml`
- 완료 기준:
  - `trend_following`, `dual_momentum`, `factor_investing` 각각의 KR cron이 설정 가능하다.
  - 기존 `auto_trading.kr.schedule_cron`은 fallback으로만 유지된다.

#### Task 3.3 runtime job 분리

- 목표:
  - KR 단일 strategy cycle job을 전략별 job으로 나눈다.
- 대상:
  - `execution/runtime.py`
- 완료 기준:
  - `strategy_cycle_kr_trend_following`
  - `strategy_cycle_kr_dual_momentum`
  - `strategy_cycle_kr_factor_investing`
  - 리밸런싱 전략이 불필요한 15분 no-op를 남기지 않는다.

### Task 4. 운영 진단 정합성 보강

#### Task 4.1 strategy별 cycle log 확장

- 목표:
  - strategy 단위 결과와 skip reason을 로그에 남긴다.
- 대상:
  - `system_logs.extra_json` 작성 경로
- 완료 기준:
  - 최소 필드 `strategy_name`, `strategy_cycle_status`, `strategy_skip_reason`, `factor_input_available`가 기록된다.

#### Task 4.2 dashboard diagnostics 확장

- 목표:
  - auto-trading diagnostics가 strategy별 상태를 구분해서 보여주도록 한다.
- 대상:
  - `monitor/dashboard.py`
  - `monitor/dashboard_app.py`
- 완료 기준:
  - 예: `trend_following: completed`
  - 예: `factor_investing: skipped (factor_input_unavailable)`

### Task 5. 문서 및 검증 동기화

#### Task 5.1 후속 계획 참조 반영

- 목표:
  - 기존 Phase 4 완료 문서를 유지하면서 후속 범위를 연결한다.
- 대상:
  - `docs/plans/phase4_execution_plan.md`
- 완료 기준:
  - 후속 계획 문서 참조가 추가된다.

#### Task 5.2 운영 기준 문서 업데이트

- 목표:
  - 전략별 KR 스케줄과 diagnostics 해석 기준을 문서에 반영한다.
- 대상:
  - `docs/PRD_v1.4.md`
  - `docs/layer5_usage_runbook.md`
- 완료 기준:
  - PRD 운영 기준과 runbook 진단 문구가 구현 결과와 맞는다.

#### Task 5.3 검증 순서 실행

- 목표:
  - 구현 완료 후 검증 순서를 문서 기준으로 고정한다.
- 대상:
  - 테스트 및 운영 검증 절차
- 완료 기준:
  - `run-once smoke -> scheduled VTS smoke -> soak` 순서가 실제 검증 체크리스트로 남는다.

## Verification Plan

### 1. 단위/통합 테스트

- `factor_investing`이 factor input 존재 시 ranking대로 buy/sell 생성
- factor input 부재 시 factor strategy가 명시적 skip으로 기록
- `auto_trading.strategies=["factor_investing"]`가 settings validation 통과
- 전략별 job 등록 테스트
- `trend_following` job이 factor strategy를 호출하지 않음
- `factor_investing` job이 input 부재 시 실패하지 않고 skip 로그만 남김
- dashboard diagnostics가 strategy별 결과를 표시

### 2. VTS 검증

- 사전 점검:
  - `env=vts`
  - `auto_trading.enabled=true`
  - 전략별 cron 적용
  - factor input source 준비 여부 확인
- smoke validation:
  - `trend_following`: 장중 정상 cycle 유지
  - `dual_momentum`: rebalance day run-once 검증
  - `factor_investing`: rebalance month/day 조건에서 run-once 또는 controlled scheduled validation
- acceptance:
  - factor strategy가 최소 1회 `input loaded` 또는 명시적 skip reason을 남김
  - 주문 발생 시 기존 `order_executions -> trades -> positions` 경로 유지

### 3. Soak

- 최소 1거래일
- 확인 항목:
  - 리밸런싱 전략이 불필요한 15분 no-op를 남기지 않는지
  - strategy별 logs가 구분되어 해석 가능한지
  - blocked/stale/mismatch가 strategy scheduling과 섞여 혼선이 없는지

## Important Interface Changes

- `AutoTradingSettings`
  - 전략별 KR cron 필드 추가
  - `factor_investing` 허용
- `AutoTrader`
  - 전략 subset 실행 계약 추가
- `KRStrategyDataProvider`
  - `get_factor_inputs()` 실제 구현
- `system_logs.extra_json`
  - strategy별 diagnostics scalar 추가
- `DashboardSnapshot.auto_trading_diagnostics`
  - strategy별 상태 지원

## Assumptions / Defaults

- 1차 범위는 계속 `KR only`
- factor input source는 외부 신규 서비스 도입 없이 loader 주입형으로 구현
- `factor_investing`은 리밸런싱 전략으로 유지
- 전략별 주기 분리는 이번 작업 범위에 포함
- 구현 후 검증은 `run-once -> scheduled smoke -> soak` 순서로 진행

## Recommended Next Task

`Task 1.2`, 즉 loader 부재 시 factor strategy를 hard failure가 아니라 명시적 skip/diagnostics 상태로 남기는 규칙을 고정한다.

이유:

- `Task 1.1`로 provider 반환 계약과 테스트는 고정됐다.
- 이제 실제 runtime에서 필요한 다음 blocker는 loader 부재를 운영상 skip으로 처리할지, 오류로 처리할지의 단일 규칙이다.
- 이 규칙이 고정돼야 `main.py`, `AutoTrader`, diagnostics 표면을 다시 흔들지 않고 연결할 수 있다.
