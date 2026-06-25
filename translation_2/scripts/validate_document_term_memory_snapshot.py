from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from translation_pipeline.common.document_term_memory_structure import (  # noqa: E402
    validate_document_term_memory_snapshot,
)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python scripts/validate_document_term_memory_snapshot.py <snapshot.json> [...]")
        return 2
    failed = False
    for value in argv[1:]:
        path = Path(value)
        data = json.loads(path.read_text(encoding="utf-8"))
        problems = validate_document_term_memory_snapshot(data)
        print(json.dumps({"path": str(path), "problem_count": len(problems), "problems": problems}, ensure_ascii=False, indent=2))
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

