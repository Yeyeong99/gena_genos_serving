import { LoaderCircle } from "lucide-react";

export function UploadingState() {
  return (
    <div className="flex h-full flex-col items-center justify-center">
      <div className="spinner" />
      <div className="mt-8 flex items-center gap-3 text-[var(--primary)]">
        <LoaderCircle className="h-5 w-5 animate-spin" />
        <span className="text-lg font-semibold">문서를 업로드하고 번역하고 있습니다.</span>
      </div>
      <p className="mt-3 max-w-[440px] text-center text-sm leading-7 text-[var(--muted)]">
        원문 구조를 정리하고 번역 블록을 생성하는 중입니다. 문서 크기에 따라 잠시 시간이 걸릴 수 있습니다.
      </p>
    </div>
  );
}
