"""Isolated Aspose.Cells HTML export helper."""

from __future__ import annotations

import argparse
import os


def _apply_license(cells_module) -> None:
    license_path = os.getenv("AI_TRANSLATION_ASPOSE_LICENSE_PATH") or os.getenv("ASPOSE_LICENSE_PATH")
    if not license_path:
        return
    license_path = os.path.expanduser(license_path)
    if not os.path.exists(license_path):
        return
    license = cells_module.License()
    license.set_license(license_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_xlsx")
    parser.add_argument("output_html")
    args = parser.parse_args()

    import aspose.cells as cells  # type: ignore

    _apply_license(cells)
    workbook = cells.Workbook(args.input_xlsx)
    try:
        workbook.calculate_formula()
    except Exception:
        pass
    options = cells.HtmlSaveOptions(cells.SaveFormat.HTML)
    options.calculate_formula = True
    options.export_grid_lines = True
    options.export_images_as_base64 = True
    options.export_worksheet_css_separately = False
    options.save_as_single_file = True
    options.show_all_sheets = True
    workbook.save(args.output_html, options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
