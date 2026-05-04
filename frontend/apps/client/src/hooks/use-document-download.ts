"use client";

import { useMutation } from "@tanstack/react-query";
import { requestDocumentTranslation } from "@/api/document-translation";
import { LANGUAGE_OPTIONS } from "@/components/document-translation/types";
import { useDocumentTranslationStore } from "@/store/document-translation-store";

type DownloadRequestArgs = {
  fileBase64: string;
  filename: string;
};

function decodeBase64ToBlob(base64: string, mimeType: string) {
  const binary = window.atob(base64);
  const bytes = new Uint8Array(binary.length);

  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }

  return new Blob([bytes], { type: mimeType || "application/octet-stream" });
}

function triggerFileDownload(base64: string, filename: string, mimeType: string) {
  const blob = decodeBase64ToBlob(base64, mimeType);
  const objectUrl = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  window.URL.revokeObjectURL(objectUrl);
}

function toPatchId(id: string | undefined) {
  if (id == null) {
    return "";
  }
  return Number.isNaN(Number(id)) ? id : Number(id);
}

export function useDocumentDownload() {
  const { editableBlocks, editedPatches, editedTranslations, debugLastRequestedLanguage, response } =
    useDocumentTranslationStore();

  return useMutation({
    mutationFn: async ({ fileBase64, filename }: DownloadRequestArgs) => {
      const selectedLanguage = useDocumentTranslationStore.getState().selectedLanguage;
      const fallbackLanguage =
        LANGUAGE_OPTIONS.find((item) => item.key === selectedLanguage)?.format ?? "English";
      const format = debugLastRequestedLanguage || fallbackLanguage;

      const editedTranslationPairs =
        Object.keys(editedPatches).length > 0
          ? Object.entries(editedPatches).map(([id, patch]) => ({
              id: toPatchId(editableBlocks.find((block) => block.id === id)?.backendId ?? id),
              translated: patch.translated,
              font_size: patch.font_size,
              line_break: patch.line_break,
            }))
          : Object.keys(editedTranslations).length > 0
          ? Object.entries(editedTranslations).map(([id, translated]) => ({
              id: toPatchId(editableBlocks.find((block) => block.id === id)?.backendId ?? id),
              translated,
            }))
          : [];

      const requestPayload = response?.job_id
        ? {
            format,
            job_id: response.job_id,
            file: fileBase64,
            filename,
            is_return_file: true,
            edited_translation_pairs: editedTranslationPairs,
          }
        : {
            format,
            file: fileBase64,
            filename,
            is_return_file: true,
            edited_translation_pairs: editedTranslationPairs,
          };

      const downloadResponse = await requestDocumentTranslation(requestPayload);

      if (!downloadResponse.file_base64) {
        throw new Error("저장된 파일 데이터가 응답에 포함되지 않았습니다.");
      }

      const downloadName = downloadResponse.output_filename || buildEditedFilename(filename);
      triggerFileDownload(downloadResponse.file_base64, downloadName, downloadResponse.mime_type || "");
      return downloadResponse;
    },
  });
}

function buildEditedFilename(filename: string) {
  const lastDotIndex = filename.lastIndexOf(".");
  if (lastDotIndex === -1) {
    return `${filename}_edited_translated`;
  }

  const name = filename.slice(0, lastDotIndex);
  const ext = filename.slice(lastDotIndex);
  return `${name}_edited_translated${ext}`;
}
