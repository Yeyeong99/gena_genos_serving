"use client";

import { type CSSProperties, type ReactNode, useEffect, useRef, useState } from "react";
import { EditableBlock, type TranslationRevisionScope } from "@/components/document-translation/types";
import { cn } from "@/lib/utils";
import { useDocumentTranslationStore } from "@/store/document-translation-store";

type DocumentSurfaceProps = {
  title: string;
  statusText?: string;
  statusLoading?: boolean;
  titleActions?: ReactNode;
  blocks: EditableBlock[];
  editable: boolean;
  loadingOverlay?: boolean;
  loadingOverlayMessage?: string;
  progressOverlay?: {
    current: number;
    total: number;
    message?: string;
  } | null;
  fileType?: string;
  previewHtmlUrl?: string;
  previewImages?: string[];
  previewPageSizes?: PageSize[];
  previewRenderMode?: PreviewRenderMode;
  onPreviewHtmlLoaded?: () => void;
  selectedBlockId?: string | null;
  hoveredBlockId?: string | null;
  onSelectBlock?: (id: string) => void;
  onHoverBlock?: (id: string | null) => void;
  focusBlockRequest?: { id: string; nonce: number } | null;
  iframeRevisionScopes?: TranslationRevisionScope[];
  selectedRevisionScopeKey?: string;
  onIframeRevisionScopeSelect?: (scope: TranslationRevisionScope) => void;
};

type PreviewPage = {
  key: string;
  label: string;
  blocks: EditableBlock[];
  previewImage?: string;
  size: PageSize;
};

type PageSize = {
  width: number;
  height: number;
};

type PreviewRenderMode = "actual" | "synthetic" | "html";

type OverlayRect = {
  left: string;
  top: string;
  width: string;
  height: string;
};

type SearchableIframeElement = {
  element: HTMLElement;
  text: string;
  area: number;
};

const PAGE_WIDTH = 960;
const PAGE_HEIGHT = 620;
const DEFAULT_PAGE_SIZE: PageSize = { width: PAGE_WIDTH, height: PAGE_HEIGHT };

export function DocumentSurface({
  title,
  statusText,
  statusLoading = false,
  titleActions,
  blocks,
  editable,
  loadingOverlay = false,
  loadingOverlayMessage = "번역본 실제 미리보기를 준비하고 있습니다. 잠시만 기다려주세요.",
  progressOverlay = null,
  fileType,
  previewHtmlUrl,
  previewImages,
  previewPageSizes,
  previewRenderMode = "synthetic",
  onPreviewHtmlLoaded,
  selectedBlockId,
  hoveredBlockId,
  onSelectBlock,
  onHoverBlock,
  focusBlockRequest,
  iframeRevisionScopes,
  selectedRevisionScopeKey,
  onIframeRevisionScopeSelect,
}: DocumentSurfaceProps) {
  const updateBlock = useDocumentTranslationStore((state) => state.updateBlock);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const latestBlocksRef = useRef(blocks);
  const selectedRevisionScopeKeyRef = useRef(selectedRevisionScopeKey);
  const lastIframeScrollTopRef = useRef(0);
  const iframeScrollWindowRef = useRef<Window | null>(null);
  const lastIframeSrcRef = useRef("");
  const visibleBlocks = blocks.filter((block) => block.original || block.translated);
  const shouldRenderHtmlPreview = previewRenderMode === "html" && !editable;
  const pages = buildPreviewPages(
    visibleBlocks,
    fileType,
    shouldRenderHtmlPreview ? undefined : previewImages,
    previewPageSizes,
    previewRenderMode,
    editable,
  );

  useEffect(() => {
    latestBlocksRef.current = blocks;
  }, [blocks]);

  useEffect(() => {
    selectedRevisionScopeKeyRef.current = selectedRevisionScopeKey;
    updateIframeRevisionMarkerSelection(iframeRef.current?.contentDocument ?? null, selectedRevisionScopeKey);
  }, [selectedRevisionScopeKey]);

  useEffect(() => {
    if (!shouldRenderHtmlPreview || editable || !previewHtmlUrl) {
      lastIframeSrcRef.current = "";
      return;
    }

    const iframe = iframeRef.current;
    const nextSource = normalizePreviewSource(previewHtmlUrl);
    if (!iframe || !nextSource || lastIframeSrcRef.current === nextSource) {
      return;
    }

    const previousSource = lastIframeSrcRef.current;
    lastIframeSrcRef.current = nextSource;
    if (!previousSource) {
      iframe.src = nextSource;
      return;
    }

    try {
      iframe.contentWindow?.location.replace(nextSource);
    } catch {
      iframe.src = nextSource;
    }
  }, [editable, previewHtmlUrl, shouldRenderHtmlPreview]);

  useEffect(() => {
    if (!editable || !focusBlockRequest) {
      return;
    }

    const textarea = document.getElementById(
      getTextareaDomId(focusBlockRequest.id),
    ) as HTMLTextAreaElement | null;

    if (!textarea) {
      return;
    }

    requestAnimationFrame(() => {
      textarea.focus();
      const cursorPosition = textarea.value.length;
      textarea.setSelectionRange(cursorPosition, cursorPosition);
    });
  }, [editable, focusBlockRequest]);

  useEffect(() => {
    if (!selectedBlockId) {
      return;
    }

    const target =
      document.getElementById(getBlockDomId(editable, selectedBlockId)) ??
      (editable ? document.getElementById(getTextareaDomId(selectedBlockId)) : null);
    const scrollContainer = scrollContainerRef.current;
    if (!target || !scrollContainer) {
      return;
    }

    requestAnimationFrame(() => {
      const targetRect = target.getBoundingClientRect();
      const containerRect = scrollContainer.getBoundingClientRect();
      const offsetTop =
        targetRect.top -
        containerRect.top +
        scrollContainer.scrollTop -
        containerRect.height * 0.35;

      scrollContainer.scrollTo({
        top: Math.max(0, offsetTop),
        behavior: "smooth",
      });
    });
  }, [editable, selectedBlockId]);

  useEffect(() => {
    if (!previewHtmlUrl || editable) {
      return;
    }

    const iframe = iframeRef.current;
    if (!iframe) {
      return;
    }

    const detachScrollListener = () => {
      const existingWindow = iframeScrollWindowRef.current;
      if (!existingWindow) {
        return;
      }
      existingWindow.removeEventListener("scroll", handleFrameScroll);
      iframeScrollWindowRef.current = null;
    };

    const handleFrameScroll = () => {
      const scrollWindow = iframe.contentWindow;
      if (!scrollWindow) {
        return;
      }
      lastIframeScrollTopRef.current = Math.max(
        scrollWindow.scrollY,
        scrollWindow.document?.documentElement?.scrollTop ?? 0,
      );
    };

    let loadNotified = false;
    const notifyLoaded = () => {
      if (loadNotified || !onPreviewHtmlLoaded) {
        return;
      }
      loadNotified = true;
      requestAnimationFrame(() => {
        onPreviewHtmlLoaded();
      });
    };

    const applyIframeRevisionMarkers = () => {
      const doc = iframe.contentDocument;
      const win = iframe.contentWindow;
      if (!doc?.body || !win || fileType !== "docx" || editable || !iframeRevisionScopes?.length) {
        doc?.getElementById("ai-docx-section-marker-layer")?.remove();
        doc?.getElementById("ai-docx-section-marker-style")?.remove();
        return;
      }

      const oldLayer = doc.getElementById("ai-docx-section-marker-layer");
      oldLayer?.remove();
      doc.querySelectorAll("[data-ai-docx-section]").forEach((element) => {
        element.removeAttribute("data-ai-docx-section");
        element.removeAttribute("data-ai-docx-section-selected");
      });

      if (!doc.getElementById("ai-docx-section-marker-style")) {
        const style = doc.createElement("style");
        style.id = "ai-docx-section-marker-style";
        style.textContent = `
          #ai-docx-section-marker-layer {
            position: absolute;
            left: 0;
            top: 0;
            width: 100%;
            pointer-events: none;
            z-index: 2147483000;
          }
          .ai-docx-section-marker {
            position: absolute;
            display: block;
            box-sizing: border-box;
            border: 1px solid transparent;
            background: transparent;
            cursor: pointer;
            pointer-events: auto;
            transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
          }
          [data-ai-docx-section] {
            transition: background-color 120ms ease, outline-color 120ms ease;
          }
          .ai-docx-section-marker:hover,
          .ai-docx-section-marker[data-selected="true"] {
            border-color: rgba(98, 88, 245, 0.42);
            background: rgba(98, 88, 245, 0.08);
            box-shadow: inset 0 0 0 1px rgba(98, 88, 245, 0.16);
          }
          .ai-docx-section-marker:hover ~ [data-ai-docx-section],
          [data-ai-docx-section-selected="true"] {
            background-color: rgba(98, 88, 245, 0.06);
            outline: 1px solid rgba(98, 88, 245, 0.16);
            outline-offset: 2px;
          }
          .ai-docx-section-marker-label {
            position: absolute;
            left: 8px;
            top: 8px;
            display: inline-flex;
            padding: 4px 8px;
            border-radius: 999px;
            background: rgba(255,255,255,0.94);
            color: #6258f5;
            font: 600 12px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            box-shadow: 0 6px 18px rgba(15,23,42,0.12);
            opacity: 0;
            transform: translateY(-2px);
            transition: opacity 120ms ease, transform 120ms ease;
          }
          .ai-docx-section-marker:hover .ai-docx-section-marker-label,
          .ai-docx-section-marker[data-selected="true"] .ai-docx-section-marker-label {
            opacity: 1;
            transform: translateY(0);
          }
        `;
        doc.head?.appendChild(style);
      }

      const layer = doc.createElement("div");
      layer.id = "ai-docx-section-marker-layer";
      const scrollHeight = Math.max(
        doc.documentElement.scrollHeight,
        doc.body.scrollHeight,
        iframe.clientHeight,
      );
      layer.style.height = `${scrollHeight}px`;
      layer.style.pointerEvents = "none";

      const scopes = iframeRevisionScopes.filter(
        (scope) => scope.type === "batch" && typeof scope.index === "number",
      );
      if (scopes.length === 0) {
        return;
      }

      const searchableElements = collectSearchableIframeElements(doc);
      scopes.forEach((scope) => {
        const scopeKey = getIframeRevisionScopeKey(scope);
        const sectionElements = findIframeRevisionSectionElements(
          doc,
          latestBlocksRef.current,
          scope,
          title === "원본" ? "original" : "translated",
          searchableElements,
        );

        sectionElements.forEach((element) => {
          element.dataset.aiDocxSection = scopeKey;
          element.dataset.aiDocxSectionSelected =
            selectedRevisionScopeKeyRef.current === scopeKey ? "true" : "false";
        });

        const rects = sectionElements.slice(0, 24).flatMap((element) => {
          const rect = element.getBoundingClientRect();
          return rect.width > 2 && rect.height > 2 ? [rect] : [];
        });
        rects.forEach((rect, index) => {
          const marker = doc.createElement("button");
          marker.type = "button";
          marker.className = "ai-docx-section-marker";
          marker.dataset.scopeKey = scopeKey;
          marker.dataset.selected = selectedRevisionScopeKeyRef.current === scopeKey ? "true" : "false";
          marker.style.left = `${rect.left + win.scrollX}px`;
          marker.style.top = `${rect.top + win.scrollY}px`;
          marker.style.width = `${rect.width}px`;
          marker.style.height = `${rect.height}px`;
          marker.style.pointerEvents = "auto";
          marker.setAttribute("aria-label", scope.label ?? `구간 ${scope.index ?? index + 1}`);

          if (index === 0) {
            const label = doc.createElement("span");
            label.className = "ai-docx-section-marker-label";
            label.textContent = scope.label ?? `구간 ${scope.index ?? index + 1}`;
            marker.appendChild(label);
          }

          marker.addEventListener("mouseenter", () => {
            sectionElements.forEach((element) => {
              element.dataset.aiDocxSectionSelected = "true";
            });
          });
          marker.addEventListener("mouseleave", () => {
            sectionElements.forEach((element) => {
              element.dataset.aiDocxSectionSelected =
                selectedRevisionScopeKeyRef.current === scopeKey ? "true" : "false";
            });
          });
          marker.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            onIframeRevisionScopeSelect?.(scope);
          });

          layer.appendChild(marker);
        });
      });

      if (layer.childElementCount > 0) {
        doc.body.appendChild(layer);
      }
      updateIframeRevisionMarkerSelection(doc, selectedRevisionScopeKeyRef.current);
    };

    const applySlideProgress = () => {
      const doc = iframe.contentDocument;
      const win = iframe.contentWindow;
      if (!doc?.body || !win) {
        return;
      }

      detachScrollListener();
      iframeScrollWindowRef.current = win;
      win.addEventListener("scroll", handleFrameScroll, { passive: true });

      const slides = Array.from(doc.querySelectorAll("div.slide")) as HTMLDivElement[];
      const placeholderId = "translation-progress-placeholder";
      const existingPlaceholder = doc.getElementById(placeholderId);

      if (!progressOverlay || progressOverlay.total <= 0) {
        slides.forEach((slide) => {
          slide.style.display = "";
        });
        existingPlaceholder?.remove();
        applyIframeRevisionMarkers();
        notifyLoaded();
        return;
      }

      const visibleCount = Math.max(0, Math.min(progressOverlay.current, progressOverlay.total));
      slides.forEach((slide, index) => {
        slide.style.display = index < visibleCount ? "" : "none";
      });

      if (visibleCount >= progressOverlay.total) {
        existingPlaceholder?.remove();
        applyIframeRevisionMarkers();
        notifyLoaded();
        return;
      }

      let placeholder = existingPlaceholder as HTMLDivElement | null;
      if (!placeholder) {
        placeholder = doc.createElement("div");
        placeholder.id = placeholderId;
        placeholder.innerHTML = `
          <div id="translation-progress-placeholder-card">
            <div id="translation-progress-placeholder-dots">
              <span></span><span></span><span></span>
            </div>
            <p id="translation-progress-placeholder-title">번역 중입니다</p>
            <p id="translation-progress-placeholder-message"></p>
          </div>
        `;
        doc.body.appendChild(placeholder);
      }

      const message = progressOverlay.message ?? "다음 슬라이드를 생성하고 있습니다.";
      const titleText = message;
      const messageNode = doc.getElementById("translation-progress-placeholder-message");

      placeholder.style.display = "flex";
      placeholder.style.alignItems = "center";
      placeholder.style.justifyContent = "center";
      placeholder.style.minHeight = "480px";
      placeholder.style.padding = "36px 24px 64px";
      placeholder.style.boxSizing = "border-box";

      const card = doc.getElementById("translation-progress-placeholder-card");
      if (card) {
        card.setAttribute(
          "style",
          "display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;min-height:180px;max-width:360px;width:100%;padding:24px;background:rgba(255,255,255,0.96);border:1px solid rgba(98,88,245,0.16);border-radius:22px;box-shadow:0 12px 36px rgba(15,23,42,0.10);text-align:center;",
        );
      }

      const dots = Array.from(doc.querySelectorAll("#translation-progress-placeholder-dots span"));
      dots.forEach((dot, index) => {
        (dot as HTMLElement).setAttribute(
          "style",
          `display:inline-block;width:10px;height:10px;margin:0 4px;border-radius:9999px;background:var(--primary, #6258f5);animation:translation-placeholder-pulse 1.2s infinite;animation-delay:${index * 120}ms;`,
        );
      });

      const title = doc.getElementById("translation-progress-placeholder-title");
      if (title) {
        title.textContent = titleText;
        title.setAttribute(
          "style",
          "margin:0;font-size:14px;font-weight:600;letter-spacing:-0.01em;color:#1f2638;",
        );
      }

      if (messageNode) {
        messageNode.textContent = "";
        messageNode.setAttribute(
          "style",
          "display:none;",
        );
      }

      if (!doc.getElementById("translation-progress-placeholder-style")) {
        const style = doc.createElement("style");
        style.id = "translation-progress-placeholder-style";
        style.textContent = `
          @keyframes translation-placeholder-pulse {
            0%, 80%, 100% { transform: scale(0.85); opacity: 0.45; }
            40% { transform: scale(1); opacity: 1; }
          }
        `;
        doc.head?.appendChild(style);
      }

      requestAnimationFrame(() => {
        const maxScrollTop = Math.max(
          0,
          (doc.scrollingElement?.scrollHeight ?? doc.body.scrollHeight) - iframe.clientHeight,
        );
        const restoredScrollTop = Math.min(lastIframeScrollTopRef.current, maxScrollTop);
        win.scrollTo({
          top: restoredScrollTop,
          behavior: "auto",
        });
      });

      notifyLoaded();
      applyIframeRevisionMarkers();
    };

    iframe.addEventListener("load", applySlideProgress);
    applySlideProgress();
    const raf = requestAnimationFrame(() => {
      if (iframe.contentDocument?.readyState === "complete") {
        applySlideProgress();
      }
    });
    return () => {
      cancelAnimationFrame(raf);
      iframe.removeEventListener("load", applySlideProgress);
      detachScrollListener();
    };
  }, [
    editable,
    fileType,
    iframeRevisionScopes,
    onIframeRevisionScopeSelect,
    onPreviewHtmlLoaded,
    previewHtmlUrl,
    progressOverlay,
    title,
  ]);

  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
      <div className="mb-2 flex min-h-8 items-center justify-between gap-3 px-1">
        <div className="flex min-w-0 items-center gap-3">
          <p className="truncate text-sm font-semibold tracking-[0.02em] text-[var(--accent)]">
            {title}
          </p>
          {statusText ? (
            <span className="inline-flex min-w-0 items-center gap-2 text-xs font-semibold text-[var(--accent)]">
              {statusLoading ? (
                <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-[rgba(98,88,245,0.22)] border-t-[var(--primary)]" />
              ) : null}
              <span className="truncate">{statusText}</span>
            </span>
          ) : null}
        </div>
        {titleActions ? (
          <div className="flex shrink-0 items-center gap-2">{titleActions}</div>
        ) : null}
      </div>

      <div className="relative min-h-0 flex-1 overflow-hidden rounded-[18px] bg-white/74">
        <div
          ref={scrollContainerRef}
          className={cn(
            "h-full",
            shouldRenderHtmlPreview
              ? "overflow-hidden"
              : "scrollbar-thin overflow-auto p-3",
          )}
        >
          {shouldRenderHtmlPreview ? (
            <div className="flex h-full min-h-0 items-stretch justify-center p-2">
              <div className="mx-auto flex h-full min-h-0 w-full">
                {previewHtmlUrl ? (
                  <iframe
                    ref={iframeRef}
                    title={title}
                    className="h-full min-h-0 w-full rounded-[16px] border border-[#e1e8f5] bg-white"
                  />
                ) : (
                  <div className="flex h-full w-full items-center justify-center rounded-[16px] border border-[#e1e8f5] bg-white text-sm font-medium text-[var(--muted)]">
                    HTML 미리보기를 생성하는 중입니다.
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="relative">
              {pages.length > 0 ? (
                <div className={cn("space-y-5", loadingOverlay && "pointer-events-none select-none opacity-60")}>
                  {pages.map((page) => (
                    <PreviewPageCanvas
                      key={page.key}
                      page={page}
                      editable={editable}
                      fileType={fileType}
                      previewRenderMode={previewRenderMode}
                      selectedBlockId={selectedBlockId}
                      hoveredBlockId={hoveredBlockId}
                      onSelectBlock={onSelectBlock}
                      onHoverBlock={onHoverBlock}
                      onChange={updateBlock}
                    />
                  ))}
                </div>
              ) : (
                <div className="flex h-full items-center justify-center rounded-[20px] bg-white text-sm text-[var(--muted)]">
                  표시할 문서 내용이 없습니다.
                </div>
              )}
            </div>
          )}
        </div>

        {loadingOverlay ? (
          <div className="absolute inset-0 z-20 flex items-center justify-center rounded-[22px] bg-[rgba(255,255,255,0.72)] backdrop-blur-[2px]">
            <div className="mx-auto flex max-w-[420px] flex-col items-center gap-4 rounded-[24px] border border-[rgba(98,88,245,0.16)] bg-white/96 px-7 py-8 text-center shadow-[0_16px_48px_rgba(15,23,42,0.10)]">
              <div className="flex items-center gap-2">
                <span className="h-3 w-3 animate-pulse rounded-full bg-[var(--primary)]" />
                <span className="h-3 w-3 animate-pulse rounded-full bg-[var(--primary)] [animation-delay:120ms]" />
                <span className="h-3 w-3 animate-pulse rounded-full bg-[var(--primary)] [animation-delay:240ms]" />
              </div>
              <div>
                <p className="text-base font-semibold tracking-[-0.02em] text-[#1f2638]">
                  번역본 미리보기를 생성하는 중입니다
                </p>
                <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
                  {loadingOverlayMessage}
                </p>
              </div>
            </div>
          </div>
        ) : null}

      </div>
    </section>
  );
}

function PreviewPageCanvas({
  page,
  editable,
  fileType,
  previewRenderMode,
  selectedBlockId,
  hoveredBlockId,
  onSelectBlock,
  onHoverBlock,
  onChange,
}: {
  page: PreviewPage;
  editable: boolean;
  fileType?: string;
  previewRenderMode: PreviewRenderMode;
  selectedBlockId?: string | null;
  hoveredBlockId?: string | null;
  onSelectBlock?: (id: string) => void;
  onHoverBlock?: (id: string | null) => void;
  onChange: (id: string, translated: string) => void;
}) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const [canvasScale, setCanvasScale] = useState(1);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    const updateScale = () => {
      setCanvasScale(Math.max(0.7, Math.min(1.8, canvas.getBoundingClientRect().width / page.size.width)));
    };
    const resizeObserver = new ResizeObserver(updateScale);
    resizeObserver.observe(canvas);
    updateScale();

    return () => resizeObserver.disconnect();
  }, [page.size.width]);

  return (
    <div className="mx-auto max-w-[980px]">
      <div
        ref={canvasRef}
        className={cn(
          "relative overflow-hidden rounded-[22px] border border-[#e1e8f5] bg-white",
          fileType === "xlsx" && "bg-[#fbfdff]",
        )}
        style={{ aspectRatio: `${page.size.width} / ${page.size.height}` }}
      >
        {page.previewImage ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={normalizePreviewSource(page.previewImage)}
            alt={page.label}
            className="absolute inset-0 h-full w-full object-contain"
          />
        ) : (
          <FallbackPreviewBackground fileType={fileType} />
        )}

        <div className="absolute inset-0" onMouseLeave={() => onHoverBlock?.(null)}>
          {page.blocks.map((block, index) => {
            const rect = getOverlayRect(
              block,
              index,
              page.blocks.length,
              fileType,
              page.size,
              previewRenderMode,
              editable,
            );
            const isSelected = selectedBlockId === block.id;
            const isHovered = hoveredBlockId === block.id;
            const isEdited = Boolean(block.isEdited);
            const shouldShowOriginalText = !editable && previewRenderMode !== "actual";
            const isActualOfficeOverlay =
              (fileType === "docx" || fileType === "pptx" || fileType === "xlsx") &&
              previewRenderMode === "actual";
            const useDirectEditOverlay = editable && previewRenderMode === "actual";
            const shouldShowEditor = !useDirectEditOverlay || isSelected || isEdited;
            if (editable) {
              if (!shouldShowEditor) {
                return (
                  <button
                    key={block.id}
                    id={getBlockDomId(editable, block.id)}
                    data-block-id={block.id}
                    type="button"
                    aria-label={`번역 영역 선택 ${index + 1}`}
                    className="absolute rounded-[8px] border border-transparent bg-transparent outline-none"
                    style={rect}
                    onFocus={() => onSelectBlock?.(block.id)}
                    onClick={() => onSelectBlock?.(block.id)}
                    onMouseEnter={() => onHoverBlock?.(block.id)}
                    onMouseLeave={() => onHoverBlock?.(null)}
                  />
                );
              }

              return (
                <textarea
                  key={block.id}
                  data-block-id={block.id}
                  id={getTextareaDomId(block.id)}
                  value={block.translated}
                  aria-label={`번역 텍스트 편집 ${index + 1}`}
                  className={cn(
                    "overlay-textarea-scrollbar absolute resize-none overflow-auto border text-[#182033] outline-none transition [overflow-wrap:anywhere]",
                    isActualOfficeOverlay ? "rounded-[4px] px-1 py-0.5" : "rounded-[8px] px-2 py-1.5",
                    useDirectEditOverlay
                      ? isSelected
                        ? "border border-[rgba(98,88,245,0.26)] bg-white shadow-[0_10px_24px_rgba(15,23,42,0.08)] ring-0"
                        : "border border-[rgba(226,232,240,0.92)] bg-white shadow-[0_8px_20px_rgba(15,23,42,0.06)] ring-0"
                      : isSelected
                        ? "border-transparent bg-[rgba(98,88,245,0.20)] ring-0"
                        : isHovered
                          ? "border-transparent bg-[rgba(64,70,84,0.24)] ring-0"
                          : "border-transparent bg-white/82 hover:bg-[rgba(64,70,84,0.18)]",
                  )}
                  style={{
                    ...rect,
                    ...buildOverlayTextStyle(block, canvasScale, fileType, previewRenderMode),
                  }}
                  onFocus={() => onSelectBlock?.(block.id)}
                  onClick={() => onSelectBlock?.(block.id)}
                  onMouseEnter={() => onHoverBlock?.(block.id)}
                  onMouseLeave={() => onHoverBlock?.(null)}
                  onChange={(event) => onChange(block.id, event.target.value)}
                />
              );
            }

            return (
              <button
                key={block.id}
                id={getBlockDomId(editable, block.id)}
                data-block-id={block.id}
                type="button"
                className={cn(
                  "absolute overflow-hidden rounded-[8px] border border-transparent px-2 py-1.5 text-left text-[#182033] transition [overflow-wrap:anywhere]",
                  isHovered
                    ? "bg-[rgba(64,70,84,0.24)]"
                    : isSelected
                      ? "bg-[rgba(98,88,245,0.16)]"
                      : "bg-transparent hover:bg-[rgba(64,70,84,0.18)]",
                )}
                style={{
                  ...rect,
                  ...buildOverlayTextStyle(block, canvasScale, fileType, previewRenderMode),
                }}
                onClick={() => onSelectBlock?.(block.id)}
                onMouseEnter={() => onHoverBlock?.(block.id)}
                onMouseLeave={() => onHoverBlock?.(null)}
                title={block.original}
              >{shouldShowOriginalText ? block.original : null}</button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function FallbackPreviewBackground({ fileType }: { fileType?: string }) {
  if (fileType === "xlsx") {
    return (
      <div className="absolute inset-0 bg-[linear-gradient(#e4eaf4_1px,transparent_1px),linear-gradient(90deg,#e4eaf4_1px,transparent_1px)] bg-[size:120px_38px]">
        <div className="h-9 border-b border-[#d9e1ee] bg-[#f6f9fd]" />
        <div className="absolute left-0 top-0 h-full w-12 border-r border-[#d9e1ee] bg-[#f6f9fd]" />
      </div>
    );
  }

  if (fileType === "pptx") {
    return (
      <div className="absolute inset-0 bg-white">
        <div className="absolute inset-x-10 top-10 h-10 rounded-full bg-[#eef1f6]" />
        <div className="absolute left-12 top-24 h-[70%] w-[28%] rounded-[18px] bg-[#f3f6fb]" />
        <div className="absolute right-16 top-24 h-[65%] w-[48%] rounded-[18px] bg-[#f5f6f8]" />
      </div>
    );
  }

  if (fileType === "docx") {
    return <div className="absolute inset-0 bg-white" />;
  }

  return (
    <div className="absolute inset-0 bg-white">
      <div className="mx-auto mt-12 h-[80%] w-[72%] rounded-[18px] bg-[#f8fafc]" />
      <div className="absolute left-[20%] right-[20%] top-[18%] space-y-4">
        {Array.from({ length: 9 }).map((_, index) => (
          <div key={index} className="h-3 rounded-full bg-[#e9eef6]" />
        ))}
      </div>
    </div>
  );
}

function buildPreviewPages(
  blocks: EditableBlock[],
  fileType?: string,
  previewImages?: string[],
  previewPageSizes?: PageSize[],
  previewRenderMode: PreviewRenderMode = "synthetic",
  editable = false,
): PreviewPage[] {
  const grouped = new Map<string, EditableBlock[]>();

  for (const block of blocks) {
    const key = getPageKey(block, fileType, previewRenderMode, editable);
    const current = grouped.get(key) ?? [];
    current.push(block);
    grouped.set(key, current);
  }

  if (previewRenderMode === "actual" && previewImages && previewImages.length > 0) {
    return previewImages.map((previewImage, index) => {
      const key = `page-${index + 1}`;
      return {
        key,
        label: getPageLabel(key, fileType, previewRenderMode),
        blocks: grouped.get(key) ?? [],
        previewImage,
        size: normalizePageSize(previewPageSizes?.[index]),
      };
    });
  }

  return Array.from(grouped.entries()).map(([key, pageBlocks], index) => ({
    key,
    label: getPageLabel(key, fileType, previewRenderMode),
    blocks: pageBlocks,
    previewImage: previewImages?.[index],
    size: normalizePageSize(previewPageSizes?.[index]),
  }));
}

function getPageKey(
  block: EditableBlock,
  fileType?: string,
  previewRenderMode: PreviewRenderMode = "synthetic",
  editable = false,
) {
  if (previewRenderMode === "actual") {
    const actualPage = editable
      ? block.location?.translated_page ?? block.location?.page
      : block.location?.page;
    return `page-${actualPage ?? block.location?.slide ?? 1}`;
  }

  if (fileType === "xlsx") {
    return `sheet-${block.location?.sheet ?? "Sheet1"}`;
  }

  if (fileType === "pptx") {
    return `slide-${block.location?.slide ?? 1}`;
  }

  return `page-${block.location?.page ?? 1}`;
}

function getPageLabel(
  key: string,
  fileType?: string,
  previewRenderMode: PreviewRenderMode = "synthetic",
) {
  if (previewRenderMode === "actual" && key.startsWith("page-") && fileType === "pptx") {
    return `Slide ${key.replace("page-", "")}`;
  }

  if (key.startsWith("sheet-")) {
    return key.replace("sheet-", "");
  }

  if (key.startsWith("slide-")) {
    return `Slide ${key.replace("slide-", "")}`;
  }

  if (fileType === "docx") {
    return "Document preview";
  }

  return `Page ${key.replace("page-", "")}`;
}

function getOverlayRect(
  block: EditableBlock,
  index: number,
  total: number,
  fileType?: string,
  pageSize: PageSize = DEFAULT_PAGE_SIZE,
  previewRenderMode: PreviewRenderMode = "synthetic",
  editable = false,
): OverlayRect {
  const bbox =
    editable && previewRenderMode === "actual"
      ? block.location?.translated_bbox ?? block.location?.bbox
      : block.location?.original_bbox ?? block.location?.bbox;
  const isSyntheticDocx = fileType === "docx" && previewRenderMode !== "actual";

  if (isSyntheticDocx) {
    const text = (block.translated || block.original || "").trim();
    const isTitleLike = isLikelyDocxTitleText(text, index);
    if (isTitleLike) {
      return toRect(14, 10, 72, 8.5, {
        minWidth: 56,
        minHeight: 7.5,
        maxWidth: 82,
        maxHeight: 10,
      });
    }

    const estimatedLines = estimateDocxFallbackLines(text);
    const height = Math.max(42, Math.min(68, estimatedLines * 4.35));
    return toRect(14, 16, 72, height, {
      minWidth: 64,
      minHeight: 42,
      maxWidth: 82,
      maxHeight: 72,
    });
  }

  if (bbox && bbox.length >= 4) {
    const [x0, y0, x1, y1] = bbox;
    const isActualDocx = fileType === "docx" && previewRenderMode === "actual";
    const isActualPptx = fileType === "pptx" && previewRenderMode === "actual";
    const isActualXlsx = fileType === "xlsx" && previewRenderMode === "actual";
    const rawLeft = (x0 / pageSize.width) * 100;
    const rawTop = (y0 / pageSize.height) * 100;
    const rawWidth = ((x1 - x0) / pageSize.width) * 100;
    const rawHeight = ((y1 - y0) / pageSize.height) * 100;

    if (isActualDocx) {
      const left = clampPercent(rawLeft - 0.35, 0, 96);
      const top = clampPercent(rawTop - 0.2, 0, 96);
      const right = clampPercent(rawLeft + rawWidth + Math.max(2.4, rawWidth * 0.08), left + 1, 97);
      const width = Math.max(rawWidth, right - left);
      const height = Math.max(rawHeight * 1.12, 1.15);

      return toRect(left, top, width, height, {
        minWidth: 2,
        minHeight: 1,
        maxWidth: 94,
        maxHeight: 32,
      });
    }

    if (isActualPptx) {
      return toRect(rawLeft - 0.18, rawTop - 0.12, rawWidth + 0.36, rawHeight + 0.24, {
        minWidth: 1.4,
        minHeight: 0.9,
        maxWidth: 92,
        maxHeight: 18,
      });
    }

    if (isActualXlsx) {
      return toRect(rawLeft - 0.05, rawTop - 0.03, rawWidth + 0.1, rawHeight + 0.06, {
        minWidth: 1,
        minHeight: 0.65,
        maxWidth: 92,
        maxHeight: 2.8,
      });
    }

    const left = clampPercent(rawLeft);
    const top = clampPercent(rawTop);
    const width = clampPercent(rawWidth, 1, 88);
    const height = clampPercent(rawHeight, 1, 64);
    return toRect(left, top, width, height);
  }

  if (fileType === "xlsx") {
    const row = Math.max(1, block.location?.row ?? index + 1);
    const col = Math.max(1, block.location?.col ?? 1);
    return toRect(7 + (col - 1) * 17, 7 + (row - 1) * 6, 16.5, 5.2);
  }

  if (fileType === "pptx") {
    const column = index % 2;
    const row = Math.floor(index / 2);
    return toRect(8 + column * 45, 13 + row * 9, 36, 7.4);
  }

  const safeTotal = Math.max(1, total);
  return toRect(16, 11 + (index * 78) / safeTotal, 68, Math.max(7, Math.min(16, 62 / safeTotal)));
}

function toRect(
  left: number,
  top: number,
  width: number,
  height: number,
  bounds: {
    minWidth?: number;
    minHeight?: number;
    maxWidth?: number;
    maxHeight?: number;
  } = {},
): OverlayRect {
  const minWidth = bounds.minWidth ?? 4;
  const minHeight = bounds.minHeight ?? 4;
  const maxWidth = bounds.maxWidth ?? 88;
  const maxHeight = bounds.maxHeight ?? 64;

  return {
    left: `${clampPercent(left)}%`,
    top: `${clampPercent(top)}%`,
    width: `${clampPercent(width, minWidth, maxWidth)}%`,
    height: `${clampPercent(height, minHeight, maxHeight)}%`,
  };
}

function clampPercent(value: number, min = 0, max = 96) {
  return Math.max(min, Math.min(max, value));
}

function normalizePageSize(size?: PageSize): PageSize {
  if (!size || !Number.isFinite(size.width) || !Number.isFinite(size.height)) {
    return DEFAULT_PAGE_SIZE;
  }

  return {
    width: Math.max(1, size.width),
    height: Math.max(1, size.height),
  };
}

function normalizePreviewSource(source: string) {
  if (source.startsWith("data:")) {
    return source;
  }

  if (source.startsWith("http://") || source.startsWith("https://")) {
    try {
      const url = new URL(source);
      if (url.pathname.startsWith("/preview-files/")) {
        const proxiedPath = url.pathname.replace(
          /^\/preview-files\//,
          "/api/document-translation/preview-files/",
        );
        return `${proxiedPath}${url.search}${url.hash}`;
      }
    } catch {
      return source;
    }
    return source;
  }

  return `data:image/png;base64,${source}`;
}

function getIframeRevisionScopeKey(scope: TranslationRevisionScope | null): string {
  if (!scope) {
    return "document";
  }
  const index = scope.index ?? scope.label ?? "";
  return `${scope.type}:${index}`;
}

function findIframeRevisionSectionElements(
  doc: Document,
  blocks: EditableBlock[],
  scope: TranslationRevisionScope,
  side: "original" | "translated",
  searchableElements: SearchableIframeElement[],
): HTMLElement[] {
  if (typeof scope.index !== "number") {
    return [];
  }

  const sectionBlocks = blocks.filter((block) => {
    const page = block.location?.page ?? block.location?.translated_page;
    return page === scope.index;
  });
  const sectionTexts = pickDocxMarkerSampleBlocks(sectionBlocks)
    .flatMap((block) => getDocxMarkerCandidateTexts(block, side))
    .flatMap(buildIframeMarkerNeedles)
    .filter(Boolean);

  const elements: HTMLElement[] = [];
  const seen = new Set<HTMLElement>();
  for (const needle of sectionTexts) {
    const match = findSmallestIframeElementContainingText(searchableElements, needle);
    if (!match || seen.has(match)) {
      continue;
    }
    seen.add(match);
    elements.push(match);
  }
  return elements;
}

function pickDocxMarkerSampleBlocks(blocks: EditableBlock[]): EditableBlock[] {
  if (blocks.length <= 24) {
    return blocks;
  }

  const first = blocks.slice(0, 8);
  const last = blocks.slice(-10);
  const middleStart = Math.max(8, Math.floor(blocks.length / 2) - 3);
  const middle = blocks.slice(middleStart, middleStart + 6);
  const seen = new Set<string>();
  return [...first, ...middle, ...last].filter((block) => {
    if (seen.has(block.id)) {
      return false;
    }
    seen.add(block.id);
    return true;
  });
}

function getDocxMarkerCandidateTexts(block: EditableBlock, side: "original" | "translated"): string[] {
  if (side === "original") {
    return [block.original];
  }

  const texts = [block.translated, block.original].filter(Boolean);
  return Array.from(new Set(texts));
}

function buildIframeMarkerNeedles(text: string): string[] {
  const compact = normalizeIframeMarkerText(text);
  if (!compact || compact.length < 4) {
    return [];
  }
  if (compact.length <= 36) {
    return [compact];
  }

  const needles = [compact.slice(0, 48)];
  const middleStart = Math.max(0, Math.floor(compact.length / 2) - 24);
  needles.push(compact.slice(middleStart, middleStart + 48));
  if (compact.length > 72) {
    needles.push(compact.slice(-48));
  }
  return Array.from(new Set(needles.filter((item) => item.length >= 8)));
}

function collectSearchableIframeElements(doc: Document): SearchableIframeElement[] {
  const root = doc.body;
  if (!root) {
    return [];
  }

  const walker = doc.createTreeWalker(root, 1);
  const elements: SearchableIframeElement[] = [];
  let current = walker.nextNode();
  while (current) {
    const element = getIframeElement(current);
    if (element && isSearchableIframeElement(element)) {
      const text = normalizeIframeMarkerText(element.textContent ?? "");
      if (text.length >= 4) {
        const rect = element.getBoundingClientRect();
        elements.push({
          element,
          text,
          area: rect.width * rect.height,
        });
      }
    }
    current = walker.nextNode();
  }
  return elements;
}

function findSmallestIframeElementContainingText(
  searchableElements: SearchableIframeElement[],
  needle: string,
): HTMLElement | null {
  const matches = searchableElements.filter((item) => item.text.includes(needle));
  if (matches.length === 0) {
    return null;
  }

  matches.sort((left, right) => {
    const leftTextLength = left.text.length;
    const rightTextLength = right.text.length;
    if (leftTextLength !== rightTextLength) {
      return leftTextLength - rightTextLength;
    }
    return left.area - right.area;
  });
  return matches[0]?.element ?? null;
}

function getIframeElement(node: Node): HTMLElement | null {
  if (node.nodeType !== 1) {
    return null;
  }
  const element = node as HTMLElement;
  if (typeof element.getBoundingClientRect !== "function" || typeof element.tagName !== "string") {
    return null;
  }
  return element;
}

function isSearchableIframeElement(element: HTMLElement): boolean {
  const tagName = element.tagName.toLowerCase();
  if (["html", "body", "script", "style", "button", "meta", "link"].includes(tagName)) {
    return false;
  }
  if (element.id === "ai-docx-section-marker-layer" || element.closest("#ai-docx-section-marker-layer")) {
    return false;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 2 && rect.height > 2;
}

function normalizeIframeMarkerText(text: string): string {
  return text
    .replace(/\u00a0/g, " ")
    .replace(/\s+/g, "")
    .toLowerCase();
}

function updateIframeRevisionMarkerSelection(doc: Document | null, selectedKey?: string) {
  if (!doc) {
    return;
  }

  doc.querySelectorAll(".ai-docx-section-marker").forEach((element) => {
    const item = getIframeElement(element);
    if (item) {
      item.dataset.selected = item.dataset.scopeKey === selectedKey ? "true" : "false";
    }
  });
  doc.querySelectorAll("[data-ai-docx-section]").forEach((element) => {
    const item = getIframeElement(element);
    if (item) {
      item.dataset.aiDocxSectionSelected = item.dataset.aiDocxSection === selectedKey ? "true" : "false";
    }
  });
}

function isLikelyDocxTitleText(text: string, index: number) {
  const compact = text.replace(/\s+/g, " ").trim();
  if (!compact) {
    return index === 0;
  }
  if (index !== 0) {
    return false;
  }
  return compact.length <= 80 && !/[.!?。！？]/.test(compact);
}

function estimateDocxFallbackLines(text: string) {
  const compact = text.replace(/\s+/g, " ").trim();
  if (!compact) {
    return 10;
  }
  return Math.max(10, Math.min(18, Math.ceil(compact.length / 52)));
}

function getTextareaDomId(blockId: string) {
  return `translation-textarea-${encodeURIComponent(blockId)}`;
}

function getBlockDomId(editable: boolean, blockId: string) {
  return `${editable ? "translated" : "original"}-block-${encodeURIComponent(blockId)}`;
}

function buildOverlayTextStyle(
  block: EditableBlock,
  canvasScale: number,
  fileType?: string,
  previewRenderMode: PreviewRenderMode = "synthetic",
): CSSProperties {
  const isActualDocx = fileType === "docx" && previewRenderMode === "actual";
  const isActualPptx = fileType === "pptx" && previewRenderMode === "actual";
  const isActualXlsx = fileType === "xlsx" && previewRenderMode === "actual";
  const isSyntheticDocx = fileType === "docx" && previewRenderMode !== "actual";
  const isActualOffice = isActualDocx || isActualPptx || isActualXlsx;
  const text = (block.translated || block.original || "").trim();
  const isTitleLike = isSyntheticDocx && isLikelyDocxTitleText(text, 0);
  const baseFontSize = block.style?.font_size
    ? isActualDocx
      ? Math.max(12, Math.min(32, Math.round(block.style.font_size * 1.45)))
      : isSyntheticDocx
        ? isTitleLike
          ? Math.max(30, Math.min(42, Math.round(block.style.font_size * 2.2)))
          : Math.max(24, Math.min(32, Math.round(block.style.font_size * 1.9)))
      : isActualOffice
        ? Math.max(10, Math.min(24, Math.round(block.style.font_size * 1.05)))
        : Math.max(13, Math.min(28, Math.round(block.style.font_size * 1.35)))
    : isActualDocx
      ? 14
      : isSyntheticDocx
        ? isTitleLike
          ? 34
          : 24
      : isActualOffice
        ? 11
        : 18;
  const fontSize = isActualDocx
    ? Math.max(12, Math.min(30, Math.round(baseFontSize * canvasScale)))
    : isSyntheticDocx
      ? isTitleLike
        ? Math.max(28, Math.min(42, Math.round(baseFontSize * canvasScale)))
        : Math.max(24, Math.min(30, Math.round(baseFontSize * canvasScale)))
    : isActualOffice
      ? Math.max(9, Math.min(22, Math.round(baseFontSize * canvasScale)))
      : Math.max(10, Math.min(34, Math.round(baseFontSize * canvasScale)));
  const lineHeight = isActualDocx
    ? Math.max(15, Math.round(fontSize * 1.24))
    : isSyntheticDocx
      ? Math.max(isTitleLike ? 34 : 38, Math.round(fontSize * (isTitleLike ? 1.2 : 1.55)))
    : isActualOffice
      ? Math.max(11, Math.round(fontSize * 1.18))
      : Math.max(14, Math.round(fontSize * 1.32));

  return {
    fontSize: `${fontSize}px`,
    fontWeight: block.style?.bold ? 700 : 400,
    fontStyle: block.style?.italic ? "italic" : "normal",
    textDecoration: block.style?.underline ? "underline" : "none",
    textAlign: normalizeAlign(block.style?.align),
    lineHeight: `${lineHeight}px`,
    fontFamily:
      block.style?.font_name || '"Pretendard Variable", "Pretendard", "Apple SD Gothic Neo", sans-serif',
  };
}

function normalizeAlign(align?: string) {
  if (align === "CENTER" || align === "center") {
    return "center";
  }
  if (align === "RIGHT" || align === "right") {
    return "right";
  }
  if (align === "JUSTIFY" || align === "justify" || align === "both") {
    return "justify";
  }
  return "left";
}
