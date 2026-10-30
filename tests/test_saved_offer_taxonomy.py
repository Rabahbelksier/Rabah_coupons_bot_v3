import re
import unittest
from collections import Counter
from pathlib import Path

from core.categories import (
    SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES,
    saved_offer_filter_from_subcategory,
)
from core.db import _post_match_score


_REFERENCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "attached_assets"
    / "Pasted---1790259010503_1790259010505.txt"
)
_REFERENCE_FILTERS = {
    "إلكترونيات": "saved_electronics",
    "كمبيوتر": "saved_computer",
    "الأطفال": "saved_children",
    "المنزل": "saved_home",
    "رياضة": "saved_sports",
    "الجمال": "saved_beauty",
    "الإكسسوارات": "saved_accessories",
    "الأزياء": "saved_fashion",
    "المركبات": "saved_vehicles",
    "الأدوات": "saved_tools",
    "متنوعات": "saved_miscellaneous",
    "قطع غيار": "saved_spare_parts",
    "مستلزمات إلكترونية": "saved_electronic_supplies",
    "هواتف": "saved_phones",
    "طابلات": "saved_tablets",
}


def _parse_reference_names(raw, candidates, label):
    remaining = raw.strip()
    ordered_candidates = sorted(set(candidates), key=len, reverse=True)
    names = []

    while remaining:
        remaining = remaining.lstrip()
        name = next(
            (
                candidate
                for candidate in ordered_candidates
                if remaining.startswith(candidate)
                and (
                    len(remaining) == len(candidate)
                    or remaining[len(candidate):].lstrip().startswith(",")
                )
            ),
            None,
        )
        if name is None:
            raise AssertionError(
                f"Unmapped category under {label}: {remaining[:100]!r}"
            )
        names.append(name)
        remaining = remaining[len(name):].lstrip()
        if remaining:
            if not remaining.startswith(","):
                raise AssertionError(
                    f"Expected a comma after {name!r} under {label}"
                )
            remaining = remaining[1:]

    return names


class SavedOfferTaxonomyTests(unittest.TestCase):
    def test_all_saved_offer_names_match_the_supplied_reference(self):
        sections = {}
        for line in _REFERENCE_PATH.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^🔸\s*(.*?):\s*(.*)$", line)
            if not match:
                continue
            label, raw_names = match.groups()
            key = _REFERENCE_FILTERS[label.strip()]
            sections[key] = _parse_reference_names(
                raw_names,
                SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES[key],
                label,
            )

        self.assertEqual(set(sections), set(SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES))
        for key, expected_names in SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES.items():
            with self.subTest(category=key):
                self.assertEqual(
                    Counter(sections[key]),
                    Counter(expected_names),
                )
                for name in sections[key]:
                    self.assertEqual(
                        saved_offer_filter_from_subcategory(name),
                        key,
                    )

    def test_home_audio_is_electronics_and_never_phones(self):
        post = {"second_level_category_name": "Home Audio & Video"}

        self.assertEqual(
            saved_offer_filter_from_subcategory("Home Audio & Video"),
            "saved_electronics",
        )
        self.assertIsNone(
            _post_match_score(
                post,
                {
                    "request_type": "trending",
                    "keywords": [],
                    "saved_subcategory": "saved_phones",
                },
                [],
            )
        )
        self.assertIsNotNone(
            _post_match_score(
                post,
                {
                    "request_type": "trending",
                    "keywords": [],
                    "saved_subcategory": "saved_electronics",
                },
                [],
            )
        )
