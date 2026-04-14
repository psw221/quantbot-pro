# Phase 2 Execution Plan

## 목적

이 문서는 QuantBot Pro Phase 2 작업의 실행 기준과 진행 상태를 한 곳에서 관리하기 위한 작업 문서입니다.

- 제품/운영 정책 기준: `docs/PRD_v1.4.md`
- 저장 구조/원장/정합성 기준: `docs/DB_SCHEMA_v1.2.md`
- 저장소 구현 규칙 기준: `AGENTS.md`

Phase 2의 우선순위는 실거래 API 완성보다 `테스트 가능한 구조`, `안전한 상태 관리`, `polling/reconciliation 골격`, `원장 일관성`을 먼저 고정하는 것입니다.

## 현재 상태 요약

- 상태: `in_progress`
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
| P2-08 | 브로커 polling adapter 정교화 | todo | open orders / positions / cash 응답 표준화 | mock reconciliation 테스트 |
| P2-09 | cancel/retry 흐름 보강 | todo | `cancel_pending`, retry_count 동작 고정 | 상태 전이 테스트 |
| P2-10 | signal/order/fill persistence 보강 | todo | reject/order/fill 메타데이터 저장 규칙 정리 | DB 테스트 |
| P2-11 | broader verification + 정리 | todo | 전체 관련 테스트와 문서 점검 완료 | `pytest tests/ -v` |

## 다음 구현 우선순위

1. `P2-08 브로커 polling adapter 정교화`
2. `P2-09 cancel/retry 흐름 보강`
3. `P2-10 signal/order/fill persistence 보강`
4. `P2-11 broader verification + 정리`

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
