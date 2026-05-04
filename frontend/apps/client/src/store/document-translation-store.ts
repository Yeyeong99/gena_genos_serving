"use client";

import { create } from "zustand";
import {
  type EditedTranslationPatch,
  LANGUAGE_OPTIONS,
  type EditableBlock,
  type DocumentBlock,
  type LanguageKey,
  type TranslationStyleOptionKey,
  type TranslationStyleOptions,
  type TranslatePreviewStatusResponse,
  type TranslateWorkflowResponse,
  getDefaultStyleOptions,
} from "@/components/document-translation/types";

type SelectedFile = {
  file: File;
  base64: string;
};

type DocumentTranslationState = {
  selectedLanguage: LanguageKey;
  styleOptions: TranslationStyleOptions;
  selectedFile: SelectedFile | null;
  stage: "idle" | "uploading" | "translated";
  isTranslating: boolean;
  response: TranslateWorkflowResponse | null;
  editableBlocks: EditableBlock[];
  editedTranslations: Record<string, string>;
  editedPatches: Record<string, Omit<EditedTranslationPatch, "id">>;
  debugLastRequestedLanguage: string;
  setLanguage: (language: LanguageKey) => void;
  setStyleOption: (key: TranslationStyleOptionKey, value: string) => void;
  setSelectedFile: (file: SelectedFile | null) => void;
  startTranslating: () => void;
  setDebugLastRequestedLanguage: (value: string) => void;
  setStartResult: (response: TranslateWorkflowResponse) => void;
  setResult: (response: TranslateWorkflowResponse) => void;
  mergeDeferredPreview: (jobId: string, update: TranslatePreviewStatusResponse) => void;
  mergeStreamingUpdate: (jobId: string, update: Partial<TranslateWorkflowResponse>) => void;
  updateBlock: (id: string, translated: string, patch?: Partial<Omit<EditedTranslationPatch, "id" | "translated">>) => void;
  resetAll: () => void;
};

function splitVisibleLines(text: string | undefined) {
  return (text ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function buildLineAlignedBlocks(response: TranslateWorkflowResponse): EditableBlock[] {
  const originalLines = splitVisibleLines(response.input_text);
  const translatedLines = splitVisibleLines(response.text);
  const maxLength = Math.max(originalLines.length, translatedLines.length);

  if (maxLength === 0) {
    return [];
  }

  return Array.from({ length: maxLength }, (_, index) => ({
    id: `line-${index}`,
    original: originalLines[index] ?? "",
    translated: translatedLines[index] ?? "",
    accent: index % 4 === 0 ? "highlight" : "plain",
  }));
}

function buildFrontendBlockId(block: DocumentBlock, index: number) {
  const backendId = String(block.id ?? index);
  const location = block.location;
  const scopeParts = [
    location?.slide != null ? `slide-${location.slide}` : "",
    location?.translated_page != null ? `translated-page-${location.translated_page}` : "",
    location?.page != null ? `page-${location.page}` : "",
    location?.sheet ? `sheet-${location.sheet}` : "",
    location?.row != null ? `row-${location.row}` : "",
    location?.col != null ? `col-${location.col}` : "",
    location?.cell ? `cell-${location.cell}` : "",
    location?.bbox?.length ? `bbox-${location.bbox.map((value: number) => Math.round(value * 100)).join("-")}` : "",
  ].filter(Boolean);

  return scopeParts.length > 0 ? `${backendId}::${scopeParts.join("::")}` : backendId;
}

function buildEditableBlocks(response: TranslateWorkflowResponse): EditableBlock[] {
  const documentBlocks = response.document_blocks ?? [];

  if (documentBlocks.length > 0) {
    return documentBlocks.map((block, index) => ({
      id: buildFrontendBlockId(block, index),
      backendId: String(block.id ?? index),
      original: block.original ?? "",
      translated: block.translated ?? "",
      baseTranslated: block.translated ?? "",
      isEdited: false,
      accent: index % 5 === 0 ? "highlight" : "plain",
      group: block.group,
      source: block.source,
      type: block.type,
      style: block.style,
      location: block.location
        ? {
            page: block.location.page,
            translated_page: block.location.translated_page,
            slide: block.location.slide,
            sheet: block.location.sheet,
            row: block.location.row,
            col: block.location.col,
            cell: block.location.cell,
            bbox: block.location.bbox,
            original_bbox: block.location.original_bbox,
            translated_bbox: block.location.translated_bbox,
          }
        : undefined,
    }));
  }

  const pairs = response.translation_pairs ?? [];

  if (pairs.length > 0) {
    return pairs.map((pair, index) => ({
      id: String(pair.id ?? index),
      backendId: String(pair.id ?? index),
      original: pair.original,
      translated: pair.translated,
      baseTranslated: pair.translated,
      isEdited: false,
      accent: index % 4 === 0 ? "highlight" : "plain",
    }));
  }

  const lineBlocks = buildLineAlignedBlocks(response);
  if (lineBlocks.length > 0) {
    return lineBlocks;
  }

  return [];
}

function applyEditedPatchesToBlocks(
  blocks: EditableBlock[],
  patches: Record<string, Omit<EditedTranslationPatch, "id">>,
): EditableBlock[] {
  if (!blocks.length || !Object.keys(patches).length) {
    return blocks;
  }

  return blocks.map((block) => {
    const patched = patches[block.id];
    if (patched == null) {
      return block;
    }
    return {
      ...block,
      translated: patched.translated,
      style: {
        ...block.style,
        font_size: patched.font_size ?? block.style?.font_size,
      },
      isEdited: patched.translated !== (block.baseTranslated ?? block.translated),
    };
  });
}

function mergeEditableBlocks(
  currentBlocks: EditableBlock[],
  nextResponse: TranslatePreviewStatusResponse,
): EditableBlock[] {
  const nextBlocks = nextResponse.document_blocks ?? [];
  if (nextBlocks.length === 0) {
    return currentBlocks;
  }

  const nextById = new Map(nextBlocks.map((block, index) => [buildFrontendBlockId(block, index), block]));

  return currentBlocks.map((block) => {
    const incoming = nextById.get(block.id);
    if (!incoming) {
      return block;
    }

    return {
      ...block,
      original: incoming.original ?? block.original,
      translated: block.isEdited ? block.translated : (incoming.translated ?? block.translated),
      baseTranslated: incoming.translated ?? block.baseTranslated ?? block.translated,
      isEdited: block.isEdited ?? false,
      group: incoming.group ?? block.group,
      source: incoming.source ?? block.source,
      type: incoming.type ?? block.type,
      style: incoming.style ?? block.style,
      location: incoming.location
        ? {
            page: incoming.location.page,
            translated_page: incoming.location.translated_page,
            slide: incoming.location.slide,
            sheet: incoming.location.sheet,
            row: incoming.location.row,
            col: incoming.location.col,
            cell: incoming.location.cell,
            bbox: incoming.location.bbox,
            original_bbox: incoming.location.original_bbox,
            translated_bbox: incoming.location.translated_bbox,
          }
        : block.location,
    };
  });
}

export const useDocumentTranslationStore = create<DocumentTranslationState>((set) => ({
  selectedLanguage: LANGUAGE_OPTIONS[0].key,
  styleOptions: getDefaultStyleOptions(LANGUAGE_OPTIONS[0].key),
  selectedFile: null,
  stage: "idle",
  isTranslating: false,
  response: null,
  editableBlocks: [],
  editedTranslations: {},
  editedPatches: {},
  debugLastRequestedLanguage: "",
  setLanguage: (language) =>
    set({
      selectedLanguage: language,
      styleOptions: getDefaultStyleOptions(language),
    }),
  setStyleOption: (key, value) =>
    set((state) => ({
      styleOptions: {
        ...state.styleOptions,
        [key]: value,
      },
    })),
  setSelectedFile: (selectedFile) => set({ selectedFile }),
  startTranslating: () =>
    set({
      isTranslating: true,
      stage: "uploading",
      response: null,
      editableBlocks: [],
      editedTranslations: {},
      editedPatches: {},
    }),
  setDebugLastRequestedLanguage: (value) => set({ debugLastRequestedLanguage: value }),
  setStartResult: (response) =>
    set((state) => {
      const nextBlocks =
        response.translation_status === "pending" && !response.text ? [] : buildEditableBlocks(response);
      return {
        response,
        editableBlocks: applyEditedPatchesToBlocks(nextBlocks, state.editedPatches),
        isTranslating: false,
        stage: "translated",
      };
    }),
  setResult: (response) =>
    set((state) => ({
      response,
      editableBlocks: applyEditedPatchesToBlocks(buildEditableBlocks(response), state.editedPatches),
      isTranslating: false,
      stage: "translated",
    })),
  mergeDeferredPreview: (jobId, update) =>
    set((state) => {
      if (!state.response || state.response.translated_preview_job_id !== jobId) {
        return state;
      }

      const mergedResponse: TranslateWorkflowResponse = {
        ...state.response,
        ...update,
        translated_preview_job_id: jobId,
        translated_preview_status:
          update.translated_preview_status ?? state.response.translated_preview_status,
        translated_preview_images:
          update.translated_preview_images ?? state.response.translated_preview_images,
        translated_preview_html_url:
          update.translated_preview_html_url ?? state.response.translated_preview_html_url,
        preview_page_sizes: update.preview_page_sizes ?? state.response.preview_page_sizes,
        preview_render_mode: update.preview_render_mode ?? state.response.preview_render_mode,
        document_blocks: update.document_blocks ?? state.response.document_blocks,
        created_at: update.created_at ?? state.response.created_at,
        completed_at: update.completed_at ?? state.response.completed_at,
        elapsed_ms: update.elapsed_ms ?? state.response.elapsed_ms,
        llm_model_name: update.llm_model_name ?? state.response.llm_model_name,
        llm_provider_sort: update.llm_provider_sort ?? state.response.llm_provider_sort,
        current_page: update.current_page ?? state.response.current_page,
        total_pages: update.total_pages ?? state.response.total_pages,
        debug_page_timings: update.debug_page_timings ?? state.response.debug_page_timings,
      };

      return {
        response: mergedResponse,
        editableBlocks: applyEditedPatchesToBlocks(
          mergeEditableBlocks(state.editableBlocks, update),
          state.editedPatches,
        ),
      };
    }),
  mergeStreamingUpdate: (jobId, update) =>
    set((state) => {
      if (!state.response || state.response.job_id !== jobId) {
        return state;
      }

      const mergedResponse: TranslateWorkflowResponse = {
        ...state.response,
        ...update,
        job_id: jobId,
        document_blocks: update.document_blocks ?? state.response.document_blocks,
        translation_pairs: update.translation_pairs ?? state.response.translation_pairs,
        pairs: update.pairs ?? state.response.pairs,
        original_preview_images: update.original_preview_images ?? state.response.original_preview_images,
        original_preview_html_url:
          update.original_preview_html_url ?? state.response.original_preview_html_url,
        translated_preview_images:
          update.translated_preview_images ?? state.response.translated_preview_images,
        translated_preview_html_url:
          update.translated_preview_html_url ?? state.response.translated_preview_html_url,
        original_preview_status:
          update.original_preview_status ?? state.response.original_preview_status,
        preview_page_sizes: update.preview_page_sizes ?? state.response.preview_page_sizes,
        preview_render_mode: update.preview_render_mode ?? state.response.preview_render_mode,
        translated_preview_status:
          update.translated_preview_status ?? state.response.translated_preview_status,
        translation_status: update.translation_status ?? state.response.translation_status,
        text: update.text ?? state.response.text,
        translation_error: update.translation_error ?? state.response.translation_error,
        current_scope: update.current_scope ?? state.response.current_scope,
        current_slide: update.current_slide ?? state.response.current_slide,
        total_slides: update.total_slides ?? state.response.total_slides,
        current_page: update.current_page ?? state.response.current_page,
        total_pages: update.total_pages ?? state.response.total_pages,
        current_sheet: update.current_sheet ?? state.response.current_sheet,
        current_sheet_name: update.current_sheet_name ?? state.response.current_sheet_name,
        total_sheets: update.total_sheets ?? state.response.total_sheets,
        event_phase: update.event_phase ?? state.response.event_phase,
        created_at: update.created_at ?? state.response.created_at,
        completed_at: update.completed_at ?? state.response.completed_at,
        elapsed_ms: update.elapsed_ms ?? state.response.elapsed_ms,
        llm_model_name: update.llm_model_name ?? state.response.llm_model_name,
        llm_provider_sort: update.llm_provider_sort ?? state.response.llm_provider_sort,
        debug_page_timings: update.debug_page_timings ?? state.response.debug_page_timings,
      };

      const editableBlocks = update.document_blocks
        ? state.editableBlocks.length === 0
          ? applyEditedPatchesToBlocks(buildEditableBlocks(mergedResponse), state.editedPatches)
          : applyEditedPatchesToBlocks(
              mergeEditableBlocks(state.editableBlocks, update as TranslatePreviewStatusResponse),
              state.editedPatches,
            )
        : state.editableBlocks;

      return {
        response: mergedResponse,
        editableBlocks,
      };
    }),
  updateBlock: (id, translated, patch) =>
    set((state) => ({
      editedTranslations: {
        ...state.editedTranslations,
        [id]: translated,
      },
      editedPatches: {
        ...state.editedPatches,
        [id]: {
          translated,
          font_size: patch?.font_size ?? state.editedPatches[id]?.font_size,
          line_break: patch?.line_break ?? translated.includes("\n"),
        },
      },
      editableBlocks: state.editableBlocks.map((block) =>
        block.id === id
          ? {
              ...block,
              translated,
              style: {
                ...block.style,
                font_size: patch?.font_size ?? state.editedPatches[id]?.font_size ?? block.style?.font_size,
              },
              isEdited: translated !== (block.baseTranslated ?? block.translated),
            }
          : block,
      ),
    })),
  resetAll: () =>
    set({
      selectedFile: null,
      stage: "idle",
      isTranslating: false,
      response: null,
      editableBlocks: [],
      editedTranslations: {},
      editedPatches: {},
      debugLastRequestedLanguage: "",
      styleOptions: getDefaultStyleOptions(LANGUAGE_OPTIONS[0].key),
    }),
}));
