"""Keep copyable self-hosting examples valid for the first-run validator."""

import re
from pathlib import Path

from fiscalm.validate import iban_valid


def test_documented_example_ibans():
    text = (Path(__file__).parents[1] / "docs" / "self-hosting.md").read_text()
    examples = re.findall(r"RO[0-9]{2}TEST[0-9]+", text)
    assert examples
    assert all(iban_valid(value) for value in examples)
