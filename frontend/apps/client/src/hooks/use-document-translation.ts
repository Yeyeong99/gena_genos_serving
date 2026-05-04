"use client";

import { useMutation } from "@tanstack/react-query";
import {
  createDocumentTranslationEventSource,
  parseTranslationStreamEvent,
  requestDocumentPreviewStatus,
  requestDocumentTranslationStart,
} from "@/api/document-translation";
import { LANGUAGE_OPTIONS } from "@/components/document-translation/types";
import { useDocumentTranslationStore } from "@/store/document-translation-store";

type TranslateRequestArgs = {
  fileBase64: string;
  filename: string;
  languageKey: string;
};

export function useDocumentTranslation() {
  const {
    startTranslating,
    setStartResult,
    mergeDeferredPreview,
    mergeStreamingUpdate,
    setDebugLastRequestedLanguage,
    styleOptions,
  } = useDocumentTranslationStore();

  const pollDeferredPreview = async (jobId: string) => {
    try {
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        const status = await requestDocumentPreviewStatus(jobId);
        if (status.translated_preview_status === "pending") {
          continue;
        }
        mergeDeferredPreview(jobId, status);
        break;
      }
    } catch (error) {
      console.error("Deferred preview polling failed", error);
    }
  };

  return useMutation({
    mutationFn: async ({ fileBase64, filename, languageKey }: TranslateRequestArgs) => {
      const language = LANGUAGE_OPTIONS.find((item) => item.key === languageKey);
      if (!language) {
        throw new Error("지원하지 않는 언어입니다.");
      }

      setDebugLastRequestedLanguage(language.format);
      startTranslating();

      return requestDocumentTranslationStart({
        format: language.format,
        file: fileBase64,
        filename,
        is_return_file: false,
        style_options: styleOptions,
      });
    },
    onSuccess: (response) => {
      setStartResult(response);

      if (response.job_id && response.translation_status !== "done" && response.translation_status !== "error") {
        const source = createDocumentTranslationEventSource(response.job_id);
        const closeSource = () => {
          source.close();
        };

        source.addEventListener("translation_started", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("original_preview_ready", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("slide_translated", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("slide_translation_started", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("slide_injected", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("slide_html_ready", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("page_translation_started", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("page_translated", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("page_injected", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("page_html_ready", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("sheet_translated", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("sheet_translation_started", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("sheet_injected", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("sheet_html_ready", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("blocks_translated", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
        });
        source.addEventListener("completed", (event) => {
          const parsed = parseTranslationStreamEvent(event as MessageEvent<string>);
          mergeStreamingUpdate(response.job_id!, parsed.data);
          closeSource();
          if (
            parsed.data.translated_preview_job_id &&
            parsed.data.translated_preview_status === "pending"
          ) {
            void pollDeferredPreview(parsed.data.translated_preview_job_id);
          }
        });
        source.addEventListener("job_error", (event) => {
          if (event instanceof MessageEvent && event.data) {
            const parsed = parseTranslationStreamEvent(event);
            mergeStreamingUpdate(response.job_id!, parsed.data);
          }
          closeSource();
        });
        source.onerror = () => {
          closeSource();
        };
      }

      if (
        response.translated_preview_job_id &&
        response.translated_preview_status === "pending"
      ) {
        void pollDeferredPreview(response.translated_preview_job_id);
      }
    },
    onError: () => {
      useDocumentTranslationStore.setState({
        isTranslating: false,
        stage: "idle",
      });
    },
  });
}
