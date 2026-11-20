"""Hard validators: IBAN mod-97 and Romanian amount-in-words parsing."""

from __future__ import annotations

import re

# IBAN lengths by country (ISO 13616 registry, SWIFT IBAN Registry): EU/EEA, Switzerland and the UK.
IBAN_LENGTHS = {
    "AT": 20,
    "BE": 16,
    "BG": 22,
    "CH": 21,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "EE": 20,
    "ES": 24,
    "FI": 18,
    "FR": 27,
    "GB": 22,
    "GR": 27,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IS": 26,
    "IT": 27,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MT": 31,
    "NL": 18,
    "NO": 15,
    "PL": 28,
    "PT": 25,
    "RO": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
}


def iban_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check, and the country's length where IBAN_LENGTHS knows it. Letters (incl. the literal X in
    treasury IBANs) count."""
    s = (iban or "").replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s):
        return False
    if s[:2] in IBAN_LENGTHS and len(s) != IBAN_LENGTHS[s[:2]]:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


# --- Romanian number words -------------------------------------------------
# OPs spell amounts concatenated, lowercase, no diacritics, suffix "lei":
#   nouazecisiunulei = 91, unamiesasesuteoptzecisiunulei = 1681.
# Parsing is greedy longest-token over a small vocabulary.

_UNITS = {
    "zero": 0,
    "unu": 1,
    "una": 1,
    "un": 1,
    "o": 1,
    "doua": 2,
    "doi": 2,
    "trei": 3,
    "patru": 4,
    "cinci": 5,
    "sase": 6,
    "sapte": 7,
    "opt": 8,
    "noua": 9,
}
_TEENS = {
    "zece": 10,
    "unsprezece": 11,
    "unusprezece": 11,
    "doisprezece": 12,
    "douasprezece": 12,
    "treisprezece": 13,
    "paisprezece": 14,
    "patrusprezece": 14,
    "cincisprezece": 15,
    "saisprezece": 16,
    "sasesprezece": 16,
    "saptesprezece": 17,
    "optsprezece": 18,
    "nouasprezece": 19,
}
_TENS = {
    "douazeci": 20,
    "treizeci": 30,
    "patruzeci": 40,
    "cincizeci": 50,
    "saizeci": 60,
    "sasezeci": 60,
    "saptezeci": 70,
    "optzeci": 80,
    "nouazeci": 90,
}
_MARKERS = {"suta": 100, "sute": 100, "mie": 1000, "mii": 1000, "si": 0, "de": 0}

_VOCAB: dict[str, tuple[str, int]] = {}
for _w, _v in _UNITS.items():
    _VOCAB[_w] = ("unit", _v)
for _w, _v in _TEENS.items():
    _VOCAB[_w] = ("teen", _v)
for _w, _v in _TENS.items():
    _VOCAB[_w] = ("tens", _v)
for _w, _v in _MARKERS.items():
    _VOCAB[_w] = (_w, _v)
_WORDS_BY_LEN = sorted(_VOCAB, key=len, reverse=True)


class WordsError(ValueError):
    pass


def _tokenize(s: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(s):
        for w in _WORDS_BY_LEN:
            if s.startswith(w, i):
                tokens.append(w)
                i += len(w)
                break
        else:
            raise WordsError(f"cannot parse amount words at {s[i:]!r} (in {s!r})")
    return tokens


def _parse_below_1000(tokens: list[str]) -> int:
    """Parse a group like 'sase sute opt zeci si unu' (already tokenized)."""
    if not tokens:
        return 0
    total = 0
    toks = [t for t in tokens if t != "de"]
    # hundreds
    if "suta" in toks or "sute" in toks:
        idx = toks.index("suta") if "suta" in toks else toks.index("sute")
        head = toks[:idx]
        if not head:
            mult = 1
        elif len(head) == 1 and _VOCAB[head[0]][0] == "unit":
            mult = _VOCAB[head[0]][1]
        else:
            raise WordsError(f"bad hundreds group {tokens}")
        total += mult * 100
        toks = toks[idx + 1 :]
    # tens / teens / units
    if toks and _VOCAB[toks[0]][0] == "teen":
        if len(toks) != 1:
            raise WordsError(f"trailing words after teen {tokens}")
        return total + _VOCAB[toks[0]][1]
    if toks and _VOCAB[toks[0]][0] == "tens":
        total += _VOCAB[toks[0]][1]
        toks = toks[1:]
        if toks:
            if toks[0] != "si" or len(toks) != 2 or _VOCAB[toks[1]][0] != "unit":
                raise WordsError(f"bad tens group {tokens}")
            total += _VOCAB[toks[1]][1]
        return total
    if toks:
        if len(toks) != 1 or _VOCAB[toks[0]][0] != "unit":
            raise WordsError(f"bad units group {tokens}")
        total += _VOCAB[toks[0]][1]
    return total


def ro_words_to_int(words: str) -> int:
    """Parse Romanian amount words (0..999,999) into an int.

    Accepts spaced or concatenated text, with or without a trailing 'lei'.
    Raises WordsError on anything it does not fully understand.
    """
    s = re.sub(r"[^a-z]", "", (words or "").lower())
    for suffix in ("lei", "leu"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    if not s:
        raise WordsError("empty amount words")
    tokens = _tokenize(s)
    if "mie" in tokens or "mii" in tokens:
        idx = tokens.index("mie") if "mie" in tokens else tokens.index("mii")
        thousands = _parse_below_1000(tokens[:idx]) if tokens[:idx] else 1
        if thousands == 0:
            raise WordsError(f"bad thousands group in {words!r}")
        rest = _parse_below_1000(tokens[idx + 1 :])
        return thousands * 1000 + rest
    return _parse_below_1000(tokens)
