from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANGUL = re.compile(r"[\uac00-\ud7a3]")

# These literals are data used to parse localized Windows output, not text
# shown by the application.  The legacy shortcut is retained solely so the
# installer can remove that exact previous-release artifact after migration.
ALLOWED_FUNCTIONAL_LITERALS = {
    "codex_reset_guard.py": (
        '"동기화되지 않음"',
        '"마지막으로 동기화한 시간: 지정되지 않음"',
    ),
    "install.ps1": (
        "'(?i)Local\\s+CMOS\\s+Clock|로컬\\s*CMOS|unsynchroni[sz]ed|동기화되지\\s*않'",
        "'(?i)^\\s*(Source|원본)\\s*:'",
        "'(?i)Local\\s+CMOS\\s+Clock|로컬\\s*CMOS'",
        "'Codex 초기화권 자동 사용.lnk'",
    ),
}

AUDITED_FILES = (
    "codex_reset_guard.py",
    "codex_reset_manager.py",
    "install.ps1",
    "setup.cmd",
    "README.md",
)


class EnglishUserFacingStringsTests(unittest.TestCase):
    def test_only_explicit_functional_korean_literals_remain(self) -> None:
        unexpected: list[str] = []
        found_allowed: dict[tuple[str, str], int] = {}

        for relative_path in AUDITED_FILES:
            text = (ROOT / relative_path).read_text(encoding="utf-8-sig")
            allowed = ALLOWED_FUNCTIONAL_LITERALS.get(relative_path, ())
            for literal in allowed:
                count = text.count(literal)
                found_allowed[(relative_path, literal)] = count
                text = text.replace(literal, "")

            for line_number, line in enumerate(text.splitlines(), start=1):
                if HANGUL.search(line):
                    unexpected.append(f"{relative_path}:{line_number}: {line.strip()}")

        self.assertFalse(
            unexpected,
            "Unexpected Korean user-facing text remains:\n" + "\n".join(unexpected),
        )

        for (relative_path, literal), count in found_allowed.items():
            self.assertEqual(
                count,
                1,
                f"Functional allowlisted literal must appear exactly once: {relative_path}: {literal}",
            )


if __name__ == "__main__":
    unittest.main()
