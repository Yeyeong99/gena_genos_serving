# 아키텍처 개요

| 항목 | 내용 |
|------|------|
| 대상 독자 | GenA 전체 개발자 |
| 문서 유형 | 아키텍처 가이드 |
| 관련 이슈 | 없음 |

> **문서 신선도**: 마지막 확인 2026-04-13 | 기준 소스: `frontend/apps/client/src/`, `services/` | 갱신 조건: 디렉터리 구조·레이어 구조·서비스 목록 변경 시

---

## 전체 시스템 구성

```
브라우저 (Next.js App Router)
  └── frontend/apps/client/
        ├── app/                → 라우트 (page.tsx, layout.tsx)
        ├── components/         → UI 컴포넌트 (@gena/design-system 기반)
        ├── hooks/              → TanStack Query 훅 (서버 상태)
        ├── store/              → Zustand 스토어 (클라이언트 상태)
        ├── api/                → API 함수 레이어
        └── lib/                → 유틸리티

API 게이트웨이
  └── services/gateway/        → 라우팅·인증·레이트 리미팅

백엔드 서비스
  ├── services/agent-service/  → AI 에이전트 실행·관리
  ├── services/auth-service/   → 인증·인가 (JWT, OAuth)
  ├── services/billing-service/→ 크레딧·과금 관리
  ├── services/task-service/   → 태스크 큐·상태 추적
  └── services/preprocessor/  → 데이터 전처리
```

---

## 프론트엔드 레이어 구조

```
app/ (라우트)
  └── page.tsx           → 서버 컴포넌트 우선 (SSR)
        └── <FeatureSection />
              ├── 서버 컴포넌트: 데이터 페칭 (fetch 직접)
              └── 클라이언트 컴포넌트 ("use client")
                    └── hooks/use<Feature>.ts (TanStack Query)
                          └── api/<feature>.ts (API 레이어)
```

### 상태 관리 분리 원칙

```
서버 상태 (API 데이터)
  └── TanStack Query  ← useQuery / useMutation
        └── api/<feature>.ts

클라이언트 상태 (UI 전용)
  └── Zustand store
        └── store/<feature>Store.ts
```

---

## 백엔드 레이어 구조 (서비스별 동일)

```
services/<서비스명>/
  ├── main.py              → FastAPI 앱 진입점
  ├── api/                 → 라우터 (엔드포인트 정의)
  │   └── v1/
  │       └── <domain>.py  → APIRouter
  ├── services/            → 비즈니스 로직 (순수 함수)
  ├── models/              → SQLAlchemy ORM 모델
  ├── schemas/             → Pydantic 요청/응답 스키마
  ├── crud/                → DB CRUD 함수
  ├── core/                → 설정·의존성 주입
  └── alembic/             → DB 마이그레이션
```

### 요청 흐름

```
HTTP 요청
  → api/<domain>.py (라우터) — 요청 파싱, 의존성 주입
  → services/<domain>.py     — 비즈니스 로직 실행
  → crud/<domain>.py         — DB 조회·쓰기
  → models/<domain>.py       — ORM 객체
  → schemas/<domain>.py      — 응답 직렬화
```

---

## 프로젝트 디렉터리 구조

```
GenA/
├── CLAUDE.md                          ← 하네스 (진입점)
├── frontend/
│   ├── apps/
│   │   └── client/                    ← Next.js 15 App Router
│   │       ├── src/
│   │       │   ├── app/               ← 라우트
│   │       │   │   ├── (auth)/        ← 인증 그룹
│   │       │   │   ├── (dashboard)/   ← 대시보드 그룹
│   │       │   │   └── layout.tsx     ← 루트 레이아웃
│   │       │   ├── components/        ← 공유 UI 컴포넌트
│   │       │   ├── hooks/             ← 커스텀 훅 (TanStack Query)
│   │       │   ├── api/               ← API 함수 레이어
│   │       │   ├── store/             ← Zustand 스토어
│   │       │   ├── types/             ← 공유 TypeScript 타입
│   │       │   └── lib/               ← 유틸리티·상수
│   │       ├── public/
│   │       ├── next.config.ts
│   │       └── package.json
│   └── packages/
│       └── design-system/             ← @gena/design-system
│
├── services/
│   ├── agent-service/
│   ├── auth-service/
│   ├── billing-service/
│   ├── gateway/
│   ├── task-service/
│   └── preprocessor/
│
└── docs/                              ← 이 문서들
```

---

## 서비스 간 통신

| 통신 방식 | 사용처 |
|----------|--------|
| REST (HTTP) | FE → Gateway → 서비스 |
| 내부 REST | 서비스 간 동기 호출 |
| 메시지 큐 | 비동기 태스크 (task-service) |

---

## 개발 환경 실행

```bash
# 프론트엔드
cd frontend
pnpm install
pnpm --filter client dev          # http://localhost:3000

# 백엔드 (각 서비스)
cd services/agent-service
uv sync
uv run uvicorn main:app --reload --port 8001

cd services/auth-service
uv sync
uv run uvicorn main:app --reload --port 8002
```
