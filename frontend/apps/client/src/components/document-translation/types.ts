export const LANGUAGE_OPTIONS = [
  { key: "ko", label: "한국어", format: "Korean" },
  { key: "en", label: "영어", format: "English" },
  { key: "ja", label: "일본어", format: "Japanese" },
  { key: "zh", label: "중국어", format: "Chinese" },
] as const;

export type LanguageKey = (typeof LANGUAGE_OPTIONS)[number]["key"];

export type TranslationStyleOptionKey = "purpose" | "formality" | "terminology" | "script";

export type TranslationStyleOptions = Partial<Record<TranslationStyleOptionKey, string>>;

export type TranslationStyleOption = {
  label: string;
  value: string;
  description?: string;
};

export type TranslationStyleOptionGroup = {
  key: TranslationStyleOptionKey;
  label: string;
  helper?: string;
  options: readonly TranslationStyleOption[];
};

const TERMINOLOGY_OPTIONS = [
  { label: "원문 용어 유지", value: "preserve_key_terms" },
  { label: "자연스럽게 번역", value: "natural_translation" },
  { label: "전문 용어 우선", value: "technical_terms" },
] as const;

const PURPOSE_OPTIONS = [
  { label: "기본", value: "default" },
  { label: "발표", value: "presentation" },
  { label: "일상", value: "casual_use" },
  { label: "업무", value: "business" },
] as const;

const KOREAN_FORMALITY_OPTIONS = [
  { label: "격식체", value: "formal_hamnida" },
  { label: "비격식체", value: "informal_friendly" },
  { label: "음·함체", value: "eum_ham" },
] as const;

const GENERAL_FORMALITY_OPTIONS = [
  { label: "격식체", value: "formal_hamnida" },
  { label: "비격식체", value: "informal_friendly" },
] as const;

export const STYLE_OPTION_GROUPS_BY_LANGUAGE: Record<
  LanguageKey,
  readonly TranslationStyleOptionGroup[]
> = {
  ko: [
    {
      key: "purpose",
      label: "목적",
      helper: "번역 결과가 쓰일 상황을 맞춥니다.",
      options: PURPOSE_OPTIONS,
    },
    {
      key: "formality",
      label: "문체",
      helper: "문장 끝맺음과 격식 수준을 맞춥니다.",
      options: KOREAN_FORMALITY_OPTIONS,
    },
    {
      key: "terminology",
      label: "용어 처리",
      options: TERMINOLOGY_OPTIONS,
    },
  ],
  en: [
    {
      key: "purpose",
      label: "목적",
      helper: "번역 결과가 쓰일 상황을 맞춥니다.",
      options: PURPOSE_OPTIONS,
    },
    {
      key: "formality",
      label: "문체",
      helper: "문장 톤과 격식 수준을 맞춥니다.",
      options: GENERAL_FORMALITY_OPTIONS,
    },
    {
      key: "terminology",
      label: "용어 처리",
      options: TERMINOLOGY_OPTIONS,
    },
  ],
  ja: [
    {
      key: "purpose",
      label: "목적",
      helper: "번역 결과가 쓰일 상황을 맞춥니다.",
      options: PURPOSE_OPTIONS,
    },
    {
      key: "formality",
      label: "문체",
      helper: "문장 톤과 격식 수준을 맞춥니다.",
      options: GENERAL_FORMALITY_OPTIONS,
    },
    {
      key: "terminology",
      label: "용어 처리",
      options: TERMINOLOGY_OPTIONS,
    },
  ],
  zh: [
    {
      key: "purpose",
      label: "목적",
      helper: "번역 결과가 쓰일 상황을 맞춥니다.",
      options: PURPOSE_OPTIONS,
    },
    {
      key: "formality",
      label: "문체",
      helper: "문장 톤과 격식 수준을 맞춥니다.",
      options: GENERAL_FORMALITY_OPTIONS,
    },
    {
      key: "script",
      label: "문자 체계",
      options: [
        { label: "간체", value: "simplified" },
        { label: "번체", value: "traditional" },
      ],
    },
    {
      key: "terminology",
      label: "용어 처리",
      options: TERMINOLOGY_OPTIONS,
    },
  ],
};

export function getDefaultStyleOptions(language: LanguageKey): TranslationStyleOptions {
  const groups = STYLE_OPTION_GROUPS_BY_LANGUAGE[language] ?? [];
  return groups.reduce<TranslationStyleOptions>((acc, group) => {
    const firstOption = group.options[0];
    if (firstOption) {
      acc[group.key] = firstOption.value;
    }
    return acc;
  }, {});
}

export type TranslationPair = {
  id?: number;
  original: string;
  translated: string;
  type?: string;
  source?: string;
};

export type TranslateWorkflowPayload = {
  format: string;
  job_id?: string;
  input_text?: string;
  file?: string;
  filename?: string;
  is_return_file?: boolean;
  style_options?: TranslationStyleOptions;
  edited_translation_pairs?: EditedTranslationPatch[];
};

export type TranslationRevisionScope = {
  type: "slide" | "sheet" | "batch";
  index?: number;
  label?: string;
};

export type TranslateRevisionPayload = {
  job_id: string;
  format: string;
  scope?: TranslationRevisionScope | null;
  style_options?: TranslationStyleOptions;
  instruction?: string;
  is_return_file?: boolean;
};

export type EditedTranslationPatch = {
  id: number | string;
  translated: string;
  font_size?: number;
  line_break?: boolean;
};

export type TranslateWorkflowResponse = TranslateWorkflowPayload & {
  job_id?: string;
  input_text?: string;
  text: string;
  pairs?: TranslationPair[];
  translation_pairs?: TranslationPair[];
  document_blocks?: DocumentBlock[];
  file_path?: string;
  output_filename?: string;
  file_base64?: string;
  mime_type?: string;
  original_preview_images?: string[];
  original_preview_html_url?: string;
  translated_preview_images?: string[];
  translated_preview_html_url?: string;
  preview_page_sizes?: Array<{
    width: number;
    height: number;
  }>;
  preview_render_mode?: "actual" | "synthetic" | "html";
  translated_preview_job_id?: string;
  original_preview_status?: "pending" | "done" | "error";
  translated_preview_status?: "pending" | "done" | "error";
  preview_status?: "pending" | "done" | "failed" | "error";
  preview_error?: string;
  translation_status?: "pending" | "translating" | "translated" | "done" | "error";
  translation_error?: string;
  translation_notice?: string;
  translation_skipped_reason?: "same_language" | string;
  current_scope?: string;
  current_slide?: number;
  total_slides?: number;
  current_page?: number;
  total_pages?: number;
  current_sheet?: number;
  current_sheet_name?: string;
  total_sheets?: number;
  event_phase?:
    | "translation_started"
    | "blocks_translated"
    | "page_translation_started"
    | "page_translated"
    | "page_injected"
    | "page_html_ready"
    | "slide_translation_started"
    | "slide_translated"
    | "slide_injected"
    | "slide_html_ready"
    | "sheet_translation_started"
    | "sheet_translated"
    | "sheet_injected"
    | "sheet_html_ready"
    | "original_preview_ready"
    | "completed"
    | "job_error";
  created_at?: number;
  completed_at?: number;
  elapsed_ms?: number;
  llm_model_name?: string;
  llm_provider_sort?: string;
  debug_page_timings?: DebugPageTiming[];
  revision_status?: "pending" | "done" | "error";
  revision_scope?: TranslationRevisionScope | null;
};

export type DebugPageTiming = {
  kind?: "slide" | "sheet" | "page" | string;
  index?: number;
  label?: string;
  scope?: string;
  html_ready_elapsed_ms?: number;
  html_render_ms?: number;
};

export type TranslatePreviewStatusResponse = Partial<TranslateWorkflowResponse> & {
  translated_preview_job_id: string;
  translated_preview_status: "pending" | "done" | "error";
  message?: string;
};

export type TranslationStreamEventName =
  | "translation_started"
  | "blocks_translated"
  | "page_translation_started"
  | "page_translated"
  | "page_injected"
  | "page_html_ready"
  | "slide_translation_started"
  | "slide_translated"
  | "slide_injected"
  | "slide_html_ready"
  | "sheet_translation_started"
  | "sheet_translated"
  | "sheet_injected"
  | "sheet_html_ready"
  | "original_preview_ready"
  | "completed"
  | "job_error";

export type TranslationStreamEvent = {
  event: TranslationStreamEventName;
  data: Partial<TranslateWorkflowResponse> & {
    job_id: string;
    current_scope?: string;
    current_slide?: number;
    total_slides?: number;
    current_page?: number;
    total_pages?: number;
    current_sheet?: number;
    current_sheet_name?: string;
    total_sheets?: number;
    event_phase?: TranslateWorkflowResponse["event_phase"];
    llm_model_name?: string;
    llm_provider_sort?: string;
    debug_page_timings?: DebugPageTiming[];
  };
};

export type RealtimeTranslatePayload = {
  format: string;
  input_text: string;
};

export type RealtimeTranslateResponse = RealtimeTranslatePayload & {
  text: string;
};

export type EditableBlock = {
  id: string;
  backendId?: string;
  original: string;
  translated: string;
  baseTranslated?: string;
  isEdited?: boolean;
  accent?: "plain" | "highlight";
  group?: string;
  source?: string;
  type?: string;
  style?: {
    font_size?: number;
    bold?: boolean;
    italic?: boolean;
    underline?: boolean;
    font_name?: string;
    align?: string;
    fill?: string;
    color?: string;
  };
  location?: {
    page?: number;
    translated_page?: number;
    slide?: number;
    sheet?: string;
    row?: number;
    col?: number;
    cell?: string;
    bbox?: number[];
    original_bbox?: number[];
    translated_bbox?: number[];
  };
};

export type TranslationStage = "idle" | "uploading" | "translated";

export type DocumentBlock = {
  id: number;
  type?: string;
  source?: string;
  group?: string;
  order?: number;
  original: string;
  translated: string;
  style?: {
    font_size?: number;
    bold?: boolean;
    italic?: boolean;
    underline?: boolean;
    font_name?: string;
    align?: string;
    fill?: string;
    color?: string;
  };
  location?: {
    page?: number;
    translated_page?: number;
    slide?: number;
    sheet?: string;
    row?: number;
    col?: number;
    cell?: string;
    bbox?: number[];
    original_bbox?: number[];
    translated_bbox?: number[];
  };
  container?: {
    section?: string;
    paragraph_style?: string;
    chart_kind?: string;
  };
};
