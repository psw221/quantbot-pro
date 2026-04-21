# Layer 5 Remaining Work Plan

## Title
Layer 5 모니터링 및 DR 잔여 작업 계획

## Status
- state: in_progress
- default_env: vts
- scope_focus: layer5_user_surface

## Summary
- Layer 5의 운영 백엔드(`healthcheck`, `telegram`, `restore`, `tax calculator`, `dashboard snapshot builder`, `backtest result persistence`)는 대부분 구현되었다.
- 남은 일은 사용자-facing 표면과 운영 연결 마감이다.
- 우선순위는 `대시보드 UI`, `세후 성과 리포트 출력`, `DR/운영 알림 연결`, `운영 지표 노출 강화` 순서로 잡는다.
- 완료 기준은 운영자가 코드/DB 직접 조회 없이 Layer 5 정보를 화면, 리포트, 알림으로 확인할 수 있는 상태다.

## Current State
- `monitor/dashboard.py`는 Streamlit UI가 아니라 read-only snapshot builder다.
- `monitor/telegram_bot.py`는 notifier 구현이 완료되었고 runtime, restore, reconcile hold 이벤트 일부가 실제로 연결돼 있다.
- `monitor/healthcheck.py`는 external canonical health(`normal`, `warning`, `critical`)를 제공한다.
- `tax/tax_calculator.py`는 `calculate_yearly_summary()`와 `build_trade_report()`를 제공하지만 출력물 생성/배포 계층은 없다.
- `scripts/restore_portfolio.py`는 `dry-run`/`apply`, `manual_restore`, reconciliation 기록까지 구현됐지만 DR 관련 Telegram 자동 연계는 없다.
- `backtest/backtest_runner.py`는 결과 저장과 system log 기록이 가능하고 dashboard snapshot에서 recent backtests를 읽을 수 있다.
- 따라서 Layer 5는 운영 엔진은 구현됐고, 최종 표면은 부분 구현 상태다.

## Work Breakdown
| ID | Task | Status | Done Criteria |
| --- | --- | --- | --- |
| L5-01 | Dashboard App Skeleton | done | Streamlit 앱에서 health, open orders, recent trades, reconciliation, logs 섹션이 보인다 |
| L5-02 | Operations Summary Panel | done | `operational_summary`가 카드형으로 렌더링된다 |
| L5-03 | Restore/Backtest Panels | todo | recent manual restores / recent backtests를 UI에서 볼 수 있다 |
| L5-04 | Auto-Trading Diagnostics Panel | done | 최근 cycle의 signals/candidates/rejections를 UI에서 볼 수 있다 |
| L5-05 | Strategy Budget Panel | done | 현재 cash 기반 전략별 목표 주문금액과 단일 종목 상한을 UI에서 볼 수 있다 |
| L5-06 | Tax Report Export Interface | done | 연간 세후 추산 리포트를 JSON/CSV로 생성할 수 있다 |
| L5-07 | Tax Dashboard Summary | done | tax summary 핵심 숫자를 dashboard에서 볼 수 있다 |
| L5-08 | Monthly/Periodic Report Shape | todo | 월간 세후 성과 리포트의 최소 출력 포맷이 고정된다 |
| L5-09 | DR Telegram Integration | done | `restore_portfolio.py`가 `dr_restore_started/completed/failed`를 apply 흐름에서 자동 발송한다 |
| L5-10 | Reconcile Hold Notification | done | `reconcile_hold` 상태 전환 시 telegram 자동 발송이 연결된다 |
| L5-11 | FX Alert Wiring Or Defer | todo | `fx_alert` 자동 호출을 구현하거나 deferred로 문서화한다 |
| L5-12 | Dashboard Snapshot Enrichment | todo | strategy budget / tax summary / auto-trading diagnostics가 snapshot에 포함된다 |
| L5-13 | Runbook/Usage Docs | todo | dashboard 실행, tax export, restore/telegram 해석 방법이 문서화된다 |

## Implementation Notes
- `L5-01`은 `monitor/dashboard_app.py`를 추가해 read-only Streamlit 엔트리포인트를 만들고, `monitor/dashboard.py`의 read-model을 그대로 재사용한다.
- `L5-01`의 health 표면은 runtime 직접 참조가 아니라 `token_store`, 최근 reconciliation, 최근 error log를 이용한 read-only fallback으로 구성한다.
- `L5-02`는 `operational_summary`를 그대로 재사용하고, dashboard 상단에 health/block/stale/mismatch 중심 요약 카드를 배치한다.
- `L5-04`는 `system_logs.extra_json`의 auto-trading cycle scalar payload를 사용해 최근 cycle의 signals/candidates/rejections와 skip/fail 이유를 요약한다.
- `L5-05`는 `latest_portfolio_snapshot.cash_krw`와 `allocation`, `strategy_weights`, `risk.max_single_stock_domestic`, `auto_trading.max_order_notional_per_cycle` 설정을 사용해 현재 전략별 목표 주문금액과 단일 종목 상한을 계산한다.
- `L5-06`은 `tax/report_export.py`와 `scripts/export_tax_report.py`를 추가해 `TaxCalculator` 결과를 JSON bundle 또는 summary/trades CSV로 출력한다.
- `L5-07`은 `TaxCalculator.calculate_yearly_summary()`를 재사용해 dashboard에서 연간 세후 summary 카드와 by-market 표를 렌더링한다.
- `L5-08`은 `tax/tax_calculator.py`를 계산 엔진으로 유지하고, periodic output 계층만 추가한다.
- `L5-09`는 `restore_portfolio.py`의 apply 흐름에 `dr_restore_started/completed/failed`를 best-effort로 연결하고, `dry-run`은 no-write/no-notify 계약을 유지한다.
- `L5-10`은 `OrderManager.flag_reconciliation_hold()`에 `reconcile_hold`를 연결하고, 이미 hold 상태면 중복 발송하지 않는다.
- `L5-11`은 이번 계획에서 기본적으로 deferred를 추천한다. 현재 `fx_alert`는 notifier 표면만 있고 자동 호출 정책이 없다.
- `L5-12`는 dashboard가 별도 계산 없이 snapshot만 렌더링할 수 있도록 read-model을 확장하는 단계다.
- `L5-13`은 운영자가 실행 절차를 문서만 보고 따라갈 수 있을 정도의 사용 문서를 목표로 한다.

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
1. `L5-13 Runbook/Usage Docs`
2. `L5-03 Restore/Backtest Panels`
3. `L5-11 FX Alert Wiring Or Defer`

## First Recommended Task
- `L5-13 Runbook/Usage Docs`
- 이유:
  - restore와 reconcile hold 텔레그램 연계가 둘 다 닫혀서 Layer 5 운영 알림 표면은 거의 완성됐다.
  - 다음은 dashboard 실행, tax export, restore/telegram 이벤트 해석 절차를 문서화해 운영자가 코드나 DB 없이 Layer 5 기능을 사용할 수 있게 만드는 순서가 가장 자연스럽다.
  - 그 뒤에 restore/backtest UI 패널이나 `fx_alert` deferred 정리를 같은 용어 체계로 이어가기 쉽다.
