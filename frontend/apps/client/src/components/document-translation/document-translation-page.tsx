"use client";

import { ChevronLeft, ChevronRight, Languages } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button, Card } from "@gena/design-system";
import { useDocumentTranslation } from "@/hooks/use-document-translation";
import { useDocumentTranslationStore } from "@/store/document-translation-store";
import { DocumentFilePicker } from "./document-file-picker";
import { TranslationWorkspace } from "./translation-workspace";
import { UploadingState } from "./uploading-state";
import { TargetLanguageSelector } from "./target-language-selector";

export function DocumentTranslationPage() {
  const { stage, selectedFile, response, selectedLanguage } = useDocumentTranslationStore();
  const translateMutation = useDocumentTranslation();
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);

  const helperText = useMemo(() => {
    if (selectedFile) {
      return `${selectedFile.file.name} 문서가 업로드 되었습니다.`;
    }
    return "docx, pptx, xlsx 문서만 업로드할 수 있습니다.";
  }, [selectedFile]);

  useEffect(() => {
    if (stage === "translated") {
      setIsSidebarOpen(false);
      return;
    }

    if (stage === "idle") {
      setIsSidebarOpen(true);
    }
  }, [stage]);

  return (
    <main className="page-shell">
      <section className="glass-card flex h-screen w-full flex-col overflow-hidden border-0 rounded-none bg-white/75">
        <div className="flex min-h-0 flex-1 gap-0 overflow-hidden px-6 py-6">
          {isSidebarOpen ? (
            <div className="hidden h-full min-h-0 w-[340px] shrink-0 overflow-hidden lg:block">
              <div className="scrollbar-thin flex h-full min-h-0 flex-col gap-5 overflow-y-auto">
                <Card className="rise-in p-4">
                  <div className="mb-4">
                    <h2 className="text-[26px] font-semibold tracking-[-0.04em] text-[#20273a]">
                      문서 업로드
                    </h2>
                    <p
                      className="mt-2 line-clamp-2 min-h-[44px] break-all text-sm leading-5 text-[var(--muted)]"
                      title={helperText}
                    >
                      {helperText}
                    </p>
                  </div>

                  <DocumentFilePicker />

                  <Button
                    size="lg"
                    className="mt-3 w-full shrink-0"
                    disabled={!selectedFile || translateMutation.isPending}
                    onClick={() => {
                      if (!selectedFile) {
                        return;
                      }
                      translateMutation.mutate({
                        fileBase64: selectedFile.base64,
                        filename: selectedFile.file.name,
                        languageKey: selectedLanguage,
                      });
                    }}
                  >
                    업로드 문서 번역 시작
                  </Button>
                </Card>

                <TargetLanguageSelector />
              </div>
            </div>
          ) : null}

          <div className="flex min-h-0 flex-1 flex-col gap-6 lg:hidden">
            <Card className="rise-in p-4">
              <div className="mb-4">
                <h2 className="text-[26px] font-semibold tracking-[-0.04em] text-[#20273a]">
                  문서 업로드
                </h2>
                <p
                  className="mt-2 line-clamp-2 min-h-[44px] break-all text-sm leading-5 text-[var(--muted)]"
                  title={helperText}
                >
                  {helperText}
                </p>
              </div>

              <DocumentFilePicker />

              <Button
                size="lg"
                className="mt-3 w-full"
                disabled={!selectedFile || translateMutation.isPending}
                onClick={() => {
                  if (!selectedFile) {
                    return;
                  }
                  translateMutation.mutate({
                    fileBase64: selectedFile.base64,
                    filename: selectedFile.file.name,
                    languageKey: selectedLanguage,
                  });
                }}
              >
                업로드 문서 번역 시작
              </Button>
            </Card>

            <TargetLanguageSelector />
          </div>

          {stage === "idle" ? (
            <button
              type="button"
              className="sidebar-toggle-button hidden lg:flex"
              onClick={() => setIsSidebarOpen((prev) => !prev)}
              aria-label={isSidebarOpen ? "왼쪽 패널 접기" : "왼쪽 패널 열기"}
              title={isSidebarOpen ? "왼쪽 패널 접기" : "왼쪽 패널 열기"}
            >
              {isSidebarOpen ? (
                <ChevronLeft className="h-3.5 w-3.5" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5" />
              )}
            </button>
          ) : null}

          <Card className="rise-in relative h-full min-h-0 min-w-0 flex-1 overflow-hidden border-0 p-4">
            <div className="pointer-events-none absolute inset-0 grid-dots opacity-60" />
            <div className="relative z-10 flex h-full min-h-0 flex-col overflow-hidden">
              {stage === "idle" && <IdlePreview />}
              {stage === "uploading" && <UploadingState />}
              {stage === "translated" && response && <TranslationWorkspace />}
            </div>
          </Card>
        </div>
      </section>
    </main>
  );
}

function IdlePreview() {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="flex flex-1 items-center justify-center rounded-[28px] bg-white/80">
        <div className="text-center">
          <div className="mx-auto flex h-24 w-24 items-center justify-center rounded-[30px] bg-[linear-gradient(180deg,#f3f7ff,#e7eefc)] text-[#4b556a]">
            <Languages className="h-11 w-11" />
          </div>
          <p className="mt-8 text-[28px] font-semibold tracking-[-0.04em] text-[#2d3448]">
            문서를 업로드하고 번역을 시작하세요.
          </p>
          <p className="mt-3 text-base text-[var(--muted)]">번역 전 대기 상태입니다.</p>
        </div>
      </div>
    </div>
  );
}
