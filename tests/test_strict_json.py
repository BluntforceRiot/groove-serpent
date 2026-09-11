from __future__ import annotations

import unittest

from groove_serpent.strict_json import decode_strict_json


class StrictJsonTests(unittest.TestCase):
    def test_decodes_unambiguous_utf8_json(self) -> None:
        self.assertEqual(
            decode_strict_json(b'{"album":{"title":"Groove Serpent"},"tracks":[1,2]}'),
            {"album": {"title": "Groove Serpent"}, "tracks": [1, 2]},
        )

    def test_rejects_duplicate_fields_at_every_depth(self) -> None:
        for payload in (
            b'{"title":"first","title":"second"}',
            b'{"album":{"title":"first","title":"second"}}',
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                ValueError,
                "Duplicate JSON field",
            ):
                decode_strict_json(payload)

    def test_rejects_nonfinite_numbers(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity", b"1e9999", b"-1e9999"):
            with self.subTest(constant=constant), self.assertRaisesRegex(
                ValueError,
                "non-finite JSON number",
            ):
                decode_strict_json(b'{"confidence":' + constant + b"}")

    def test_rejects_non_utf8_input(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            decode_strict_json(b'{"title":"\xff"}')


if __name__ == "__main__":
    unittest.main()
