# Layer 5 Remaining Work Plan

## Title
Layer 5 모니터링 및 DR 잔여 작업 계획

## Status
- state: done
- default_env: vts
- scope_focus: layer5_user_surface

## Summary
- Layer 5의 운영 백엔드(`healthcheck`, `telegram`, `restore`, `tax calculator`, `dashboard snapshot builder`, `backtest result persistence`)는 대부분 구현되었다.
- 남은 일은 사용자-facing 표면과 운영 연결 마감이다.
- 우선순위는 `대시보드 UI`, `세후 성과 리포트 출력`, `DR/운영 알림 연결`, `운영 지표 노출 강화` 순서로 잡는다.
- 완료 기준은 운영자가 코드/DB 직접 조회 없이 Layer 5 정보를 화면, 리포트, 알림으로 확인할 수 있는 상태다.

## Current State
- `monitor/dashboard.py`는 read-only snapshot builder로 유지되며 `auto_trading_diagnostics`, `strategy_budget_summary`, `tax_summary`까지 snapshot에 포함한다.
- `monitor/telegram_bot.py`는 notifier 구현이 완료되었고 runtime, restore, reconcile hold 이벤트가 실제로 연결돼 있다.
- `monitor/healthcheck.py`는 external canonical health(`normal`, `warning`, `critical`)를 제공한다.
- `tax/tax_calculator.py`는 `calculate_yearly_summary()`와 `build_trade_report()`를 제공하고, `tax/report_export.py`가 연간/월간 출력 계층을 담당한다.
- `scripts/restore_portfolio.py`는 `dry-run`/`apply`, `manual_restore`, reconciliation 기록과 DR 관련 Telegram 자동 연계까지 구현됐다.
- `backtest/backtest_runner.py`는 결과 저장과 system log 기록이 가능하고 dashboard snapshot에서 recent backtests를 읽을 수 있다.
- 따라서 Layer 5는 운영 엔진과 사용자-facing 표면이 현재 계획 범위 기준으로 모두 구현된 상태다.

## Work Breakdown
| ID | Task | Status | Done Criteria |
| --- | --- | --- | --- |
| L5-01 | Dashboard App Skeleton | done | Streamlit 앱에서 health, open orders, recent trades, reconciliation, logs 섹션이 보인다 |
| L5-02 | Operations Summary Panel | done | `operational_summary`가 카드형으로 렌더링된다 |
| L5-03 | Restore/Backtest Panels | done | recent manual restores / recent backtests를 UI에서 볼 수 있다 |
| L5-04 | Auto-Trading Diagnostics Panel | done | 최근 cycle의 signals/candidates/rejections를 UI에서 볼 수 있다 |
| L5-05 | Strategy Budget Panel | done | 현재 cash 기반 전략별 목표 주문금액과 단일 종목 상한을 UI에서 볼 수 있다 |
| L5-06 | Tax Report Export Interface | done | 연간 세후 추산 리포트를 JSON/CSV로 생성할 수 있다 |
| L5-07 | Tax Dashboard Summary | done | tax summary 핵심 숫자를 dashboard에서 볼 수 있다 |
| L5-08 | Monthly/Periodic Report Shape | done | 월간 세후 성과 리포트의 최소 출력 포맷이 `period_summary + trade_report_rows`로 고정된다 |
| L5-09 | DR Telegram Integration | done | `restore_portfolio.py`가 `dr_restore_started/completed/failed`를 apply 흐름에서 자동 발송한다 |
| L5-10 | Reconcile Hold Notification | done | `reconcile_hold` 상태 전환 시 telegram 자동 발송이 연결된다 |
| L5-11 | FX Alert Wiring Or Defer | done | `fx_alert` 자동 호출은 policy 미정으로 deferred이며, notifier 표면만 유지된다고 문서화한다 |
| L5-12 | Dashboard Snapshot Enrichment | done | strategy budget / tax summary / auto-trading diagnostics가 snapshot에 포함된다 |
| L5-13 | Runbook/Usage Docs | done | dashboard 실행, tax export, restore/telegram 해석 방법이 문서화된다 |

## Implementation Notes
- `L5-01`은 `monitor/dashboard_app.py`를 추가해 read-only Streamlit 엔트리포인트를 만들고, `monitor/dashboard.py`의 read-model을 그대로 재사용한다.
- `L5-01`의 health 표면은 runtime 직접 참조가 아니라 `token_store`, 최근 reconciliation, 최근 error log를 이용한 read-only fallback으로 구성한다.
- `L5-02`는 `operational_summary`를 그대로 재사용하고, dashboard 상단에 health/block/stale/mismatch 중심 요약 카드를 배치한다.
- `L5-03`은 snapshot builder가 이미 제공하는 `recent_manual_restores`, `recent_backtests`를 Streamlit UI에 그대로 렌더링한다.
- `L5-04`는 `system_logs.extra_json`의 auto-trading cycle scalar payload를 사용해 최근 cycle의 signals/candidates/rejections와 skip/fail 이유를 요약한다.
- `L5-05`는 `latest_portfolio_snapshot.cash_krw`와 `allocation`, `strategy_weights`, `risk.max_single_stock_domestic`, `auto_trading.max_order_notional_per_cycle` 설정을 사용해 현재 전략별 목표 주문금액과 단일 종목 상한을 계산한다.
- `L5-06`은 `tax/report_export.py`와 `scripts/export_tax_report.py`를 추가해 `TaxCalculator` 결과를 JSON bundle 또는 summary/trades CSV로 출력한다.
- `L5-07`은 `TaxCalculator.calculate_yearly_summary()`를 재사용해 dashboard에서 연간 세후 summary 카드와 by-market 표를 렌더링한다.
- `L5-08`은 `tax/tax_calculator.py`를 계산 엔진으로 유지하고, `sell_date` calendar month 기준의 `period_summary + trade_report_rows` 출력 계층만 추가한다.
- `L5-09`는 `restore_portfolio.py`의 apply 흐름에 `dr_restore_started/completed/failed`를 best-effort로 연결하고, `dry-run`은 no-write/no-notify 계약을 유지한다.
- `L5-10`은 `OrderManager.flag_reconciliation_hold()`에 `reconcile_hold`를 연결하고, 이미 hold 상태면 중복 발송하지 않는다.
- `L5-11`은 deferred로 마감한다. 현재 `fx_alert`는 notifier 표면만 있고, 임계치·입력 소스·시장별 정책이 없어 자동 호출은 연결하지 않는다.
- `L5-12`는 `monitor/dashboard.py`가 `auto_trading_diagnostics`, `strategy_budget_summary`, `tax_summary`를 snapshot 생성 시점에 채우도록 확장한다.
- `L5-13`은 `docs/layer5_usage_runbook.md`에 dashboard 실행, tax export, restore, telegram 이벤트 해석 절차를 고정한다.

## Public Interfaces To Add
- dashboard snapshot
  - `strategy_budget_summary`
  - `auto_trading_diagnostics`
  - `tax_summary`
- tax export surface
  - `yearly_summary`
  - `trade_report_rows`
  - optional JSON/CSV output
- restore telegram events
  - `dr_restore_started`
  - `dr_restore_completed`
  - `dr_restore_failed`

## Verification Plan
```bash
python -m pytest tests\test_execution -q
python -m pytest tests\ -q
python -m compileall monitor tax scripts tests main.py
```

작업별 최소 검증:
- dashboard UI smoke test
- dashboard snapshot regression
- tax summary/export tests
- notifier no-op / formatting / sensitive-field tests
- restore telegram integration tests

## Recommended Start Order
1. `Layer 5 종료 검토`
2. `Phase 4 또는 운영 soak 후속 작업 분리`
3. `필요 시 Layer 5 polish만 별도 관리`

## First Recommended Task
- `Layer 5 문서/코드 상태 재정리`
- 이유:
  - `L5-01`부터 `L5-13`까지 현재 계획 범위는 모두 닫혔다.
  - 다음은 Layer 5를 별도 구현 단계로 더 확장하기보다, 종료 검토 후 운영 soak 또는 Phase 4/후속 계획으로 넘기는 편이 안전하다.
