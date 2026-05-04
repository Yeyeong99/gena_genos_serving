# FE API 레이어 패턴

| 항목 | 내용 |
|------|------|
| 대상 독자 | 프론트엔드 개발자 |
| 문서 유형 | 패턴 가이드 |
| 관련 이슈 | 없음 |

> **문서 신선도**: 마지막 확인 2026-04-13 | 기준 소스: `frontend/apps/client/src/api/`, `src/hooks/` | 갱신 조건: API 클라이언트 구조·인증 방식·TanStack Query 버전 변경 시

---

## 1. API 레이어 구조

컴포넌트에서 직접 `fetch`를 호출하지 않는다. 반드시 `src/api/` 레이어를 경유한다.

```
src/
├── api/
│   ├── client.ts          ← fetch 기반 HTTP 클라이언트 (인터셉터 포함)
│   ├── user.ts            ← 도메인별 API 함수
│   ├── agent.ts
│   └── auth.ts
└── hooks/
    ├── useUserList.ts     ← TanStack Query 훅
    └── useAgentRun.ts
```

---

## 2. API 클라이언트 (`src/api/client.ts`)

```typescript
// src/api/client.ts

const BASE_URL = process.env.NEXT_PUBLIC_API_URL

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getAccessToken()  // 토큰 가져오기

  const response = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  })

  if (response.status === 401) {
    // 토큰 갱신 시도
    await refreshToken()
    // 재시도 로직
  }

  if (!response.ok) {
    const error = await response.json()
    throw new ApiError(response.status, error)
  }

  return response.json()
}

export const apiClient = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}
```

---

## 3. 도메인별 API 함수

```typescript
// src/api/user.ts

import { apiClient } from './client'
import type { User, UserCreate, UserListResponse } from '@/types/user.types'

export const userApi = {
  // GET — 목록 조회
  list: (params?: { page?: number; limit?: number }) =>
    apiClient.get<UserListResponse>(`/api/v1/users?${new URLSearchParams(params as Record<string, string>)}`),

  // GET — 단건 조회
  get: (id: string) =>
    apiClient.get<User>(`/api/v1/users/${id}`),

  // POST — 생성
  create: (data: UserCreate) =>
    apiClient.post<User>('/api/v1/users', data),

  // PUT — 수정
  update: (id: string, data: Partial<UserCreate>) =>
    apiClient.put<User>(`/api/v1/users/${id}`, data),

  // DELETE — 삭제
  delete: (id: string) =>
    apiClient.delete<void>(`/api/v1/users/${id}`),
}
```

---

## 4. TanStack Query 훅 패턴

### useQuery — 데이터 조회

```typescript
// src/hooks/useUserList.ts
'use client'

import { useQuery } from '@tanstack/react-query'
import { userApi } from '@/api/user'

// queryKey 네이밍: ['도메인', '액션', ...파라미터]
export function useUserList(params?: { page?: number }) {
  return useQuery({
    queryKey: ['users', 'list', params],
    queryFn: () => userApi.list(params),
    staleTime: 5 * 60 * 1000,   // 5분
  })
}

export function useUser(id: string) {
  return useQuery({
    queryKey: ['users', 'detail', id],
    queryFn: () => userApi.get(id),
    enabled: Boolean(id),        // id 없으면 실행 안 함
  })
}
```

### useMutation — 데이터 변경

```typescript
// src/hooks/useUserMutations.ts
'use client'

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { userApi } from '@/api/user'
import type { UserCreate } from '@/types/user.types'

export function useCreateUser() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (data: UserCreate) => userApi.create(data),
    onSuccess: () => {
      // 목록 캐시 무효화
      queryClient.invalidateQueries({ queryKey: ['users', 'list'] })
    },
  })
}

export function useDeleteUser() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (id: string) => userApi.delete(id),
    onSuccess: (_, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ['users', 'list'] })
      queryClient.removeQueries({ queryKey: ['users', 'detail', deletedId] })
    },
  })
}
```

### 컴포넌트에서 사용

```tsx
// components/UserList.tsx
'use client'

import { useUserList } from '@/hooks/useUserList'
import { useDeleteUser } from '@/hooks/useUserMutations'

export function UserList() {
  const { data, isLoading, error } = useUserList()
  const { mutate: deleteUser, isPending } = useDeleteUser()

  if (isLoading) return <Skeleton />
  if (error) return <ErrorMessage error={error} />

  return (
    <ul>
      {data?.items.map((user) => (
        <li key={user.id}>
          {user.name}
          <button
            onClick={() => deleteUser(user.id)}
            disabled={isPending}
          >
            삭제
          </button>
        </li>
      ))}
    </ul>
  )
}
```

---

## 5. queryKey 네이밍 컨벤션

```typescript
// ✅ 일관된 queryKey 형식: ['도메인', '액션', ...파라미터]
['users', 'list']                          // 목록
['users', 'list', { page: 1 }]            // 파라미터 있는 목록
['users', 'detail', userId]               // 단건
['agents', 'list']
['agents', 'detail', agentId]
['agents', 'run', agentId, sessionId]     // 특정 세션의 실행 상태

// invalidateQueries — 상위 키로 하위 전체 무효화
queryClient.invalidateQueries({ queryKey: ['users'] })  // users 관련 전체
queryClient.invalidateQueries({ queryKey: ['users', 'list'] })  // 목록만
```

---

## 6. 에러 처리

```typescript
// src/api/client.ts
export class ApiError extends Error {
  constructor(
    public status: number,
    public data: { code?: string; message?: string },
  ) {
    super(data.message ?? '요청에 실패했습니다.')
  }
}

// 컴포넌트에서 에러 처리
const { error } = useUserList()

if (error instanceof ApiError) {
  if (error.status === 403) return <PermissionDenied />
  if (error.status === 404) return <NotFound />
}
```

---

## 7. 서버 컴포넌트에서 직접 fetch (SSR)

서버 컴포넌트는 TanStack Query 없이 API 함수를 직접 호출할 수 있다.

```tsx
// app/users/page.tsx (서버 컴포넌트)
import { userApi } from '@/api/user'

export default async function UsersPage() {
  // 서버에서 직접 실행 — 토큰은 cookies()로 가져옴
  const data = await userApi.list()
  return <UserList initialData={data} />
}
```

---

## 8. 금지 패턴

| 금지 | 이유 | 대안 |
|------|------|------|
| 컴포넌트 내 `fetch()` 직접 호출 | 인터셉터 누락, 중복 코드 | `src/api/` 레이어 사용 |
| `useEffect` 내 `fetch` | TanStack Query 패턴 위반 | `useQuery` 사용 |
| API 응답을 Zustand에 저장 | 서버 상태 중복 관리 | TanStack Query 캐시 사용 |
| 무분별한 `queryClient.invalidateQueries()` | 불필요한 리페치 | 필요한 키만 정확히 무효화 |
