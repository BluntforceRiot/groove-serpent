from __future__ import annotations

import math
import unittest

from groove_serpent.errors import ProjectValidationError
from groove_serpent.validation import strict_finite_number


class StrictNumericValidationTests(unittest.TestCase):
    def test_accepts_only_representable_finite_json_numbers(self) -> None:
        self.assertEqual(strict_finite_number(1, "Value"), 1.0)
        self.assertEqual(strict_finite_number(1.25, "Value"), 1.25)

        for invalid in (True, "1", None, math.nan, math.inf, -(math.inf), 10**400):
            with self.subTest(type=type(invalid).__name__):
                with self.assertRaisesRegex(
                    ProjectValidationError, "must be a finite JSON number"
                ):
                    strict_finite_number(invalid, "Value")


if __name__ == "__main__":
    unittest.main()
