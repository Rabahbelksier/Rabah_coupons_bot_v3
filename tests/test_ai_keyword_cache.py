import unittest
from unittest.mock import MagicMock, patch

from core.db import (
    _keyword_cache_phrases,
    _post_match_score,
    rebuild_ai_keyword_cache_from_processed_titles,
    refresh_ai_keyword_cache_from_title,
)


EXAMPLE_TITLE = (
    "Adaptateur scanner OBD2 L17 Bluetooth sans fil - "
    "Lecteur de codes OBD II – "
    "Réinitialisation et Effacement du voyant moteur"
)


class AiKeywordPhraseCacheTests(unittest.TestCase):
    def test_extracts_source_phrases_and_repeats_the_shared_object(self):
        self.assertEqual(
            _keyword_cache_phrases(EXAMPLE_TITLE),
            [
                "Adaptateur scanner OBD2 L17 Bluetooth sans fil",
                "Lecteur de codes OBD II",
                "Réinitialisation du voyant moteur",
                "Effacement du voyant moteur",
            ],
        )

    def test_extraction_preserves_hyphens_inside_product_names(self):
        title = "Samsung Galaxy S23-Ultra 5G Model-Z"

        self.assertEqual(_keyword_cache_phrases(title), [title])

    def test_arabic_title_spacing_is_preserved_and_remains_searchable(self):
        title = "جهاز فحص السيارات OBD2 L17"

        self.assertEqual(_keyword_cache_phrases(title), [title])
        self.assertIsNotNone(
            _post_match_score(
                {"title": title, "price": 20},
                {
                    "request_type": "product_best",
                    "keywords": [title],
                    "category": None,
                },
                [title],
            )
        )

    def test_nfkc_compatible_delimiters_still_split_source_phrases(self):
        title = "Samsung Galaxy S23； Xiaomi Redmi Note 14"

        self.assertEqual(
            _keyword_cache_phrases(title),
            ["Samsung Galaxy S23", "Xiaomi Redmi Note 14"],
        )

    def test_hyphenated_phrase_still_matches_its_offer(self):
        phrase = "Samsung Galaxy S23-Ultra 5G Model-Z"
        intent = {
            "request_type": "product_best",
            "keywords": [phrase],
            "category": "phones",
        }

        self.assertIsNotNone(
            _post_match_score(
                {"title": phrase, "price": 20},
                intent,
                [phrase],
            )
        )

    def test_phrase_matching_accepts_underscores_and_spaces(self):
        for phrase in (
            "Adaptateur_scanner_OBD2_L17_Bluetooth_sans_fil",
            "Adaptateur scanner OBD2 L17 Bluetooth sans fil",
        ):
            with self.subTest(phrase=phrase):
                intent = {
                    "request_type": "product_best",
                    "keywords": [phrase],
                    "category": None,
                }

                self.assertIsNotNone(
                    _post_match_score(
                        {
                            "title": (
                                "Adaptateur scanner OBD2 L17 Bluetooth sans fil"
                            ),
                            "price": 20,
                        },
                        intent,
                        [phrase],
                    )
                )
                self.assertIsNone(
                    _post_match_score(
                        {
                            "title": "Automotive OBD2 code reader",
                            "price": 20,
                        },
                        intent,
                        [phrase],
                    )
                )

    @patch("core.db.DATABASE_URL", "postgresql://local-test")
    @patch("core.db.get_db_connection")
    def test_title_refresh_upserts_phrases_with_source_spacing(self, get_conn):
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        get_conn.return_value = connection

        phrase_count = refresh_ai_keyword_cache_from_title(EXAMPLE_TITLE)

        expected_phrases = {
            "Adaptateur scanner OBD2 L17 Bluetooth sans fil",
            "Lecteur de codes OBD II",
            "Réinitialisation du voyant moteur",
            "Effacement du voyant moteur",
        }
        self.assertEqual(phrase_count, len(expected_phrases))
        connection.commit.assert_called_once()
        insert_statement, rows = cursor.executemany.call_args.args
        self.assertIn("ON CONFLICT (keyword)", insert_statement)
        self.assertEqual({row[0] for row in rows}, expected_phrases)

    @patch("core.db.DATABASE_URL", "postgresql://local-test")
    @patch("core.db.get_db_connection")
    def test_rebuild_replaces_cache_from_all_processed_titles(self, get_conn):
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchall.return_value = [
            (EXAMPLE_TITLE,),
            ("Second OBD2 scanner diagnostic tool",),
            (EXAMPLE_TITLE,),
        ]
        get_conn.return_value = connection

        phrase_count = rebuild_ai_keyword_cache_from_processed_titles()

        self.assertEqual(phrase_count, 5)
        self.assertEqual(connection.commit.call_count, 1)
        self.assertEqual(connection.rollback.call_count, 0)

        statements = [
            call.args[0]
            for call in cursor.execute.call_args_list
        ]
        self.assertEqual(
            cursor.execute.call_args_list[0].args,
            ("SELECT pg_advisory_xact_lock(%s)", (0x41494B4559434143,)),
        )
        select_statement = statements[1]
        self.assertIn("FROM statu", select_statement)
        self.assertIn("processing_status = 'processed'", select_statement)
        self.assertNotIn("48 hours", select_statement)
        self.assertEqual(statements[2], "DELETE FROM ai_keyword_cache_keywords")

        insert_statement, rows = cursor.executemany.call_args.args
        self.assertIn("CURRENT_TIMESTAMP + INTERVAL '48 hours'", insert_statement)
        inserted_phrases = {row[0] for row in rows}
        self.assertEqual(
            inserted_phrases,
            {
                "Adaptateur scanner OBD2 L17 Bluetooth sans fil",
                "Lecteur de codes OBD II",
                "Réinitialisation du voyant moteur",
                "Effacement du voyant moteur",
                "Second OBD2 scanner diagnostic tool",
            },
        )


if __name__ == "__main__":
    unittest.main()