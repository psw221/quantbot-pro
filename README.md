# QuantBot Pro

한국투자증권 Open Trading API(KIS API)를 사용하는 자동매매/운영 모니터링 프로젝트입니다.  
현재 원격 기준으로 바로 재현 가능한 기본 범위는 아래와 같습니다.

- `VTS` 환경 기준 런타임 실행
- `KR` 자동매매 사이클 실행
- Streamlit 대시보드 조회
- 세금 추산 리포트 export
- 포트폴리오 restore preview/apply

주의:
- 현재 자동매매는 `VTS` 우선 운영 기준입니다.
- 현재 auto-trading 범위는 `KR only`, `dual_momentum + trend_following`입니다.
- `env != vts`면 auto-trading cycle은 안전 가드로 skip 됩니다.

## 1. 현재 지원 범위

현재 저장소 기준 주요 실행 표면:

- 런타임: `python main.py`
- Windows용 시작/중지 스크립트:
  - `pwsh -File scripts/start_auto_trading.ps1`
  - `pwsh -File scripts/stop_auto_trading.ps1`
- 대시보드:
  - `streamlit run monitor/dashboard_app.py`
- 세금 리포트 export:
  - `python scripts/export_tax_report.py ...`
- DR restore:
  - `python scripts/restore_portfolio.py ...`

Layer 5 운영 표면은 현재 아래까지 구현되어 있습니다.

- Health / Operations Summary
- Open Orders / Recent Trades / Reconciliation / Recent Logs
- Recent Manual Restores / Recent Backtests
- Auto-Trading Diagnostics
- Strategy Budget
- Tax Summary
- Telegram 운영 알림

## 2. 권장 실행 환경

권장:

- Python `3.11.x`
- Windows PowerShell 7+ 또는 macOS/Linux shell
- SQLite 로컬 파일 사용 가능 환경

참고:

- 저장소의 `requirements.txt`는 핵심 런타임 의존성 위주입니다.
- `streamlit`은 대시보드 실행 시 필요합니다.
- `pykrx`는 KR 가격 이력 보조 로더용 선택 의존성입니다.
  - 없어도 KIS 일봉 조회 fallback이 있으면 auto-trading은 계속 동작할 수 있습니다.

## 3. 설치

### 3.1 저장소 clone

```bash
git clone <your-remote-url>
cd quantbot-pro
```

### 3.2 가상환경 생성

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3.3 기본 의존성 설치

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3.4 선택 의존성 설치

대시보드까지 사용할 경우:

```bash
python -m pip install streamlit
```

KR 가격 이력 보조 로더까지 사용할 경우:

```bash
python -m pip install pykrx pandas numpy
```

## 4. 설정 파일 준비

### 4.1 `.env` 생성

```powershell
Copy-Item config\.env.example config\.env
```

`config/.env`에 아래 값을 채웁니다.

```dotenv
KIS_APP_KEY=
KIS_APP_SECRET=
KIS_ACCOUNT_NO=
KIS_PRODUCT_CODE=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

설명:

- `KIS_*` 값은 브로커 연동용 필수 값입니다.
- `TELEGRAM_*` 값이 없으면 notifier는 안전한 no-op으로 동작합니다.

### 4.2 `config/config.yaml` 확인

기본 원칙:

- 먼저 `env: vts`로 시작합니다.
- 현재 auto-trading은 `KR only`입니다.
- 현재 시작 스크립트는 `env=vts`와 `auto_trading.enabled=true`를 전제로 합니다.

중요 항목:

```yaml
env: vts

auto_trading:
  enabled: true
  markets: [KR]
  strategies: [dual_momentum, trend_following]
  max_orders_per_cycle: 1
  max_order_notional_per_cycle: 500000

monitor:
  telegram:
    enabled: true

database:
  path: data/quantbot.db
```

운영 팁:

- 다른 작업환경에서 먼저 확인할 때는 `env: vts` 유지
- 텔레그램을 쓰지 않으면 `monitor.telegram.enabled: false`
- 주문을 막고 read-only에 가깝게 점검하려면 `auto_trading.enabled: false`

## 5. 최초 실행 순서

다른 작업환경에서 가장 안전한 시작 순서:

1. `config/.env` 채우기
2. `config/config.yaml`에서 `env=vts` 확인
3. DB 초기화
4. 런타임 실행
5. 대시보드 확인
6. 필요 시 tax export / restore 사용

### 5.1 DB 초기화

```bash
python scripts/init_db.py
```

성공 시 예시:

```text
Initialized database at ...
Schema initialization is idempotent.
```

### 5.2 런타임 실행

직접 실행:

```bash
python main.py
```

정상 시작 시 예시:

```text
QuantBot Pro runtime ready: env=vts, db=data/quantbot.db
```

Windows에서 백그라운드 실행:

```powershell
pwsh -File scripts/start_auto_trading.ps1
```

중지:

```powershell
pwsh -File scripts/stop_auto_trading.ps1
```

시작 스크립트가 만드는 파일:

- PID metadata: `data/auto_trading.pid.json`
- stdout log: `logs/auto_trading.stdout.log`
- stderr log: `logs/auto_trading.stderr.log`

## 6. 대시보드 실행

Windows에서 백그라운드 실행:

```powershell
pwsh -File scripts/start_dashboard.ps1
```

중지:

```powershell
pwsh -File scripts/stop_dashboard.ps1
```

포트 변경:

```powershell
pwsh -File scripts/start_dashboard.ps1 -Port 8502
```

포그라운드 실행:

```powershell
pwsh -File scripts/start_dashboard.ps1 -Foreground
```

기본 접속 주소:

- `http://localhost:8501`

시작 스크립트가 만드는 파일:

- PID metadata: `data/dashboard.pid.json`
- stdout log: `logs/dashboard.stdout.log`
- stderr log: `logs/dashboard.stderr.log`

현재 패널:

- Operations Summary
- Health
- Open Orders
- Recent Trades
- Reconciliation
- Recent Manual Restores
- Recent Backtests
- Recent Logs
- Auto-Trading Diagnostics
- Strategy Budget
- Tax Summary

운영자가 먼저 볼 항목:

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
- `Strategy Budget`
  - `Cash KRW`
  - `KR Budget`
  - `Single-Stock Cap`
  - `Cycle Cap`

## 7. 세금 리포트 export

연간 JSON:

```bash
python scripts/export_tax_report.py --year 2026 --format json
```

연간 CSV:

```bash
python scripts/export_tax_report.py --year 2026 --market US --format csv
```

월간 JSON:

```bash
python scripts/export_tax_report.py --year 2026 --month 4 --format json
```

기본 출력 경로:

- `reports/tax/`

현재 출력 계약:

- yearly:
  - `yearly_summary`
  - `trade_report_rows`
- monthly:
  - `yearly_summary`
  - `period_summary`
  - `trade_report_rows`

참고:

- 현재는 일일 세후 리포트는 없습니다.
- 월간 리포트는 `sell_date` calendar month 기준입니다.

## 8. Restore 사용

Dry-run:

```bash
python scripts/restore_portfolio.py --dry-run --market ALL --snapshot-file <path>
```

Apply:

```bash
python scripts/restore_portfolio.py --apply --market ALL --snapshot-file <path>
```

현재 restore 계약:

- `dry-run`
  - mismatch summary만 출력
  - DB write 없음
  - telegram 발송 없음
- `apply`
  - `manual_restore` reconciliation 기록
  - `broker_positions`, `system_logs`, optional `portfolio_snapshot` 기록
  - `dr_restore_started`, `dr_restore_completed`, `dr_restore_failed` best-effort telegram 발송
- direct fill/order/lot correction은 하지 않음

## 9. Telegram 운영 알림

현재 notifier 표면:

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

현재 자동 호출이 연결된 대표 이벤트:

- `trading_blocked`
- `polling_mismatch`
- `reconcile_hold`
- `writer_queue_degraded`
- `pre_close_cancel_failure`
- `dr_restore_*`

참고:

- `fx_alert`는 notifier 표면만 예약돼 있고 현재 자동 호출은 deferred 상태입니다.
- 민감정보(`token`, `account`, `authorization`, `raw payload`, `app key/secret`)는 메시지에서 제거됩니다.

## 10. 주요 파일/경로

- 런타임 진입점: `main.py`
- 설정:
  - `config/config.yaml`
  - `config/.env`
- DB:
  - `data/quantbot.db`
- 로그:
  - `logs/`
- 대시보드:
  - `monitor/dashboard_app.py`
- 운영 runbook:
  - `docs/layer5_usage_runbook.md`
- Layer 5 완료 상태:
  - `docs/plans/layer5_remaining_work_plan.md`

## 11. 현재 운영 제약

현재 원격 기준으로 알고 있어야 할 제한:

- auto-trading은 `VTS` 우선 기준
- auto-trading은 `KR only`
- auto-trading 전략은 현재 `dual_momentum`, `trend_following`
- `same ticker + same strategy` 추가 진입은 차단
- `fx_alert` 자동 호출은 아직 없음
- 일일 세후 리포트는 아직 없음

## 12. 문제 해결

### 런타임은 뜨는데 주문이 안 나감

우선 확인:

- `config/config.yaml`의 `env: vts`
- `auto_trading.enabled: true`
- 대시보드 `Operations Summary`
- 대시보드 `Auto-Trading Diagnostics`

대표 원인:

- `market_closed`
- `polling_stale`
- `trading_blocked`
- `writer_queue_degraded`
- `existing_position_reentry_blocked`
- `no_position_to_sell`

### Telegram이 안 옴

확인:

- `monitor.telegram.enabled: true`
- `config/.env`에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 존재
- 네트워크 outbound 허용

### 대시보드가 안 뜸

확인:

- `python -m pip install streamlit`
- `streamlit run monitor/dashboard_app.py`

## 13. 참고 문서

- 제품 요구사항: `docs/PRD_v1.4.md`
- DB 스키마: `docs/DB_SCHEMA_v1.2.md`
- Layer 5 운영 runbook: `docs/layer5_usage_runbook.md`
- Layer 5 남은 작업/완료 상태: `docs/plans/layer5_remaining_work_plan.md`
- 저장소 규칙: `AGENTS.md`
