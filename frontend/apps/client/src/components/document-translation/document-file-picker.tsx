"use client";

import { FileText, Upload, X } from "lucide-react";
import { ChangeEvent, useRef } from "react";
import { Button } from "@gena/design-system";
import { useDocumentTranslationStore } from "@/store/document-translation-store";

const ACCEPTED_TYPES = ".docx,.pptx,.xlsx";

async function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("파일을 읽지 못했습니다."));
        return;
      }
      const [, base64 = ""] = result.split(",");
      resolve(base64);
    };
    reader.onerror = () => reject(new Error("파일 변환 중 오류가 발생했습니다."));
    reader.readAsDataURL(file);
  });
}

export function DocumentFilePicker() {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const { selectedFile, setSelectedFile } = useDocumentTranslationStore();

  const handleFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const nextFile = event.target.files?.[0];
    if (!nextFile) {
      return;
    }

    const extension = nextFile.name.split(".").pop()?.toLowerCase();
    if (!extension || !["docx", "pptx", "xlsx"].includes(extension)) {
      window.alert("docx, pptx, xlsx 문서만 업로드할 수 있습니다.");
      return;
    }

    const base64 = await fileToBase64(nextFile);
    setSelectedFile({ file: nextFile, base64 });
  };

  return (
    <div className="rounded-[22px] border border-[#dbe4f2] bg-[#fbfcff] p-3">
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        accept={ACCEPTED_TYPES}
        onChange={handleFileChange}
      />

      <div className="flex flex-wrap items-center gap-2 overflow-hidden">
        <Button variant="outline" onClick={() => inputRef.current?.click()} className="h-12 px-4">
          <Upload className="h-5 w-5" />
          파일 선택
        </Button>

        {selectedFile ? (
          <div className="flex min-h-12 min-w-0 flex-1 items-center gap-2 overflow-hidden rounded-2xl border border-[#d8e3f5] bg-white px-3 py-2 text-[#2a3142]">
            <FileText className="h-5 w-5 shrink-0 text-[var(--primary)]" />
            <span className="line-clamp-1 min-w-0 flex-1 text-sm font-medium">{selectedFile.file.name}</span>
            <button
              type="button"
              onClick={() => setSelectedFile(null)}
              className="rounded-full p-1 text-[#6d7791] transition hover:bg-[#eef3ff] hover:text-[#1d2438]"
              aria-label="선택한 파일 제거"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
        ) : (
          <div className="flex min-h-12 flex-1 items-center rounded-2xl border border-dashed border-[#d8e3f5] px-3 text-sm leading-5 text-[var(--muted)]">
            업로드할 문서를 선택해 주세요.
          </div>
        )}
      </div>
    </div>
  );
}
