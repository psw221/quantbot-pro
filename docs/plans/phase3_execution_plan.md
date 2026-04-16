# Phase 3 Execution Plan

## 목적

이 문서는 QuantBot Pro Phase 3 작업의 실행 기준과 진행 상태를 관리하기 위한 작업 문서입니다.

- 제품/운영 정책 기준: `docs/PRD_v1.4.md`
- 저장 구조/원장/정합성 기준: `docs/DB_SCHEMA_v1.2.md`
- 저장소 구현 규칙 기준: `AGENTS.md`
- 선행 구현/상태 기준: `docs/plans/phase2_execution_plan.md`

Phase 3의 우선순위는 주문 실행 코어 재설계가 아니라, Phase 2에서 고정된 주문 상태 전이, polling, reconciliation, writer queue, settlement FX 추적 구조를 유지한 채 운영 계층을 완성하는 것입니다.

## 현재 상태 요약

- 상태: `done`
- 기준 브랜치 가정: `master`
- 구현 원칙:
  - WAL + single writer queue 유지
  - canonical 주문 상태 / reconciliation 상태 유지
  - `trade_fx_rate`, `settlement_date`, `settlement_fx_rate`, `fx_rate_source` 유지
  - 운영 계층 완성 우선
  - 작은 작업 단위 우선
  - 신규 의존성 추가 금지

## 문서 해석 요약

- Phase 2에서 이미 고정된 실행 코어는 유지합니다.
  - 주문 상태: `pending`, `validated`, `submitted`, `partially_filled`, `filled`, `cancel_pending`, `cancelled`, `rejected`, `reconcile_hold`, `failed`
  - reconciliation 상태: `idle`, `scheduled_polling`, `mismatch_detected`, `reconciling`, `reconciled`, `failed`
- Phase 3 범위는 `monitor/*`, `tax/tax_calculator.py`, `scripts/restore_portfolio.py`, `backtest/backtest_runner.py` 중심의 운영 기능입니다.
- PRD 로드맵 표의 “Phase 3 = 주문 실행, 부분체결 처리”는 현재 저장소 상태와 다릅니다.
  - 현재 저장소에서는 주문 실행, 부분체결, polling, reconciliation이 이미 구현되어 있습니다.
  - 따라서 본 계획에서는 Phase 3를 “운영 가시성, 알림, 세금 추산, DR 복구 지원, 백테스트 실행/저장 정교화”로 해석합니다.

## 현재 확인된 충돌 및 선결정 사항

### 충돌

1. 초기 계획 시점에는 `docs/PRD_v1.4.md`의 Phase 3 설명과 현재 코드 상태가 달랐습니다.
   - PRD는 Phase 3에 주문 실행/부분체결 처리를 포함했습니다.
   - 현재 저장소에서는 해당 범위가 이미 Phase 2/후속 작업으로 구현되어 있었고, 이번 문서 동기화에서 운영 기능 중심 정의로 정리했습니다.

### 선결정

- `monitor/healthcheck.py`를 external canonical health 판단 계층으로 둡니다.
- notifier는 판단 로직이 아니라 송신 로직만 담당합니다.
- `restore_portfolio.py --apply`는 Phase 3에서 자동 원장 보정이 아니라, reconciliation 기록과 복구 판단 지원까지로 제한합니다.
- `backtest_runner.py`는 단일 실행 + 결과 저장까지만 다루고, walk-forward/최적화는 범위 밖으로 둡니다.
- `tax_calculator.py`는 세금 추산 및 리포트까지만 다루고, 신고 자동화는 범위 밖으로 둡니다.

## 현재 기준 Phase 3 범위

### 포함

- 모니터링/헬스체크 설계 및 정합성 보강
- 텔레그램 알림 설계 및 이벤트 연계
- 세금 추산 모듈 설계 및 정합성 보강
- restore_portfolio 복구 흐름 정교화
- backtest runner 정합성 보강
- 운영 상태 요약 지표 설계
- 테스트 전략과 검증 순서
- 문서 동기화

### 제외

- 주문 실행 코어의 전면 재설계
- polling/reconciliation 상태 머신 재정의
- 신규 외부 서비스 도입
- 대규모 UI 확장
- 실전 배포 자동화
- 범위가 큰 아키텍처 리팩터링
- 세무 신고 자동화
- 강한 auto-healing 성격의 포지션 강제 보정

## 모듈 책임 정의

### `monitor/healthcheck.py`

- `TradingRuntime.health_snapshot()`를 입력으로 external health를 계산합니다.
- `normal`, `warning`, `critical` 판정만 담당합니다.
- DB write를 수행하지 않습니다.

### `monitor/dashboard.py`

- runtime 상태와 DB read-model을 조회해 dashboard snapshot을 조합합니다.
- 조회 대상:
  - open orders
  - recent trades
  - latest portfolio snapshot
  - reconciliation summary
  - recent system logs
  - health summary

### `monitor/telegram_bot.py`

- 운영 이벤트 메시지 포맷과 송신만 담당합니다.
- 상태 판단은 상위 orchestration에서 수행합니다.

### `monitor/operations.py`

- `system_logs`, `portfolio_snapshots`의 writer queue 경유 write facade입니다.

### `tax/tax_calculator.py`

- canonical source:
  - `tax_events`
  - `trades`
  - `position_lots`
- 연도별 summary와 trade-level report를 생성합니다.
- 기본적으로 read/report 계층으로 유지합니다.

### `scripts/restore_portfolio.py`

- broker snapshot file 로드
- internal vs broker diff preview
- apply 시 reconciliation 기록, broker snapshot 저장, system log 기록
- 직접 주문/체결 보정은 수행하지 않습니다.

### `backtest/backtest_runner.py`

- 전략 인터페이스와 `StrategyDataProvider`를 그대로 재사용합니다.
- `vectorbt` 우선, fallback 보조 엔진 구조를 유지합니다.
- 결과는 `backtest_results`에 저장합니다.

## 모니터링 및 알림 설계 요약

### healthcheck 입력

- `scheduler_running`
- `writer_queue.running`
- `writer_queue.degraded`
- `writer_queue.queue_depth`
- `last_token_refresh_at`
- `last_poll_success_at`
- `consecutive_poll_failures`
- `trading_blocked`
- `last_error`

### healthcheck 판정 규칙

- `critical`
  - `trading_blocked=True`
  - `writer_queue_degraded=True`
- `warning`
  - token stale
  - polling stale
  - 마지막 오류 존재
- `normal`
  - 위 조건 없음

### stale 기본값

- token: 24시간
- polling: 20분

### Telegram 이벤트 표면

- `token_refresh_failure`
- `trading_blocked`
- `reconcile_hold`
- `writer_queue_degraded`
- `polling_mismatch`
- `pre_close_cancel_failure`
- `dr_restore_started`
- `dr_restore_completed`
- `dr_restore_failed`
- `fx_alert`

### 반영 원칙

- notifier는 상위 계층이 이벤트를 확정한 뒤 호출합니다.
- `mismatch_detected`와 `trading_blocked`는 별도 이벤트로 추적 가능해야 합니다.
- `token_stale`, `polling_stale`는 우선 dashboard warning으로 반영하고 즉시 장애 알림과는 분리합니다.

## tax_calculator 설계 요약

### 현재 구현 기준

- US 매도는 `tax_events` 우선
- 누락 시 `trades` 기반 FIFO fallback
- KR 거래는 환율 `NULL` 허용
- FX 우선순위:
  - sell: `settlement_fx_rate -> trade_fx_rate`
  - buy: `buy_settlement_fx_rate -> buy_trade_fx_rate`

### Phase 3 정리 목표

- yearly summary / trade report의 출력 계약 고정
- US/KR 경로 모두 테스트로 고정
- `position_lots`는 fallback FIFO 정합성 교차검증 소스로 확장 검토

## restore_portfolio / reconciliation 연계 설계

### canonical 흐름

#### `--dry-run`

- snapshot 로드
- market filter 적용
- internal positions / open orders와 broker snapshot 차이 계산
- mismatch summary 출력
- DB write 없음

#### `--apply`

- `trading_blocked=True` 확인
- `manual_restore` 성격의 reconciliation run 기록
- `broker_positions` snapshot 저장
- `system_logs` 기록
- optional `portfolio_snapshot` payload가 있으면 `portfolio_snapshots` upsert
- direct fill insert / order state correction은 수행하지 않음

### 목표

- scheduled polling과 manual restore를 운영 관점에서 구분 가능하게 만듭니다.
- restore는 “보정 실행기”가 아니라 “복구 판단 및 기록 도구”로 둡니다.

## backtest_runner 설계 요약

### 현재 인터페이스 유지

- `StrategyDataProvider`
- 기존 전략 클래스
- 기존 settings 구조

### 목표

- 전략명/기간/입력 universe 검증
- `vectorbt -> fallback` 엔진 우선순위 유지
- 실행 결과와 저장 메타데이터 정합성 확보
- `backtest_results`와 `system_logs` 연계

### 범위 제한

- walk-forward 자동화 제외
- parameter optimization 제외
- 대규모 batch runner 제외

## 테스트 계획

### 모니터링

- healthcheck
  - normal
  - warning
  - critical
- dashboard snapshot
  - open orders
  - recent trades
  - reconciliation summary
  - latest portfolio snapshot
  - recent logs
- notifier
  - 이벤트별 메시지 포맷
  - disabled/no credentials no-op

### tax

- US 매도 tax_event 경로
- US 매도 FIFO fallback 경로
- KR 매도 FX null 경로
- `settlement_fx_rate -> trade_fx_rate` 우선순위

### restore

- dry-run mismatch summary
- apply requires trading_blocked
- apply write 범위 검증
- `manual_restore` 구분 검증
- optional portfolio snapshot upsert

### backtest

- 전략별 최소 실행
- `backtest_results` 저장
- engine metadata 정합성
- invalid strategy/date/universe rejection

### 검증 순서

1. 새 테스트 파일만 먼저 실행
2. `tests/test_execution` 관련 묶음 실행
3. `python -m pytest tests\\ -q`
4. `python -m compileall core data execution strategy risk monitor tax backtest scripts tests main.py`

## 구현 작업 분할안

| ID | 작업 | 상태 | 완료 기준 | 검증 |
|---|---|---|---|---|
| P3-01 | Health/Monitoring Contract 정리 | done | external health 기준이 healthcheck/dashboard/test에서 동일 | healthcheck/dashboard 테스트 |
| P3-02 | Operations Recorder 보강 | done | `system_logs`, `portfolio_snapshots` write 계약과 민감정보 금지 규칙 고정 | recorder/dashboard 테스트 |
| P3-03 | Telegram 운영 이벤트 연계 | done | notifier 호출 지점과 이벤트 표면이 명시적 | notifier/orchestration 테스트 |
| P3-04 | Tax Calculator 정합성 보강 | done | US/KR summary/report와 FX precedence가 테스트로 고정 | `test_tax_calculator.py` |
| P3-05 | Restore Preview/Apply 정교화 | done | dry-run no-write, apply write 범위, manual restore 성격 고정 | restore 테스트 |
| P3-06 | Backtest Runner 정합성 수정 | done | engine metadata와 저장 결과가 실제 실행 경로와 일치 | backtest 테스트 |
| P3-07 | 운영 상태 요약 지표 마감 | done | blocked/mismatch/stale/restore/backtest 결과를 dashboard에서 추적 가능 | dashboard 회귀 테스트 |
| P3-08 | 문서 동기화 | done | PRD/DB_SCHEMA/plan의 Phase 3 정의 일치 | 문서 대조 |

## 작업 단위 상세

### P3-01 Health/Monitoring Contract 정리

- 범위:
  - healthcheck canonical 규칙 고정
  - runtime internal health와 external health 역할 분리
- 완료 기준:
  - `monitor/healthcheck.py`가 canonical external health로 문서/코드 합의
- 검증:
  - healthcheck 단위 테스트
  - dashboard snapshot 회귀 테스트
- 구현 메모:
  - `monitor/healthcheck.py`에 external canonical health 입력/판정 순수 함수(`evaluate_health_snapshot`)를 고정했습니다.
  - runtime internal `health_status`는 보조 상태로 두고, dashboard는 external canonical health snapshot을 소비하도록 정리했습니다.

### P3-02 Operations Recorder 보강

- 범위:
  - `system_logs`, `portfolio_snapshots` write facade 계약 정리
- 완료 기준:
  - 운영 write는 writer queue 경유 단일 경로 유지
- 검증:
  - recorder 단위 테스트
- 구현 메모:
  - `monitor/operations.py`에 `SystemLogPayload`, `PortfolioSnapshotPayload` 기반 입력 계약을 고정했습니다.
  - `system_logs.extra_json`은 recorder 레벨에서 민감 key를 제거하고, 중첩 payload는 `<omitted>` 처리하도록 정리했습니다.
  - `system_logs`, `portfolio_snapshots` write는 계속 writer queue 단일 경로로만 수행됩니다.

### P3-03 Telegram 운영 이벤트 연계

- 범위:
  - notifier 호출 위치와 이벤트 매핑 정리
- 완료 기준:
  - blocked/degraded/mismatch/token 실패 이벤트가 명시적 호출 경로를 가짐
- 검증:
  - notifier 테스트
  - 호출 지점 테스트
- 구현 메모:
  - `monitor/telegram_bot.py`에 `TelegramEvent`, `TelegramDispatchResult`를 추가해 notifier 입력 계약을 명시했습니다.
  - notifier는 이벤트 판단 없이 입력된 이벤트를 메시지 포맷/송신만 수행하고, disabled 또는 자격증명 미설정 시 no-op으로 동작합니다.
  - 민감 detail field는 메시지에서 제외하고, 송신 예외는 결과 객체로 흡수하도록 정리했습니다.

### P3-04 Tax Calculator 정합성 보강

- 범위:
  - FX precedence
  - FIFO fallback
  - report schema
- 완료 기준:
  - US/KR 경로 모두 테스트로 닫힘
- 검증:
  - `tests/test_execution/test_tax_calculator.py`
- 구현 메모:
  - `tax/tax_calculator.py`는 US 매도에서 `tax_events`를 우선 사용하고, 누락 시 `position_lots + source_trade`를 이용해 FIFO fallback을 수행하도록 정리했습니다.
  - sell FX는 `settlement_fx_rate -> trade_fx_rate`, buy FX는 `buy_settlement_fx_rate -> buy_trade_fx_rate` 또는 fallback lot의 `open_settlement_fx_rate -> open_trade_fx_rate` 순서를 따릅니다.
  - KR 거래는 기존대로 FX `NULL`을 허용하고, FIFO fallback 경로에서 KRW 금액을 그대로 유지합니다.

### P3-05 Restore Preview/Apply 정교화

- 범위:
  - `manual_restore` 성격 반영
  - dry-run no-write 보장
  - apply write 범위 고정
- 완료 기준:
  - restore가 DR runbook 입력 도구로 사용 가능
- 검증:
  - restore 단위 테스트
- 구현 메모:
  - `scripts/restore_portfolio.py`의 apply 경로는 `ReconciliationService.reconcile_snapshot(..., run_type="manual_restore")`를 사용하도록 정리했습니다.
  - dry-run은 mismatch preview만 수행하고 `reconciliation_runs`, `broker_positions`, `system_logs`, `portfolio_snapshots`에 write 하지 않도록 테스트로 고정했습니다.
  - apply는 `trading_blocked=True` 확인 후 `manual_restore` reconciliation 기록, broker snapshot 저장, 운영 로그 기록, optional portfolio snapshot upsert까지만 수행합니다.

### P3-06 Backtest Runner 정합성 수정

- 범위:
  - vectorbt/fallback metadata 정합성
  - 저장 결과 정합성
- 완료 기준:
  - 실제 엔진과 저장 메타데이터 일치
- 검증:
  - backtest runner 테스트
- 구현 메모:
  - `backtest/backtest_runner.py`는 `_run_engine()`이 반환한 실제 `engine` 값을 `BacktestRunResult`, `backtest_results.notes`, `system_logs.extra_json`에 그대로 반영하도록 정리했습니다.
  - 잘못된 기간뿐 아니라 빈 `universe` 입력도 명시적으로 거부하도록 테스트로 고정했습니다.

### P3-07 운영 상태 요약 지표 마감

- 범위:
  - dashboard read-model에 blocked/mismatch/stale/recent restore/recent backtest 요약 추가
- 완료 기준:
  - 운영자가 health/reconciliation/restore/backtest 상태를 한 번에 읽을 수 있음
- 검증:
  - dashboard 회귀 테스트
- 구현 메모:
  - `monitor/dashboard.py`는 기존 health/open orders/trades/snapshot/reconciliation/logs 외에 `recent_manual_restores`, `recent_backtests`, `operational_summary`를 함께 반환하도록 정리했습니다.
  - `operational_summary`는 external health 결과를 다시 판정하지 않고, canonical health/reconciliation read-model을 요약해 `trading_blocked`, `token_stale`, `poll_stale`, `writer_queue_degraded`, `has_recent_mismatch`, 최근 restore/backtest 상태를 노출합니다.

### P3-08 문서 동기화

- 범위:
  - PRD / DB_SCHEMA / plan 문서의 Phase 3 정의와 현재 저장소 구현 상태 정렬
- 완료 기준:
  - 문서상 Phase 3 설명이 운영 기능 중심 구현 상태와 일치
- 검증:
  - 문서 대조
- 구현 메모:
  - `docs/PRD_v1.4.md`의 Phase 3 로드맵을 운영 기능 중심으로 수정하고 monitoring / tax / DR 운영 계약을 현재 구현 표면에 맞게 정리했습니다.
  - `docs/DB_SCHEMA_v1.2.md`에는 `backtest_results.notes`, `system_logs.extra_json`의 현재 운영 메모 규칙을 반영했습니다.
  - `docs/plans/phase2_execution_plan.md`, `docs/plans/phase3_execution_plan.md`는 Phase 3 handoff와 완료 상태를 닫는 방향으로 정리했습니다.

## 문서 업데이트 필요 예상 항목

### `docs/PRD_v1.4.md`

- 완료
- Phase 3 로드맵 설명을 현재 저장소 상태에 맞게 조정했습니다.
- monitoring / DR / tax / backtest 운영 요구사항을 현재 공개 표면 기준으로 정리했습니다.

### `docs/DB_SCHEMA_v1.2.md`

- 완료
- `manual_restore` run type과 현재 `system_logs`/`backtest_results` 운영 메모 규칙을 문서에 맞췄습니다.

### `docs/plans/phase2_execution_plan.md`

- 완료
- Phase 2 이후 handoff가 Phase 3 문서 동기화까지 닫혔다는 메모를 보강했습니다.

### `AGENTS.md`

- 전역 규칙 변경이 없으면 수정 불필요
- 단, `monitor/operations.py` 책임을 저장소 규칙으로 승격할 필요가 있으면 반영 검토

## 추천 구현 시작 순서

1. `P3-05 Restore Preview/Apply 정교화`
2. `P3-04 Tax Calculator 정합성 보강`
3. `P3-06 Backtest Runner 정합성 수정`
4. `P3-07 운영 상태 요약 지표 마감`
5. `P3-08 문서 동기화`

## 가장 먼저 구현할 작업 1개

`P3-01 Health/Monitoring Contract 정리`

이유:

- 현재 가장 직접적인 문서/코드 충돌은 health 상태의 canonical 판단 위치가 runtime 내부와 monitor 외부에 이중으로 존재한다는 점입니다.
- 이 계약이 먼저 고정되어야 dashboard, telegram, restore, system log가 같은 상태 정의를 공유할 수 있습니다.
- 범위가 작고, 1~2시간 내 리뷰 가능한 선행 작업입니다.
