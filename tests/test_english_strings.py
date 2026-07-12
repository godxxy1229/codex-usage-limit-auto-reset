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

OLD_USER_FACING_TERMS = (
    "Codex Reset Credit Manager",
    "Auto-redeem",
    "Exact credit",
)

USER_FACING_TERMINOLOGY_FILES = AUDITED_FILES + (
    "README.ko.md",
    "docs/images/social-preview.svg",
)

# Retired names may remain only as exact migration identifiers. Technical RPC
# and schema names such as ``creditId`` are intentionally outside this audit.
ALLOWED_RETIRED_LITERALS = {
    "install.ps1": ("'Codex Reset Credit Manager.lnk'",),
}


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

    def test_retired_product_terms_do_not_return_to_user_facing_text(self) -> None:
        found_allowed: dict[tuple[str, str], int] = {}
        for relative_path in USER_FACING_TERMINOLOGY_FILES:
            path = ROOT / relative_path
            text = path.read_text(encoding="utf-8-sig")
            for literal in ALLOWED_RETIRED_LITERALS.get(relative_path, ()):
                count = text.count(literal)
                found_allowed[(relative_path, literal)] = count
                text = text.replace(literal, "")
            for term in OLD_USER_FACING_TERMS:
                self.assertNotIn(term, text, f"Retired product term in {path}: {term}")

        self.assertNotIn(
            "초기화권",
            (ROOT / "README.ko.md").read_text(encoding="utf-8-sig"),
            "The Korean guide should use the ChatGPT usage-limit terminology",
        )

        for (relative_path, literal), count in found_allowed.items():
            self.assertEqual(
                count,
                1,
                f"Retired migration literal must appear exactly once: {relative_path}: {literal}",
            )


if __name__ == "__main__":
    unittest.main()
