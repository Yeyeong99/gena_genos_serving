"use client";

import { useMutation } from "@tanstack/react-query";
import { requestDocumentTranslationStart } from "@/api/document-translation";
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
    setDebugLastRequestedLanguage,
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

      return requestDocumentTranslationStart({
        format: language.format,
        file: fileBase64,
        filename,
        is_return_file: true,
        style_options: styleOptions,
      });
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
