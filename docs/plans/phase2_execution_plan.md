# Phase 2 Execution Plan

## 목적

이 문서는 QuantBot Pro Phase 2 작업의 실행 기준과 진행 상태를 한 곳에서 관리하기 위한 작업 문서입니다.

- 제품/운영 정책 기준: `docs/PRD_v1.4.md`
- 저장 구조/원장/정합성 기준: `docs/DB_SCHEMA_v1.2.md`
- 저장소 구현 규칙 기준: `AGENTS.md`

Phase 2의 우선순위는 실거래 API 완성보다 `테스트 가능한 구조`, `안전한 상태 관리`, `polling/reconciliation 골격`, `원장 일관성`을 먼저 고정하는 것입니다.

## 현재 상태 요약

- 상태: `done`
- 기준 브랜치 가정: `master`
- 구현 원칙:
  - WAL + single writer queue 유지
  - 문서 우선
  - 작은 작업 단위 우선
  - 신규 의존성 추가 금지

## 결정 완료 항목

### 주문 상태

- `pending`
- `validated`
- `submitted`
- `partially_filled`
- `filled`
- `cancel_pending`
- `cancelled`
- `rejected`
- `reconcile_hold`
- `failed`

### Reconciliation 상태

- `idle`
- `scheduled_polling`
- `mismatch_detected`
- `reconciling`
- `reconciled`
- `failed`

### 세금/원장 범위

- Phase 2는 `Minimal Tax Hook`
- `order_executions`, `trades`, `position_lots`에 settlement/FX 필드를 유지
- `tax_events`는 미국 매도 체결의 FIFO/FX 추적 hook까지 포함
- 최종 세금 계산은 Phase 3 이후

### mismatch 차단 정책

- 기본값: `계정/환경 단위 신규 주문 중단`
- 더 작은 차단 범위로 줄이려면 별도 테스트와 운영 근거 필요

## 모듈 책임 기준

### `core.models`

- 공용 도메인 타입
- 상태 enum
- risk/sizing/reconciliation 결과 모델

### `strategy.base`

- 전략 공통 인터페이스

### `strategy.signal_resolver`

- 전략 간 신호 충돌 해소
- 매도 우선
- 손절 우선
- 복수 매수 합산

### `risk.risk_manager`

- 주문 승인/거부 판정
- 손절/일일 손실/MDD/시스템 차단 게이트

### `risk.position_sizer`

- 버킷 비중, 전략 가중치, 현금 버퍼, 종목 상한 반영
- 목표 수량 계산

### `execution.order_manager`

- 신호 저장
- 주문 초안 생성
- 주문 상태 전이 시작점
- broker submit
- polling 진입점
- mismatch 시 hold 전환

### `execution.fill_processor`

- 체결 반영
- `order_executions`, `trades`, `position_lots`, `positions` 원자적 갱신
- US 매도 `tax_events` hook

### `execution.reconciliation`

- polling 결과 비교
- mismatch 분류
- `reconciliation_runs` 기록
- `broker_positions` 스냅샷 저장

## 작업 분할 및 상태

| ID | 작업 | 상태 | 완료 기준 | 검증 |
|---|---|---|---|---|
| P2-00 | 문서 선행 업데이트 | done | PRD/DB 문서에 상태 전이/정책 반영 | 문서 대조 |
| P2-01 | 도메인/설정 표면 고정 | done | 공용 타입, allocation/strategy/risk 설정 모델 반영 | 설정 테스트 |
| P2-02 | 전략 뼈대 + signal_resolver | done | 충돌 해소 규칙 코드/테스트 고정 | resolver 테스트 |
| P2-03 | risk_manager + position_sizer | done | 게이트와 수량 계산 분리 | risk/sizer 테스트 |
| P2-04 | order_manager skeleton | done | 신호 저장, validated/submitted 상태 전이 | execution 테스트 |
| P2-05 | fill_processor skeleton | done | partial/full fill, FIFO, tax hook 반영 | execution 테스트 |
| P2-06 | reconciliation skeleton | done | mismatch 탐지, run log, broker snapshot 저장 | execution 테스트 |
| P2-07 | 핵심 테스트 묶음 | done | strategy/risk/execution 핵심 경로 통과 | `pytest` |
| P2-08 | 브로커 polling adapter 정교화 | done | open orders / positions / cash 응답 표준화 | mock reconciliation 테스트 |
| P2-09 | cancel/retry 흐름 보강 | done | `cancel_pending`, retry_count 동작 고정 | 상태 전이 테스트 |
| P2-10 | signal/order/fill persistence 보강 | done | reject/order/fill 메타데이터 저장 규칙 정리 | DB 테스트 |
| P2-11 | broader verification + 정리 | done | 전체 관련 테스트와 문서 점검 완료 | `pytest tests/ -v` |

## 다음 구현 우선순위

1. 후속 세부 구현 또는 실브로커 adapter 확장 작업 정의

## Phase 2 이후 후속 작업 목록

### F1. 실브로커 KIS adapter 세부 확정

- 국내/미국 주문 응답 필드 매핑 확정
- 취소/정정 응답 표준화
- polling 응답의 국내/미국 계좌별 차이 흡수
- mock payload와 실제 payload 비교 검증

### F2. 운영 스케줄 연결

- 10분 polling 스케줄 실제 연결
- 장 종료 전 미체결 주문 취소 흐름 연결
- 토큰 갱신/헬스체크와 주문 동기화 흐름 연결

### F3. DB/문서 완전 일치 정리

- `event_calendar`, `portfolio_snapshots` 등 미반영 ORM 보강
- 문서 스키마와 실제 ORM 차이 재점검
- 필요 시 migration/초기화 규칙 정리

### F4. 전략/리스크 실구현 확장

- skeleton 위에 실제 전략 계산 로직 연결
- 이벤트 필터/시장 상태 필터 연결
- 거시적 리밸런싱 판단 로직 연결

### F5. Phase 3 준비

- 세금 계산기 본체 구체화
- 모니터링/알림 연계
- DR 복구 흐름 세부화

## 선행 권장 순서

1. `F1. 실브로커 KIS adapter 세부 확정`
2. `F2. 운영 스케줄 연결`
3. `F3. DB/문서 완전 일치 정리`
4. `F4. 전략/리스크 실구현 확장`
5. `F5. Phase 3 준비`

## 가장 먼저 선행되어야 할 작업

`F1. 실브로커 KIS adapter 세부 확정`

이유:

- 현재 Phase 2는 mock 가능한 skeleton 기준으로 안정화되어 있고, 실제 운영으로 넘어가기 전 가장 큰 불확실성은 브로커 응답 표면입니다.
- polling, cancel/retry, reconciliation, 주문 상태 전이는 모두 실제 KIS 응답 필드가 확정되어야 안전하게 닫힙니다.
- 이 작업이 먼저 끝나야 이후 `운영 스케줄 연결`이나 `전략/리스크 실구현 확장`이 잘못된 broker contract 위에 쌓이지 않습니다.

## F1 실행 체크리스트

| ID | 작업 | 상태 | 완료 기준 |
|---|---|---|---|
| F1-01 | KIS 대상 API 목록 확정 | done | 주문, 취소/정정, 미체결, 잔고, 주문가능금액 API 대상 확정 |
| F1-02 | 응답 필드 매핑표 작성 | done | raw field -> 내부 표준 필드 표 작성 |
| F1-03 | 표준 결과 모델 확정 | done | `BrokerOrderSnapshot`, `BrokerPositionSnapshot`, `BrokerPollingSnapshot` 확정 |
| F1-04 | 국내 주문 응답 정규화 구현 | done | submit/cancel 결과를 공통 모델로 변환 |
| F1-05 | 미국 주문 응답 정규화 구현 | done | submit/cancel 결과를 공통 모델로 변환 |
| F1-06 | 국내 polling 응답 정규화 구현 | done | positions/open orders/cash -> polling snapshot |
| F1-07 | 미국 polling 응답 정규화 구현 | done | positions/open orders/cash -> polling snapshot |
| F1-08 | order_manager raw 응답 의존 제거 | done | 주문 모듈이 raw payload parsing 제거 |
| F1-09 | reconciliation raw 응답 의존 제거 | todo | 정규화 snapshot만 사용 |
| F1-10 | 실패/재시도 분류 규칙 고정 | todo | retryable/terminal/auth/reconcile_hold 분류 고정 |
| F1-11 | adapter 전용 테스트 추가 | done | 국내/미국 submit/cancel/polling 테스트 추가 |
| F1-12 | 문서 업데이트 | todo | PRD/DB/plan 문서에 정규화 계약 반영 |

## F1 응답 필드 매핑표

기준:

- 공식 문서: `apiportal.koreainvestment.com` API 목록
- 공식 샘플: `github.com/koreainvestment/open-trading-api` 및 개발자센터에서 안내하는 공식 샘플 코드
- 아래 표에서 국내 필드는 공식 샘플에서 반복적으로 확인되는 필드 기준입니다.
- 미국 필드 중 일부는 공식 샘플 기준 1차 초안이며, 실제 VTS payload로 재확인이 필요합니다.

상태 값 기준:

- `confirmed`: 공식 문서와 실제 응답으로 확인 완료
- `sample_confirmed`: 공식 샘플/기존 응답 예시 기준 확인
- `needs_vts_confirmation`: 실제 VTS 응답 확인 필요

### 1. 국내 주문 제출 / 취소 응답

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `accepted` | `rt_cd` | `rt_cd == "0"` | 없음 | `sample_confirmed` | 성공 여부 |
| `broker_order_no` | `output.ODNO` | `str(...)` | `output.order_no` | `sample_confirmed` | 주문번호 |
| `error_code` | `msg_cd` | `str(...)` | 없음 | `sample_confirmed` | 실패 시 |
| `error_message` | `msg1` | `str(...)` | 없음 | `sample_confirmed` | 실패 시 |

예시 payload:

```json
{
  "rt_cd": "0",
  "msg_cd": "APBK0013",
  "msg1": "정상처리 되었습니다.",
  "output": {
    "ODNO": "..."
  }
}
```

### 2. 국내 미체결 / 정정취소가능주문조회

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `order_no` | `ODNO` | `str(...)` | 없음 | `sample_confirmed` | 주문번호 |
| `ticker` | `PDNO` | `str(...)` | 없음 | `sample_confirmed` | 종목코드 |
| `market` | 고정값 | `"KR"` | 없음 | `confirmed` | 국내 |
| `side` | `SLL_BUY_DVSN_CD` | 코드 매핑 | `sll_buy_dvsn_cd` | `sample_confirmed` | 매수/매도 |
| `quantity` | `ORD_QTY` | `int(...)` | `ord_qty` | `sample_confirmed` | 주문수량 |
| `remaining_quantity` | `ORD_PSBL_QTY` | `int(...)` | `ord_psbl_qty` | `sample_confirmed` | 잔여/정정취소 가능 수량 |
| `price` | `ORD_UNPR` | `float(...)` | `ord_unpr` | `sample_confirmed` | 주문단가 |
| `status` | 상태 코드 필드 | 코드 매핑 | 없음 | `sample_confirmed` | 상태값 |

코드값 해석:

| 의미 | raw 값 | 내부 값 | 상태 | 비고 |
|---|---|---|---|---|
| 매수 | `02` 또는 샘플 매수 코드 | `buy` | `sample_confirmed` | 실제 값은 VTS로 재확인 권장 |
| 매도 | `01` 또는 샘플 매도 코드 | `sell` | `sample_confirmed` | 실제 값은 VTS로 재확인 권장 |

### 3. 국내 잔고조회

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `ticker` | `pdno` | `str(...)` | 없음 | `sample_confirmed` | 종목코드 |
| `quantity` | `hldg_qty` | `int(...)` | 없음 | `sample_confirmed` | 보유수량 |
| `avg_cost` | `pchs_avg_pric` | `float(...)` | 없음 | `sample_confirmed` | 평균단가 |
| `currency` | 고정값 | `"KRW"` | 없음 | `confirmed` | 국내 |
| `market` | 고정값 | `"KR"` | 없음 | `confirmed` | 국내 |

예시 payload:

```json
{
  "output1": [
    {
      "pdno": "005930",
      "hldg_qty": "10",
      "pchs_avg_pric": "70000"
    }
  ]
}
```

### 4. 국내 주문가능금액조회

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `cash_available` | `output.ord_psbl_cash` | `float(...)` | 없음 | `sample_confirmed` | 주문가능현금 |
| `error_code` | `msg_cd` | `str(...)` | 없음 | `sample_confirmed` | 실패 시 |
| `error_message` | `msg1` | `str(...)` | 없음 | `sample_confirmed` | 실패 시 |

### 5. 미국 주문 제출 / 취소 응답

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `accepted` | `rt_cd` | `rt_cd == "0"` | 없음 | `sample_confirmed` | 성공 여부 |
| `broker_order_no` | `output.ODNO` | `str(...)` | 별도 해외 주문번호 필드 | `sample_confirmed` | 공식 샘플 기준 채택 |
| `error_code` | `msg_cd` | `str(...)` | 없음 | `sample_confirmed` | 실패 시 |
| `error_message` | `msg1` | `str(...)` | 없음 | `sample_confirmed` | 실패 시 |

### 6. 미국 미체결내역

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `order_no` | `odno` | `str(...)` | `ODNO` | `sample_confirmed` | 주문번호 |
| `ticker` | `ovrs_pdno` | `str(...)` | `pdno` | `sample_confirmed` | 종목코드 후보 |
| `market` | `ovrs_excg_cd` | 거래소 코드 매핑 | 없음 | `sample_confirmed` | NASD/NYSE 등 |
| `side` | 매수/매도 구분 코드 | 코드 매핑 | 없음 | `sample_confirmed` | 샘플 기준 |
| `quantity` | `ord_qty` | `int(...)` | 없음 | `sample_confirmed` | 주문수량 |
| `remaining_quantity` | `nccs_qty` 또는 미체결수량 필드 | `int(...)` | 없음 | `sample_confirmed` | 샘플 기준 |
| `price` | `ovrs_ord_unpr` | `float(...)` | 없음 | `sample_confirmed` | 주문단가 |
| `status` | 상태 코드 필드 | 코드 매핑 | 없음 | `sample_confirmed` | 샘플 기준 |

코드값 해석:

| 의미 | raw 값 | 내부 값 | 상태 | 비고 |
|---|---|---|---|---|
| NASDAQ | `NASD` | `US` + exchange metadata | `sample_confirmed` | 거래소 보관 여부 별도 결정 |
| NYSE | `NYSE` | `US` + exchange metadata | `sample_confirmed` | 거래소 보관 여부 별도 결정 |
| AMEX | `AMEX` | `US` + exchange metadata | `sample_confirmed` | 거래소 보관 여부 별도 결정 |

### 7. 미국 잔고

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `ticker` | `ovrs_pdno` | `str(...)` | 없음 | `sample_confirmed` | 해외 종목코드 |
| `quantity` | `ovrs_cblc_qty` | `int(...)` | 없음 | `sample_confirmed` | 보유수량 |
| `avg_cost` | `ovrs_pchs_avg_pric` 또는 평균단가 필드 | `float(...)` | `ovrs_now_pric1` | `sample_confirmed` | 샘플 기준 fallback 허용 |
| `currency` | `crcy_cd` 또는 통화 필드 | `str(...)` | `"USD"` | `sample_confirmed` | 샘플 기준 |
| `market` | `OVRS_EXCG_CD` | 거래소 코드 매핑 | 없음 | `sample_confirmed` | NASD/NYSE 등 |

예시 payload:

```json
{
  "output1": [
    {
      "ovrs_pdno": "AAPL",
      "ovrs_cblc_qty": "5"
    }
  ]
}
```

### 8. 미국 주문가능금액조회

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `cash_available` | `ovrs_ord_psbl_amt` 또는 주문가능금액 필드 | `float(...)` | `frcr_ord_psbl_amt1` | `sample_confirmed` | 샘플 기준 |
| `fx_reference` | 환율 관련 필드 | `float(...)` | 없음 | `sample_confirmed` | 선택 보관 |
| `error_code` | `msg_cd` | `str(...)` | 없음 | `sample_confirmed` | 실패 시 |
| `error_message` | `msg1` | `str(...)` | 없음 | `sample_confirmed` | 실패 시 |

## F1 미확정 항목 목록

| API | 항목 | 현재 가정 | 확인 필요 이유 |
|---|---|---|---|
| 미국 주문응답 | `broker_order_no` | `output.ODNO` 또는 별도 필드 | 샘플 채택 완료, 실 VTS 교차확인 권장 |
| 미국 미체결 | `status` | 상태 코드 필드 존재 가정 | 샘플 채택 완료, retry/cancel 분기 재확인 권장 |
| 미국 미체결 | `remaining_quantity` | `nccs_qty` 우선 사용 | 샘플 채택 완료, reconciliation 재확인 권장 |
| 미국 잔고 | `avg_cost` | `ovrs_pchs_avg_pric` 우선, fallback 허용 | 샘플 채택 완료, 평균단가 정확성 재확인 권장 |
| 미국 주문가능금액 | `cash_available` | `ovrs_ord_psbl_amt` 우선 사용 | 샘플 채택 완료, sizing 정확성 재확인 권장 |

## F1 확인 필요 payload

- [ ] 국내 주문 성공 응답
- [ ] 국내 주문 실패 응답
- [ ] 국내 취소 성공/실패 응답
- [ ] 국내 미체결 조회 응답
- [ ] 국내 잔고 조회 응답
- [ ] 국내 주문가능금액 조회 응답
- [ ] 미국 주문 성공 응답
- [ ] 미국 주문 실패 응답
- [ ] 미국 취소 성공/실패 응답
- [ ] 미국 미체결 조회 응답
- [ ] 미국 잔고 조회 응답
- [ ] 미국 주문가능금액 조회 응답

## F1-02 후속 메모

- 국내 `ord_psbl_cash`, `pdno`, `hldg_qty`, `pchs_avg_pric`, `ODNO`, `PDNO`, `ORD_QTY`, `ORD_PSBL_QTY`, `ORD_UNPR`는 공식 샘플과 개발자센터 구조에서 우선 채택 가능합니다.
- 미국 `ovrs_pdno`, `ovrs_cblc_qty`는 공식 샘플 기준으로 1차 채택 가능합니다.
- 미국 주문번호, 평균단가, 주문상태, 주문가능금액 세부 필드는 실제 VTS 응답 캡처 또는 개발자센터 상세 필드표 확인이 필요합니다.
- 따라서 F1의 다음 직접 작업은 `reconciliation raw 응답 의존 제거`와 `실 VTS payload 확보 후 sample_confirmed -> confirmed 승격`입니다.

## 현재 검증 기준

작은 검증부터 수행:

1. `python -m compileall core data execution strategy risk tests main.py`
2. `python -m pytest tests\test_strategy tests\test_risk tests\test_execution -q`
3. 필요 시 `python -m pytest tests\ -v`

## 구현 메모

- `AGENTS.md`는 현재 사용자 변경이 있어 별도 수정하지 않음
- `config/config.yaml`은 Phase 2 필요 최소 설정만 반영
- DB 스키마 전체 완전 일치는 아직 아님
  - Phase 2 직접 경로에 필요한 `tax_events`만 우선 ORM 반영
- 실브로커 연동보다 mock 가능한 execution 흐름 유지가 우선

## 업데이트 규칙

이 문서는 Phase 2 진행 중 계속 갱신합니다.

- 작업 시작 시: 해당 ID 상태를 `in_progress`로 변경
- 작업 완료 시: 상태를 `done`으로 변경
- 범위 변경 시: 본 문서와 관련 PRD/DB 문서를 함께 갱신
- 검증 명령이 바뀌면 본 문서의 검증 기준도 같이 수정
