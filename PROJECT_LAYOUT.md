# QuantBot Pro 최종 문서 배치 경로

실제 프로젝트에 반영할 기준 경로는 아래와 같습니다.

```text
quantbot-pro/
├── AGENTS.md
└── docs/
    ├── PRD_v1.4.md
    └── DB_SCHEMA_v1.2.md
```

## 파일 설명

- `AGENTS.md`
  - Codex 및 개발자 구현 가이드
  - SQLite WAL / Writer Queue / 폴링 정합성 규칙 포함

- `docs/PRD_v1.4.md`
  - 제품 요구사항 문서
  - 거시적 리밸런싱 정책 및 10분 주기 브로커 폴링 요구사항 포함

- `docs/DB_SCHEMA_v1.2.md`
  - 데이터 저장 구조 문서
  - 결제일 환율 필드, WAL/Writer Queue 메타데이터, reconciliation 이력 포함

