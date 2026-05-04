"use client";

import { Card } from "@gena/design-system";
import {
  LANGUAGE_OPTIONS,
  STYLE_OPTION_GROUPS_BY_LANGUAGE,
} from "@/components/document-translation/types";
import { cn } from "@/lib/utils";
import { useDocumentTranslationStore } from "@/store/document-translation-store";

export function TargetLanguageSelector() {
  const { selectedLanguage, setLanguage, styleOptions, setStyleOption } =
    useDocumentTranslationStore();
  const styleGroups = STYLE_OPTION_GROUPS_BY_LANGUAGE[selectedLanguage] ?? [];

  return (
    <Card className="rise-in p-4">
      <section>
        <h3 className="text-[23px] font-semibold tracking-[-0.04em] text-[#20273a]">
          타겟 언어 선택
        </h3>
        <p className="mt-2 text-sm leading-6 text-[var(--muted)]">
          선택지는 한글, 영어, 일본어, 중국어 선택이 가능합니다.
        </p>
      </section>

      <div className="mt-4 grid grid-cols-2 gap-2">
        {LANGUAGE_OPTIONS.map((language) => {
          const active = selectedLanguage === language.key;

          return (
            <button
              key={language.key}
              type="button"
              onClick={() => setLanguage(language.key)}
              className={cn(
                "h-12 rounded-2xl border text-base font-semibold transition",
                active
                  ? "border-[var(--primary)] bg-[var(--primary)] text-white shadow-none"
                  : "border-[#dce5f3] bg-white text-[#252c3f] hover:border-[#afc0ef] hover:bg-[#f7f9ff]",
              )}
            >
              {language.label}
            </button>
          );
        })}
      </div>

      <section className="mt-5 border-t border-[#e7edf8] pt-4">
        <div>
          <h4 className="text-[18px] font-semibold tracking-[-0.035em] text-[#20273a]">
            번역 스타일
          </h4>
          <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
            선택한 값은 번역 요청과 함께 전달됩니다.
          </p>
        </div>

        {styleGroups.length > 0 ? (
          <div className="mt-4 space-y-4">
            {styleGroups.map((group) => (
              <div key={group.key}>
                <div className="flex items-end justify-between gap-3">
                  <p className="text-xs font-semibold text-[#2d3448]">{group.label}</p>
                  {group.helper ? (
                    <p className="text-right text-[10px] leading-4 text-[#7b859b]">
                      {group.helper}
                    </p>
                  ) : null}
                </div>

                <div className="mt-2 flex flex-wrap gap-1.5">
                  {group.options.map((option) => {
                    const active = styleOptions[group.key] === option.value;

                    return (
                      <button
                        key={`${group.key}-${option.value}`}
                        type="button"
                        onClick={() => setStyleOption(group.key, option.value)}
                        className={cn(
                          "rounded-full border px-3 py-1.5 text-xs font-semibold transition",
                          active
                            ? "border-[#4f46e5] bg-[#eef0ff] text-[#4338ca]"
                            : "border-[#dce5f3] bg-white text-[#3b4357] hover:border-[#afc0ef] hover:bg-[#f7f9ff]",
                        )}
                      >
                        {option.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="mt-4 rounded-2xl border border-dashed border-[#d6e1f2] bg-white/70 p-3 text-xs leading-5 text-[#7b859b]">
            타겟 언어를 선택하면 문체와 용어 처리 옵션이 표시됩니다.
          </div>
        )}
      </section>
    </Card>
  );
}
