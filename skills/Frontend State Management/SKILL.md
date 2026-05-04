# 상태관리 패턴

| 항목 | 내용 |
|------|------|
| 대상 독자 | 프론트엔드 개발자 |
| 문서 유형 | 패턴 가이드 |
| 관련 이슈 | 없음 |

> **문서 신선도**: 마지막 확인 2026-04-13 | 기준 소스: `frontend/apps/client/src/store/`, `src/hooks/` | 갱신 조건: Zustand·TanStack Query 버전·상태관리 전략 변경 시

---

## 1. 상태 분리 원칙

```
상태 종류              관리 도구            위치
─────────────────────────────────────────────────
서버 데이터 (API 응답)  TanStack Query  →  src/hooks/
UI 전역 상태           Zustand          →  src/store/
컴포넌트 로컬 상태     useState         →  컴포넌트 내
폼 상태               React Hook Form  →  컴포넌트 내
```

**핵심 규칙**: API 응답 데이터를 Zustand에 넣지 않는다.

---

## 2. Zustand 스토어 패턴

### 스토어 파일 구조

```
src/store/
├── uiStore.ts          ← UI 전역 상태 (사이드바·모달·테마 등)
├── sessionStore.ts     ← 인증 세션 정보 (userId, role 등)
└── <feature>Store.ts   ← 기능별 클라이언트 상태
```

### 스토어 작성 패턴

```typescript
// src/store/uiStore.ts
import { create } from 'zustand'

interface UiState {
  // State
  isSidebarOpen: boolean
  activeModal: string | null

  // Actions
  toggleSidebar: () => void
  openModal: (name: string) => void
  closeModal: () => void
}

export const useUiStore = create<UiState>((set) => ({
  // 초기 상태
  isSidebarOpen: true,
  activeModal: null,

  // 액션
  toggleSidebar: () =>
    set((state) => ({ isSidebarOpen: !state.isSidebarOpen })),

  openModal: (name) => set({ activeModal: name }),
  closeModal: () => set({ activeModal: null }),
}))
```

### 도메인별 클라이언트 상태 예시

```typescript
// src/store/chatStore.ts
import { create } from 'zustand'

interface ChatState {
  // 클라이언트 전용 상태 (서버 데이터 아님)
  inputText: string
  isComposing: boolean  // IME 조합 중 여부
  selectedFiles: File[]

  setInputText: (text: string) => void
  setIsComposing: (value: boolean) => void
  addFile: (file: File) => void
  clearFiles: () => void
  reset: () => void
}

export const useChatStore = create<ChatState>((set) => ({
  inputText: '',
  isComposing: false,
  selectedFiles: [],

  setInputText: (text) => set({ inputText: text }),
  setIsComposing: (value) => set({ isComposing: value }),
  addFile: (file) =>
    set((state) => ({ selectedFiles: [...state.selectedFiles, file] })),
  clearFiles: () => set({ selectedFiles: [] }),
  reset: () => set({ inputText: '', selectedFiles: [] }),
}))
```

---

## 3. Zustand 사용 시 주의사항

### ✅ Zustand에 넣어도 되는 것

```typescript
// UI 상태
isSidebarOpen, activeModal, toastMessages

// 인증 세션 (토큰 아님 — userId, role 등 파생 정보)
currentUserId, currentUserRole

// 기능별 클라이언트 상태
chatInputText, selectedFiles, isComposing
```

### ❌ Zustand에 넣으면 안 되는 것

```typescript
// API 응답 데이터 — TanStack Query 사용
userList, agentList, taskHistory

// 서버에서 가져오는 모든 비동기 데이터
// → 캐싱·동기화·로딩 상태가 TanStack Query에 내장됨
```

---

## 4. TanStack Query 고급 패턴

### prefetchQuery — 서버 컴포넌트에서 데이터 미리 로드

```tsx
// app/agents/page.tsx (서버 컴포넌트)
import { dehydrate, HydrationBoundary, QueryClient } from '@tanstack/react-query'
import { agentApi } from '@/api/agent'

export default async function AgentsPage() {
  const queryClient = new QueryClient()

  await queryClient.prefetchQuery({
    queryKey: ['agents', 'list'],
    queryFn: () => agentApi.list(),
  })

  return (
    <HydrationBoundary state={dehydrate(queryClient)}>
      <AgentList />  {/* 클라이언트에서 캐시 즉시 사용 */}
    </HydrationBoundary>
  )
}
```

### Optimistic Update — 즉각적인 UI 반응

```typescript
// src/hooks/useToggleFavorite.ts
export function useToggleFavorite() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (agentId: string) => agentApi.toggleFavorite(agentId),

    onMutate: async (agentId) => {
      // 진행 중인 쿼리 취소
      await queryClient.cancelQueries({ queryKey: ['agents', 'detail', agentId] })

      // 이전 값 저장
      const previous = queryClient.getQueryData(['agents', 'detail', agentId])

      // 낙관적 업데이트
      queryClient.setQueryData(['agents', 'detail', agentId], (old: Agent) => ({
        ...old,
        isFavorite: !old.isFavorite,
      }))

      return { previous }
    },

    onError: (_, agentId, context) => {
      // 실패 시 롤백
      queryClient.setQueryData(['agents', 'detail', agentId], context?.previous)
    },
  })
}
```

### 무한 스크롤

```typescript
// src/hooks/useInfiniteAgents.ts
import { useInfiniteQuery } from '@tanstack/react-query'

export function useInfiniteAgents() {
  return useInfiniteQuery({
    queryKey: ['agents', 'infinite'],
    queryFn: ({ pageParam = 1 }) => agentApi.list({ page: pageParam }),
    initialPageParam: 1,
    getNextPageParam: (lastPage) =>
      lastPage.hasMore ? lastPage.nextPage : undefined,
  })
}
```

---

## 5. staleTime 설정 기준

```typescript
// 실시간성이 중요하지 않은 데이터: 5분
staleTime: 5 * 60 * 1000

// 거의 변하지 않는 설정 데이터: 30분
staleTime: 30 * 60 * 1000

// 실시간 데이터: 0 (기본값 — 항상 최신)
staleTime: 0

// 사용자 프로필: 10분
staleTime: 10 * 60 * 1000
```

---

## 6. 금지 패턴

| 금지 | 이유 | 대안 |
|------|------|------|
| API 응답을 Zustand에 저장 | 서버 상태 중복·동기화 불가 | TanStack Query 캐시 사용 |
| 하나의 스토어에 모든 상태 | 불필요한 리렌더링 | 도메인별 스토어 분리 |
| `useEffect`로 서버 데이터 페칭 | TanStack Query 패턴 위반 | `useQuery` 사용 |
| Zustand에서 직접 API 호출 | 레이어 분리 위반 | 컴포넌트에서 훅 경유 |
