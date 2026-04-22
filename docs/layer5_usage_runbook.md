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
- `Recent Logs`
- `Auto-Trading Diagnostics`
- `Strategy Budget`
- `Tax Summary`

### What To Check First
- `Operations Summary`
  - `Health`
  - `Trading Blocked`
  - `Poll Stale`
  - `Writer Queue`
  - `Recent Mismatch`
- `Auto-Trading Diagnostics`
  - `Cycle Status`
  - `Signals`
  - `Candidates`
  - `Rejected`
  - `Submitted`
  - `Top Rejections`
- `Strategy Budget`
  - `Cash KRW`
  - `KR Budget`
  - `Single-Stock Cap`
  - `Cycle Cap`

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
3. `Auto-Trading Diagnostics`에서 최근 cycle 결과와 rejection reason을 본다.
4. 복구가 필요하면 `restore_portfolio.py --dry-run`을 먼저 실행한다.
5. 연간 세후 추산이 필요하면 `scripts/export_tax_report.py`를 사용한다.
6. telegram 이벤트는 dashboard와 같은 용어(`polling_mismatch`, `reconcile_hold`, `dr_restore_*`)로 해석한다.

## Notes
- 이 문서는 Layer 5 운영 사용 절차만 다룬다.
- 장기 soak 운용 정책, 스케줄 작업 등록, Phase 4 전략 파라미터 조정은 별도 문서/계획에서 관리한다.
