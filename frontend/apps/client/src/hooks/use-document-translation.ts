"use client";

import { useMutation } from "@tanstack/react-query";
import { requestDocumentTranslationStart, type GenosStreamEvent } from "@/api/document-translation";
import { LANGUAGE_OPTIONS, type TranslateWorkflowResponse } from "@/components/document-translation/types";
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
    setDebugLastRequestedLanguage,
    mergeStreamingUpdate,
    styleOptions,
  } = useDocumentTranslationStore();

  return useMutation({
    mutationFn: async ({ fileBase64, filename, languageKey }: TranslateRequestArgs) => {
      const language = LANGUAGE_OPTIONS.find((item) => item.key === languageKey);
      if (!language) {
        throw new Error("지원하지 않는 언어입니다.");
      }

      setDebugLastRequestedLanguage(language.format);
      startTranslating();

      return requestDocumentTranslationStart(
        {
          format: language.format,
          file: fileBase64,
          filename,
          is_return_file: true,
          style_options: styleOptions,
        },
        (event) => {
          const update = getTranslationUpdateFromGenosEvent(event);
          if (!update?.job_id) {
            return;
          }
          mergeStreamingUpdate(update.job_id, update);
        },
      );
    },
    onSuccess: (response) => {
      setStartResult(response);
    },
    onError: () => {
      useDocumentTranslationStore.setState({
        isTranslating: false,
        stage: "idle",
      });
    },
  });
}

function getTranslationUpdateFromGenosEvent(
  event: GenosStreamEvent,
): Partial<TranslateWorkflowResponse> | null {
  if (event.event !== "translationEvent" || !isRecord(event.data)) {
    return null;
  }

  const data = event.data.data;
  if (!isRecord(data) || typeof data.job_id !== "string") {
    return null;
  }

  return data as Partial<TranslateWorkflowResponse>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
