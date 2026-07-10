"""Unit tests for SMC part normalization (no Selenium required)."""

import re
import unittest


def normalize_smc_part(part: str) -> str:
    raw = str(part or "").strip().upper()
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw)
    if re.match(r"^\d{1,2}[A-Z]", compact) and "-" not in compact:
        m = re.match(r"^(\d{1,2})([A-Z].+)$", compact)
        if m:
            body = m.group(2)
            if len(body) > 6 and "-" not in body:
                return f"{m.group(1)}-{body[:6]}-{body[6:]}"
            return f"{m.group(1)}-{body}"
    return raw


def smc_lookup_keys(part_no: str) -> list[str]:
    raw = str(part_no or "").strip()
    keys: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            keys.append(value)

    add(raw)
    add(raw.upper())
    add(normalize_smc_part(raw))
    compact = re.sub(r"[^A-Z0-9]", "", raw.upper())
    add(compact)
    return keys


class SmcNormalizationTests(unittest.TestCase):
    def test_compact_to_hyphenated(self):
        self.assertEqual(normalize_smc_part("10KQ2H06M5N"), "10-KQ2H06-M5N")

    def test_preserve_existing_hyphens(self):
        self.assertEqual(normalize_smc_part("10-KQ2H06-M5N"), "10-KQ2H06-M5N")

    def test_lookup_keys_include_variants(self):
        keys = smc_lookup_keys("10-KQ2H06-M5N")
        self.assertIn("10-KQ2H06-M5N", keys)
        self.assertTrue(any("KQ2H06" in k for k in keys))


if __name__ == "__main__":
    unittest.main()
