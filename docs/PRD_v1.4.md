# QuantBot Pro — 주식 자동 매매 시스템 PRD (최종본)

> **Product Requirements Document**  
> Version: `v1.4` | 작성일: 2026년 4월 | 상태: 최종본  
> 대상 시장: KRX / NYSE / NASDAQ  
> 핵심 API: 한국투자증권 Open Trading API (단일 통합)

---

## 목차

1. [문서 개요](#1-문서-개요)
2. [프로젝트 배경 및 목표](#2-프로젝트-배경-및-목표)
3. [대상 시장 및 전략 운영 원칙](#3-대상-시장-및-전략-운영-원칙)
4. [시스템 아키텍처](#4-시스템-아키텍처)
5. [기능 요구사항](#5-기능-요구사항)
6. [비기능 요구사항](#6-비기능-요구사항)
7. [개발 로드맵](#7-개발-로드맵)
8. [리스크 및 제약사항](#8-리스크-및-제약사항)
9. [부록](#9-부록)

---

## 변경 이력

| 버전 | 날짜 | 변경 내용 |
|------|------|-----------|
| v1.0 | 2025-04 | 최초 작성 |
| v1.1 | 2025-04 | API 단일화: KIS API 통합 |
| v1.2 | 2025-04 | 토큰 자동 갱신, 수정주가, Vol Parity, 세금/이벤트/DR 보강 |
| v1.3 | 2026-04 | 주문/체결 분리, 자산 배분 공식 명확화, KPI 정의 보강 |
| v1.4 | 2026-04 | 거시적 리밸런싱 정책 추가, 10분 주기 브로커 폴링 동기화 요구사항 추가 |

---

## 1. 문서 개요

### 1.1 목적

본 문서는 국내(KRX) 및 미국(NYSE/NASDAQ) 주식 시장을 대상으로 하는 자동 매매 시스템 **QuantBot Pro**의 제품 요구사항을 정의합니다.

### 1.2 범위

- KIS API 기반 국내·미국 주식 동시 운용
- 멀티 전략 운용
- 백테스팅, 실시간 신호 감지, 자동 주문 실행, 리스크 관리
- 성과 모니터링, 알림, 장애 복구, 세금 추산

### 1.3 구현 원칙

- 기능 요구사항 기준: `docs/PRD_v1.4.md`
- 저장 구조 기준: `docs/DB_SCHEMA_v1.2.md`
- 구현 규칙 기준: `AGENTS.md`

---

## 2. 프로젝트 배경 및 목표

### 2.1 배경

감정 배제, 다종목 동시 대응, 미국 야간장 대응, 리스크 일관성 확보를 위해 신뢰성 높은 자동 매매 시스템이 필요합니다.

### 2.2 핵심 목표

> 🎯 연간 Alpha 목표: 벤치마크 대비 **+8% ~ +15%**  
> 📊 Sharpe Ratio **≥ 1.5**  
> 🛡️ Maximum Drawdown **≤ 15%**

### 2.3 성공 지표 (KPI)

모든 수익률 KPI는 **세후 기준**으로 측정합니다.

| 지표 | 목표값 | 측정 정의 |
|------|--------|-----------|
| 연간 수익률 (Alpha, 세후) | +8 ~ 15% | 벤치마크 대비 연환산 초과 수익 |
| Sharpe Ratio | ≥ 1.5 | 일별 세후 수익률 기준 |
| Maximum Drawdown | ≤ 15% | 일별 포트폴리오 가치 기준 |
| 승률 | ≥ 52% | 전략별 종료 거래 단위 기준 |
| 주문 실행 지연 | ≤ 200ms | 신호 해소 완료 시점 → 브로커 주문 요청 수락 응답 시점 |
| 시스템 가용성 | ≥ 99.5% | 시장 운영 시간 기준, 계획 점검 제외 |
| Live-to-Backtest Gap | ≤ 3%p | 월간 세후 수익률 기준 |

---

## 3. 대상 시장 및 전략 운영 원칙

### 3.1 대상 시장

| 시장 | 거래소 | 운영 시간 (KST) | 사용 API |
|------|--------|-----------------|----------|
| 국내 주식 | KRX | 09:00 ~ 15:30 | KIS API |
| 미국 주식 | NYSE / NASDAQ | 23:30 ~ 06:00 | KIS API |

### 3.2 국내 시장 특수 제약

| 제약 항목 | 내용 | 시스템 대응 |
|-----------|------|-------------|
| 가격 제한폭 | 전일 종가 대비 ±30% | 주문 생성 전 검증 |
| 동시호가 | 08:30~09:00, 15:20~15:30 | 신규 주문 차단 옵션 |
| 공매도 | 개인 투자자 공매도 불가 | 보유 수량 이내 매도 |
| 단기 과열 종목 | 거래 제한 가능 | Universe 제외 |
| 최소 주문 수량 | 1주 단위 | 수량 올림 처리 |
| 결제일 | T+2 | 미결제 자금 반영 |

### 3.3 전체 자산 배분 정책

| 버킷 | 비율 | 설명 |
|------|------|------|
| 국내 주식 버킷 | 60% | 정보 우위 및 환율 리스크 완화 |
| 미국 주식 버킷 | 30% | 글로벌 분산 및 달러 자산 노출 |
| 현금 버퍼 | 10% | 긴급 대응 및 리밸런싱 재원 |

### 3.4 전략 가중치 정책

전략 가중치는 **각 시장 버킷 내부**에 적용합니다.

| 전략 | 가중치 | 성격 |
|------|--------|------|
| 듀얼 모멘텀 | 30% | 안정성 중심 |
| 추세 추종 + Vol Target | 25% | 추세 국면 대응 |
| 멀티 팩터 | 45% | 핵심 Alpha 원천 |

### 3.5 자산 배분 공식

```text
총자산
  ├─ 국내 버킷 60%
  │    ├─ 듀얼 모멘텀 30%
  │    ├─ 추세 추종 25%
  │    └─ 멀티 팩터 45%
  ├─ 미국 버킷 30%
  │    ├─ 듀얼 모멘텀 30%
  │    ├─ 추세 추종 25%
  │    └─ 멀티 팩터 45%
  └─ 현금 버퍼 10%
```

### 3.6 거시적 리밸런싱 정책

자산 버킷 수준의 리밸런싱은 전략 리밸런싱과 별도 계층으로 관리합니다.

#### 트리거 조건

아래 둘 중 하나를 만족하면 거시적 리밸런싱 후보로 판정합니다.

1. 국내/미국/현금 버킷 비중이 목표 대비 **±5%p 이상 이탈**
2. **매월 말일 EOD 기준** 정기 점검

#### 평가 시점

- 평가는 장 마감 후 EOD 기준으로 수행합니다.
- 장중 실시간 가격 변동만으로 즉시 리밸런싱을 실행하지 않습니다.

#### 집행 시점

- 정기/조건부 리밸런싱은 **다음 거래일 정규장**에 실행합니다.
- 이벤트 리스크 또는 거래정지 상태에서는 집행을 연기할 수 있습니다.

#### 우선순위

거시적 리밸런싱은 아래 우선순위를 따릅니다.

```text
손절 / 익절 → 리스크 차단 → 거시적 리밸런싱 → 전략 리밸런싱
```

#### 집행 원칙

- 목표 복원에 필요한 최소 수량만 거래합니다.
- 단일 거래로 목표 비중을 완벽히 맞추기보다 거래비용을 고려한 점진 복원을 허용합니다.
- 현금 버퍼 10%는 가능한 유지하며, 리스크 이벤트 시 일시적으로 상향될 수 있습니다.

### 3.7 전략 개요

#### 전략 1 — 듀얼 모멘텀

- 절대 모멘텀 12개월
- 상대 모멘텀 상위 10종목
- 월 1회 리밸런싱
- 손절: KR -7%, US -5%

#### 전략 2 — 추세 추종 + 변동성 조절

- EMA(20) / EMA(60)
- 목표 변동성 연 13%
- ATR(14) 기반 손절
- RSI 30 미만 신규 진입 제한

#### 전략 3 — 멀티 팩터

- Value / Quality / Momentum / Low Vol 결합
- 분기 1회 리밸런싱
- 상위 25종목 편입
- 손절: KR -7%, US -5%

### 3.8 전략 인터페이스 표준

- `generate_signals(...) -> list[Signal]`
- `get_exit_signal(...) -> Signal | None`

### 3.9 전략 신호 충돌 처리

| 상황 | 처리 방식 |
|------|-----------|
| 복수 전략 동시 매수 | 포지션 합산 가능. 단, 단일 종목 상한 초과 시 비례 축소 |
| 매수 + 매도 충돌 | 매도 우선 |
| 복수 전략 동시 매도 | 전량 청산 |
| 손절 + 리밸런싱 매수 충돌 | 손절 우선, 당일 재매수 금지 |

추가 운영 규칙:

- 신호 충돌 해소는 `signal_resolver`에서 단일 책임으로 수행합니다.
- 충돌 해소 순서는 `매도 우선 -> 손절 우선 -> 복수 매수 합산 -> 종목 상한 비례 축소`를 기본 규칙으로 합니다.
- 리스크 게이트를 통과하지 못한 신호는 주문으로 진행하지 않고 reject 사유를 남깁니다.

### 3.10 이벤트 리스크 대응

| 이벤트 | 감지 방법 | 대응 |
|--------|-----------|------|
| FOMC | 경제 캘린더 API | 당일 미국 신규 매수 중단 |
| 금통위 | 경제 캘린더 API | 당일 국내 신규 매수 중단 |
| 미국 CPI/PPI | 경제 캘린더 API | 발표 1시간 전후 신규 주문 보류 |
| 개별 종목 어닝 | 공시/외부 데이터 | 당일 해당 종목 신규 매수 금지 |
| VIX > 30 | 실시간 조회 | 미국 포지션 50% 이하 축소 |
| VKOSPI > 25 | 실시간 조회 | 국내 포지션 50% 이하 축소 |

---

## 4. 시스템 아키텍처

### 4.1 전체 레이어

```text
Layer 1  데이터 수집
  - KIS REST / WebSocket
  - pykrx
  - yfinance
  - 경제 캘린더

Layer 2  전략 엔진
  - 듀얼 모멘텀
  - 추세 추종
  - 멀티 팩터
  - 신호 충돌 해소

Layer 3  리스크 관리
  - Vol Parity
  - 손절 / 익절 / Trailing Stop
  - 이벤트 필터
  - 시장 국면 필터
  - 거시적 리밸런싱

Layer 4  주문 실행 및 동기화
  - 토큰 갱신
  - 주문 생성 / 전송
  - 웹소켓 체결 수신
  - 브로커 폴링 재동기화
  - 부분체결 처리
  - 브로커/내부 상태 정합성 관리

Layer 5  모니터링 및 DR
  - Streamlit 대시보드
  - Telegram 알림
  - 세후 성과 리포트
  - 헬스체크
  - 복구 스크립트
```

### 4.2 기술 스택

| 구분 | 기술 | 용도 |
|------|------|------|
| 언어 | Python 3.11+ | 메인 개발 언어 |
| 매매 API | KIS API | 국내/미국 주문 |
| API 통신 | requests, websockets | REST / WebSocket |
| 데이터 수집 | pykrx, yfinance | 과거 시세/재무 |
| 데이터 분석 | pandas, numpy, scipy | 팩터 및 수치 계산 |
| 백테스팅 | vectorbt | 기본 백테스팅 엔진 |
| 스케줄러 | APScheduler | 토큰/점검/리밸런싱 |
| 데이터베이스 | SQLite / InfluxDB | 원장 / 시계열 |
| 대시보드 | Streamlit | 모니터링 |
| 알림 | python-telegram-bot | 이벤트/오류 알림 |

### 4.3 KIS API 환경 분리

| 구분 | 도메인 | 용도 |
|------|--------|------|
| VTS | `openapivts.koreainvestment.com:29443` | 개발 / 테스트 / 페이퍼 트레이딩 |
| PROD | `openapi.koreainvestment.com:9443` | 실전 운용 |
| 실시간 (실전) | `ops.koreainvestment.com:21000` | 실전 시세 |
| 실시간 (모의) | `ops.koreainvestment.com:31000` | 모의 시세 |

---

## 5. 기능 요구사항

## 5.1 데이터 수집 모듈

- [ ] KIS REST로 국내·미국 OHLCV 수집
- [ ] KIS WebSocket으로 실시간 시세 수신
- [ ] `pykrx`로 국내 재무 데이터 수집
- [ ] `yfinance`로 미국 보조 데이터 수집
- [ ] 수정주가 자동 반영
- [ ] 결측치 / 이상치 검증
- [ ] 경제 캘린더 수집 및 저장

## 5.2 KIS API 인증 관리 모듈

- [ ] Access Token 발급 및 만료 시각 저장
- [ ] 매일 08:00 KST 토큰 갱신
- [ ] 갱신 실패 시 3회 재시도 후 Emergency Stop
- [ ] VTS/PROD 토큰 분리
- [ ] 토큰 값 비영속화

운영 기본값:

- runtime 시작 시 token warmup을 1회 즉시 수행합니다.
- 런타임 token refresh는 `in-process` scheduler가 담당합니다.
- startup warmup 또는 정기 refresh가 3회 연속 실패하면 신규 주문을 차단합니다.

## 5.3 전략 엔진 모듈

- [ ] 전략별 표준 인터페이스 구현
- [ ] 팩터 계산 엔진
- [ ] 시장 국면 탐지
- [ ] Walk-Forward 검증
- [ ] 전략 신호 충돌 해소
- [ ] 이벤트 리스크 필터
- [ ] 거시적 리밸런싱 판단 로직

## 5.4 포지션 사이징 모듈

- [ ] Volatility Parity 기반 비중 계산
- [ ] 목표 변동성 연 13% 기준 적용
- [ ] 단일 종목 상한 적용
- [ ] 최소 편입 비중 미만 종목 제외

## 5.5 리스크 관리 모듈

- [ ] 포트폴리오 MDD 계산 및 방어
- [ ] 개별 종목 손절
- [ ] 일일 최대 손실 한도
- [ ] Trailing Stop
- [ ] 현금 버퍼 유지
- [ ] 거시적 리밸런싱 우선순위 반영

## 5.6 주문 실행 및 동기화 모듈

- [ ] 신호 수신 → 포지션 사이징 → 주문 생성 → 브로커 전송 파이프라인
- [ ] 시장가 / 지정가 지원
- [ ] 미체결 재주문 및 최대 재시도 횟수 관리
- [ ] 분할 매수/매도 지원
- [ ] 중복 주문 방지 Lock
- [ ] 부분체결 처리
- [ ] 장 종료 전 미체결 주문 자동 취소

### 브로커 상태 동기화 요구사항

웹소켓 누락 또는 순서 역전 문제를 보완하기 위해 브로커 상태 동기화를 요구합니다.

- [ ] **시장 운영 시간 중 10분 주기 브로커 폴링 수행**
- [ ] 폴링 대상: 미체결 주문, 보유 수량, 가용 현금, 부분체결 누락 여부
- [ ] 웹소켓 이벤트와 폴링 결과가 다르면 정합성 mismatch로 기록
- [ ] mismatch 발생 시 신규 주문을 일시 중단하고 재동기화 수행
- [ ] 브로커 스냅샷을 저장하여 DR 및 추적에 활용

운영 기본값:

- 스케줄러는 `in-process APScheduler`로 구동합니다.
- polling은 시장 세션 인지형으로 동작하며, 현재 세션 우선순위는 `KR -> US`입니다.
- 현재 저장소 기준 KR polling은 `주식일별주문체결조회`를 사용해 브로커 cumulative fill을 `ExecutionFill` delta로 자동 변환한 뒤 `fill_processor`에 반영합니다.
- 현재 저장소 기준 KR VTS polling에서는 `미체결/정정취소가능주문조회` 미지원 응답을 adapter에서 empty `open_orders` snapshot으로 흡수합니다.
- 현재 저장소 기준 US 자동 fill ingestion은 미구현이며, KR 경로만 broker fill auto-sync를 지원합니다.
- polling의 read-only broker query는 rate limit / temporary broker error에 대해 짧은 재시도를 수행한 뒤에만 polling failure로 집계합니다.
- polling 예외는 연속 실패 횟수로 관리하고 3회 연속 실패 시 신규 주문을 차단합니다.
- 장 종료 전 미체결 취소는 국내 `15:25 KST`, 미국 `05:55 KST` 기본값을 사용합니다.
- 국내 장 종료 전 취소는 브로커 주문번호 외에 주문조직번호(`broker_order_orgno`)까지 저장된 주문만 브로커 취소 대상으로 사용합니다.
- Phase 4 KR scheduled auto-trading 기본 가드는 같은 `ticker + strategy` 포지션이 이미 열려 있으면 추가 매수 진입을 금지합니다.
- 동일 종목의 추가 진입은 명시적 피라미딩 정책이 정의되기 전까지 허용하지 않습니다.

### 브로커 응답 정규화 계약

주문 실행 계층은 KIS raw payload를 직접 비즈니스 로직에 노출하지 않고 아래 내부 표준 표면으로 정규화합니다.

- 주문 제출/취소 결과: `accepted`, `broker_order_no`, `broker_order_orgno`, `error_code`, `error_message`
- 미체결 주문 스냅샷: `order_no`, `ticker`, `market`, `side`, `quantity`, `remaining_quantity`, `status`, `price`
- 브로커 포지션 스냅샷: `ticker`, `market`, `quantity`, `avg_cost`, `currency`, `snapshot_at`, `source_env`
- 브로커 polling 스냅샷: `positions`, `open_orders`, `cash_available`

운영 원칙:

- `order_manager`, `reconciliation`은 raw KIS field name에 직접 의존하지 않습니다.
- 국내/미국 필드 차이는 adapter 계층에서 흡수합니다.
- 공식 샘플 기준으로 구현한 항목은 `sample_confirmed`로 관리하고, 실제 VTS payload 확보 후 `confirmed`로 승격합니다.
- 원본 브로커 응답 전문은 민감정보와 계약 변경 리스크 때문에 DB에 저장하지 않습니다.

### 주문 상태 전이 기준

Phase 2 기준 canonical 상태는 아래와 같습니다.

| 상태 | 의미 |
|------|------|
| `pending` | 신호는 생성되었으나 주문 검증 전 |
| `validated` | 리스크 및 제약 검증을 통과한 주문 초안 |
| `submitted` | 브로커 제출 완료, 체결 대기 |
| `partially_filled` | 부분체결 발생, 잔량 존재 |
| `filled` | 전량 체결 완료 |
| `cancel_pending` | 취소 요청 후 브로커 확인 대기 |
| `cancelled` | 취소 완료 |
| `rejected` | 리스크 또는 브로커 거부 |
| `reconcile_hold` | 정합성 mismatch로 신규 진행 중단 상태 |
| `failed` | 복구 불가 오류 상태 |

### Reconciliation 상태 전이 기준

| 상태 | 의미 |
|------|------|
| `idle` | 대기 상태 |
| `scheduled_polling` | 10분 주기 점검 수행 중 |
| `mismatch_detected` | mismatch 탐지 |
| `reconciling` | 복구 또는 fill re-sync 진행 중 |
| `reconciled` | 내부 상태와 브로커 상태 일치 확인 |
| `failed` | 복구 실패 |

운영 기본값:

- mismatch 발생 시 신규 주문 중단 범위는 Phase 2 기본안으로 **계정/환경 단위**로 둡니다.
- 더 작은 범위로 축소하려면 테스트와 운영 문서 근거가 먼저 필요합니다.

### 주문 제출 실패 분류 규칙

주문 제출 실패는 아래 네 가지로 분류합니다.

| 분류 | 기준 | 처리 |
|------|------|------|
| `retryable` | rate limit, timeout, temporary broker error, HTTP `408/409/425/429/500/502/503/504` | submit/cancel write path에서 짧은 자동 재시도 후에도 실패하면 `retry_count` 증가, 재시도 한도 미만이면 `validated` 유지 |
| `terminal` | 잘못된 주문 파라미터, 비재시도 브로커 거절, 일반 `4xx` 오류 | 즉시 `failed` |
| `auth` | token/auth/access 오류, `AuthenticationError` | 즉시 `failed`, 신규 주문 차단 |
| `reconcile_hold` | broker/internal state mismatch, sync required, `ReconciliationError` | 주문을 `reconcile_hold`로 전환하고 reconciliation 수행 |

추가 운영 원칙:

- `auth`, `reconcile_hold`는 `trading_blocked=True`를 유발합니다.
- `reconcile_hold`는 개별 주문 실패가 아니라 정합성 복구 흐름의 시작점으로 간주합니다.
- 주문 제출과 장 종료 전 취소의 broker write path는 retryable rate-limit/temporary 오류에 대해 짧은 in-process 자동 재시도를 수행합니다.
- 재시도 가능 오류도 최대 재시도 횟수 초과 시 `failed`로 종결합니다.

## 5.7 국내 시장 특수 제약 검증

- [ ] 가격 제한폭 검증
- [ ] 동시호가 구간 자동 감지
- [ ] 공매도 방지
- [ ] 단기 과열 종목 제외
- [ ] 최소 1주 단위 주문 수량 처리
- [ ] T+2 결제 자금 반영

## 5.8 모니터링 및 알림 모듈

- [ ] Streamlit 대시보드
- [ ] 텔레그램 알림
- [ ] 일일/월간 세후 성과 리포트
- [ ] 시스템 헬스체크
- [ ] 환율 영향 알림
- [ ] 브로커 상태 mismatch 알림
- [ ] writer queue backlog 알림

헬스체크 기본 요약 기준:

- `normal`
  - scheduler running
  - writer queue 정상
  - token stale 아님
  - polling stale 아님
- `warning`
  - token stale
  - polling stale
  - 마지막 오류 존재
- `critical`
  - `trading_blocked=True`
  - writer queue degraded

운영 계약:

- 헬스체크는 external canonical health 기준을 사용하며 `normal`, `warning`, `critical`만 외부 표면으로 노출합니다.
- 대시보드는 read-only snapshot 계층으로 유지하고 아래 항목을 함께 요약합니다.
  - 현재 저장소 기준 Streamlit dashboard skeleton은 아래 기본 섹션을 이미 제공합니다.
    - health
    - auto-trading diagnostics
    - strategy budget
    - tax summary
    - open orders
    - recent trades
    - reconciliation summary
    - recent system logs
  - open orders
  - recent trades
  - latest portfolio snapshot
  - recent reconciliation summary
  - recent system logs
  - recent manual restore runs
  - recent backtest results
  - blocked / stale / mismatch 운영 상태 요약
- Telegram notifier는 상태 판단을 하지 않고, 상위 계층이 확정한 운영 이벤트를 메시지 포맷/송신만 수행합니다.
- 최소 운영 이벤트 표면:
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

---

## 6. 비기능 요구사항

### 6.1 성능

| 항목 | 목표 |
|------|------|
| 신호 감지 → 주문 요청 수락 | ≤ 200ms |
| 팩터 계산 | ≤ 30초 |
| 실시간 시세 처리 | 초당 50건 이상 |
| KIS API Rate Limit 준수 | 초당 ≤ 20건 |
| 브로커 폴링 처리 | 10분 주기 내 안정 수행 |

### 6.2 안정성

- KIS API 연결 끊김 시 자동 재연결
- 토큰 갱신 실패 시 즉시 알림 및 Emergency Stop
- 브로커 mismatch 발생 시 신규 주문 중단
- 웹소켓 누락이 있어도 폴링으로 최종 상태 복원 가능해야 함
- 일일 백업 및 복원 검증 수행

### 6.3 보안

- API Key, 계좌번호, 비밀번호는 `.env` 또는 안전한 외부 비밀 저장소 사용
- 소스코드에 민감정보 하드코딩 금지
- Access Token은 메모리에만 저장
- 로그 마스킹 적용

### 6.4 유지보수성

- 전략 모듈 플러그인 구조
- 설정값 외부화
- 단위 테스트 커버리지 70% 이상
- 주요 함수 docstring 필수
- SQLite write 경로 단일화

### 6.5 세금 처리 정책

- [ ] FIFO 취득가 방식 적용
- [ ] 미국 주식 기본 공제 추적
- [ ] 월간 리포트에 세금 추산 반영
- [ ] 결제일 환율 기반 세금 추산 가능 구조 확보

Phase 2 범위:

- `order_executions`, `trades`, `position_lots`에 `settlement_date`, `settlement_fx_rate`, `trade_fx_rate`, `fx_rate_source`를 유지합니다.
- 매도 체결 시 `tax_events` 생성은 hook 수준까지 포함할 수 있으며, Phase 3 범위의 세금 기능은 추산 및 리포트까지로 둡니다.
- 최종 신고 정책 완성 및 신고 자동화는 후속 단계로 둡니다.

Phase 3 운영 기준:

- canonical source는 `tax_events`, `trades`, `position_lots`입니다.
- 미국 매도는 `tax_events`를 우선 사용하고, 누락 시 FIFO fallback을 허용합니다.
- KR 거래는 FX `NULL`을 허용합니다.
- FX 우선순위:
  - sell: `settlement_fx_rate -> trade_fx_rate`
  - buy: `buy_settlement_fx_rate -> buy_trade_fx_rate`
- Phase 3의 세금 기능은 추산 및 리포트까지로 제한하며, 신고 자동화는 범위 밖으로 둡니다.
- 현재 저장소 기준 연간 세금 추산은 yearly summary / trade-level report를 JSON 또는 CSV로 export할 수 있습니다.
- 현재 저장소 기준 dashboard는 `TaxCalculator.calculate_yearly_summary()` 결과를 기반으로 연간 tax summary를 화면에 요약할 수 있습니다.

### 6.6 장애 복구 (DR)

- [ ] 장애 감지 시 신규 주문 중단
- [ ] 브로커 포지션과 내부 원장 비교
- [ ] mismatch 복구 지원
- [ ] 브로커 스냅샷 기준 재동기화 지원
- [ ] 복구 후 소액 테스트 주문 검증

Phase 3 운영 기준:

- `restore_portfolio.py`는 기본값을 `dry-run`으로 두는 복구 판단 및 기록 도구로 유지합니다.
- `dry-run`은 내부 원장과 broker snapshot의 차이 계산만 수행하고 DB write를 하지 않습니다.
- `apply`는 `trading_blocked=True` 확인 후에만 수행합니다.
- `apply`는 `manual_restore` reconciliation run, broker snapshot 저장, system log 기록, optional portfolio snapshot upsert까지만 수행합니다.
- direct fill insert, direct order correction, direct lot correction은 허용하지 않습니다.

---

## 7. 개발 로드맵

| 단계 | 기간 | 주요 산출물 | 상태 |
|------|------|-------------|------|
| Phase 1 | 4주 | KIS 연동(VTS), Token 자동 갱신, DB/WAL/Writer Queue, 데이터 수집 | 계획 |
| Phase 2 | 5주 | 전략 구현, 백테스트, 포지션 사이징, 리스크 관리, 브로커 폴링 동기화 | 계획 |
| Phase 3 | 3주 | 대시보드, 알림, 세금 추산, DR 복구 지원, 백테스트 결과 저장/운영 가시성 | 완료 |
| Phase 4 | 4주 | 소액 실전 운용, 자동 복구 정책 정교화, 정합성/성능 보완 | 계획 |
| Phase 5 | 지속 | 전략 최적화, 환헤지 검토, 자본 확대 | 예정 |

---

## 8. 리스크 및 제약사항

| 리스크 | 심각도 | 완화 방안 |
|--------|--------|-----------|
| KIS API 연결 불안정 | 높음 | 자동 재연결 + 헬스체크 |
| 토큰 만료 중 주문 실패 | 높음 | 자동 갱신 + 재시도 + Emergency Stop |
| 과최적화 | 높음 | Walk-Forward 검증 |
| 수정주가 미반영 | 높음 | 일일 데이터 무결성 검증 |
| 슬리피지 과소 추정 | 중간 | 보수적 비용 가정 |
| 환율 리스크 | 중간 | 미국 비중 제한 + 환율 모니터링 |
| 웹소켓 누락 | 높음 | **10분 주기 브로커 폴링 + 정합성 검증** |
| SQLite 잠금 경쟁 | 높음 | **WAL + Single Writer Queue** |
| 이벤트 리스크 미대응 | 중간 | 이벤트 필터 |
| 세금 과소 계산 | 중간 | **결제일 환율 기반 추적 구조** |

---

## 9. 부록

### 9.1 config.yaml 핵심 구조 예시

```yaml
env: vts

allocation:
  domestic: 0.60
  overseas: 0.30
  cash_buffer: 0.10

strategy_weights:
  dual_momentum: 0.30
  trend_following: 0.25
  factor_investing: 0.45

strategies:
  min_position_fraction: 0.01
  event_filter_enabled: true
  dual_momentum:
    lookback_days: 252
    top_n: 10
    rebalance_day_of_month: 1
    absolute_momentum_floor: 0.0
  trend_following:
    ema_fast_period: 20
    ema_slow_period: 60
    atr_period: 14
    rsi_period: 14
    rsi_entry_floor: 30.0
    target_volatility: 0.13
    atr_stop_multiple: 2.0
  factor_investing:
    top_n: 25
    rebalance_months: [1, 4, 7, 10]
    rebalance_day_of_month: 1
    value_weight: 0.25
    quality_weight: 0.25
    momentum_weight: 0.25
    low_vol_weight: 0.25

rebalancing:
  macro_threshold_pct_point: 0.05
  macro_check: monthly_eom
  broker_poll_interval_min: 10

risk:
  max_single_stock_domestic: 0.05
  max_single_stock_overseas: 0.03
  max_sector_weight: 0.25
  stop_loss_domestic: -0.07
  stop_loss_overseas: -0.05
  trailing_stop: -0.10
  daily_max_loss: -0.02
  max_drawdown_limit: -0.15
```

### 9.2 실제 프로젝트 폴더 구조

```text
quantbot-pro/
├── AGENTS.md
├── docs/
│   ├── PRD_v1.4.md
│   └── DB_SCHEMA_v1.2.md
├── core/
├── data/
├── execution/
├── monitor/
└── scripts/
```
