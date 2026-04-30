# Intraday Momentum Replacement Plan

## Summary

이 문서는 기존 월간 `dual_momentum` 전략을 데이트레이딩용 `intraday_momentum` 전략으로 교체하기 위한 실행 계획입니다.

목표 전략은 KOSPI200 종목을 기준으로 한 **Opening Range + VWAP Trend** 전략입니다. 기존 `dual_momentum` 이름과 월간 리밸런싱 동작은 제거하고, 자동매매 표면에는 새 전략명 `intraday_momentum`을 사용합니다.

핵심 결정:

- 전략명은 `dual_momentum`을 유지하지 않고 `intraday_momentum`으로 변경한다.
- 데이터 소스는 KIS 국내주식 분봉 API를 1차 소스로 사용한다.
- KOSPI200 전체를 매 cycle 조회하지 않고, 전일 거래대금 상위 50개와 현재 보유 종목만 후보로 삼는다.
- 포지션은 당일 청산을 원칙으로 한다.
- 초기 리스크는 보수적으로 시작한다.

## Current State

- `strategy/dual_momentum.py`는 일봉 기반 12개월 절대/상대 모멘텀 전략이다.
- `dual_momentum`은 `rebalance_day_of_month`가 아니면 신호를 생성하지 않는다.
- `config/config.yaml` 기준 `dual_momentum`은 매월 1일 09:00에 실행되도록 설정되어 있다.
- 현재 `PriceBar`에는 거래량 필드가 없고, 기존 `StrategyDataProvider.get_price_history()`는 일봉 중심 인터페이스다.
- `data.collector`와 `execution.kis_api`에는 국내 일봉 조회 경로만 있고, 분봉 조회 adapter는 아직 없다.
- DB의 `orders`, `trades`, `positions`는 `strategy`를 TEXT로 저장하므로 새 전략명 추가에 DB migration은 필요하지 않다.
- 기존 `dual_momentum` 과거 원장 데이터는 historical record로 유지한다.

## Strategy Specification

### 대상과 후보군

- 시장: `KR`
- 기본 universe: KOSPI200
- 실제 분봉 조회 후보:
  - 전일 거래대금 기준 KOSPI200 상위 50개
  - 현재 내부 포지션 보유 종목
- 비-KR 시장에서는 신호를 생성하지 않는다.

### 진입 조건

`intraday_momentum`은 09:30 이후부터 신규 진입을 평가한다.

매수 신호 조건:

- 09:00-09:30 opening range가 계산되어 있다.
- 현재가가 opening range high를 상향 돌파한다.
- 현재가가 당일 VWAP보다 높다.
- 최근 거래량 또는 누적 거래량이 opening range 평균 대비 증가했다.
- 해당 종목은 당일 `intraday_momentum`으로 이미 신규 진입한 적이 없다.
- 현재 `ticker + strategy` 포지션이 열려 있지 않다.

신호 metadata:

- `entry_reason: opening_range_vwap_breakout`
- `opening_range_high`
- `opening_range_low`
- `vwap`
- `latest_price`
- `volume_ratio`

### 청산 조건

청산 신호 조건:

- 현재가가 VWAP 아래로 하락
- 현재가가 opening range low 아래로 하락
- 손절 기준 도달
- trailing stop 기준 도달
- 강제 청산 시각 도달

기본 청산 정책:

- 15:10 이후 신규 진입 금지
- 15:15 이후 `intraday_momentum` 포지션은 전량 청산 신호 생성
- 보유 포지션을 다음 거래일로 넘기지 않는 것을 기본 정책으로 한다.

## Configuration Changes

`strategies.dual_momentum` 설정을 제거하고 `strategies.intraday_momentum`을 추가한다.

추천 기본값:

```yaml
strategies:
  intraday_momentum:
    opening_range_minutes: 30
    bar_interval_minutes: 1
    candidate_top_n_by_turnover: 50
    max_positions: 2
    max_entries_per_ticker_per_day: 1
    stop_loss_pct: -0.007
    take_profit_pct: 0.015
    trailing_stop_pct: -0.006
    no_entry_before_kst: "09:30"
    no_entry_after_kst: "15:10"
    force_exit_time_kst: "15:15"
```

자동매매 설정 추천값:

```yaml
auto_trading:
  strategies: [intraday_momentum, trend_following]
  kr:
    strategy_schedule_crons:
      intraday_momentum: "*/10 9-15 * * 1-5"
      trend_following: "*/15 9-15 * * 1-5"
      factor_investing: "5 9 1 1,4,7,10 *"
```

주의:

- `dual_momentum`이 `auto_trading.strategies`에 남아 있으면 설정 검증에서 실패하도록 한다.
- `strategy_weights`도 `dual_momentum` 대신 `intraday_momentum`으로 교체한다.

## Key Implementation Changes

### 1. Core model/settings

- `core.models.StrategyName`에 `intraday_momentum`을 추가하고 `dual_momentum`을 제거한다.
- 장중 OHLCV를 위한 `IntradayBar` dataclass를 추가한다.
- `core.settings`에 `IntradayMomentumSettings`를 추가한다.
- `SUPPORTED_AUTO_TRADING_STRATEGIES`를 `intraday_momentum`, `trend_following`, `factor_investing` 기준으로 갱신한다.
- `StrategyWeightsSettings`도 새 전략명 기준으로 변경한다.

### 2. KIS 분봉 adapter

- `execution.kis_api.KISApiClient`에 국내 분봉 조회 method를 추가한다.
- 사용할 endpoint:
  - path: `/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice`
  - TR: `FHKST03010200`
- raw payload parsing은 `kis_api.py` adapter 안에만 둔다.
- normalize 결과는 `IntradayBar` 호환 mapping으로 반환한다.
- raw payload, 인증 헤더, 민감정보는 로그/DB에 저장하지 않는다.

### 3. Data provider

- `StrategyDataProvider`에 `get_intraday_bars(tickers, market, as_of, lookback_minutes)`를 추가한다.
- `KRStrategyDataProvider`는 intraday loader를 주입받아 장중 bar를 제공한다.
- 같은 cycle 안에서 같은 종목을 반복 조회하지 않도록 cache key를 `ticker + market + as_of minute` 단위로 둔다.
- 기존 `get_price_history()`는 일봉 전략용으로 유지한다.

### 4. Universe/candidate loader

- KOSPI200 universe loader는 유지한다.
- 유동성 후보 loader를 추가한다.
- 1차 구현은 전일 일봉 데이터의 거래대금 또는 KIS quote data에서 계산 가능한 값을 사용한다.
- 유동성 후보 산출 실패 시 fallback은 KOSPI200 cache 상위 50개와 보유 종목으로 제한한다.

### 5. Strategy implementation

- `strategy/intraday_momentum.py`를 추가한다.
- `BaseStrategy`를 상속한다.
- opening range, VWAP, volume ratio 계산은 strategy 내부의 순수 함수로 분리한다.
- 데이터 부족, opening range 미완성, VWAP 계산 불가 시 빈 신호를 반환한다.
- 진입 신호와 청산 신호 모두 `strategy="intraday_momentum"`으로 생성한다.

### 6. Auto-trading wiring

- `execution.auto_trader._default_strategy_builders()`에서 `dual_momentum` builder를 제거하고 `intraday_momentum` builder를 추가한다.
- `main.build_strategy_cycle_runner()`에서 `KRStrategyDataProvider` 생성 시 intraday loader를 주입한다.
- existing position reentry block, open order block, risk manager, position sizer, order manager 경로는 기존 pipeline을 그대로 사용한다.

### 7. Documentation

- `docs/PRD_v1.4.md`의 전략 설명과 설정 예시를 갱신한다.
- `docs/plans/phase2_execution_plan.md` 또는 후속 구현 계획 문서에 `dual_momentum -> intraday_momentum` 교체 완료 이력을 남긴다.
- `README.md`의 전략/운영 설명을 새 전략명 기준으로 갱신한다.
- `docs/DB_SCHEMA_v1.2.md`는 schema 변경이 없으므로 업데이트하지 않는다. 단, 전략명 예시가 `dual_momentum`에 고정되어 있으면 예시만 갱신한다.

## Detailed Work Plan

### Task 1. 문서/설정 표면 고정

상태: `done` (`2026-04-30`)

완료 메모:

- `config/config.yaml` 활성 자동매매 전략, 전략 가중치, 전략별 cron을 `intraday_momentum` 기준으로 교체했다.
- `core.settings`에 `IntradayMomentumSettings`를 추가하고 `SUPPORTED_AUTO_TRADING_STRATEGIES`, `StrategyWeightsSettings`를 새 전략명 기준으로 갱신했다.
- `core.models.StrategyName`에 `intraday_momentum`을 반영하고 장중 OHLCV 계약용 `IntradayBar`를 추가했다.
- `docs/PRD_v1.4.md`, `README.md`에 활성 전략명과 기존 `dual_momentum` 원장 non-migration 정책을 반영했다.
- `docs/DB_SCHEMA_v1.2.md`는 strategy가 TEXT인 기존 schema와 일치하며 schema 변경이 없어 수정하지 않았다.

완료 기준:

- `config/config.yaml` 예시와 PRD의 전략 설정이 `intraday_momentum` 기준으로 정리된다.
- `dual_momentum`이 더 이상 활성 자동매매 전략으로 설명되지 않는다.

작업:

1. `IntradayMomentumSettings` 필드와 기본값을 확정한다.
2. `strategy_weights`에서 `dual_momentum`을 `intraday_momentum`으로 교체한다.
3. 자동매매 cron 기본값을 확정한다.
4. 문서에 기존 `dual_momentum` 과거 원장은 migration하지 않는다고 명시한다.

검증:

- `python scripts/validate_config.py`
- 설정 모델 단위 테스트

### Task 2. 장중 데이터 모델과 provider 계약 추가

상태: `done` (`2026-04-30`)

완료 메모:

- `StrategyDataProvider` protocol에 `get_intraday_bars(tickers, market, as_of, lookback_minutes)`를 추가했다.
- `KRStrategyDataProvider`에 `intraday_bar_loader` 주입 표면과 장중 OHLCV 정규화/필터링/cache 경로를 추가했다.
- intraday cache key는 `ticker + market + as_of minute` 단위로 두고, 같은 minute 안의 짧은 lookback 요청은 기존 loader 결과를 재사용한다.
- loader 미주입, 비-KR 시장, 빈 ticker, 잘못된 lookback은 빈 결과를 반환한다.
- 테스트용 fake provider는 기본적으로 빈 intraday 결과를 반환하도록 갱신했다.

완료 기준:

- 전략이 OHLCV 분봉 데이터를 typed interface로 받을 수 있다.

작업:

1. `IntradayBar` 모델을 추가한다.
2. `StrategyDataProvider` protocol에 `get_intraday_bars()`를 추가한다.
3. 테스트용 fake provider를 갱신한다.
4. 기존 전략 테스트가 깨지지 않도록 기본 fake provider에 빈 intraday 반환을 제공한다.

검증:

- `python -m pytest tests\test_strategy -q`
- `python -m pytest tests\test_execution\test_strategy_data_provider.py -q`

### Task 3. KIS 분봉 조회 adapter 구현

상태: `done` (`2026-04-30`)

완료 메모:

- `KISApiClient.get_intraday_price_history()`를 추가해 국내 분봉 조회 endpoint `/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice`를 호출한다.
- 국내 분봉 TR ID는 `FHKST03010200`으로 고정했다.
- `normalize_intraday_price_history()`를 추가해 KIS raw row를 `IntradayBar` compatible mapping으로 정규화한다.
- KIS 국내 date/time field는 KST로 해석한 뒤 UTC `datetime`으로 변환한다.
- raw payload parsing은 `execution/kis_api.py` adapter 내부에만 둔다.

완료 기준:

- KIS 국내 분봉 raw 응답을 `IntradayBar` compatible rows로 정규화할 수 있다.

작업:

1. `get_intraday_price_history()`를 추가한다.
2. `normalize_intraday_price_history()`를 추가한다.
3. VTS/PROD TR ID 차이가 있으면 adapter 내부에서 환경별로 흡수한다.
4. 응답 field 후보를 테스트 fixture로 고정한다.
5. 실패 시 예외를 상위로 raw하게 노출하지 않고 기존 broker API error handling 패턴을 따른다.

검증:

- KIS client contract test
- normalize fixture test

### Task 4. 후보군 축소 loader 구현

상태: `done` (`2026-04-30`)

완료 메모:

- `data.collector.build_kr_intraday_candidate_loader()`를 추가해 KOSPI200 universe 중 거래대금 상위 N개와 현재 KR 보유 종목을 후보군으로 반환한다.
- `rank_tickers_by_turnover()`를 추가해 거래대금 내림차순, 기존 universe 순서 tie-break로 안정적인 ranking을 제공한다.
- `build_pykrx_kr_previous_turnover_loader()`를 추가해 pykrx가 있을 때 최근 가용 거래일의 `거래대금`을 ranking input으로 사용한다.
- 거래대금 데이터가 없거나 loader가 실패하면 universe/cache 순서의 상위 N개와 보유 종목으로 fallback한다.
- 후보군 loader는 아직 strategy/runtime에 강제 연결하지 않고, `intraday_momentum` 전략 연결 단계에서 주입하도록 남긴다.

완료 기준:

- KOSPI200 전체 중 유동성 상위 50개와 보유 종목만 intraday 조회 대상으로 선택된다.

작업:

1. KOSPI200 universe loader 재사용.
2. 전일 거래대금 기준 ranking helper 추가.
3. ranking data가 없으면 cache 순서 기반 상위 50개로 fallback.
4. 보유 종목은 항상 후보군에 포함.
5. 중복 ticker 제거와 순서 안정성 보장.

검증:

- ranking 가능/불가 fallback 테스트
- 보유 종목 포함 테스트

### Task 5. `IntradayMomentumStrategy` 구현

상태: `done` (`2026-04-30`)

완료 메모:

- `strategy.intraday_momentum.IntradayMomentumStrategy`를 추가해 `BaseStrategy` 기반의 KR 전용 Opening Range + VWAP breakout 전략을 구현했다.
- opening range, VWAP, volume ratio 계산을 strategy-local 순수 함수로 분리했다.
- 09:30 이전과 15:10 이후 신규 진입을 차단하고, 15:15 이후 보유 포지션 강제 청산 신호를 생성한다.
- 장중 stop-loss/trailing stop은 `IntradayMomentumSettings`의 보수적 전략별 기준을 사용한다.
- 당일 종목별 진입 횟수 제한은 `entry_history_loader(ticker, trading_day)` 주입 표면으로 반영했으며, 실제 DB read wiring은 Task 6/7에서 연결한다.
- 데이터 부족, opening range 미완성, VWAP 계산 불가, 거래량 증가 미충족 시 신호를 생성하지 않는다.

완료 기준:

- Opening Range + VWAP 조건에 따라 buy/sell signal을 생성한다.

작업:

1. 09:00-09:30 opening range 계산 함수 추가.
2. VWAP 계산 함수 추가.
3. volume ratio 계산 함수 추가.
4. 신규 진입 시간 제한 적용.
5. 당일 종목별 1회 진입 제한 metadata/DB read path 설계 반영.
6. force-exit 시간 도달 시 exit signal 생성.
7. stop-loss/trailing stop은 기존 `ExitManager` 또는 strategy-local helper 중 더 작은 변경으로 구현한다.

검증:

- opening range high 돌파 buy 테스트
- VWAP 미충족 시 no signal 테스트
- volume 부족 시 no signal 테스트
- 09:30 전 no entry 테스트
- 15:10 이후 no new entry 테스트
- 15:15 force exit 테스트
- stop-loss/trailing stop exit 테스트

### Task 6. AutoTrader/runtime 연결

완료 기준:

- scheduled auto-trading cycle에서 `intraday_momentum`만 선택 실행할 수 있다.

작업:

1. 기본 strategy builder 갱신.
2. `KR_STRATEGY_CYCLE_JOB_IDS`에 `intraday_momentum` job id 추가.
3. `dual_momentum` job id와 설정 허용 경로 제거.
4. strategy diagnostics에 새 전략명이 표시되는지 확인.
5. 기존 order/risk/sizing pipeline 재사용.

검증:

- runtime job 등록 테스트
- AutoTrader cycle 테스트
- dashboard diagnostics 테스트

### Task 7. 안전장치와 운영 검증

완료 기준:

- 데이트레이딩 전략이 과도한 주문을 내지 않도록 최소 안전장치가 있다.

작업:

1. 종목당 하루 1회 신규 진입 제한.
2. 전략 전체 최대 동시 포지션 2개 제한.
3. 장 마감 전 강제 청산 신호.
4. 신규 진입 차단 시간 반영.
5. `max_orders_per_cycle`과 함께 동작하는지 확인.
6. mismatch/reconcile hold 상태에서는 기존 runtime skip 정책을 그대로 따른다.

검증:

- 당일 재진입 차단 테스트
- max positions 제한 테스트
- reconcile hold/trading blocked skip 회귀 테스트

### Task 8. 최종 문서와 전체 검증

완료 기준:

- 코드, 설정, 문서가 같은 전략명과 운영 정책을 설명한다.

작업:

1. PRD 업데이트.
2. README 업데이트.
3. phase plan 또는 본 문서에 완료 기록 추가.
4. schema 변경 없음 사유 기록.

검증 명령:

```powershell
python -m compileall core data execution strategy risk monitor tests main.py
python -m pytest tests\test_strategy tests\test_execution\test_strategy_data_provider.py tests\test_execution\test_auto_trader.py -q
python -m pytest tests\ -q
python scripts\validate_config.py
```

## Test Scenarios

- 09:29 KST: opening range 미완성으로 신규 진입 없음.
- 09:35 KST: opening range high 돌파, 현재가 > VWAP, volume 증가 시 buy.
- 09:35 KST: high 돌파했지만 현재가 <= VWAP이면 no signal.
- 10:00 KST: 이미 당일 진입한 종목이면 재진입 차단.
- 13:00 KST: 보유 포지션이 VWAP 아래로 이탈하면 sell.
- 14:00 KST: stop-loss 또는 trailing stop 조건이면 sell.
- 15:11 KST: 신규 진입 신호 없음.
- 15:15 KST: 남은 `intraday_momentum` 포지션 전량 sell.
- KIS 분봉 payload 빈 응답: no signal, cycle failure 아님.
- 분봉 API 일부 종목 실패: 실패 종목 제외, 나머지 종목으로 진행.

## Risks and Mitigations

- API 호출량 증가
  - KOSPI200 전체가 아니라 유동성 상위 50개만 조회한다.
- 장중 데이터 품질 부족
  - opening range/VWAP 계산 불가 시 신호를 생성하지 않는다.
- 과도한 회전율
  - 종목당 하루 1회 진입, 최대 2포지션, 15:10 이후 신규 진입 금지.
- 기존 `dual_momentum` 원장과 새 전략명 혼재
  - historical record로 유지하고 migration하지 않는다.
- 휴장일 처리 미흡
  - runtime market session logic 개선은 별도 작업으로 분리하되, 이 전략은 KIS payload가 비거나 주문 실패할 경우 신호/주문이 진행되지 않도록 방어한다.

## Documentation Update Rules

구현 시 반드시 함께 업데이트할 문서:

- `docs/PRD_v1.4.md`
- `docs/plans/phase2_execution_plan.md` 또는 본 문서의 진행 기록
- `README.md`

조건부 업데이트:

- `docs/DB_SCHEMA_v1.2.md`
  - DB schema 변경은 없으므로 원칙적으로 업데이트하지 않는다.
  - 단, 문서 내 전략명 예시가 `dual_momentum`에 고정되어 혼동을 만들면 예시만 갱신한다.
