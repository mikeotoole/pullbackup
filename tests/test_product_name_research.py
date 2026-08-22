"""Deterministic guard for docs/research/product-name.md.

The naming research note is a decision artifact: downstream work (rename PR,
counsel review, domain acquisition) depends on it keeping its structure, its
evidence, and its disclaimers. These tests fail loudly if a future edit drops a
required section, breaks the screening matrix, or leaves a citation dangling.

No network access; everything is derived from the checked-in file.
"""

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NOTE = REPOSITORY_ROOT / "docs" / "research" / "product-name.md"

REQUIRED_HEADINGS = [
    "## 1. Executive conclusion",
    "## 2. Scope and methodology",
    "## 3. The incumbent: why \"Pullback\" fails",
    "## 4. Screening matrix — full candidate set",
    "## 5. Rejections, with the strongest direct source",
    "## 6. Finalists",
    "## 7. Risk assessment by intended use",
    "## 8. Recommendation",
    "## 9. Questions for trademark counsel",
    "## 10. Limitations and disclaimers",
    "## Sources",
]

REQUIRED_CHANNEL_TERMS = [
    "USPTO",
    "npm",
    "PyPI",
    "Docker Hub",
    "GitHub",
    "App Store",
    "Google Play",
    "RDAP",
]

DISPOSITIONS = {"Preferred", "Conditional", "Reserve", "Hold", "Reject"}

KNOWN_COLLISIONS = ["PullBackup", "sudaraka/pullback"]

DISCLAIMER_PHRASES = [
    "not a legal clearance opinion",
    "Search date:",
    "Jurisdiction:",
    "No trademark application was filed",
]


def read_note() -> str:
    return NOTE.read_text(encoding="utf-8")


def matrix_rows(text: str):
    """Return the candidate rows of the two screening-matrix tables.

    A candidate row looks like: | 7 | Sheave | taken | ... | Hold |
    """
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 10:
            continue
        if not re.fullmatch(r"\d+", cells[0]):
            continue
        rows.append(cells)
    return rows


class ProductNameNoteStructureTests(unittest.TestCase):
    def setUp(self):
        self.text = read_note()

    def test_note_exists_and_is_substantial(self):
        self.assertTrue(NOTE.is_file(), f"missing research note: {NOTE}")
        self.assertGreater(len(self.text), 5000,
                           "research note is too short to be decision-grade")

    def test_every_required_section_is_present_in_order(self):
        position = -1
        for heading in REQUIRED_HEADINGS:
            found = self.text.find(heading)
            self.assertNotEqual(found, -1, f"missing required section: {heading}")
            self.assertGreater(found, position,
                               f"section out of order: {heading}")
            position = found

    def test_search_date_is_present_and_iso_formatted(self):
        match = re.search(r"\*\*Search date:\*\*\s*(\d{4}-\d{2}-\d{2})", self.text)
        self.assertIsNotNone(match, "no ISO-formatted search date declared")

    def test_disclaimers_and_jurisdiction_limits_are_stated(self):
        for phrase in DISCLAIMER_PHRASES:
            self.assertIn(phrase, self.text,
                          f"missing required disclaimer text: {phrase!r}")

    def test_known_collisions_are_documented(self):
        for collision in KNOWN_COLLISIONS:
            self.assertIn(collision, self.text,
                          f"known collision evidence missing: {collision}")


class ScreeningMatrixTests(unittest.TestCase):
    def setUp(self):
        self.text = read_note()
        self.rows = matrix_rows(self.text)

    def test_candidate_count_is_within_the_required_range(self):
        self.assertGreaterEqual(len(self.rows), 15,
                                "fewer than 15 screened candidates")
        self.assertLessEqual(len(self.rows), 40,
                             "unexpectedly many matrix rows; check parsing")

    def test_candidate_numbering_is_contiguous_from_one(self):
        numbers = [int(row[0]) for row in self.rows]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)),
                         "candidate numbering is not contiguous")

    def test_candidate_names_are_unique(self):
        names = [row[1].strip("*").lower() for row in self.rows]
        self.assertEqual(len(names), len(set(names)),
                         "duplicate candidate names in the screening matrix")

    def test_every_row_carries_a_recognised_disposition(self):
        for row in self.rows:
            disposition = row[-1].strip("*")
            self.assertIn(disposition, DISPOSITIONS,
                          f"unrecognised disposition {disposition!r} for {row[1]}")

    def test_all_screening_channels_appear_in_the_methodology(self):
        methodology = self.text.split("## 3.")[0]
        for term in REQUIRED_CHANNEL_TERMS:
            self.assertIn(term, methodology,
                          f"channel not documented in methodology: {term}")

    def test_three_to_five_finalists_are_named(self):
        preferred = [r for r in self.rows if r[-1].strip("*") == "Preferred"]
        conditional = [r for r in self.rows if r[-1].strip("*") == "Conditional"]
        finalists = preferred + conditional
        self.assertGreaterEqual(len(finalists), 3,
                                "fewer than 3 preferred/conditional finalists")
        self.assertLessEqual(len(finalists), 5,
                             "more than 5 preferred/conditional finalists")

    def test_each_finalist_has_its_own_detail_subsection(self):
        finalists = [r[1].strip("*") for r in self.rows
                     if r[-1].strip("*") in {"Preferred", "Conditional"}]
        finalist_section = self.text.split("## 6. Finalists")[1].split("## 7.")[0]
        # Match subsection HEADINGS, not raw substrings: a bare containment check
        # passes when a finalist name merely appears inside unrelated prose (e.g.
        # "Inhaul" inside "Inhauler"), silently accepting a missing subsection.
        headings = [
            line for line in finalist_section.splitlines() if line.startswith("###")
        ]
        for name in finalists:
            self.assertTrue(
                any(
                    re.search(rf"^###\s*[\d.]*\s*{re.escape(name)}\b", h)
                    for h in headings
                ),
                f"finalist {name} has no detail subsection heading in section 6",
            )


class CitationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.text = read_note()
        body, _, sources = self.text.partition("## Sources")
        self.body = body
        self.sources_block = sources

    def _source_ids(self):
        return {int(m) for m in
                re.findall(r"^\[(\d+)\]\s+https?://", self.sources_block, re.M)}

    def _cited_ids(self):
        ids = set()
        for match in re.finditer(r"\[(\d+)\]", self.body):
            ids.add(int(match.group(1)))
        return ids

    def test_sources_block_is_present_and_populated(self):
        self.assertTrue(self.sources_block.strip(), "empty Sources block")
        self.assertGreaterEqual(len(self._source_ids()), 20,
                                "fewer than 20 listed sources")

    def test_every_inline_citation_resolves_to_a_listed_source(self):
        dangling = sorted(self._cited_ids() - self._source_ids())
        self.assertEqual(dangling, [],
                         f"inline citations with no Sources entry: {dangling}")

    def test_every_listed_source_is_actually_cited(self):
        uncited = sorted(self._source_ids() - self._cited_ids())
        self.assertEqual(uncited, [],
                         f"Sources entries never cited in the body: {uncited}")

    def test_source_urls_are_absolute_and_independently_checkable(self):
        urls = re.findall(r"^\[\d+\]\s+(\S+)", self.sources_block, re.M)
        self.assertTrue(urls, "no source URLs found")
        for url in urls:
            self.assertTrue(url.startswith("https://"),
                            f"source URL is not https: {url}")

    def test_source_ids_are_unique(self):
        ids = re.findall(r"^\[(\d+)\]\s+https?://", self.sources_block, re.M)
        self.assertEqual(len(ids), len(set(ids)), "duplicate Sources ids")

    def test_federal_and_marketplace_evidence_are_both_cited(self):
        urls = " ".join(re.findall(r"^\[\d+\]\s+(\S+)", self.sources_block, re.M))
        self.assertIn("tsdr.uspto.gov", urls,
                      "no federal record citation (TSDR) in Sources")
        self.assertIn("github.com", urls,
                      "no developer-marketplace citation in Sources")
        self.assertIn("rdap", urls, "no domain (RDAP) citation in Sources")

    def test_federal_records_carry_serial_numbers(self):
        serials = re.findall(r"serials?\s+(\d{8})", self.body)
        self.assertGreaterEqual(len(set(serials)), 5,
                                "fewer than 5 distinct federal serial numbers cited")


if __name__ == "__main__":
    unittest.main()
