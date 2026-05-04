# FE 코딩 컨벤션

| 항목 | 내용 |
|------|------|
| 대상 독자 | 프론트엔드 개발자 |
| 문서 유형 | 컨벤션 가이드 |
| 관련 이슈 | 없음 |

> **문서 신선도**: 마지막 확인 2026-04-13 | 기준 소스: `frontend/apps/client/src/` | 갱신 조건: TypeScript 설정·컨벤션·App Router 패턴 변경 시

---

## 1. App Router 파일 컨벤션

| 파일 | 역할 |
|------|------|
| `page.tsx` | 라우트 진입점 (서버 컴포넌트 기본) |
| `layout.tsx` | 공유 레이아웃 래퍼 |
| `loading.tsx` | Suspense 로딩 UI |
| `error.tsx` | 에러 바운더리 |
| `not-found.tsx` | 404 페이지 |

### 서버 컴포넌트 vs 클라이언트 컴포넌트

```tsx
// ✅ 서버 컴포넌트 (기본 — "use client" 없음)
// app/dashboard/page.tsx
import { getUsers } from '@/api/users'

export default async function DashboardPage() {
  const users = await getUsers()  // 서버에서 직접 fetch
  return <UserList users={users} />
}

// ✅ 클라이언트 컴포넌트 — 상호작용·훅 필요 시만
// components/UserList.tsx
'use client'

import { useState } from 'react'

export function UserList({ users }: { users: User[] }) {
  const [filter, setFilter] = useState('')
  // ...
}
```

**`"use client"` 추가 기준**:
- `useState`, `useEffect` 등 React 훅 사용 시
- 브라우저 이벤트 핸들러 (`onClick`, `onChange` 등)
- `useQuery` 등 TanStack Query 훅 사용 시
- Zustand 스토어 접근 시

---

## 2. 컴포넌트 네이밍·구조

### 네이밍 규칙

| 유형 | 규칙 | 예시 |
|------|------|------|
| 컴포넌트 파일 | PascalCase | `UserCard.tsx` |
| 훅 파일 | camelCase + `use` 접두사 | `useUserList.ts` |
| 유틸리티 | camelCase | `formatDate.ts` |
| 타입 파일 | camelCase | `user.types.ts` |
| 상수 | UPPER_SNAKE_CASE | `API_BASE_URL` |

### Named Export 사용

```tsx
// ✅ named export (tree-shaking 용이)
export function UserCard({ user }: { user: User }) {
  return <div>{user.name}</div>
}

// ❌ default export 지양 (page.tsx 등 Next.js 필수 케이스 제외)
export default function UserCard() { ... }
```

---

## 3. TypeScript 컨벤션

### 타입 정의

```tsx
// ✅ interface for object shapes
interface User {
  id: string
  name: string
  email: string
  createdAt: Date
}

// ✅ type for unions/intersections
type Status = 'active' | 'inactive' | 'pending'
type UserWithStatus = User & { status: Status }

// ❌ any 사용 금지 — unknown으로 대체
const data: unknown = fetchData()
```

### Props 타입

```tsx
// ✅ Props 인터페이스 별도 정의
interface UserCardProps {
  user: User
  onSelect?: (id: string) => void
  className?: string
}

export function UserCard({ user, onSelect, className }: UserCardProps) {
  // ...
}
```

---

## 4. Import 순서

```tsx
// 1. React / Next.js
import { useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import Image from 'next/image'

// 2. 외부 라이브러리
import { useQuery } from '@tanstack/react-query'

// 3. @gena/design-system
import { Button, Input } from '@gena/design-system'

// 4. 내부 경로 (@/ alias)
import { useUserList } from '@/hooks/useUserList'
import { userApi } from '@/api/user'
import type { User } from '@/types/user.types'

// 5. 상대 경로
import { UserCard } from './UserCard'
```

---

## 5. 디렉터리별 역할

```
src/
├── app/                ← Next.js 라우트만 (page.tsx, layout.tsx 등)
│                         비즈니스 로직 최소화
├── components/         ← 재사용 가능한 UI 컴포넌트
│   ├── ui/             ← @gena/design-system 확장 컴포넌트
│   └── <feature>/      ← 기능별 컴포넌트
├── hooks/              ← TanStack Query + 비즈니스 로직 훅
├── api/                ← API 함수 레이어 (fetch 래퍼)
├── store/              ← Zustand 스토어
├── types/              ← 공유 TypeScript 타입·인터페이스
└── lib/                ← 순수 유틸리티 함수·상수
```

---

## 6. 금지 패턴

| 금지 | 이유 | 대안 |
|------|------|------|
| 컴포넌트 내 직접 `fetch` | API 레이어 우회 → 인터셉터 누락 | `src/api/` 레이어 사용 |
| `any` 타입 | 타입 안전성 파괴 | `unknown` + 타입 가드 |
| `useEffect` 내 데이터 페칭 | TanStack Query 패턴 위반 | `useQuery` 사용 |
| 서버 컴포넌트에서 클라이언트 훅 | 빌드 오류 | `"use client"` 추가 또는 분리 |
| 인라인 스타일 (`style={{}}`) | Tailwind 컨벤션 위반 | Tailwind 클래스 사용 |

---

## 7. 검증 명령

```bash
# 타입 체크
cd frontend/apps/client
pnpm tsc --noEmit

# 린트
pnpm lint

# 빌드 확인
pnpm build
```
