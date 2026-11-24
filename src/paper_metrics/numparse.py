"""Number handling shared by the tools.

Abstracts write numbers in many styles: "1,204", "2 870" (thin space), "0·74"
(Lancet middle dot), "0.61–0.90" (en dash range), "−0.3" or "–0.3" (minus written
as U+2212 or an en dash), "P = .003" (no leading zero). These helpers turn text
into the signed numbers it contains, so the validator can check that every
number an agent extracted really appears in its quote - with the right sign.
"""

from __future__ import annotations

import re

SIGN = "\x00"  # internal marker for a minus that is certainly a sign (U+2212, or an en dash before a number)

_EN_DASH_RANGE = re.compile(r"(?<=[\d%])\s*–\s*(?=[−–-]?\.?\d)")   # "0.61–0.90", "83 – 92"
_EN_DASH_SIGN = re.compile(r"–(?=\.?\d)")                                    # "CI –3.0"
_THOUSANDS_SPACE = re.compile(r"(?<=\d)[   ](?=\d{3}\b)")
_THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}\b)")
# An optional sign, then a number with or without a leading zero. The lookbehind stops a match
# starting in the middle of another number ("0.94" must not also yield "94" or ".94").
_NUMBER = re.compile(r"(?<![\d.])([-\x00]?)(\d+(?:\.\d+)?|\.\d+)")
# An ASCII "-" right after one of these is a range separator or hyphen ("0.70-0.94", "COVID-19"), not a sign.
_NOT_A_SIGN_AFTER = re.compile(r"[\w)\]%]")


def normalise_text(text: str) -> str:
    """Normalise typography so numbers can be matched reliably.

    An en dash between two numbers is a range ("0.61–0.90" -> "0.61 to 0.90"); an en dash directly
    before a number elsewhere is a minus sign ("CI –3.0"), and so is U+2212.
    """
    text = text.replace("·", ".")                  # middle dot used as a decimal point
    text = _EN_DASH_RANGE.sub(" to ", text)
    text = _EN_DASH_SIGN.sub(SIGN, text)
    text = text.replace("−", SIGN)
    text = text.replace("–", " ").replace("—", " ")
    text = _THOUSANDS_SPACE.sub("", text)
    text = _THOUSANDS_COMMA.sub("", text)
    return text


def collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def is_sign_position(text: str, i: int) -> bool:
    """True if the ASCII '-' at text[i] (normalised text) is a minus sign, not a range dash or hyphen."""
    before = text[i - 1] if i > 0 else " "
    return not _NOT_A_SIGN_AFTER.match(before)


def numbers_in(text: str) -> list[float]:
    """Every number in the text, in order, with its sign.

    "0.70-0.94" -> [0.70, 0.94] (a range), "(95% CI -0.5 to -0.1)" -> [95, -0.5, -0.1],
    "CI –3.0" -> [-3.0], "P = .003" -> [0.003], "COVID-19" -> [19].
    """
    text = normalise_text(text)
    out = []
    for m in _NUMBER.finditer(text):
        sign, digits = m.groups()
        value = float(digits)
        if sign == SIGN or (sign == "-" and is_sign_position(text, m.start())):
            value = -value
        out.append(value)
    return out


def number_appears(value, text: str, tol: float = 1e-9, signed: bool = True) -> bool:
    """True if the value is written in the text (with the same sign, unless signed=False)."""
    if value is None:
        return True
    target = float(value)
    if signed:
        return any(abs(n - target) <= tol for n in numbers_in(text))
    return any(abs(abs(n) - abs(target)) <= tol for n in numbers_in(text))


def p_value_number(p) -> float | None:
    """'0.002' -> 0.002, '<0.001' -> 0.001, 'P = .03' -> 0.03."""
    if p is None:
        return None
    match = re.search(r"(\d*\.\d+|\d+)", normalise_text(str(p)))
    return float(match.group(1)) if match else None


def clean_p_value(p) -> str | None:
    """Store p-values one way: 'P=0.002' -> '0.002', 'p < .001' -> '<.001', '0·79' -> '0.79'."""
    if p is None:
        return None
    s = normalise_text(str(p)).strip()
    s = re.sub(r"^[Pp](?:\s*-?\s*value)?", "", s).replace(" ", "")
    s = s.replace("≤", "<=").replace("≥", ">=")
    s = s.lstrip("=").replace(SIGN, "-")
    return s or None
