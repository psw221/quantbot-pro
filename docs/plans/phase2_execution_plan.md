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
| F1-09 | reconciliation raw 응답 의존 제거 | done | 정규화 snapshot만 사용 |
| F1-10 | 실패/재시도 분류 규칙 고정 | done | retryable/terminal/auth/reconcile_hold 분류 고정 |
| F1-11 | adapter 전용 테스트 추가 | done | 국내/미국 submit/cancel/polling 테스트 추가 |
| F1-12 | 문서 업데이트 | done | PRD/DB/plan 문서에 정규화 계약 반영 |

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

### 실 VTS 검증 메모

- 검증 일시: `2026-04-15 KST`
- 검증 방식: VTS `read-only` 조회 API 실호출, 민감정보 비출력, 필드명만 확인
- 확인 완료:
  - 국내 잔고조회 `inquire-balance`
  - 국내 주문가능금액조회 `inquire-psbl-order`
  - 미국 잔고조회 `inquire-balance`
  - 미국 주문가능금액조회 `inquire-psamount`
- 검증 중 확인된 제약:
  - 접근토큰 발급은 `1분당 1회` 제한으로 `EGW00133`이 발생할 수 있음
  - 국내 미체결/정정취소가능주문조회는 VTS에서 `모의투자에서는 해당업무가 제공되지 않습니다` 응답을 반환
  - 미국 잔고조회는 실제 응답 확보에 성공했으나, `output1`이 비어 있어 보유 종목 row 필드는 확인하지 못함
  - 국내 주문 실검증 시 현재 계좌는 `40910000 / 모의투자 주문이 불가한 계좌입니다.` 응답을 반환하여 성공 주문 payload 확보가 불가했음
  - 계좌 교체 후 국내 주문 성공 응답을 추가 확보했고, `output`에 `KRX_FWDG_ORD_ORGNO`, `ODNO`, `ORD_TMD`가 포함됨을 확인함
  - 낮은 지정가 주문 후 즉시 취소로 국내 취소 성공 응답도 확보했고, 취소 응답 `output` 역시 `KRX_FWDG_ORD_ORGNO`, `ODNO`, `ORD_TMD`를 포함함을 확인함
  - 미국 주문 실검증에서 `VTTT1002U`는 모의투자 매수 주문 TR 로 동작했으나 `2026-04-15 14:xx KST` 기준 `40570000 / 모의투자 장시작전 입니다.` 응답을 반환함
  - 미국 주문 실검증에서 `JTTT1002U`는 `EGW2004 / 모의투자 TR 이 아닙니다.` 응답을 반환함
  - 미국 매도 후보 `VTTT1006U`는 `90000000 / 모의투자에서는 해당업무가 제공되지 않습니다.` 응답을 반환함

### 1. 국내 주문 제출 / 취소 응답

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `accepted` | `rt_cd` | `rt_cd == "0"` | 없음 | `confirmed` | 실제 VTS 주문 성공/취소 성공 응답 확인 |
| `broker_order_no` | `output.ODNO` | `str(...)` | `output.order_no` | `confirmed` | 실제 VTS 주문 성공/취소 성공 응답 확인 |
| `error_code` | `msg_cd` | `str(...)` | 없음 | `confirmed` | 실제 VTS 주문 성공/취소 성공 응답 확인 |
| `error_message` | `msg1` | `str(...)` | 없음 | `confirmed` | 실제 VTS 주문 성공/취소 성공 응답 확인 |

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
| `side` | `SLL_BUY_DVSN_CD` | 코드 매핑 | `sll_buy_dvsn_cd` | `sample_confirmed` | VTS mock 미지원으로 실데이터 미확인 |
| `quantity` | `ORD_QTY` | `int(...)` | `ord_qty` | `sample_confirmed` | VTS mock 미지원으로 실데이터 미확인 |
| `remaining_quantity` | `ORD_PSBL_QTY` | `int(...)` | `ord_psbl_qty` | `sample_confirmed` | VTS mock 미지원으로 실데이터 미확인 |
| `price` | `ORD_UNPR` | `float(...)` | `ord_unpr` | `sample_confirmed` | VTS mock 미지원으로 실데이터 미확인 |
| `status` | 상태 코드 필드 | 코드 매핑 | 없음 | `sample_confirmed` | VTS mock 미지원으로 실데이터 미확인 |

코드값 해석:

| 의미 | raw 값 | 내부 값 | 상태 | 비고 |
|---|---|---|---|---|
| 매수 | `02` 또는 샘플 매수 코드 | `buy` | `sample_confirmed` | 실제 값은 VTS로 재확인 권장 |
| 매도 | `01` 또는 샘플 매도 코드 | `sell` | `sample_confirmed` | 실제 값은 VTS로 재확인 권장 |

### 3. 국내 잔고조회

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `ticker` | `pdno` | `str(...)` | 없음 | `confirmed` | 실제 VTS `output1` row 확인 |
| `quantity` | `hldg_qty` | `int(...)` | 없음 | `confirmed` | 실제 VTS `output1` row 확인 |
| `avg_cost` | `pchs_avg_pric` | `float(...)` | 없음 | `confirmed` | 실제 VTS `output1` row 확인 |
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
| `cash_available` | `output.ord_psbl_cash` | `float(...)` | 없음 | `confirmed` | 실제 VTS `output` 확인 |
| `error_code` | `msg_cd` | `str(...)` | 없음 | `confirmed` | 실제 VTS 성공 응답 필드 확인 |
| `error_message` | `msg1` | `str(...)` | 없음 | `confirmed` | 실제 VTS 성공 응답 필드 확인 |

### 5. 미국 주문 제출 / 취소 응답

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `accepted` | `rt_cd` | `rt_cd == "0"` | 없음 | `sample_confirmed` | 성공 여부 |
| `broker_order_no` | `output.ODNO` | `str(...)` | 별도 해외 주문번호 필드 | `sample_confirmed` | 공식 샘플 기준 채택 |
| `error_code` | `msg_cd` | `str(...)` | 없음 | `sample_confirmed` | 실패 시, 주문 응답 실VTS 미확보 |
| `error_message` | `msg1` | `str(...)` | 없음 | `sample_confirmed` | 실패 시, 주문 응답 실VTS 미확보 |

### 6. 미국 미체결내역

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `order_no` | `odno` | `str(...)` | `ODNO` | `sample_confirmed` | 주문번호 |
| `ticker` | `ovrs_pdno` | `str(...)` | `pdno` | `sample_confirmed` | 종목코드 후보 |
| `market` | `ovrs_excg_cd` | 거래소 코드 매핑 | 없음 | `sample_confirmed` | NASD/NYSE 등, 실주문 미체결 응답 미확보 |
| `side` | 매수/매도 구분 코드 | 코드 매핑 | 없음 | `sample_confirmed` | 실주문 미체결 응답 미확보 |
| `quantity` | `ord_qty` | `int(...)` | 없음 | `sample_confirmed` | 주문수량 |
| `remaining_quantity` | `nccs_qty` 또는 미체결수량 필드 | `int(...)` | 없음 | `sample_confirmed` | 실주문 미체결 응답 미확보 |
| `price` | `ovrs_ord_unpr` | `float(...)` | 없음 | `sample_confirmed` | 실주문 미체결 응답 미확보 |
| `status` | 상태 코드 필드 | 코드 매핑 | 없음 | `sample_confirmed` | 실주문 미체결 응답 미확보 |

코드값 해석:

| 의미 | raw 값 | 내부 값 | 상태 | 비고 |
|---|---|---|---|---|
| NASDAQ | `NASD` | `US` + exchange metadata | `sample_confirmed` | 거래소 보관 여부 별도 결정 |
| NYSE | `NYSE` | `US` + exchange metadata | `sample_confirmed` | 거래소 보관 여부 별도 결정 |
| AMEX | `AMEX` | `US` + exchange metadata | `sample_confirmed` | 거래소 보관 여부 별도 결정 |

### 7. 미국 잔고

| 내부 필드 | raw field | 변환 규칙 | fallback | 상태 | 비고 |
|---|---|---|---|---|---|
| `ticker` | `ovrs_pdno` | `str(...)` | 없음 | `sample_confirmed` | 실제 VTS 응답은 확보했지만 `output1` empty |
| `quantity` | `ovrs_cblc_qty` | `int(...)` | 없음 | `sample_confirmed` | 실제 VTS 응답은 확보했지만 `output1` empty |
| `avg_cost` | `ovrs_pchs_avg_pric` 또는 평균단가 필드 | `float(...)` | `ovrs_now_pric1` | `sample_confirmed` | 실제 VTS 응답은 확보했지만 `output1` empty |
| `currency` | `crcy_cd` 또는 통화 필드 | `str(...)` | `"USD"` | `sample_confirmed` | 실제 VTS 응답은 확보했지만 `output1` empty |
| `market` | `OVRS_EXCG_CD` | 거래소 코드 매핑 | 없음 | `sample_confirmed` | 실제 VTS 응답은 확보했지만 `output1` empty |

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
| `cash_available` | `ovrs_ord_psbl_amt` | `float(...)` | `frcr_ord_psbl_amt1` | `confirmed` | 실제 VTS `output` 확인 |
| `fx_reference` | `exrt` | `float(...)` | 없음 | `confirmed` | 실제 VTS `output` 확인 |
| `error_code` | `msg_cd` | `str(...)` | 없음 | `confirmed` | 실제 VTS 성공 응답 필드 확인 |
| `error_message` | `msg1` | `str(...)` | 없음 | `confirmed` | 실제 VTS 성공 응답 필드 확인 |

## F1 미확정 항목 목록

| API | 항목 | 현재 가정 | 확인 필요 이유 |
|---|---|---|---|
| 미국 주문응답 | `broker_order_no` | `output.ODNO` 또는 별도 필드 | 샘플 채택 완료, 실 VTS 교차확인 권장 |
| 미국 미체결 | `status` | 상태 코드 필드 존재 가정 | 샘플 채택 완료, retry/cancel 분기 재확인 권장 |
| 미국 미체결 | `remaining_quantity` | `nccs_qty` 우선 사용 | 샘플 채택 완료, reconciliation 재확인 권장 |
| 미국 잔고 | `avg_cost` | `ovrs_pchs_avg_pric` 우선, fallback 허용 | 샘플 채택 완료, 평균단가 정확성 재확인 권장 |
| 국내 미체결 | `side`, `quantity`, `remaining_quantity`, `price`, `status` | `inquire-psbl-rvsecncl` row 필드 사용 | VTS mock 미지원으로 실데이터 확인 불가 |
| 미국 주문 TR | `VTTT1002U` | 모의투자 미국 매수 주문 TR 후보 | 실검증 결과 장시작전 응답까지 확인, 장중 재검증 필요 |

## F1 확인 필요 payload

- [ ] 국내 주문 성공 응답
- [ ] 국내 주문 실패 응답
- [x] 국내 취소 성공/실패 응답
- [x] 국내 미체결 조회 응답
- [x] 국내 잔고 조회 응답
- [x] 국내 주문가능금액 조회 응답
- [ ] 미국 주문 성공 응답
- [ ] 미국 주문 실패 응답
- [ ] 미국 취소 성공/실패 응답
- [ ] 미국 미체결 조회 응답
- [x] 미국 잔고 조회 응답
- [x] 미국 주문가능금액 조회 응답

## F1-02 후속 메모

- 국내 `ord_psbl_cash`, `pdno`, `hldg_qty`, `pchs_avg_pric`, `ODNO`, `PDNO`, `ORD_QTY`, `ORD_PSBL_QTY`, `ORD_UNPR`는 공식 샘플과 개발자센터 구조에서 우선 채택 가능합니다.
- 미국 `ovrs_pdno`, `ovrs_cblc_qty`는 공식 샘플 기준으로 1차 채택 가능합니다.
- 미국 주문번호, 평균단가, 주문상태, 주문가능금액 세부 필드는 실제 VTS 응답 캡처 또는 개발자센터 상세 필드표 확인이 필요합니다.
- `2026-04-15` 실 VTS 검증으로 국내 잔고/국내 주문가능금액/미국 주문가능금액은 `confirmed`로 승격했습니다.
- `2026-04-15` 실 VTS 주문 성공 응답 검증으로 국내 주문 제출 응답(`rt_cd`, `msg_cd`, `msg1`, `output.ODNO`)을 `confirmed`로 승격했습니다.
- `2026-04-15` 낮은 지정가 주문 후 즉시 취소 검증으로 국내 취소 응답(`rt_cd`, `msg_cd`, `msg1`, `output.ODNO`)도 `confirmed` 범위로 포함했습니다.
- 미국 주문은 `VTTT1002U`가 모의투자 매수 TR 후보로 가장 유력하며, 현재는 장시작전 응답까지만 확인했습니다.
- 국내 미체결은 VTS mock 미지원, 미국 잔고 row 필드는 무보유 계좌로 인해 `output1` empty여서 추가 승격은 보류합니다.
- 국내 주문 실검증은 시도했으나 현재 계좌가 모의투자 주문 가능 계좌가 아니어서 성공/실패 주문 payload를 추가 확보하지 못했습니다.

## F1-10 실패/재시도 분류 규칙

- `retryable`
  - 기준: rate limit, timeout, temporary broker error, HTTP `408/409/425/429/500/502/503/504`
  - 처리: `retry_count` 증가, `max_submit_retries` 미만이면 `validated` 유지, 초과 시 `failed`
- `terminal`
  - 기준: 잘못된 주문 파라미터, 비재시도 브로커 거절, HTTP `4xx` 일반 오류
  - 처리: 즉시 `failed`
- `auth`
  - 기준: token/auth/access 관련 오류 또는 `AuthenticationError`
  - 처리: 즉시 `failed`, `trading_blocked=True`
- `reconcile_hold`
  - 기준: broker/internal state mismatch, sync required, `ReconciliationError`
  - 처리: 주문을 `reconcile_hold`로 전환, `trading_blocked=True`, reconciliation run 기록

## F2 운영 스케줄 연결 계획

### F2 목표

- `in-process APScheduler` 기반 런타임으로 운영 스케줄을 연결합니다.
- 범위는 `KR+US session-aware` 기준으로 고정합니다.
- F2는 실운영 연결보다 `안전한 orchestration`, `상태 관리`, `테스트 가능 구조`를 우선합니다.

### F2 기본 정책

- 스케줄러는 프로세스 내부에서 상시 구동합니다.
- `main.py`는 단순 bootstrap에서 runtime entrypoint로 확장합니다.
- 국내/미국 시장 세션을 각각 인지해 polling/cancel job 실행 여부를 결정합니다.
- token은 `startup warmup 1회 + 매일 08:00 KST refresh + 필요 시 on-demand refresh`로 운용합니다.
- polling은 `broker_poll_interval_min` 기준으로 수행합니다.
- 장 종료 전 미체결 취소는 시장 close 5분 전 기본값으로 둡니다.
- 스케줄러는 직접 DB write 하지 않고 기존 `TokenManager`, `OrderManager`, `ReconciliationService`, `WriterQueue`만 통해 동작합니다.
- `auth`, `reconcile_hold`, `writer_queue degraded` 상태에서는 신규 주문 차단을 유지합니다.

### F2 런타임 책임 경계

- runtime/scheduler 계층
  - job 등록
  - job 실행 순서 orchestration
  - runtime blocked/degraded/last_success 상태 관리
- `TokenManager`
  - token refresh/invalidate만 담당
- `OrderManager`
  - 취소 요청, 주문 차단, 주문 상태 전이만 담당
- `ReconciliationService`
  - polling snapshot 비교와 mismatch 기록만 담당
- healthcheck 표면
  - writer queue, token freshness, 최근 polling 성공 시각, blocked 상태 집계

### F2 대상 job

- `token_refresh_job`
  - startup 즉시 1회
  - 매일 `08:00 KST`
  - 실패 시 3회 재시도 후 `trading_blocked=True`
- `broker_poll_job`
  - 시장 세션 중에만 실행
  - token 확보 -> broker payload 조회 -> polling snapshot 정규화 -> reconciliation 수행
  - mismatch 시 주문 차단 유지
- `pre_close_cancel_job`
  - 국내 `15:25 KST`
  - 미국 `05:55 KST`
  - `submitted`, `partially_filled` 주문만 취소 요청
- `healthcheck_job`
  - 1분 주기
  - writer queue health, token freshness, 최근 polling 성공 시각, blocked 상태 점검

### F2 실행 체크리스트

| ID | 작업 | 상태 | 완료 기준 |
|---|---|---|---|
| F2-01 | 문서 선행 업데이트 | done | F2 정책과 체크리스트가 plan 문서에 고정 |
| F2-02 | 런타임 상태 모델 및 세션 판정 유틸 | done | KR/US session/pre-close 판정 함수와 runtime state 모델 확정 |
| F2-03 | APScheduler runtime 오케스트레이터 추가 | done | runtime start/stop/run_forever와 job registration 구현 |
| F2-04 | token refresh orchestration 연결 | done | startup warmup, 08:00 refresh, 실패 재시도 및 blocked 전환 구현 |
| F2-05 | polling/reconciliation orchestration 연결 | done | polling snapshot 조회/정규화/reconciliation 흐름 연결 |
| F2-06 | pre-close cancel 흐름 연결 | done | 장 종료 전 미체결 취소 orchestration 구현 |
| F2-07 | healthcheck 표면 추가 | done | runtime health snapshot 집계 가능 |
| F2-08 | broader verification + 문서 마감 | done | 관련 테스트 및 PRD/AGENTS/plan 정리 완료 |

### F2 공개 표면 초안

- `TradingRuntime.start()`
- `TradingRuntime.stop()`
- `TradingRuntime.run_forever()`
- `TradingRuntime.health_snapshot()`
- `is_market_session_open(market, now_kst)`
- `is_pre_close_window(market, now_kst, minutes_before_close=5)`
- `get_market_session_window(market, now_kst)`
- `RuntimeState`

### F2-03 구현 메모

- APScheduler는 `requirements.txt`에 명시적으로 추가합니다.
- `TradingRuntime`는 F2-03 단계에서 job registration과 lifecycle만 구현합니다.
- 실제 token refresh, polling, pre-close cancel orchestration body는 각각 `F2-04`, `F2-05`, `F2-06`에서 연결합니다.
- 현재 등록 job ID:
  - `token_refresh`
  - `broker_poll`
  - `pre_close_cancel_kr`
  - `pre_close_cancel_us`
  - `healthcheck`

### F2-04 구현 메모

- startup 시 `token_refresh_job`를 즉시 1회 실행합니다.
- `token_manager`가 없는 런타임은 token job을 no-op로 처리합니다.
- token refresh는 단일 job 안에서 최대 3회 재시도합니다.
- refresh 성공 시 `last_token_refresh_at`를 갱신하고 `trading_blocked`를 해제합니다.
- 3회 모두 실패하면 `trading_blocked=True`, `last_error`를 기록합니다.

### F2-05 구현 메모

- polling job은 세션이 열린 시장이 있을 때만 실행합니다.
- 현재 세션 선택 우선순위는 `KR -> US`입니다.
- polling 순서는 `get_valid_token -> account/open_orders/cash 조회 -> build_polling_snapshot -> reconcile_snapshot`으로 고정합니다.
- mismatch 시 `order_manager.flag_reconciliation_hold()`를 호출하고 `trading_blocked=True`를 유지합니다.
- polling 예외는 `consecutive_poll_failures`를 증가시키고 3회 연속 실패 시 `trading_blocked=True`로 전환합니다.
- 성공한 polling은 `last_poll_success_at`를 갱신하고 연속 실패 수를 0으로 초기화합니다.

### F2-06 구현 메모

- pre-close cancel job은 해당 시장의 pre-close window 안에서만 실행합니다.
- 취소 대상은 `submitted`, `partially_filled` 상태이면서 `kis_order_no`가 있는 주문으로 한정합니다.
- 취소 요청 순서는 `cancel_order -> normalize_cancel_result -> request_cancel -> confirm_cancel`로 고정합니다.
- `filled`, `cancelled`, `cancel_pending`, `failed` 주문은 취소 대상에서 제외합니다.
- pre-close cancel 실패는 `last_error`만 갱신하고 runtime 즉시 종료나 신규 추가 차단 전이는 만들지 않습니다.

### F2-07 구현 메모

- `monitor/healthcheck.py`는 `TradingRuntime.health_snapshot()` 위에 얇은 집계 계층으로 구현합니다.
- healthcheck는 `normal`, `warning`, `critical` 세 단계로 요약합니다.
- `critical`
  - `trading_blocked=True`
  - writer queue degraded
- `warning`
  - token stale
  - polling stale
  - 마지막 오류 존재
- 기본 stale 기준
  - token: 24시간
  - polling: 20분

### F2-08 마감 메모

- broader verification 기준:
  - `python -m compileall core data execution strategy risk monitor tests main.py`
  - `python -m pytest tests\\ -v`
- 문서 마감 반영:
  - `docs/PRD_v1.4.md`에 runtime scheduler, startup warmup, pre-close cancel, healthcheck 기본값 반영
  - `AGENTS.md`에 `execution/runtime.py`, `monitor/healthcheck.py` 책임과 운영 스케줄 기본값 반영
- F2 후속 직접 작업은 `F1 실VTS payload 확보 후 sample_confirmed -> confirmed 승격`과 `F3 DB/문서 완전 일치 정리`입니다.

### F2 실패 처리 기본 규칙

- token refresh 실패
  - 단일 job 내 3회 재시도
  - 계속 실패 시 `trading_blocked=True`
- polling 실패
  - 단발 실패는 기록 후 다음 주기 재시도
  - 연속 3회 실패 시 `trading_blocked=True`
- pre-close cancel 실패
  - 개별 주문 실패는 기존 submit/cancel 분류 규칙을 따름
  - 전체 job 실패는 기록하되 runtime 즉시 종료는 하지 않음
- writer queue degraded
  - 신규 write성 job 중단
  - health 상태를 `critical`로 올림

### F2 테스트 기준

- 세션 판정
  - KR 장중/장전/장후
  - US 장중/장전/장후
  - pre-close 5분 창
- runtime orchestration
  - scheduler start/stop
  - startup warmup 1회 실행
  - closed session polling skip
- token job
  - refresh 성공
  - 일시 실패 후 성공
  - 3회 실패 후 blocked
- polling job
  - 정상 reconciliation
  - mismatch 후 blocked 유지
  - 연속 실패 후 blocked
- pre-close cancel job
  - 취소 대상 없음
  - `submitted`/`partially_filled`만 취소 요청
- healthcheck
  - normal
  - writer queue degraded
  - token stale
  - polling stale

## F3 DB/문서 완전 일치 정리 계획

### F3 목표

- `docs/DB_SCHEMA_v1.2.md`를 기준으로 현재 SQLite ORM, DB 초기화 경로, 검증 테스트를 다시 일치시킵니다.
- Phase 2에서 직접 사용한 테이블만 부분 구현된 상태를 해소하고, 문서상 SQLite 스키마 전체를 ORM과 테스트에서 재현 가능하게 만듭니다.
- F3는 “스키마/ORM/초기화/검증 정합성”이 목표이며, 전략 확장이나 세금 계산 본체 구현은 포함하지 않습니다.

### 현재 확인된 충돌

- 문서에는 있으나 현재 `data/database.py` ORM에 없는 테이블:
  - `portfolio_snapshots`
  - `event_calendar`
  - `backtest_results`
- `scripts/init_db.py`는 `create_all()`만 호출하고, 문서상 전체 스키마 완전성 검증이나 migration 안내는 없습니다.
- 현재 테스트는 실행 흐름 중심이며, 문서와 ORM의 완전 일치를 잡는 별도 스키마 검증 테스트가 없습니다.

### F3 기본 원칙

- canonical source는 `docs/DB_SCHEMA_v1.2.md`입니다.
- 현재 차이는 문서 과잉이 아니라 코드 미구현으로 보고, F3에서는 코드 보강을 우선합니다.
- 모든 write 경로는 기존 `WAL + single writer queue` 제약을 그대로 유지합니다.
- `InfluxDB`, `tax_calculator.py`, `restore_portfolio.py`, `backtest_runner.py` 기능 확장은 F3 범위 밖입니다.
- `backtest_results`는 F3에서 기능 구현이 아니라 ORM/스키마 정합성까지만 닫습니다.

### F3 실행 체크리스트

| ID | 작업 | 상태 | 완료 기준 |
|---|---|---|---|
| F3-01 | 문서 선행 정리 | done | F3 충돌 목록, 목표, 검증 기준이 plan 문서에 고정 |
| F3-02 | 누락 ORM 모델 추가 | done | `portfolio_snapshots`, `event_calendar`, `backtest_results` ORM 추가 |
| F3-03 | 인덱스/제약 정합성 맞춤 | done | 문서상 canonical unique/index를 ORM에 반영 |
| F3-04 | 초기화/마이그레이션 규칙 정리 | done | `init_db` 멱등성과 migration 규칙이 저장소 기준으로 정리 |
| F3-05 | 스키마 정합성 테스트 추가 | done | 테이블/인덱스/PRAGMA 검증 자동화 |
| F3-06 | 문서 마감 | done | DB_SCHEMA/plan/필요 시 AGENTS와 코드 정의 재동기화 |

### F3 구현 작업 분할안

#### F3-01 문서 선행 정리

- 내용
  - 현재 ORM과 DB 문서 차이를 실행 계획 문서에 먼저 고정
  - 작업 범위를 “SQLite 스키마 정합성”으로 제한
- 완료 기준
  - implementer가 누락 테이블과 목표를 문서만 보고 파악 가능
- 검증
  - `AGENTS.md`, `PRD`, `DB_SCHEMA`와 충돌 없음

#### F3-02 누락 ORM 모델 추가

- 내용
  - `data/database.py`에 `PortfolioSnapshot`, `EventCalendar`, `BacktestResult` 추가
  - 필드, 기본값, unique/index는 `DB_SCHEMA_v1.2.md` 기준 적용
- 완료 기준
  - `Base.metadata.create_all()`로 문서상 SQLite 테이블 전체 생성 가능
- 검증
  - 임시 DB 생성 후 테이블 존재 확인

#### F3-03 인덱스/제약 정합성 맞춤

- 내용
  - 문서상 핵심 인덱스와 unique 제약을 ORM에 맞춤
  - 기존 Phase 2 테이블도 빠진 제약이 있으면 이 단계에서 정리
- 완료 기준
  - canonical unique/index가 ORM에 모두 표현됨
- 검증
  - SQLite schema introspection 테스트

#### F3-04 초기화/마이그레이션 규칙 정리

- 내용
  - `scripts/init_db.py`는 단순 초기화 엔트리로 유지
  - migration 규칙은 문서와 저장소에 드러나게 정리
  - 필요 시 `scripts/migrate_vYYYYMMDD.py` placeholder 기준 마련
- 완료 기준
  - 스키마 변경 반영 순서가 저장소 기준으로 명확해짐
- 검증
  - `init_db.py` 재실행 멱등성 확인

#### F3-05 스키마 정합성 테스트 추가

- 내용
  - 문서상 테이블/인덱스/PRAGMA 확인용 테스트 추가
- 완료 기준
  - 실행 흐름 테스트와 별도로 스키마 정합성 자동 검증 가능
- 검증
  - 모든 문서상 테이블 존재
  - 핵심 unique/index 존재
  - WAL/foreign_keys/busy_timeout 검증

#### F3-06 문서 마감

- 내용
  - 실제 반영 결과를 `DB_SCHEMA`, `phase2_execution_plan`, 필요 시 `AGENTS.md`에 동기화
- 완료 기준
  - 문서와 ORM의 스키마 정의가 다시 일치
- 검증
  - 문서-ORM diff 없음

### F3 테스트 기준

- 스키마 생성 테스트
  - 신규 DB에서 모든 문서상 SQLite 테이블 생성 확인
- 인덱스/제약 테스트
  - `orders.client_order_id`
  - `trades.execution_id`
  - `token_store.env`
  - `portfolio_snapshots.snapshot_date`
  - `broker_positions (ticker, market, source_env, snapshot_at)`
- PRAGMA 테스트
  - `journal_mode=WAL`
  - `synchronous=NORMAL`
  - `busy_timeout`
  - `foreign_keys=ON`
- 초기화 테스트
  - `scripts/init_db.py` 멱등성
- 회귀 테스트
  - 기존 `tests/test_execution` 유지 통과

### F3 구현 메모

- 현재 저장소에는 migration 스크립트가 아직 없습니다.
- F3는 migration 엔진 도입이 아니라 “문서상 규칙과 실제 초기화/ORM 표면을 맞추는 작업”으로 제한합니다.
- 미국 payload 미확정 상태는 F3 착수의 blocker가 아닙니다.
- F3-01은 현재 ORM과 DB 문서 차이(`portfolio_snapshots`, `event_calendar`, `backtest_results` 누락)와 검증 목표를 plan 문서에 고정한 것으로 완료 처리합니다.
- F3-02는 `data/database.py`에 `PortfolioSnapshot`, `EventCalendar`, `BacktestResult` ORM 모델을 추가한 것으로 완료 처리합니다.
- F3-03은 temporary DB schema introspection 결과 문서상 canonical index/unique 요구가 이미 ORM에 반영되어 있음을 확인한 것으로 완료 처리합니다.
- F3-04는 `scripts/init_db.py`에 멱등성 및 migration 안내 메시지를 추가하고, `scripts/migrate_vYYYYMMDD.py` placeholder를 저장소에 추가한 것으로 완료 처리합니다.
- F3-05는 `tests/test_execution/test_schema_alignment.py`를 추가해 문서상 테이블/핵심 unique/index/PRAGMA를 자동 검증하도록 만든 것으로 완료 처리합니다.
- F3-06은 `docs/DB_SCHEMA_v1.2.md`에 현재 저장소의 `init_db.py`/`migrate_vYYYYMMDD.py` 표면을 반영하고, F3 체크리스트를 모두 닫은 것으로 완료 처리합니다.

## F4 전략/리스크 실구현 확장 계획

### F4 목표

- skeleton 상태였던 전략/리스크 계층을 실제 계산 가능한 구조로 확장합니다.
- 실거래 최적화보다 `결정 가능한 신호 계산`, `정확한 리스크 차단`, `테스트 가능한 데이터 입력 경로`를 우선합니다.
- 전략 데이터 입력은 `Injected Repo` 방식으로 고정합니다.

### F4 기본 원칙

- 전략은 read-only `StrategyDataProvider`만 사용하고 DB/session에 직접 접근하지 않습니다.
- `BaseStrategy.generate_signals(universe, market, as_of)` 시그니처는 유지합니다.
- `signal_resolver -> risk_manager -> position_sizer` 경계를 그대로 유지합니다.
- 이벤트 필터는 risk 계층에서 작동하고, 변동성/최소 편입 비중은 sizing 계층에서 반영합니다.
- 전략은 주문 실행을 하지 않고 `Signal`만 생성합니다.

### F4 실행 체크리스트

| ID | 작업 | 상태 | 완료 기준 |
|---|---|---|---|
| F4-01 | 문서 선행 정리 + 데이터 인터페이스 고정 | done | `StrategyDataProvider` 표면과 F4 범위가 plan 문서에 고정 |
| F4-02 | `risk/event_filter.py` + `risk/exit_manager.py` | done | 이벤트 게이트와 trailing/ATR/stop-loss exit helper 구현 |
| F4-03 | `strategy/dual_momentum.py` | done | 12개월 절대/상대 모멘텀, 월간 리밸런싱 신호 생성 |
| F4-04 | `strategy/trend_following.py` | done | EMA crossover, RSI filter, ATR/trailing exit metadata 구현 |
| F4-05 | `strategy/factor_investing.py` | done | 멀티 팩터 점수 계산, 분기 리밸런싱 신호 생성 |
| F4-06 | resolver / risk / sizer 보강 | done | 당일 재매수 금지, 이벤트 게이트, volatility-adjusted sizing, 최소 편입 비중 반영 |
| F4-07 | broader verification + 문서 마감 | done | 전략/리스크/전체 테스트와 계획 문서 정리 완료 |

### F4 공개 표면

- `strategy.base.StrategyDataProvider`
- `strategy.dual_momentum.DualMomentumStrategy`
- `strategy.trend_following.TrendFollowingStrategy`
- `strategy.factor_investing.FactorInvestingStrategy`
- `risk.event_filter.EventFilter`
- `risk.exit_manager.ExitManager`

### F4 구현 메모

- 전략 데이터 입력 표면:
  - `get_price_history(tickers, market, as_of, lookback_days)`
  - `get_factor_inputs(tickers, market, as_of)`
  - `get_event_flags(tickers, market, as_of)`
- 듀얼 모멘텀:
  - 12개월 절대 모멘텀
  - 상대 모멘텀 상위 `top_n`
  - 월 1회 리밸런싱
- 추세 추종:
  - EMA(20)/EMA(60)
  - RSI 30 미만 신규 진입 제한
  - ATR(14), trailing stop, stop-loss 기반 exit helper 사용
- 멀티 팩터:
  - Value / Quality / Momentum / Low Vol 가중 합
  - 상위 `top_n`
  - 분기 1회 리밸런싱
- 이벤트 필터:
  - `FOMC`, `금통위`, `CPI/PPI`, `earnings`는 신규 매수 차단
  - `VIX > 30`, `VKOSPI > 25` 계열 이벤트는 `scale_factor=0.5`로 축소 신호 전달
- 포지션 사이저:
  - target volatility와 실제 volatility 비율로 notional 축소
  - 최소 편입 비중 미만이면 수량 0 처리
- resolver:
  - stop-loss / trailing / ATR exit가 매수보다 우선
  - 같은 종목에서 exit와 buy가 충돌하면 `same_day_rebuy_blocked` 메타데이터 기록

### F4 테스트 기준

- 전략 단위 테스트
  - dual momentum 월간 cadence / top_n
  - trend following EMA crossover / ATR or trailing exit
  - factor investing ranking / quarterly cadence
- 리스크 단위 테스트
  - FOMC/금통위 차단
  - VIX 축소
  - trailing stop
  - volatility scaling
  - minimum position threshold
  - same-day rebuy block metadata
- broader verification
  - `python -m compileall core strategy risk tests`
  - `python -m pytest tests\test_strategy tests\test_risk -q`
  - `python -m pytest tests\test_execution -q`
  - `python -m pytest tests\ -q`

## F5 Phase 3 확장 계획

### F5 목표

- Phase 3 운영 계층을 실제 저장소 구조에 맞게 구체화합니다.
- 범위는 `tax/tax_calculator.py`, `monitor/dashboard.py`, `monitor/telegram_bot.py`, `scripts/restore_portfolio.py`, `backtest/backtest_runner.py`를 포함합니다.
- 우선순위는 실전 최적화보다 `세금 추산 가능 구조`, `운영 가시성`, `안전한 DR 복구`, `백테스트 결과 저장`을 먼저 고정하는 것입니다.

### F5 기본 원칙

- 세금 계산은 `tax_events`, `trades`, `position_lots`를 canonical source로 사용합니다.
- 대시보드와 알림은 read-only 조회 또는 runtime 상태 집계만 사용하고 주문/원장 쓰기를 직접 수행하지 않습니다.
- DR 복구는 기본값을 `dry-run`으로 두고, `apply` 모드도 기존 reconciliation / writer queue 경로만 사용합니다.
- backtest runner는 전략 실행 + 결과 저장까지만 다루고 최적화/Walk-Forward 자동화는 범위 밖으로 둡니다.
- 민감정보 비저장 원칙과 WAL + single writer queue 제약은 그대로 유지합니다.

### F5 실행 체크리스트

| ID | 작업 | 상태 | 완료 기준 |
|---|---|---|---|
| F5-01 | 문서 선행 정리 | done | F5 범위, 원칙, 체크리스트가 plan 문서에 고정 |
| F5-02 | `tax/tax_calculator.py` | done | 연도별/시장별 세금 추산 summary와 trade-level report 생성 |
| F5-03 | `monitor/dashboard.py` | done | runtime/portfolio/mismatch/logs read-only dashboard snapshot 구현 |
| F5-04 | `monitor/telegram_bot.py` | done | blocked/degraded/mismatch/token 실패 등 핵심 운영 이벤트 알림 송신 |
| F5-05 | `scripts/restore_portfolio.py` | done | dry-run 우선 DR 복구 흐름과 선택적 apply 모드 구현 |
| F5-06 | `backtest/backtest_runner.py` | done | 전략별 백테스트 실행과 `backtest_results` 저장 |
| F5-07 | `portfolio_snapshots`/`system_logs` 운영 연계 | done | 대시보드/리포트 입력용 snapshot/log 경로 보강 |
| F5-08 | broader verification + 문서 마감 | done | F5 테스트와 관련 문서 동기화 완료 |

### F5 공개 표면

- `TaxCalculator.calculate_yearly_summary(year, market=None)`
- `TaxCalculator.build_trade_report(year)`
- `monitor.dashboard.build_dashboard_snapshot(...)`
- `TelegramNotifier.send_event(event_type, message, context=None)`
- `restore_portfolio.py --dry-run`
- `restore_portfolio.py --apply`
- `restore_portfolio.py --market KR|US|ALL`
- `restore_portfolio.py --snapshot-file <path>`
- `BacktestRunner.run(strategy, market, start_date, end_date, universe=[...])`

### F5 구현 메모

- 세금 계산:
  - KR 거래는 환율 `NULL` 허용
  - US 거래는 `settlement_fx_rate` 우선, 없으면 `trade_fx_rate` fallback
  - FIFO 취득원가 기준
- 대시보드 최소 범위:
  - runtime health
  - open orders / recent trades
  - broker/internal mismatch summary
  - portfolio snapshot summary
  - recent system logs
- F5-03은 `build_dashboard_snapshot()`와 `dashboard_snapshot_to_dict()`를 추가해 runtime health, open orders, recent trades, latest portfolio snapshot, recent reconciliation summary, recent system logs를 하나의 read-only snapshot으로 묶는 것으로 완료 처리합니다.
- Telegram 최소 이벤트:
  - token refresh failure
  - trading blocked / reconcile_hold
  - writer queue degraded
  - polling mismatch
  - pre-close cancel failure
  - DR restore started/completed/failed
- F5-04는 `.env` 기반 Telegram bot token/chat_id 설정 표면을 추가하고, `TelegramNotifier.send_event()`가 disabled 상태에서는 no-op, enabled 상태에서는 운영 이벤트 메시지를 포맷해 sender 또는 Telegram Bot HTTP API로 송신하는 것으로 완료 처리합니다.
- DR 복구 순서:
  - 신규 주문 중단 확인
  - 브로커 포지션/미체결/현금 snapshot 조회
  - 내부 원장과 비교
  - `reconciliation_runs` 기록
  - fill re-sync 또는 상태 보정 제안 생성
  - 복구 결과 요약 출력
- backtest runner:
  - `vectorbt` 우선 사용
  - 전략명은 Phase 2 전략 모듈과 동일 이름 사용
  - 단일 실행 + 결과 저장까지를 목표로 함
- F5-02는 `TaxCalculator`를 추가해 `tax_events` 우선, `trades` 기반 FIFO fallback, `settlement_fx_rate -> trade_fx_rate` 순서의 환율 적용으로 연도별 summary와 trade-level report를 생성하는 것으로 완료 처리합니다.
- F5-05는 `scripts/restore_portfolio.py`에 `RestorePortfolioService`를 추가해 snapshot file 기반 `dry-run` mismatch 요약과 `apply` 시 `reconciliation_runs`/`broker_positions` 기록, 신규 주문 차단 확인, 선택적 `portfolio_snapshot` 복구 기록을 지원하는 것으로 완료 처리합니다.
- F5-06은 `backtest/backtest_runner.py`에 `BacktestRunner`를 추가해 전략명/기간 검증, 전략 신호 기반 백테스트 실행, `backtest_results` 저장을 지원하는 것으로 완료 처리합니다.
- F5-07은 `monitor/operations.py`에 `OperationsRecorder`를 추가해 `system_logs`와 `portfolio_snapshots`를 writer queue 경유로 기록하고, restore/backtest 흐름에 연계한 것으로 완료 처리합니다.

### F5 테스트 기준

- 세금 계산
  - US 매도 FIFO 원가 추적
  - `settlement_fx_rate` 우선 / `trade_fx_rate` fallback
  - KR 거래 환율 `NULL` 허용
- 대시보드/모니터링
  - runtime healthy / warning / critical 표시
  - 최근 trades / logs / reconciliation summary 조회
- Telegram notifier
  - blocked, degraded, mismatch, token failure 이벤트별 메시지 포맷
- DR restore
  - dry-run mismatch 후보 출력
  - apply 모드에서 writer queue 경유 보정
  - 신규 주문 차단 미확인 시 복구 중단
- backtest runner
  - 전략별 실행
  - `backtest_results` 저장
  - 잘못된 전략명/기간 입력 거부
- broader verification
  - `python -m compileall core data execution strategy risk monitor tax backtest scripts tests`
  - `python -m pytest tests\ -q`

## 현재 검증 기준

작은 검증부터 수행:

1. `python -m compileall core data execution strategy risk tests main.py`
2. `python -m pytest tests\test_strategy tests\test_risk tests\test_execution -q`
3. 필요 시 `python -m pytest tests\ -v`

## 구현 메모

- `AGENTS.md`는 Phase 2 계획 문서 참조, adapter 정규화 규칙, 실패 분류 규칙까지 반영 완료
- `config/config.yaml`은 Phase 2 필요 최소 설정만 반영
- `scripts/restore_portfolio.py`, `backtest/backtest_runner.py`, `monitor/operations.py`를 추가해 F5 잔여 범위를 닫았습니다.
- `portfolio_snapshots`, `system_logs`, `backtest_results`는 모두 writer queue 경유 write 경로를 갖습니다.
- backtest runner는 `vectorbt`가 설치된 환경에서는 우선 사용하고, 없는 환경에서는 dependency-light fallback 엔진으로 검증과 결과 저장을 계속 수행합니다.
- 실브로커 연동보다 mock 가능한 execution 흐름 유지가 우선

## 업데이트 규칙

이 문서는 Phase 2 진행 중 계속 갱신합니다.

- 작업 시작 시: 해당 ID 상태를 `in_progress`로 변경
- 작업 완료 시: 상태를 `done`으로 변경
- 범위 변경 시: 본 문서와 관련 PRD/DB 문서를 함께 갱신
- 검증 명령이 바뀌면 본 문서의 검증 기준도 같이 수정
