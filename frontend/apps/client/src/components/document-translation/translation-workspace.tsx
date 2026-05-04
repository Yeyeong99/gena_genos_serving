"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Download, X } from "lucide-react";
import { Button } from "@gena/design-system";
import { requestDocumentTranslationRevision } from "@/api/document-translation";
import { useDocumentDownload } from "@/hooks/use-document-download";
import { useDocumentTranslationStore } from "@/store/document-translation-store";
import {
  LANGUAGE_OPTIONS,
  STYLE_OPTION_GROUPS_BY_LANGUAGE,
  type DebugPageTiming,
  type DocumentBlock,
  type TranslationRevisionScope,
  type TranslationStyleOptions,
} from "@/components/document-translation/types";
import { DocumentSurface } from "./workspace/document-surface";

export function TranslationWorkspace() {
  const splitContainerRef = useRef<HTMLDivElement>(null);
  const [isDownloadDialogOpen, setIsDownloadDialogOpen] = useState(false);
  const [isRevisionPanelOpen, setIsRevisionPanelOpen] = useState(true);
  const [selectedRevisionScope, setSelectedRevisionScope] = useState<TranslationRevisionScope | null>(null);
  const [revisionInstructionsByScope, setRevisionInstructionsByScope] = useState<Record<string, string>>({});
  const [revisionStyleOptions, setRevisionStyleOptions] = useState<TranslationStyleOptions>({});
  const [isRevisionScopeMenuOpen, setIsRevisionScopeMenuOpen] = useState(false);
  const [isRevisionConfirmOpen, setIsRevisionConfirmOpen] = useState(false);
  const [isRevisionSubmitting, setIsRevisionSubmitting] = useState(false);
  const [isSavingFromRevisionConfirm, setIsSavingFromRevisionConfirm] = useState(false);
  const [revisionError, setRevisionError] = useState<string | null>(null);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [hoveredBlockId, setHoveredBlockId] = useState<string | null>(null);
  const [focusBlockRequest, setFocusBlockRequest] = useState<{ id: string; nonce: number } | null>(null);
  const [leftPanePercent, setLeftPanePercent] = useState(50);
  const [isResizing, setIsResizing] = useState(false);
  const {
    selectedFile,
    editableBlocks,
    resetAll,
    response,
    selectedLanguage,
    styleOptions,
    setResult,
  } = useDocumentTranslationStore();
  const [holdLoadingOverlay, setHoldLoadingOverlay] = useState(false);
  const [liveElapsedMs, setLiveElapsedMs] = useState<number | null>(null);
  const [displayedTranslatedPreviewHtmlUrl, setDisplayedTranslatedPreviewHtmlUrl] = useState<
    string | null
  >(null);
  const [displayedTranslatedPreviewSlide, setDisplayedTranslatedPreviewSlide] = useState<number>(0);
  const [isAwaitingTranslatedPreviewLoad, setIsAwaitingTranslatedPreviewLoad] = useState(false);
  const [pendingTranslatedPreviewHtmlUrls, setPendingTranslatedPreviewHtmlUrls] = useState<
    Array<{ url: string; slide: number }>
  >([]);
  const downloadMutation = useDocumentDownload();
  const fileType = selectedFile?.file.name.split(".").pop()?.toLowerCase() ?? "";
  const previewRenderMode = response?.preview_render_mode ?? "synthetic";
  const isDeferredPreviewPending = response?.translated_preview_status === "pending";
  const hasTranslationError =
    Boolean(response?.translation_error) || response?.translation_status === "error";
  const useHtmlOnlyTranslatedPreview = ["pptx", "docx", "xlsx"].includes(fileType);
  const isSpreadsheetPreview = fileType === "xlsx";
  const isDocumentPagePreview = fileType === "docx";
  const shouldUseOriginalPreviewAsTranslation =
    useHtmlOnlyTranslatedPreview &&
    Boolean(response?.original_preview_html_url) &&
    response?.translation_status === "done" &&
    !displayedTranslatedPreviewHtmlUrl &&
    !response?.translated_preview_html_url;
  const isTranslationPending =
    (response?.translation_status === "pending" || response?.translation_status === "translating") &&
    !response?.text;
  const isPreviewTranslationInProgress =
    response?.translation_status === "pending" || response?.translation_status === "translating";
  const rightPreviewImages = isDeferredPreviewPending ? undefined : response?.translated_preview_images;
  const rightPreviewHtmlUrl = useHtmlOnlyTranslatedPreview
    ? displayedTranslatedPreviewHtmlUrl ??
      (response?.translation_status === "done"
        ? response?.translated_preview_html_url ??
          (shouldUseOriginalPreviewAsTranslation ? response?.original_preview_html_url : undefined)
        : undefined)
    : response?.translated_preview_html_url;
  const rightPreviewRenderMode =
    useHtmlOnlyTranslatedPreview
      ? "html"
      : isDeferredPreviewPending
        ? "synthetic"
        : previewRenderMode;
  const hasTranslatedHtmlPreview = Boolean(rightPreviewHtmlUrl);
  const isWaitingForTranslatedHtmlPreview =
    useHtmlOnlyTranslatedPreview && !rightPreviewHtmlUrl && !hasTranslationError;
  const showInlineTranslationStatus =
    useHtmlOnlyTranslatedPreview && isPreviewTranslationInProgress && hasTranslatedHtmlPreview;
  const showTranslationLoadingOverlay = hasTranslationError
    ? false
    : useHtmlOnlyTranslatedPreview
      ? isDocumentPagePreview
        ? false
        : isWaitingForTranslatedHtmlPreview || (!hasTranslatedHtmlPreview && holdLoadingOverlay)
      : isTranslationPending || holdLoadingOverlay;
  const rightEditable = useHtmlOnlyTranslatedPreview ? false : !rightPreviewHtmlUrl;
  const rightBlocks = useHtmlOnlyTranslatedPreview && !rightPreviewHtmlUrl ? [] : editableBlocks;
  const progressUnitLabel = isSpreadsheetPreview
    ? "시트"
    : fileType === "pptx"
      ? "슬라이드"
      : isDocumentPagePreview
        ? "구간"
        : "페이지";
  const totalProgressUnits = isSpreadsheetPreview
    ? response?.total_sheets ?? 0
    : isDocumentPagePreview
      ? response?.total_pages ?? getTotalSlides(response?.document_blocks)
      : response?.total_slides ?? getTotalSlides(response?.document_blocks);
  const currentProgressUnit =
    displayedTranslatedPreviewSlide ||
    (isSpreadsheetPreview
      ? response?.current_sheet
      : isDocumentPagePreview
        ? response?.current_page
        : response?.current_slide) ||
    0;
  const inlineTranslationStatusText =
    showInlineTranslationStatus && isDocumentPagePreview
      ? currentProgressUnit > 0
        ? totalProgressUnits > 0
          ? `번역 중 · ${Math.min(currentProgressUnit, totalProgressUnits)}/${totalProgressUnits} 페이지 완료`
          : `번역 중 · ${currentProgressUnit} 페이지 완료`
        : "번역 중 · 완료된 페이지부터 반영"
      : showInlineTranslationStatus
        ? "번역 중 · 완료된 페이지부터 반영"
        : undefined;
  const inlineProgressOverlay =
    useHtmlOnlyTranslatedPreview &&
    hasTranslatedHtmlPreview &&
    totalProgressUnits > 0 &&
    currentProgressUnit > 0 &&
    isDeferredPreviewPending
      ? {
          current: Math.min(currentProgressUnit, totalProgressUnits),
          total: totalProgressUnits,
          message:
            isDocumentPagePreview
              ? `${Math.min(currentProgressUnit, totalProgressUnits)}/${totalProgressUnits} 페이지까지 반영되었습니다. 나머지는 병렬 번역 중입니다.`
              : currentProgressUnit < totalProgressUnits
              ? `${Math.min(currentProgressUnit + 1, totalProgressUnits)} ${progressUnitLabel} 번역 중입니다.`
              : `${Math.min(currentProgressUnit, totalProgressUnits)}/${totalProgressUnits} ${progressUnitLabel}까지 반영되었습니다.`,
        }
      : null;
  const currentSheetLabel =
    response?.current_sheet_name ??
    (typeof response?.current_sheet === "number" ? `${response.current_sheet} 시트` : "엑셀 시트");
  const translationLoadingMessage = (() => {
    if (!useHtmlOnlyTranslatedPreview) {
      return "번역 텍스트를 준비하고 있습니다. 먼저 원본 문서를 확인하실 수 있습니다.";
    }
    if (isSpreadsheetPreview) {
      if (response?.event_phase === "sheet_translation_started") {
        return `${currentSheetLabel} 번역 중입니다.`;
      }
      if (response?.event_phase === "sheet_translated" || response?.event_phase === "sheet_injected") {
        return `${currentSheetLabel} HTML 미리보기를 생성하는 중입니다.`;
      }
      return `${currentSheetLabel} HTML 미리보기를 생성하는 중입니다. 변환이 끝나면 오른쪽 미리보기에 반영됩니다.`;
    }
    if (isDocumentPagePreview) {
      return "DOCX 번역을 병렬로 처리하는 중입니다. 준비된 앞쪽 페이지부터 오른쪽 미리보기에 반영됩니다.";
    }
    if (response?.event_phase === "slide_translation_started" && typeof response.current_slide === "number") {
      return `${response.current_slide} ${progressUnitLabel} 번역 중입니다.`;
    }
    return `번역본 ${progressUnitLabel}를 생성하고 있습니다. 먼저 원본 문서를 확인하실 수 있습니다.`;
  })();
  const translationElapsedLabel =
    typeof response?.elapsed_ms === "number" && response.elapsed_ms >= 0
      ? formatElapsedMs(response.elapsed_ms)
      : typeof liveElapsedMs === "number" && liveElapsedMs >= 0
        ? formatElapsedMs(liveElapsedMs)
        : null;
  const debugPageTimings = response?.debug_page_timings ?? [];
  const debugModelLabel = response?.llm_model_name
    ? `${response.llm_model_name}${response.llm_provider_sort ? ` · ${response.llm_provider_sort}` : ""}`
    : null;
  const canDownloadTranslatedFile =
    Boolean(selectedFile) &&
    !downloadMutation.isPending &&
    (!response?.job_id || response.translation_status === "done");
  const shouldConfirmNavigationAway = Boolean(
    response?.job_id &&
      response.translation_status !== "error" &&
      (response.translation_status !== "done" || response.translated_preview_status === "pending"),
  );
  const selectedLanguageFormat =
    LANGUAGE_OPTIONS.find((item) => item.key === selectedLanguage)?.format ?? response?.format ?? "Korean";
  const revisionStyleGroups = STYLE_OPTION_GROUPS_BY_LANGUAGE[selectedLanguage] ?? [];
  const canReviseDocument =
    Boolean(response?.job_id) &&
    ["pptx", "xlsx", "docx"].includes(fileType) &&
    response?.translation_status === "done";
  const revisionUnitButtons = useMemo(
    () => buildRevisionUnitButtons(fileType, response),
    [fileType, response],
  );
  const selectedRevisionUnitLabel =
    selectedRevisionScope?.label ??
    revisionUnitButtons.find(
      (unit) => unit.type === selectedRevisionScope?.type && unit.index === selectedRevisionScope?.index,
    )?.label;
  const selectedRevisionScopeKey = getRevisionScopeKey(selectedRevisionScope);
  const revisionInstruction = revisionInstructionsByScope[selectedRevisionScopeKey] ?? "";
  const pendingScopedRevisionEntries = revisionUnitButtons
    .map((unit) => ({
      scope: unit,
      instruction: (revisionInstructionsByScope[getRevisionScopeKey(unit)] ?? "").trim(),
    }))
    .filter((item) => item.instruction);
  const pendingScopedRevisionCount = pendingScopedRevisionEntries.length;
  const revisionPlaceholder = canReviseDocument
    ? "기본 옵션 이외의 수정 사항을 말씀해주세요."
    : "번역 완료 시 수정 가능합니다.";

  useEffect(() => {
    setRevisionStyleOptions(styleOptions);
  }, [styleOptions, response?.job_id]);

  useEffect(() => {
    setRevisionInstructionsByScope({});
    setSelectedRevisionScope(null);
    setIsRevisionScopeMenuOpen(false);
  }, [response?.job_id]);

  useEffect(() => {
    if (!response?.job_id || !isTranslationPending) {
      setHoldLoadingOverlay(false);
      return;
    }

    setHoldLoadingOverlay(true);
    const timer = window.setTimeout(() => {
      setHoldLoadingOverlay(false);
    }, 1600);

    return () => {
      window.clearTimeout(timer);
    };
  }, [response?.job_id, isTranslationPending]);

  useEffect(() => {
    setDisplayedTranslatedPreviewHtmlUrl(null);
    setDisplayedTranslatedPreviewSlide(0);
    setIsAwaitingTranslatedPreviewLoad(false);
    setPendingTranslatedPreviewHtmlUrls([]);
  }, [response?.job_id]);

  useEffect(() => {
    const phase = response?.event_phase;
    const nextUrl = response?.translated_preview_html_url;
    const nextSlide =
      (isSpreadsheetPreview
        ? response?.current_sheet
        : isDocumentPagePreview
          ? response?.current_page
          : response?.current_slide) ??
      (response?.translated_preview_status === "done"
        ? isSpreadsheetPreview
          ? response?.total_sheets ?? 1
          : isDocumentPagePreview
            ? response?.total_pages ?? 1
            : response?.total_slides ?? 1
        : undefined);
    if (!nextUrl || typeof nextSlide !== "number" || nextSlide <= 0) {
      return;
    }

    if (
      phase !== "slide_html_ready" &&
      phase !== "sheet_html_ready" &&
      phase !== "page_html_ready" &&
      phase !== "completed"
    ) {
      return;
    }

    if (displayedTranslatedPreviewHtmlUrl === nextUrl) {
      return;
    }

    if (!displayedTranslatedPreviewHtmlUrl && !isAwaitingTranslatedPreviewLoad) {
      setDisplayedTranslatedPreviewHtmlUrl(nextUrl);
      setDisplayedTranslatedPreviewSlide(nextSlide);
      setIsAwaitingTranslatedPreviewLoad(true);
      return;
    }

    setPendingTranslatedPreviewHtmlUrls((current) => {
      if (current.some((item) => item.url === nextUrl)) {
        return current;
      }
      return [...current, { url: nextUrl, slide: nextSlide }];
    });
  }, [
    response?.event_phase,
    response?.translated_preview_html_url,
    response?.current_slide,
    response?.current_page,
    response?.current_sheet,
    response?.translated_preview_status,
    response?.total_slides,
    response?.total_pages,
    response?.total_sheets,
    isSpreadsheetPreview,
    isDocumentPagePreview,
    displayedTranslatedPreviewHtmlUrl,
    isAwaitingTranslatedPreviewLoad,
  ]);

  useEffect(() => {
    if (pendingTranslatedPreviewHtmlUrls.length === 0 || isAwaitingTranslatedPreviewLoad) {
      return;
    }

    const nextItem = pendingTranslatedPreviewHtmlUrls[0];
    setDisplayedTranslatedPreviewHtmlUrl(nextItem.url);
    setDisplayedTranslatedPreviewSlide(nextItem.slide);
    setIsAwaitingTranslatedPreviewLoad(true);
  }, [isAwaitingTranslatedPreviewLoad, pendingTranslatedPreviewHtmlUrls]);

  const handleTranslatedPreviewLoaded = () => {
    if (!isAwaitingTranslatedPreviewLoad) {
      return;
    }
    window.setTimeout(() => {
      setPendingTranslatedPreviewHtmlUrls((current) => current.slice(1));
      setIsAwaitingTranslatedPreviewLoad(false);
    }, 450);
  };

  useEffect(() => {
    if (typeof response?.elapsed_ms === "number" && response.elapsed_ms >= 0) {
      setLiveElapsedMs(null);
      return;
    }

    if (!hasTranslatedHtmlPreview || typeof response?.created_at !== "number") {
      setLiveElapsedMs(null);
      return;
    }

    const updateElapsed = () => {
      setLiveElapsedMs(Math.max(0, Math.round(Date.now() - response.created_at! * 1000)));
    };

    updateElapsed();
    const timer = window.setInterval(updateElapsed, 500);
    return () => {
      window.clearInterval(timer);
    };
  }, [hasTranslatedHtmlPreview, response?.created_at, response?.elapsed_ms]);

  useEffect(() => {
    if (!isResizing) {
      return;
    }

    const handlePointerMove = (event: PointerEvent) => {
      const container = splitContainerRef.current;
      if (!container) {
        return;
      }

      const rect = container.getBoundingClientRect();
      const nextPercent = ((event.clientX - rect.left) / rect.width) * 100;
      setLeftPanePercent(Math.max(28, Math.min(72, nextPercent)));
    };

    const stopResizing = () => setIsResizing(false);

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResizing);
    window.addEventListener("pointercancel", stopResizing);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResizing);
      window.removeEventListener("pointercancel", stopResizing);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [isResizing]);

  useEffect(() => {
    if (!shouldConfirmNavigationAway) {
      return;
    }

    const message = "번역이 아직 진행 중입니다. 이 화면을 벗어나면 진행 중인 미리보기를 다시 볼 수 없을 수 있습니다.";
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = message;
      return message;
    };
    const cleanupListeners = () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
      window.removeEventListener("popstate", handlePopState);
    };
    const handlePopState = () => {
      if (window.confirm(`${message}\n\n그래도 뒤로 이동하시겠습니까?`)) {
        cleanupListeners();
        window.history.back();
        return;
      }
      window.history.pushState({ documentTranslationGuard: true }, "", window.location.href);
    };

    window.history.pushState({ documentTranslationGuard: true }, "", window.location.href);
    window.addEventListener("beforeunload", handleBeforeUnload);
    window.addEventListener("popstate", handlePopState);
    return cleanupListeners;
  }, [shouldConfirmNavigationAway]);

  const handleDownloadConfirm = async (options?: { closeDownloadDialog?: boolean }) => {
    if (!selectedFile) {
      return;
    }
    if (response?.job_id && response.translation_status !== "done") {
      window.alert("번역 파일이 아직 최종 저장되지 않았습니다. 번역 완료 후 다시 다운로드해 주세요.");
      return;
    }

    try {
      await downloadMutation.mutateAsync({
        fileBase64: selectedFile.base64,
        filename: selectedFile.file.name,
      });
      if (options?.closeDownloadDialog !== false) {
        setIsDownloadDialogOpen(false);
      }
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "파일 저장 및 다운로드에 실패했습니다.");
    }
  };

  const handleSaveFromRevisionConfirm = async () => {
    setIsSavingFromRevisionConfirm(true);
    try {
      await handleDownloadConfirm({ closeDownloadDialog: false });
    } finally {
      setIsSavingFromRevisionConfirm(false);
    }
  };

  const submitRevisionRequests = async (
    requests: Array<{ scope: TranslationRevisionScope | null; instruction: string }>,
  ) => {
    if (!response?.job_id) {
      return;
    }
    setRevisionError(null);
    setIsRevisionSubmitting(true);
    try {
      let mergedResponse = response;
      let lastPreviewUrl = "";
      let lastPreviewUnit = displayedTranslatedPreviewSlide;
      for (const request of requests) {
        const revised = await requestDocumentTranslationRevision({
          job_id: response.job_id,
          format: selectedLanguageFormat,
          scope: request.scope,
          style_options: revisionStyleOptions,
          instruction: request.instruction.trim(),
        });
        if (revised.translation_error) {
          throw new Error(revised.translation_error);
        }
        mergedResponse = {
          ...mergedResponse,
          ...revised,
          job_id: response.job_id,
        };
        if (revised.translated_preview_html_url) {
          lastPreviewUrl = revised.translated_preview_html_url;
          lastPreviewUnit =
            revised.current_sheet ?? revised.current_slide ?? revised.current_page ?? lastPreviewUnit;
        }
      }
      setResult(mergedResponse);
      if (lastPreviewUrl) {
        setDisplayedTranslatedPreviewHtmlUrl(lastPreviewUrl);
        setDisplayedTranslatedPreviewSlide(
          lastPreviewUnit,
        );
        setPendingTranslatedPreviewHtmlUrls([]);
        setIsAwaitingTranslatedPreviewLoad(true);
      }
      setRevisionInstructionsByScope({});
      setIsRevisionConfirmOpen(false);
    } catch (error) {
      setRevisionError(error instanceof Error ? error.message : "번역 수정에 실패했습니다.");
    } finally {
      setIsRevisionSubmitting(false);
    }
  };

  const submitRevision = async (scope: TranslationRevisionScope | null) => {
    await submitRevisionRequests([
      {
        scope,
        instruction: (revisionInstructionsByScope[getRevisionScopeKey(scope)] ?? "").trim(),
      },
    ]);
  };

  const handleRevisionApply = () => {
    if (!canReviseDocument || isRevisionSubmitting) {
      return;
    }
    if (pendingScopedRevisionEntries.length > 0) {
      void submitRevisionRequests(pendingScopedRevisionEntries);
      return;
    }
    if (!selectedRevisionScope) {
      setIsRevisionConfirmOpen(true);
      return;
    }
    void submitRevision(selectedRevisionScope);
  };

  const handleNewFileTranslation = () => {
    if (!response?.job_id) {
      resetAll();
      return;
    }

    const message =
      response.translation_status === "done"
        ? "현재 번역 결과 화면을 닫고 새 파일 번역을 시작합니다. 저장하지 않은 수정 내용은 사라질 수 있습니다."
        : "번역이 아직 진행 중입니다. 이 화면을 벗어나면 진행 중인 미리보기를 다시 볼 수 없을 수 있습니다.";

    if (window.confirm(`${message}\n\n그래도 새 파일 번역으로 이동하시겠습니까?`)) {
      resetAll();
    }
  };

  const focusTranslatedBlock = (id: string) => {
    setSelectedBlockId(id);
    setFocusBlockRequest({ id, nonce: Date.now() });
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div
          ref={splitContainerRef}
          className="flex min-h-0 flex-1 flex-col gap-6 overflow-hidden lg:grid lg:gap-0"
          style={{
            gridTemplateColumns: `${leftPanePercent}fr 14px ${100 - leftPanePercent}fr`,
          }}
        >
          <DocumentSurface
            title="원본"
            blocks={editableBlocks}
            editable={false}
            fileType={fileType}
            previewHtmlUrl={response?.original_preview_html_url}
            previewImages={response?.original_preview_images}
            previewPageSizes={response?.preview_page_sizes}
            previewRenderMode={previewRenderMode}
            selectedBlockId={selectedBlockId}
            hoveredBlockId={hoveredBlockId}
            onSelectBlock={focusTranslatedBlock}
            onHoverBlock={setHoveredBlockId}
            iframeRevisionScopes={canReviseDocument && fileType === "docx" ? revisionUnitButtons : undefined}
            selectedRevisionScopeKey={selectedRevisionScopeKey}
            onIframeRevisionScopeSelect={(scope) => {
              setSelectedRevisionScope(scope);
              setIsRevisionPanelOpen(true);
              setIsRevisionScopeMenuOpen(false);
            }}
          />
          <div
            className="hidden h-full cursor-col-resize items-center justify-center lg:flex"
            role="separator"
            aria-orientation="vertical"
            aria-label="원본과 번역본 영역 크기 조절"
            tabIndex={0}
            onPointerDown={(event) => {
              event.preventDefault();
              setIsResizing(true);
            }}
          >
            <span
              className={`h-24 w-px rounded-full transition ${
                isResizing ? "bg-[var(--primary)]" : "bg-[rgba(115,136,191,0.22)]"
              }`}
            />
          </div>
          <div className="relative min-h-0">
            <DocumentSurface
              title="번역본"
            statusText={inlineTranslationStatusText}
            statusLoading={showInlineTranslationStatus}
            titleActions={
              <>
                <Button variant="outline" className="h-8 px-3 text-xs" onClick={handleNewFileTranslation}>
                  새 파일 번역
                </Button>
                <Button
                  className="h-8 px-3 text-xs"
                  onClick={() => {
                    downloadMutation.reset();
                    setIsDownloadDialogOpen(true);
                  }}
                  disabled={!canDownloadTranslatedFile}
                >
                  <Download className="h-3.5 w-3.5" />
                  {downloadMutation.isPending ? "저장 중..." : "다운로드"}
                </Button>
              </>
            }
            blocks={rightBlocks}
              editable={rightEditable}
              fileType={fileType}
              previewHtmlUrl={rightPreviewHtmlUrl}
              previewImages={rightPreviewImages}
              previewPageSizes={response?.preview_page_sizes}
              previewRenderMode={rightPreviewRenderMode}
              progressOverlay={inlineProgressOverlay}
              onPreviewHtmlLoaded={useHtmlOnlyTranslatedPreview ? handleTranslatedPreviewLoaded : undefined}
              selectedBlockId={selectedBlockId}
              hoveredBlockId={hoveredBlockId}
              onSelectBlock={setSelectedBlockId}
              onHoverBlock={setHoveredBlockId}
              focusBlockRequest={focusBlockRequest}
              loadingOverlay={showTranslationLoadingOverlay}
              loadingOverlayMessage={translationLoadingMessage}
              iframeRevisionScopes={canReviseDocument && fileType === "docx" ? revisionUnitButtons : undefined}
              selectedRevisionScopeKey={selectedRevisionScopeKey}
              onIframeRevisionScopeSelect={(scope) => {
                setSelectedRevisionScope(scope);
                setIsRevisionPanelOpen(true);
                setIsRevisionScopeMenuOpen(false);
              }}
            />
            {!showTranslationLoadingOverlay && translationElapsedLabel ? (
              <div className="pointer-events-none absolute bottom-4 right-4 z-30">
                <div className="rounded-full border border-[rgba(98,88,245,0.16)] bg-white/92 px-3 py-1.5 text-xs font-medium tracking-[0.01em] text-[var(--muted)] shadow-[0_8px_24px_rgba(15,23,42,0.10)]">
                  번역 완료 {translationElapsedLabel}
                </div>
              </div>
            ) : null}
            {debugModelLabel || debugPageTimings.length > 0 ? (
              <div className="pointer-events-none absolute bottom-4 left-4 z-30 max-w-[min(360px,calc(100%-2rem))]">
                <div className="rounded-2xl border border-[rgba(98,88,245,0.14)] bg-white/94 px-3 py-2 text-xs text-[#47526d] shadow-[0_12px_34px_rgba(15,23,42,0.12)] backdrop-blur">
                  {debugModelLabel ? (
                    <p className="font-semibold text-[#27324a]">모델 {debugModelLabel}</p>
                  ) : null}
                  {debugPageTimings.length > 0 ? (
                    <div className="mt-1.5 space-y-1">
                      {debugPageTimings.slice(-6).map((item, index) => (
                        <p key={`${item.scope ?? item.label ?? "timing"}-${index}`} className="whitespace-nowrap">
                          {formatDebugTiming(item)}
                        </p>
                      ))}
                    </div>
                  ) : (
                    <p className="mt-1.5">페이지 렌더링 대기 중</p>
                  )}
                </div>
              </div>
            ) : null}
          </div>
        </div>

        <div className="mt-3 shrink-0 border-t border-[rgba(115,136,191,0.18)] pt-3">
          {isRevisionPanelOpen ? (
            <div className="relative rounded-2xl border border-[rgba(115,136,191,0.20)] bg-white/82 p-3 shadow-[0_10px_28px_rgba(15,23,42,0.08)]">
              <button
                type="button"
                className="absolute right-3 top-3 rounded-full border border-[#dbe3f2] bg-white p-1.5 text-[#66718a] hover:border-[var(--primary)] hover:text-[var(--primary)]"
                onClick={() => setIsRevisionPanelOpen(false)}
                aria-label="수정 영역 접기"
                title="수정 영역 접기"
              >
                <ChevronDown className="h-4 w-4" />
              </button>
              <div className="flex flex-wrap items-center justify-center gap-2 px-10">
                <button
                  type="button"
                  className={`shrink-0 rounded-full border px-3 py-1.5 text-xs font-semibold ${
                    selectedRevisionScope === null
                      ? "border-[var(--primary)] bg-[rgba(98,88,245,0.10)] text-[var(--primary)]"
                      : "border-[#dbe3f2] bg-white text-[#52607a]"
                  }`}
                  onClick={() => {
                    setSelectedRevisionScope(null);
                    setIsRevisionScopeMenuOpen(false);
                  }}
                >
                  전체
                </button>
                {["xlsx", "docx"].includes(fileType) && revisionUnitButtons.length > 0 ? (
                  <div
                    className="relative min-w-0"
                    onBlur={(event) => {
                      const nextTarget = event.relatedTarget;
                      if (!(nextTarget instanceof Node) || !event.currentTarget.contains(nextTarget)) {
                        setIsRevisionScopeMenuOpen(false);
                      }
                    }}
                  >
                    <button
                      type="button"
                      className={`flex h-9 max-w-[min(360px,55vw)] items-center gap-2 rounded-full border px-3 text-xs font-semibold ${
                        selectedRevisionScope?.type === "sheet" || selectedRevisionScope?.type === "batch"
                          ? "border-[var(--primary)] bg-[rgba(98,88,245,0.10)] text-[var(--primary)]"
                          : "border-[#dbe3f2] bg-white text-[#52607a]"
                      }`}
                      onClick={() => setIsRevisionScopeMenuOpen((current) => !current)}
                      aria-haspopup="listbox"
                      aria-expanded={isRevisionScopeMenuOpen}
                    >
                      <span className="truncate">
                        {selectedRevisionUnitLabel
                          ? `${fileType === "docx" ? "구간" : "시트"}: ${selectedRevisionUnitLabel}`
                          : `${fileType === "docx" ? "구간" : "시트"} 선택`}
                      </span>
                      {pendingScopedRevisionCount > 0 ? (
                        <span className="shrink-0 rounded-full bg-[rgba(98,88,245,0.12)] px-1.5 py-0.5 text-[10px] text-[var(--primary)]">
                          {pendingScopedRevisionCount}
                        </span>
                      ) : null}
                      <ChevronDown className="h-4 w-4 shrink-0" />
                    </button>
                    {isRevisionScopeMenuOpen ? (
                      <div
                        className="absolute bottom-full left-0 z-50 mb-2 w-[min(420px,calc(100vw-3rem))] overflow-hidden rounded-2xl border border-[#dbe3f2] bg-white shadow-[0_18px_46px_rgba(15,23,42,0.18)]"
                        role="listbox"
                      >
                        <div className="max-h-[22.5rem] overflow-y-auto p-1.5">
                          {revisionUnitButtons.map((unit) => (
                            <button
                              key={`${unit.type}-${unit.index}`}
                              type="button"
                              className={`flex h-9 w-full items-center rounded-xl px-3 text-left text-sm font-medium ${
                                selectedRevisionScope?.type === unit.type &&
                                selectedRevisionScope.index === unit.index
                                  ? "bg-[rgba(98,88,245,0.10)] text-[var(--primary)]"
                                  : "text-[#52607a] hover:bg-[#f6f8fc]"
                              }`}
                              role="option"
                              aria-selected={
                                selectedRevisionScope?.type === unit.type &&
                                selectedRevisionScope.index === unit.index
                              }
                              onClick={() => {
                                setSelectedRevisionScope(unit);
                                setIsRevisionScopeMenuOpen(false);
                              }}
                            >
                              <span className="truncate">{unit.label}</span>
                              {revisionInstructionsByScope[getRevisionScopeKey(unit)]?.trim() ? (
                                <span className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--primary)]" />
                              ) : null}
                            </button>
                          ))}
                        </div>
                      </div>
                    ) : null}
                  </div>
                ) : (
                  <div className="flex min-w-0 flex-wrap items-center justify-center gap-2">
                    {revisionUnitButtons.map((unit) => (
                      <button
                        key={`${unit.type}-${unit.index}`}
                        type="button"
                        className={`rounded-full border px-3 py-1.5 text-xs font-semibold ${
                          selectedRevisionScope?.type === unit.type && selectedRevisionScope.index === unit.index
                            ? "border-[var(--primary)] bg-[rgba(98,88,245,0.10)] text-[var(--primary)]"
                            : "border-[#dbe3f2] bg-white text-[#52607a]"
                        }`}
                        onClick={() => setSelectedRevisionScope(unit)}
                      >
                        {unit.label}
                        {revisionInstructionsByScope[getRevisionScopeKey(unit)]?.trim() ? " ·" : ""}
                      </button>
                    ))}
                  </div>
                )}
                {!["pptx", "xlsx", "docx"].includes(fileType) ? (
                  <span className="text-xs font-medium text-[var(--muted)]">
                    시트/구간 수정은 다음 단계에서 연결됩니다.
                  </span>
                ) : null}
              </div>

              <div className="mt-3 grid gap-2 md:grid-cols-3">
                {revisionStyleGroups.map((group) => (
                  <label key={group.key} className="flex min-w-0 flex-col gap-1 text-xs font-semibold text-[#52607a]">
                    {group.label}
                    <select
                      className="h-9 rounded-lg border border-[#dbe3f2] bg-white px-2 text-sm font-medium text-[#263149] outline-none focus:border-[var(--primary)]"
                      value={revisionStyleOptions[group.key] ?? ""}
                      onChange={(event) =>
                        setRevisionStyleOptions((current) => ({
                          ...current,
                          [group.key]: event.target.value,
                        }))
                      }
                    >
                      {group.options.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>

              <div className="mt-3 flex gap-3">
                <textarea
                  className="min-h-[74px] flex-1 resize-none rounded-xl border border-[#dbe3f2] bg-white px-3 py-2 text-sm leading-5 text-[#263149] outline-none focus:border-[var(--primary)]"
                  placeholder={revisionPlaceholder}
                  value={revisionInstruction}
                  onChange={(event) =>
                    setRevisionInstructionsByScope((current) => ({
                      ...current,
                      [selectedRevisionScopeKey]: event.target.value,
                    }))
                  }
                />
                <Button
                  className="self-stretch px-5"
                  disabled={!canReviseDocument || isRevisionSubmitting}
                  onClick={handleRevisionApply}
                >
                  {isRevisionSubmitting ? "반영 중..." : "반영하기"}
                </Button>
              </div>
              {revisionError ? (
                <p className="mt-2 text-xs font-semibold text-[#d14343]">{revisionError}</p>
              ) : null}
            </div>
          ) : (
            <button
              type="button"
              className="flex w-full items-center justify-center rounded-xl border border-[rgba(115,136,191,0.20)] bg-white/70 py-2 text-xs font-semibold text-[#66718a] hover:border-[var(--primary)] hover:text-[var(--primary)]"
              onClick={() => setIsRevisionPanelOpen(true)}
            >
              <ChevronUp className="mr-1.5 h-4 w-4" />
              수정 영역 열기
            </button>
          )}
        </div>
      </div>

      {isRevisionConfirmOpen ? (
        <div className="dialog-backdrop">
          <div className="dialog-panel">
            <div className="flex items-start justify-between gap-4">
              <h3 className="text-[22px] font-semibold tracking-[-0.03em] text-[#1f2638]">
                전체 문서를 다시 번역합니다.
              </h3>
              <button
                type="button"
                className="rounded-full p-1 text-[#6b7488] hover:bg-[#f1f4fa]"
                onClick={() => setIsRevisionConfirmOpen(false)}
                aria-label="닫기"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
            <p className="mt-3 text-sm leading-6 text-[var(--muted)]">
              선택된 슬라이드가 없습니다. 현재 옵션과 수정 지시를 전체 문서에 적용해 번역을 다시 실행합니다.
            </p>
            <div className="mt-6 flex flex-wrap items-center justify-end gap-3">
              <Button
                variant="outline"
                onClick={() => void handleSaveFromRevisionConfirm()}
                disabled={isRevisionSubmitting || downloadMutation.isPending || isSavingFromRevisionConfirm}
              >
                {downloadMutation.isPending || isSavingFromRevisionConfirm
                  ? "저장 중..."
                  : "현재 번역 결과 저장"}
              </Button>
              <Button onClick={() => void submitRevision(null)} disabled={isRevisionSubmitting}>
                {isRevisionSubmitting ? "다시 실행 중..." : "번역 다시 실행"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {isDownloadDialogOpen ? (
        <div className="dialog-backdrop">
          <div className="dialog-panel">
            <div className="flex items-start justify-between gap-4">
              <h3 className="text-[22px] font-semibold tracking-[-0.03em] text-[#1f2638]">
                변경 사항이 반영된 내용으로 파일을 저장합니다.
              </h3>
              <button
                type="button"
                className="rounded-full p-1 text-[#6b7488] hover:bg-[#f1f4fa]"
                onClick={() => {
                  downloadMutation.reset();
                  setIsDownloadDialogOpen(false);
                }}
                disabled={downloadMutation.isPending}
                aria-label="닫기"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
            <p className="mt-3 text-sm leading-6 text-[var(--muted)]">
              저장 및 다운로드를 누르면 오른쪽 편집 영역의 최신 내용으로 문서를 생성한 뒤 바로
              다운로드합니다.
            </p>
            {downloadMutation.error ? (
              <p className="mt-3 text-sm text-[#d14343]">
                {downloadMutation.error instanceof Error
                  ? downloadMutation.error.message
                  : "파일 저장 및 다운로드에 실패했습니다."}
              </p>
            ) : null}
            <div className="mt-6 flex items-center justify-end gap-3">
              <Button onClick={() => void handleDownloadConfirm()} disabled={downloadMutation.isPending}>
                {downloadMutation.isPending ? "저장 및 다운로드 중..." : "저장 및 다운로드"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function formatElapsedMs(elapsedMs: number): string {
  if (elapsedMs < 1000) {
    return `${elapsedMs}ms`;
  }

  const seconds = elapsedMs / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(seconds >= 10 ? 0 : 1)}초`;
  }

  const minutes = Math.floor(seconds / 60);
  const remainSeconds = Math.round(seconds % 60);
  return `${minutes}분 ${remainSeconds}초`;
}

function formatDebugTiming(item: DebugPageTiming): string {
  const label = item.label ?? (typeof item.index === "number" ? `${item.index}` : item.scope ?? "페이지");
  const ready =
    typeof item.html_ready_elapsed_ms === "number" ? formatElapsedMs(item.html_ready_elapsed_ms) : "-";
  const render =
    typeof item.html_render_ms === "number" ? formatElapsedMs(item.html_render_ms) : "-";
  return `${label} 표시 ${ready} · HTML ${render}`;
}

function getTotalSlides(blocks?: DocumentBlock[]): number {
  if (!blocks || blocks.length === 0) {
    return 0;
  }

  const slides = new Set<number>();
  for (const block of blocks) {
    const slide = block.location?.slide;
    if (typeof slide === "number" && Number.isFinite(slide)) {
      slides.add(slide);
    }
  }
  return slides.size;
}

function buildRevisionUnitButtons(
  fileType: string,
  response: {
    total_slides?: number;
    total_sheets?: number;
    total_pages?: number;
    document_blocks?: DocumentBlock[];
  } | null,
): TranslationRevisionScope[] {
  if (fileType === "pptx") {
    const totalSlides = response?.total_slides ?? getTotalSlides(response?.document_blocks);
    if (!totalSlides || totalSlides < 1) {
      return [];
    }

    return Array.from({ length: totalSlides }, (_, index) => {
      const slideIndex = index + 1;
      return {
        type: "slide",
        index: slideIndex,
        label: `${slideIndex}`,
      };
    });
  }

  if (fileType === "xlsx") {
    const sheetNames = getSheetNames(response?.document_blocks);
    if (sheetNames.length > 0) {
      return sheetNames.map((sheet, index) => ({
        type: "sheet",
        index: index + 1,
        label: `${sheet} (${index + 1})`,
      }));
    }

    const totalSheets = response?.total_sheets ?? 0;
    return Array.from({ length: totalSheets }, (_, index) => ({
      type: "sheet",
      index: index + 1,
      label: `${index + 1} 시트`,
    }));
  }

  if (fileType === "docx") {
    return getDocxRevisionSections(response?.document_blocks, response?.total_pages);
  }

  return [];
}

function getRevisionScopeKey(scope: TranslationRevisionScope | null): string {
  if (!scope) {
    return "document";
  }
  const index = scope.index ?? scope.label ?? "";
  return `${scope.type}:${index}`;
}

function getSheetNames(blocks?: DocumentBlock[]): string[] {
  if (!blocks || blocks.length === 0) {
    return [];
  }

  const sheetNames: string[] = [];
  const seen = new Set<string>();
  for (const block of blocks) {
    const sheet = block.location?.sheet;
    if (!sheet || seen.has(sheet)) {
      continue;
    }
    sheetNames.push(sheet);
    seen.add(sheet);
  }
  return sheetNames;
}

function getDocxRevisionSections(blocks?: DocumentBlock[], totalPages?: number): TranslationRevisionScope[] {
  const sections = new Map<number, string>();
  if (blocks) {
    for (const block of blocks) {
      const page = block.location?.page ?? block.location?.translated_page;
      if (typeof page !== "number" || !Number.isFinite(page) || page < 1 || sections.has(page)) {
        continue;
      }
      const sample = getDocxSectionSample(block);
      sections.set(page, sample);
    }
  }

  if (sections.size === 0 && totalPages && totalPages > 0) {
    for (let index = 1; index <= totalPages; index += 1) {
      sections.set(index, "");
    }
  }

  return Array.from(sections.entries())
    .sort(([left], [right]) => left - right)
    .map(([page, sample]) => ({
      type: "batch",
      index: page,
      label: sample ? `구간 ${page} · ${sample}` : `구간 ${page}`,
    }));
}

function getDocxSectionSample(block: DocumentBlock): string {
  const source = block.translated || block.original || "";
  return source.replace(/\s+/g, " ").trim().slice(0, 28);
}
