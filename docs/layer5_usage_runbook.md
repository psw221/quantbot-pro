# Layer 5 Usage Runbook

## Title
Layer 5 운영 사용 문서

## Scope
- Layer 5 운영 표면의 실제 사용 절차를 정리한다.
- 범위는 아래 네 가지로 제한한다.
  - dashboard 실행
  - tax report export 실행
  - restore_portfolio 사용
  - telegram 이벤트 해석

## Preconditions
- 기본 운영 환경은 `vts`다.
- `config/config.yaml`의 `env`, `auto_trading`, `monitor.telegram` 설정을 먼저 확인한다.
- 민감정보(`bot token`, 계좌번호, API key)는 문서나 로그에 직접 남기지 않는다.

## Dashboard
### Start
```powershell
streamlit run monitor/dashboard_app.py
```

기본 접속 주소:
- `http://localhost:8501`

### Current Panels
- `Operations Summary`
- `Health`
- `Open Orders`
- `Recent Trades`
- `Reconciliation`
- `Recent Manual Restores`
- `Recent Backtests`
- `Recent Logs`
- `Auto-Trading Diagnostics`
- `Strategy Budget`
- `Broker Positions`
- `Tax Summary`

### What To Check First
- `Operations Summary`
  - `Health`
  - `Trading Blocked`
  - `Poll Stale`
  - `Writer Queue`
  - `Recent Mismatch`
- `Auto-Trading Diagnostics`
  - `Strategy Status`
  - `Cycle Status`
  - `Signals`
  - `Candidates`
  - `Rejected`
  - `Submitted`
  - `Top Rejections`
- `Strategy Budget`
  - `Cash KRW`는 latest portfolio snapshot을 우선 사용한다.
  - portfolio snapshot이 없으면 broker polling reconciliation의 `cash_available` fallback을 사용한다.
  - strategy rows
  - `KR Budget`
  - `Single-Stock Cap`
  - `Cycle Cap`
- `Broker Positions`
  - 최신 broker account snapshot 기준 보유수량/평균단가를 확인한다.
  - 이 패널은 내부 strategy positions와 별도다.

### KR Strategy Schedule Defaults
- `trend_following`
  - 기본 cron: `*/15 9-15 * * 1-5`
  - 장중 추세 전략이므로 session 시간대에 반복 실행된다.
- `dual_momentum`
  - 기본 cron: `0 9 1 * *`
  - 월초 리밸런싱 전략으로 본다.
- `factor_investing`
  - 기본 cron: `5 9 1 1,4,7,10 *`
  - 분기 초 리밸런싱 전략으로 본다.
- strategy별 cron이 비어 있으면 `auto_trading.kr.schedule_cron` fallback을 사용한다.

### KR Universe Defaults
- KR 기본 전략 universe는 현재 `KOSPI 200` 구성종목을 기준으로 만든다.
- 기존 KR 보유 종목은 지수 구성에서 빠졌더라도 같은 universe에 union으로 유지한다.
- `KOSPI 200` live source를 읽지 못하면 `data/kospi200_constituents.json` 정적 캐시를 사용한다.
- live source와 정적 캐시를 모두 읽지 못하면 최소 fallback universe `005930`, `000660`, `035420`을 사용한다.
- 정적 캐시는 6자리 KRX 종목코드만 사용하며, index 정기 변경 이후에는 운영자가 파일을 갱신한다.

### Auto-Trading Diagnostics Interpretation
- `Strategy Status`
  - 현재 최신 auto-trading log의 primary strategy 상태를 바로 보여준다.
  - 예: `trend_following: completed`
  - 예: `factor_investing: skipped (factor_input_unavailable)`
- strategy rows
  - strategy별 `strategy_cycle_status`, `strategy_skip_reason`, `factor_input_available`를 표로 보여준다.
  - 현재 KR runtime job은 strategy별로 분리돼 있으므로 보통 최신 log 기준 1행만 보인다.
- 주요 해석 규칙
  - `completed`
    - 해당 전략 subset 실행이 정상 종료된 상태다.
  - `skipped (factor_input_unavailable)`
    - factor input loader/source가 준비되지 않은 상태다.
    - runtime failure나 broker mismatch가 아니라 strategy-local skip으로 해석한다.
  - `skipped (market_closed)`
    - 시장 세션 밖이므로 실행하지 않은 상태다.
  - `skipped (trading_blocked|token_stale|polling_stale|writer_queue_degraded|non_vts_environment)`
    - runtime gate가 전략 실행 전에 cycle을 막은 상태다.
    - `Operations Summary`, `Health`, telegram 이벤트를 함께 본다.
  - `failed`
    - strategy cycle runner 예외가 발생한 상태다.
    - `Recent Logs`의 `error_message`와 같은 시각대 운영 이벤트를 확인한다.

## Tax Report Export
### JSON Export
```powershell
python scripts/export_tax_report.py --year 2026 --format json
```

### CSV Export
```powershell
python scripts/export_tax_report.py --year 2026 --market US --format csv
```

### Monthly JSON Export
```powershell
python scripts/export_tax_report.py --year 2026 --month 4 --format json
```

### Output Shape
- `json`
  - single bundle file
  - includes `yearly_summary`, `trade_report_rows`
- `json` with `--month`
  - single bundle file
  - includes `yearly_summary`, `period_summary`, `trade_report_rows`
- `csv`
  - `summary.csv`
  - `trades.csv`

### Interpretation
- `yearly_summary`
  - 연간 realized gain/loss, taxable gain, fees, taxes를 본다.
- `trade_report_rows`
  - 거래별 세후 추산 행이다.
- `period_summary`
  - `sell_date` calendar month 기준의 realized gain/loss, taxable gain, fees, taxes 집계다.
- US 매도는 `tax_events` 우선, 누락 시 FIFO fallback을 사용한다.
- KR 거래는 FX가 `null`일 수 있다.

## Strategy Backtest
### Dual Momentum
```powershell
python -m scripts.run_kr_rebalance_backtest --strategy dual_momentum --start-date 2024-01-01 --end-date 2026-04-01 --tickers 005930,000660,035420
```

### Factor Investing
```powershell
python -m scripts.run_kr_rebalance_backtest --strategy factor_investing --start-date 2024-01-01 --end-date 2026-04-01 --tickers 005930,000660,035420 --factor-file <path>
```

### Persist Result
```powershell
python -m scripts.run_kr_rebalance_backtest --strategy dual_momentum --start-date 2024-01-01 --end-date 2026-04-01 --tickers 005930,000660,035420 --persist
```

### Backtest Rules
- 이 스크립트는 KR `dual_momentum`, `factor_investing` 전용이다.
- price history는 기본적으로 `pykrx` loader를 사용한다.
- `factor_investing`은 CSV/JSON factor snapshot file이 필요하다.
- factor file은 최소 아래 컬럼을 가져야 한다.
  - `date`
  - `ticker`
  - `value_score`
  - `quality_score`
  - `momentum_score`
  - `low_vol_score`
- factor snapshot 선택 기준은 `rebalance as_of` 이하에서 가장 최근 날짜다.
- `--persist`가 없으면 결과를 콘솔 JSON으로만 출력하고 DB write를 하지 않는다.
- `--persist`를 주면 `backtest_results`, `system_logs`에 저장한다.

## Restore Portfolio
### Dry Run
```powershell
python scripts/restore_portfolio.py --dry-run --market ALL --snapshot-file <path>
```

### Apply
```powershell
python scripts/restore_portfolio.py --apply --market ALL --snapshot-file <path>
```

### Restore Rules
- `dry-run`
  - mismatch summary만 출력한다.
  - DB write와 telegram 발송을 하지 않는다.
- `apply`
  - `manual_restore` reconciliation run을 기록한다.
  - `broker_positions`, `system_logs`, optional `portfolio_snapshot`을 기록한다.
  - `dr_restore_started`, `dr_restore_completed`, `dr_restore_failed`를 telegram으로 best-effort 발송한다.
- direct fill/order/lot correction은 하지 않는다.

## Manual Fill Repair
브로커 앱/HTS에서 수동 매매가 발생해 내부 원장이 broker 실보유와 달라진 경우에만 사용한다.

### Dry Run
```powershell
python -m scripts.repair_manual_fills --dry-run --market KR --ticker 005930 --strategy trend_following --snapshot-file <path>
```

### Apply
```powershell
python -m scripts.repair_manual_fills --apply --market KR --ticker 005930 --strategy trend_following --snapshot-file <path>
```

### Repair Rules
- `restore_portfolio.py`는 mismatch 판단/기록 도구로 유지하고, 실제 원장 replay는 이 maintenance 경로에서만 수행한다.
- 실행 전 조건
  - auto-trading runtime이 완전히 꺼져 있어야 한다.
  - 최신 broker snapshot file이 있어야 한다.
  - target `ticker + strategy`에 active internal order가 없어야 한다.
- `repair_manual_fills`
  - broker daily fill 중 target ticker의 sell fill만 후보로 본다.
  - candidate sell fill 총수량이 `internal_quantity - broker_quantity`와 정확히 같아야만 진행한다.
  - synthetic internal sell order/fill을 replay해 `orders`, `order_executions`, `trades`, `position_lots`, `positions`를 표준 경로로 복구한다.
  - 완료 후 `manual_restore` reconciliation을 다시 실행해 `reconciled`가 아니면 성공으로 보지 않는다.
- 실행 후에는 dashboard에서 최근 `manual_restore`, `recent logs`, broker/internal quantity 일치 여부를 다시 확인한다.

### Snapshot File Minimum Fields
- `positions`
- `open_orders`
- `cash_available`
- optional `portfolio_snapshot`

## Telegram Event Interpretation
### Health / Runtime
| Event | Meaning | Operator Action |
| --- | --- | --- |
| `token_refresh_failure` | 토큰 갱신 실패가 재시도 후에도 남았다 | 인증 상태와 KIS 자격증명을 확인한다 |
| `trading_blocked` | 신규 주문 차단 상태다 | `polling_mismatch`, `reconcile_hold`, writer queue 상태를 먼저 확인한다 |
| `writer_queue_degraded` | SQLite write 경로가 저하됐다 | 신규 주문을 멈추고 runtime/DB 상태를 확인한다 |
| `polling_mismatch` | broker/internal state mismatch가 감지됐다 | reconciliation 결과와 broker snapshot을 확인한다 |
| `pre_close_cancel_failure` | 장 마감 전 미체결 취소가 실패했다 | open order와 broker cancel 응답을 확인한다 |

### Reconciliation / DR
| Event | Meaning | Operator Action |
| --- | --- | --- |
| `reconcile_hold` | 정합성 복구가 필요해 주문 흐름이 hold로 전환됐다 | `reconciliation_runs`, `system_logs`, `broker_positions`를 확인한다 |
| `dr_restore_started` | restore apply가 시작됐다 | 같은 시간대의 `manual_restore` 기록을 확인한다 |
| `dr_restore_completed` | restore apply가 완료됐다 | `status`, `mismatch_count`, 최신 snapshot/log를 확인한다 |
| `dr_restore_failed` | restore apply가 실패했다 | `system_logs`의 에러와 snapshot 입력을 다시 확인한다 |
| `fx_alert` | 현재 자동 호출되지 않는다 | notifier 표면만 예약되어 있고, 환율 임계치/입력 소스 정책이 정해질 때까지 기대하지 않는다 |

### Message Layout
- `[ENV] Title`
- `severity=<...>`
- `time=<UTC ISO8601>`
- `summary`
- `detail fields`

민감정보 필터링:
- token
- account
- authorization
- raw payload
- app key / secret
- chat id

## Operator Quick Flow
1. `streamlit run monitor/dashboard_app.py`
2. `Operations Summary`에서 blocked/stale/mismatch를 먼저 확인한다.
3. `Auto-Trading Diagnostics`에서 `Strategy Status`, strategy rows, rejection reason을 함께 본다.
4. 복구가 필요하면 `restore_portfolio.py --dry-run`을 먼저 실행한다.
5. 연간 세후 추산이 필요하면 `scripts/export_tax_report.py`를 사용한다.
6. telegram 이벤트는 dashboard와 같은 용어(`polling_mismatch`, `reconcile_hold`, `dr_restore_*`)로 해석한다.

## Notes
- 이 문서는 Layer 5 운영 사용 절차만 다룬다.
- 장기 soak 운용 정책, 스케줄 작업 등록, Phase 4 전략 파라미터 조정은 별도 문서/계획에서 관리한다.
