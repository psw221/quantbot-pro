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

- 상태:
  - done
  - loader 부재는 `factor_input_unavailable` strategy-local skip으로 고정했다.
  - payload mismatch나 loader 예외는 skip이 아니라 hard failure로 남도록 분리했다.
- 목표:
  - runtime은 기동하되 factor strategy만 안전하게 skip되도록 계약을 고정한다.
- 대상:
  - `strategy/data_provider.py`
  - `execution/auto_trader.py`
  - `main.py`
- 완료 기준:
  - `factor_input_unavailable` 성격의 skip reason이 단일 규칙으로 정해진다.
  - hard failure와 intentional skip이 구분된다.

세부 계획:

- 범위 경계:
  - 이번 task는 "loader 부재"만 intentional skip으로 승격한다.
  - loader payload mismatch, loader 내부 예외, strategy 코드 버그는 계속 hard failure로 남긴다.
  - `factor_investing` 실제 auto-trading 허용과 settings 확장은 `Task 2.1`에서 다룬다.
  - strategy별 dashboard rendering과 로그 확장은 본격적으로는 `Task 4` 범위다.
- 현재 blocker / mismatch:
  - `main.py`는 아직 factor input loader를 wiring하지 않아 runtime이 loader 부재를 의도된 상태로 표현할 수 없다.
  - `execution/auto_trader.py`는 전략을 일괄 실행할 뿐 strategy별 skip/diagnostics 표면이 없다.
  - `execution/runtime.py`와 `monitor/dashboard.py`는 cycle-level summary만 읽기 때문에 strategy별 skip 이유를 아직 직접 노출하지 못한다.
  - `core/settings.AutoTradingSettings`는 아직 `factor_investing`을 허용하지 않으므로, `Task 1.2` 검증은 injected builder 또는 settings override 기반 테스트로 좁혀야 한다.
- 고정할 동작 계약:
  - factor input loader가 없는 상태는 `factor_input_unavailable`이라는 단일 skip reason으로 취급한다.
  - 이 상태는 cycle failure가 아니라 strategy-local skip이다.
  - skip된 factor strategy는 buy/sell signal을 생성하지 않는다.
  - skip 사실은 후속 runtime/log/dashboard 계층이 소비할 수 있도록 structured diagnostics로 남긴다.
  - 반대로 loader가 존재하지만 payload 검증에 실패하면 skip이 아니라 예외로 남겨야 한다.
- 구현 단계:
  - `strategy/data_provider.py`에 factor input availability를 조회하는 최소 표면을 추가한다.
  - 추천 표면은 boolean + reason을 함께 주는 helper이며, raw loader 존재 여부만 보는 단순 property보다 diagnostics 확장에 유리하다.
  - `main.py`는 factor input loader 주입 지점을 추가하되, 현재 기본값은 `None`으로 둬 runtime 기동을 유지한다.
  - `execution/auto_trader.py`의 cycle result에 strategy별 diagnostics container를 추가한다.
  - diagnostics 최소 필드는 `strategy_name`, `status`, `skip_reason`, `factor_input_available`로 고정한다.
  - factor strategy 실행 전 availability를 먼저 확인하고, unavailable이면 strategy를 건너뛰고 diagnostics만 기록한다.
  - dual/trend 전략은 기존과 동일하게 실행하고, factor loader 부재 때문에 함께 skip되면 안 된다.
  - strategy-local skip은 `generated_signals`, `resolved_signals`, `rejected_signals` 집계와 분리해서 남긴다.
- 테스트 계획:
  - `tests/test_execution/test_auto_trader.py`
    - factor strategy가 활성화된 구성에서 loader 부재 시 cycle이 실패하지 않고 diagnostics에 `factor_input_unavailable`가 남는지 검증한다.
    - 같은 상황에서 dual/trend 전략은 정상 실행되는지 검증한다.
    - loader payload 오류는 skip이 아니라 예외 또는 cycle failure로 드러나는지 분리 검증한다.
  - `tests/test_execution/test_main_wiring.py`
    - factor loader가 없어도 `build_strategy_cycle_runner()`가 정상 runner를 반환하는지 검증한다.
    - 추후 loader를 주입할 수 있는 bootstrap hook이 유지되는지 검증한다.
  - 필요 시 `tests/test_execution/test_runtime.py`
    - strategy diagnostics가 runtime result에 포함돼도 기존 cycle completed/skipped 로그 계약이 깨지지 않는지 최소 회귀를 확인한다.
- 비목표:
  - `factor_investing`을 `auto_trading.strategies` 허용 목록에 추가하는 일
  - strategy별 cron 분리
  - dashboard panel의 strategy별 렌더링 완성
  - factor input source의 실제 구현
- 구현 결과:
  - `strategy.base.StrategyDataProvider`에 factor input availability 표면을 추가했다.
  - `strategy.data_provider.KRStrategyDataProvider`가 loader 부재를 `factor_input_unavailable`로 보고하도록 반영했다.
  - `execution.auto_trader.AutoTradeCycleResult`에 strategy diagnostics를 추가했다.
  - `execution.auto_trader.AutoTrader`가 factor strategy 실행 전에 availability를 평가하고 unavailable이면 diagnostics만 남기고 skip하도록 반영했다.
  - dual/trend 전략은 기존처럼 계속 실행되고 factor loader 부재 때문에 함께 skip되지 않도록 유지했다.
  - `execution.runtime.py`가 cycle log extra에 `strategy_diagnostics`를 함께 남기도록 확장했다.
  - `monitor.dashboard.py`가 latest cycle diagnostics에서 `strategy_diagnostics`를 함께 읽을 수 있도록 확장했다.
  - `main.py`에 optional factor input loader wiring hook을 추가했다.
- 검증 결과:
  - `tests/test_execution/test_auto_trader.py`
    - loader 부재 시 factor strategy skip diagnostics
    - dual strategy 정상 실행 유지
    - invalid factor payload는 예외 유지
  - `tests/test_execution/test_main_wiring.py`
    - optional factor input loader hook 전달 확인
  - `tests/test_execution/test_runtime.py`
    - runtime cycle log에 `strategy_diagnostics` 전달 확인
  - `tests/test_execution/test_dashboard_app.py`
    - latest diagnostics가 `strategy_diagnostics`를 유지하는지 확인
  - broader regression:
    - `python -m pytest tests\test_strategy tests\test_execution -q`

#### Task 1.3 factor input contract 테스트 추가

- 상태:
  - done
  - `Task 1.1`, `Task 1.2`, `Task 2.1`, `Task 2.2` 결과를 기준으로 provider contract, skip diagnostics, canonical settings/default builder regression까지 마감했다.
  - injected builder 우회 경로뿐 아니라 실제 settings/default builder 경로에서 factor strategy run/execute/reentry block 동작을 고정했다.
- 목표:
  - input 존재/부재 모두 재현 가능한 테스트를 먼저 만든다.
- 대상:
  - `tests/test_strategy/*`
  - 필요 시 `tests/test_execution/*`
- 완료 기준:
  - factor input 존재 시 ranking 기반 signal 생성 테스트가 통과한다.
  - factor input 부재 시 skip 경로 테스트가 통과한다.

세부 계획:

- 범위 경계:
  - 이번 task의 남은 초점은 "이미 구현된 contract를 실제 auto-trading 표면에서 어떻게 검증 마감할지"다.
  - provider-level contract 자체는 `Task 1.1`에서 이미 검증했다.
  - loader 부재 skip/diagnostics 자체는 `Task 1.2`에서 이미 검증했다.
  - settings 허용 전략 확장과 default builder 연결은 각각 `Task 2.1`, `Task 2.2`의 구현 범위이며, `Task 1.3`은 그 결과를 검증하는 테스트 마감 범위다.
- 이미 커버된 테스트:
  - `tests/test_execution/test_strategy_data_provider.py`
    - loader 없음 시 empty response 유지
    - raw dict / `FactorSnapshot` 정규화
    - invalid payload 예외
  - `tests/test_strategy/test_f4_strategies.py`
    - quarterly ranking 기반 factor signal 생성
  - `tests/test_execution/test_auto_trader.py`
    - loader 부재 시 factor strategy skip diagnostics
    - payload mismatch는 hard failure 유지
    - loader 존재 시 `factor_input_available=True` diagnostics
    - factor strategy `execute_cycle()` order persistence / submission
    - factor position existing + loader 부재 시 exit evaluation skip
  - `tests/test_execution/test_main_wiring.py`
    - optional factor input loader hook 전달
  - `tests/test_execution/test_runtime.py`, `tests/test_execution/test_dashboard_app.py`
    - runtime log / dashboard diagnostics에 `strategy_diagnostics` 전달
- 해소된 blocker / 추가 반영:
  - `core.settings.AutoTradingSettings`가 이제 `factor_investing`을 허용해 canonical settings 기반 테스트를 직접 작성할 수 있다.
  - `execution.auto_trader._default_strategy_builders()`가 factor strategy를 포함해 injected builder 우회가 더 이상 필요하지 않다.
  - factor input이 있을 때 `strategy_diagnostics.factor_input_available=True`가 default auto-trading 경로에서도 남는지 검증했다.
  - 실제 default builder + canonical settings 경로에서 factor strategy가 `execute_cycle()` order persistence/submission 경로를 타는 테스트를 추가했다.
- 고정할 테스트 매트릭스:
  - provider contract:
    - loader 없음
    - loader 있음
    - invalid payload
  - strategy signal generation:
    - rebalance day + valid factor input
    - non-rebalance day
    - top_n ranking / sell-on-drop
  - auto-trading run cycle:
    - factor enabled + loader 없음 -> strategy skip
    - factor enabled + loader 있음 -> diagnostics available true
    - factor enabled + invalid payload -> cycle failure
  - execute cycle:
    - factor enabled + loader 있음 -> signal persistence / validated order / submitted order
  - diagnostics propagation:
    - runtime log extra에 strategy diagnostics 포함
    - dashboard latest diagnostics에 strategy diagnostics 유지
- 구현 결과:
  - `tests/test_execution/test_bootstrap.py`에서 `auto_trading.strategies=["factor_investing"]` 허용 케이스와 unsupported/duplicate/empty reject 케이스를 새 허용 집합 기준으로 정리했다.
  - `tests/test_execution/test_auto_trader.py`에서 injected builder 없이 default builder 경로를 직접 검증하도록 추가 테스트를 반영했다.
  - factor loader가 실제로 주입된 상태에서 `run_cycle()`과 `execute_cycle()` 각각의 성공 경로를 분리 검증했다.
  - no-loader / invalid-payload / valid-loader 세 경로를 같은 테스트 묶음에서 계속 비교 가능하게 유지했다.
  - factor position existing + loader unavailable 시 exit skip, existing factor position + loader available 시 reentry block 동작을 각각 고정했다.
- 완료 기준 구체화:
  - `build_settings(..., auto_trading={"strategies": ["factor_investing"]})`가 허용된다.
  - default `AutoTrader` strategy builder가 factor strategy를 포함한 상태에서 관련 테스트가 통과한다.
  - factor loader 존재 시 `strategy_diagnostics`에 `status=completed`, `factor_input_available=True`가 남는다.
  - factor loader 부재 시 `strategy_diagnostics`에 `status=skipped`, `skip_reason=factor_input_unavailable`가 남는다.
  - invalid factor payload는 skip으로 삼켜지지 않고 실패로 남는다.
- 현재까지의 검증:
  - `python -m pytest tests\test_execution\test_bootstrap.py -q`
  - `python -m pytest tests\test_execution\test_auto_trader.py -q`
  - `python -m pytest tests\test_strategy tests\test_execution -q`
- 비목표:
  - factor input source 구현 자체
  - 전략별 cron 분리 테스트
  - dashboard panel의 세부 UI 레이아웃 검증

### Task 2. Factor investing auto-trading 경로 연결

#### Task 2.1 settings 허용 전략 확장

- 상태:
  - done
  - `core.settings.AutoTradingSettings` validator가 `factor_investing`을 허용하도록 확장됐다.
  - `tests/test_execution/test_bootstrap.py`가 factor 허용 / unsupported reject / duplicate reject / empty reject 계약으로 갱신됐다.
  - `config/config.yaml`의 기본 활성 전략 목록은 `[dual_momentum, trend_following]`로 유지해 loader 부재 기본 런타임의 전략 skip 노이즈를 늘리지 않았다.
- 목표:
  - `factor_investing`을 auto-trading 허용 전략으로 추가한다.
- 대상:
  - `core/settings.py`
  - `config/config.yaml`
- 완료 기준:
  - `auto_trading.strategies=["factor_investing"]`가 validation을 통과한다.
  - 1차 범위가 `KR only`라는 제약은 유지된다.

세부 계획:

- 범위 경계:
  - 이번 task는 settings validator와 config contract를 여는 작업이다.
  - `AutoTrader` 기본 builder에 factor strategy를 추가하는 일은 `Task 2.2`에서 다룬다.
  - `main.py` bootstrap에서 실제 factor loader를 연결하는 일은 `Task 2.3`에서 다룬다.
  - 따라서 이번 task만으로 기본 auto-trading 실행 경로가 factor strategy를 즉시 실행 가능해지는 것은 아니다.
- 현재 blocker / mismatch:
  - `core.settings.AutoTradingSettings.validate_supported_scope()`는 아직 `factor_investing`을 불허한다.
  - `tests/test_execution/test_bootstrap.py`도 현재는 `auto_trading.strategies=["factor_investing"]`를 reject하는 기준을 고정하고 있다.
  - `config/config.yaml`은 전략 weights와 strategy config에는 이미 `factor_investing`이 존재하지만, `auto_trading.strategies`에는 포함되지 않아 설정 표면이 분리돼 있다.
  - 현재 `config/config.yaml`의 `auto_trading.enabled`가 `true`이므로, `Task 2.2` 전에 기본 활성 전략 목록까지 바로 바꾸면 default builder와 충돌할 수 있다.
- 고정할 설정 계약:
  - 허용 전략 집합은 `{"dual_momentum", "trend_following", "factor_investing"}`로 확장한다.
  - `markets == ["KR"]` 제약은 그대로 유지한다.
  - 중복 전략 금지, empty 전략 금지, max order/max notional 검증은 그대로 유지한다.
  - `factor_investing` 단독 구성과 혼합 구성은 validation을 통과해야 한다.
  - 미지원 전략 문자열은 계속 reject해야 한다.
- config 기본값 정책:
  - 이번 task에서는 `config/config.yaml`의 기본 `auto_trading.strategies`를 즉시 `factor_investing` 포함으로 바꾸지 않는 안을 기본안으로 둔다.
  - 이유:
    - `Task 2.2` 전에는 default strategy builder가 factor strategy를 아직 생성하지 못한다.
    - 현재 `auto_trading.enabled=true` 기본값과 결합되면 설정은 통과하지만 runtime에서 unsupported strategy failure가 날 수 있다.
  - 대신 `config/config.yaml`은 "허용 가능하지만 기본 활성은 아직 dual/trend 유지" 상태를 유지하고, 이 판단을 문서에 명시한다.
  - 만약 같은 작업에서 default builder까지 함께 열리면 그때 기본 활성 전략 목록 변경 여부를 다시 판단한다.
- 구현 단계:
  - `core/settings.py`
    - `validate_supported_scope()`의 허용 전략 집합에 `factor_investing`을 추가한다.
    - reject message도 새 허용 집합에 맞게 갱신한다.
  - `tests/test_execution/test_bootstrap.py`
    - 기존 accept contract 테스트에 `factor_investing` 허용 케이스를 추가한다.
    - 기존 reject 테스트는 US market, duplicate, empty, truly unsupported strategy 중심으로 재정리한다.
    - 기존 `factor_investing` reject 기대는 제거한다.
  - `config/config.yaml`
    - 기본 `auto_trading.strategies`는 우선 `[dual_momentum, trend_following]` 유지 여부를 명시적으로 확인한다.
    - 필요하면 주석/문서 수준 설명 대신 계획 문서에서 이 의도를 고정하고, config 값 자체는 보수적으로 유지한다.
- 테스트 계획:
  - `tests/test_execution/test_bootstrap.py`
    - `build_settings(..., auto_trading={"strategies": ["factor_investing"]})` 허용
    - `build_settings(..., auto_trading={"strategies": ["dual_momentum", "factor_investing"]})` 허용
    - `build_settings(..., auto_trading={"strategies": ["unsupported_strategy"]})` reject
    - duplicate strategy / empty strategy / non-KR market reject 유지
  - broader regression:
    - settings validation 관련 실행 경로가 깨지지 않도록 `tests/test_execution` 범위 회귀 확인
- 완료 기준 구체화:
  - validator가 `factor_investing`을 허용한다.
  - bootstrap 테스트가 새 허용 집합을 기준으로 통과한다.
  - 기본 config는 `Task 2.2` 전 runtime failure를 만들지 않도록 보수적 상태를 유지하거나, 변경 시 동일 task 안에서 안전성이 입증된다.
- 비목표:
  - factor strategy 기본 builder 등록
  - factor input loader 실제 연결
  - strategy별 cron 분리

#### Task 2.2 AutoTrader 기본 strategy builder 확장

- 상태:
  - done
  - `execution.auto_trader._default_strategy_builders()`가 `FactorInvestingStrategy`를 포함한다.
  - canonical settings/default builder 기준 `run_cycle()`, `execute_cycle()`, 동일 `ticker + strategy` 재진입 차단 회귀를 추가했다.
- 목표:
  - factor strategy를 기존 주문 파이프라인에 연결한다.
- 대상:
  - `execution/auto_trader.py`
- 완료 기준:
  - `factor_investing`이 `signal_resolver -> risk_manager -> position_sizer -> order_manager` 경로를 그대로 사용한다.
  - 동일 `ticker + strategy` 재진입 금지 규칙이 factor strategy에도 그대로 적용된다.

세부 계획:

- 범위 경계:
  - 이번 task는 `AutoTrader` 기본 builder 연결까지만 담당한다.
  - `main.py`에서 실제 factor input loader를 조립하는 bootstrap wiring은 `Task 2.3`로 남긴다.
  - `config/config.yaml` 기본 활성 전략 목록은 이번 task에서도 보수적으로 유지한다.
- 해결된 blocker / 구현 결과:
  - `execution.auto_trader._default_strategy_builders()`에 `factor_investing -> FactorInvestingStrategy(settings.strategies.factor_investing, data_provider=provider)`를 추가했다.
  - injected builder 없이 canonical settings 경로에서 factor strategy instance가 생성되도록 열었다.
  - factor strategy도 기존 `signal_resolver -> risk_manager -> position_sizer -> order_manager` 경로를 그대로 사용하도록 유지했다.
  - 동일 `ticker + strategy` 재진입 금지 규칙이 factor strategy에도 그대로 적용되는지 default builder 기준 테스트로 고정했다.
- 테스트 반영:
  - `tests/test_execution/test_auto_trader.py`
    - default builder + factor loader present -> `run_cycle()` 성공
    - default builder + existing factor position -> reentry blocked
    - default builder + factor loader present -> `execute_cycle()` signal/order persistence 및 broker submission
- 비목표:
  - factor input source 구현 자체
  - `main.py` bootstrap에서 factor loader를 실제로 연결하는 일
  - `config/config.yaml` 기본 활성 전략 변경

#### Task 2.3 bootstrap wiring 추가

- 상태:
  - done
  - canonical bootstrap이 factor input loader default builder entrypoint를 통해 loader를 resolve하도록 반영했다.
  - explicit `factor_input_loader` 주입은 계속 우선권을 갖고, 기본 builder가 source를 만들지 못하면 `None` fallback으로 runtime 기동을 유지한다.
  - 현재 저장소에는 concrete factor input source가 아직 없어 default builder는 `None`을 반환하고, 기본 runtime은 계속 `factor_input_unavailable` 진단을 남긴다.
- 목표:
  - main bootstrap에서 factor input loader를 주입한다.
- 대상:
  - `main.py`
  - 필요 시 `data/collector.py`
  - `tests/test_execution/test_main_wiring.py`
- 완료 기준:
  - loader가 있으면 factor strategy가 실행 가능 상태가 된다.
  - loader가 없어도 runtime 기동은 유지된다.

세부 계획:

- 범위 경계:
  - 이번 task는 bootstrap이 "어떤 factor input loader를 기본으로 조립할지"를 고정하는 작업이다.
  - `KRStrategyDataProvider`의 정규화 계약, unavailable skip semantics, settings/default builder 확장은 이미 끝난 상태로 전제한다.
  - factor score를 실제로 계산하거나 외부 신규 서비스를 붙이는 일은 이번 task 범위에 포함하지 않는다.
  - strategy별 cron 분리와 runtime job 분리는 계속 `Task 3` 범위로 남긴다.
- 현재 blocker / mismatch:
  - `main.build_strategy_cycle_runner()`는 이미 optional `factor_input_loader` hook을 받지만, `main.main()`은 아직 기본 loader를 조립해 넘기지 않는다.
  - `data.collector.py`에는 universe loader와 price history loader builder는 있지만, factor input loader builder는 아직 없다.
  - 현재 `tests/test_execution/test_main_wiring.py`는 "외부에서 넘긴 optional hook이 전달되는지"까지만 검증하고, bootstrap이 기본 loader를 어떻게 선택하는지는 고정하지 않는다.
  - 그 결과 canonical runtime 경로에서는 `factor_investing`이 계속 `factor_input_unavailable`로만 남고, 실제 VTS smoke 준비를 진행할 수 없다.
- 고정할 bootstrap 계약:
  - bootstrap은 factor input source를 사용할 수 있으면 `FactorInputLoader` callable을 1회 조립해 `build_strategy_cycle_runner()`에 전달한다.
  - source가 준비되지 않았거나 구성할 수 없으면 `None`을 넘겨 runtime 기동을 유지한다.
  - loader 부재는 intentional skip으로 남기고, bootstrap 단계에서 runtime 시작 자체를 막지 않는다.
  - loader 조립은 startup 시점에만 수행하고, cycle마다 builder를 다시 만들지 않는다.
  - loader 내부의 payload 오류나 실행 오류는 계속 `Task 1.2` 계약대로 hard failure로 남겨야 한다.
- 구현 단계:
  - `data/collector.py`에 bootstrap이 재사용할 수 있는 factor input loader builder를 추가한다.
  - builder 명칭은 기존 패턴에 맞춰 `build_default_kr_factor_input_loader(...)` 계열로 두는 안이 가장 자연스럽다.
  - builder는 source를 만들 수 없으면 `None` 또는 no-op callable이 아니라 명시적 `None`을 반환해 bootstrap이 unavailable 상태를 그대로 표현하게 한다.
  - `main.py`는 price history loader를 조립하는 위치와 같은 계층에서 factor input loader도 함께 조립한다.
  - `main.main()`이 `build_strategy_cycle_runner()`를 호출할 때 기본 factor loader를 전달하도록 바꾼다.
  - 이미 외부에서 `factor_input_loader`를 직접 주입하는 테스트/확장 경로가 있으므로, 그 표면은 유지하고 기본 wiring만 추가한다.
  - 인증 객체가 없어도 동작해야 하는 현재 bootstrap 계약을 깨지 않도록, factor loader 조립은 token/api client 부재와 분리해서 판단한다.
- source 선택 원칙:
  - 1차 범위는 `KR only`다.
  - loader source는 로컬/저장소 내부에서 조립 가능한 입력 또는 기존 의존성으로 접근 가능한 입력만 사용한다.
  - 새로운 네트워크 서비스나 별도 장기 저장 스키마를 이번 task에 묶지 않는다.
  - source 미구현 상태라면 bootstrap builder는 `None` fallback을 명시적으로 유지하고, 이후 source 구현 task와 분리한다.
- 테스트 계획:
  - `tests/test_execution/test_main_wiring.py`
    - bootstrap이 기본 factor loader builder를 호출하고 그 결과를 `KRStrategyDataProvider`에 전달하는지 검증한다.
    - 기본 builder가 `None`을 반환해도 runner가 정상 생성되는지 검증한다.
    - 이미 있는 explicit `factor_input_loader=` override가 기본 builder보다 우선하는지 검증한다.
  - 필요 시 `tests/test_execution/test_auto_trader.py`
    - canonical main wiring을 통해 만들어진 runner에서 factor loader present / absent 경로가 기존 diagnostics 계약과 충돌하지 않는지 최소 회귀를 확인한다.
  - broader regression:
    - `tests/test_execution`
    - 필요 시 `tests/test_strategy tests\test_execution -q`
- 완료 기준 구체화:
  - bootstrap default path에서 factor loader를 만들 수 있으면 `factor_investing`이 `factor_input_available=True` 상태로 실행된다.
  - source가 없으면 기존처럼 runtime은 뜨고 factor strategy만 `factor_input_unavailable`로 남는다.
  - 기존 explicit injection hook과 access token 전달 계약은 그대로 유지된다.
  - VTS smoke 전에 "bootstrap은 연결돼 있지만 source 준비 여부에 따라 available/unavailable이 갈린다"는 상태가 테스트로 고정된다.
- 비목표:
  - factor score 계산 로직 구현
  - factor input 저장 스키마 추가
  - `config/config.yaml` 기본 활성 전략 목록 변경
  - strategy별 job 분리
- 구현 결과:
  - `data.collector.build_default_kr_factor_input_loader()`를 추가해 canonical bootstrap entrypoint를 고정했다.
  - `main._resolve_factor_input_loader()`가 explicit loader 우선, 없으면 default builder 호출 규칙을 담당하도록 반영했다.
  - `main.build_strategy_cycle_runner()`는 resolved factor loader를 `KRStrategyDataProvider`에 전달하도록 변경했다.
  - 현재 default builder는 in-repo source 부재를 반영해 `None`을 반환하며, 이로 인해 기존 intentional skip semantics를 그대로 유지한다.
- 검증 결과:
  - `tests/test_execution/test_main_wiring.py`
    - default factor loader builder 사용 경로
    - default builder `None` fallback 경로
    - explicit factor loader override 우선순위
  - broader regression:
    - `python -m pytest tests\test_execution -q`
    - 필요 시 `python -m pytest tests\test_strategy tests\test_execution -q`

### Task 3. 전략별 스케줄 분리

#### Task 3.1 전략 subset 실행 계약 추가

- 상태:
  - done
  - `AutoTrader.run_cycle()`와 `execute_cycle()`가 optional `strategies` subset 인자를 지원한다.
  - `configured_strategies`, `strategy_diagnostics`, selected position scope, exit evaluation scope가 모두 동일 subset 기준으로 동작하도록 반영했다.
  - runtime/main wiring은 아직 기존 2-인자 runner 계약을 유지하고, subset 전달은 후속 task에서 연결한다.
- 목표:
  - 특정 전략만 실행할 수 있는 AutoTrader 표면을 추가한다.
- 대상:
  - `execution/auto_trader.py`
- 완료 기준:
  - 예: `execute_cycle(market, as_of, strategies=[...])`
  - trend job이 factor strategy를 호출하지 않는다.

세부 계획:

- 범위 경계:
  - 이번 task는 `AutoTrader`가 "설정된 전체 전략" 대신 "호출 시 지정한 subset"만 실행할 수 있게 만드는 계약 작업이다.
  - scheduler job 분리와 job 등록 변경은 `Task 3.3`에서 다룬다.
  - 전략별 cron 설정 추가는 `Task 3.2` 범위다.
  - 따라서 이번 task만으로 runtime job 수가 늘어나지는 않는다.
- 현재 blocker / mismatch:
  - `AutoTrader.run_cycle()`와 `execute_cycle()`는 현재 항상 `settings.auto_trading.strategies` 전체를 순회한다.
  - `AutoTradeCycleResult.configured_strategies`도 항상 전체 설정 전략을 담아, subset job 결과를 구분할 수 없다.
  - `strategy_diagnostics` 역시 전체 전략 기준으로 채워져, 향후 trend 전용 job이 factor skip을 함께 남기게 된다.
  - `main.build_strategy_cycle_runner()`와 `TradingRuntime.strategy_cycle_runner`는 아직 `(market, as_of)` 2-인자 계약이라 subset 전달 경로는 후속 task에서 이어 받아야 한다.
- 고정할 실행 계약:
  - `run_cycle(market, as_of, *, strategies: list[str] | None = None)`와 `execute_cycle(..., strategies: list[str] | None = None)` 형태로 subset 표면을 추가한다.
  - `strategies is None`이면 기존과 동일하게 `settings.auto_trading.strategies` 전체를 사용해 backward compatibility를 유지한다.
  - subset이 주어지면 signal generation, diagnostics, strategy instance build, exit evaluation, result metadata 모두 그 subset 기준으로만 동작해야 한다.
  - subset에 없는 전략의 skip reason이나 diagnostics는 결과에 섞이지 않아야 한다.
  - unsupported strategy name, duplicate strategy name, empty strategy list 처리 규칙은 명시적으로 고정해야 한다.
- 구현 단계:
  - `execution/auto_trader.py`에 전략 이름 목록을 정규화하는 helper를 추가한다.
  - helper는 `None -> settings.auto_trading.strategies`, duplicate 제거, unsupported strategy 검증, empty subset reject를 담당하는 쪽이 가장 안전하다.
  - `run_cycle()`는 정규화된 subset만 기준으로 strategy instance를 만들고 diagnostics를 기록하도록 바꾼다.
  - `AutoTradeCycleResult.configured_strategies`는 실제 실행 대상 subset을 담도록 바꾼다.
  - `positions`, `position_tickers`, `tickers_for_context`는 subset 전략 포지션만 기준으로 좁히는 안이 우선이다.
  - `_build_exit_signals()`도 subset 전략 포지션에 대해서만 exit evaluation을 수행하도록 고정한다.
  - `execute_cycle()`는 같은 subset을 `run_cycle()`에 그대로 전달해 submit path까지 동일 범위를 유지한다.
  - helper/메서드 시그니처 추가 외의 runtime/main wiring 변경은 이번 task에 묶지 않는다.
- 검토할 정책 포인트:
  - 현재 open-order 차단은 ticker 단위라 subset 전략과 무관하게 동작한다.
  - 이 규칙을 그대로 둘지, 전략별 job 분리 이후 `ticker + strategy` 단위로 좁힐지는 별도 판단이 필요하다.
  - 이번 task 기본안은 기존 동작을 유지하고, 필요 시 문서에 open question으로만 남긴다.
- 테스트 계획:
  - `tests/test_execution/test_auto_trader.py`
    - `strategies=["trend_following"]` 호출 시 dual/factor diagnostics가 결과에 포함되지 않는지 검증
    - factor loader가 없는 상태에서도 `strategies=["trend_following"]` 호출은 factor skip을 남기지 않는지 검증
    - `strategies=["factor_investing"]` 호출 시 factor strategy만 실행되고 canonical diagnostics가 유지되는지 검증
    - existing factor position이 있어도 `strategies=["trend_following"]` 호출이 factor exit evaluation을 하지 않는지 검증
    - duplicate/unsupported/empty subset 인자 처리 규칙 검증
  - 필요 시 `tests/test_execution/test_runtime.py`
    - 후속 task 전까지 runtime은 기존 2-인자 runner를 유지하므로, 기존 runtime 테스트가 깨지지 않는지 최소 회귀만 확인한다.
- 완료 기준 구체화:
  - subset 미지정 호출은 기존과 동일한 결과를 낸다.
  - subset 지정 호출은 해당 전략만 signal generation / diagnostics / exit evaluation / order candidate 생성에 참여한다.
  - `configured_strategies`와 `strategy_diagnostics`가 실제 subset과 일치한다.
  - trend 전용 호출이 factor 전략을 건드리지 않고, factor 전용 호출이 trend/dual 전략을 건드리지 않는다.
- 비목표:
  - runtime job registration 변경
  - strategy별 cron 설정 추가
  - `main.py` runner 시그니처 확장
  - open-order 차단 정책 변경
- 구현 결과:
  - `execution.auto_trader.AutoTrader.run_cycle()`에 `strategies: list[str] | None = None`를 추가했다.
  - `execution.auto_trader.AutoTrader.execute_cycle()`도 같은 subset 인자를 받아 submit path까지 동일 범위를 유지하도록 반영했다.
  - `AutoTrader` 내부에 strategy subset 정규화 helper를 추가해 `None -> configured strategies`, duplicate 제거, empty subset reject, settings 외 전략 reject 규칙을 고정했다.
  - strategy instance build, diagnostics, `configured_strategies`, selected position lookup, exit evaluation이 모두 subset 기준으로만 동작하게 반영했다.
  - `open_order_exists` 차단은 기존처럼 ticker 단위 정책을 유지했다.
- 검증 결과:
  - `tests/test_execution/test_auto_trader.py`
    - trend-only subset이 factor/dual diagnostics를 섞지 않는지
    - subset 기준 position scope / exit scope가 좁혀지는지
    - duplicate subset dedupe, empty subset reject, settings 외 전략 reject
    - `execute_cycle()`가 requested subset만 submit path에 반영하는지
  - broader regression:
    - `python -m pytest tests\test_execution\test_auto_trader.py -q`
    - `python -m pytest tests\test_strategy tests\test_execution -q`

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

`Task 3.2`, 즉 전략별 KR cron 설정을 추가하는 작업이다.

이유:

- `Task 3.1`로 `AutoTrader` subset 실행 계약이 생겨 runtime이 전략별 job을 받을 준비는 됐다.
- 하지만 현재 settings는 여전히 단일 `auto_trading.kr.schedule_cron`만 가지므로, 전략별 job을 등록할 수 있는 설정 표면이 없다.
- 따라서 다음 직접 blocker는 전략별 KR cron 설정 추가다.
