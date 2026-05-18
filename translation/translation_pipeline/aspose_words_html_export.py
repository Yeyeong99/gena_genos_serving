"""Isolated Aspose.Words HTML export helper."""

from __future__ import annotations

import argparse
import os


def _apply_license(aw_module) -> None:
    license_path = os.getenv("AI_TRANSLATION_ASPOSE_LICENSE_PATH") or os.getenv("ASPOSE_LICENSE_PATH")
    if not license_path:
        return
    license_path = os.path.expanduser(license_path)
    if not os.path.exists(license_path):
        return
    license = aw_module.License()
    license.set_license(license_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_docx")
    parser.add_argument("output_html")
    args = parser.parse_args()

    import aspose.words as aw  # type: ignore

    _apply_license(aw)
    doc = aw.Document(args.input_docx)
    options = aw.saving.HtmlSaveOptions(aw.SaveFormat.HTML)
    options.export_images_as_base64 = True
    options.export_fonts_as_base64 = True
    options.export_font_resources = True
    options.css_style_sheet_type = aw.saving.CssStyleSheetType.INLINE
    doc.save(args.output_html, options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
