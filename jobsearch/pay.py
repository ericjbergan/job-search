"""Pull a pay range out of free-form job description text.

Most careers sites have no salary field; when pay is posted at all it's a sentence
in the description ("The base pay range for this position is $87,069 - $124,385").
"""

import re

_AMOUNT = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*([kK])?"
_RANGE = re.compile(_AMOUNT + r"\s*(?:-|–|—|to)\s*(?:USD\s*)?" + _AMOUNT, re.I)
_SINGLE = re.compile(_AMOUNT, re.I)
_HOURLY = re.compile(r"\b(per hour|an hour|/\s?hr|/\s?hour|hourly)\b", re.I)
_YEARLY = re.compile(r"\b(per year|a year|/\s?yr|/\s?year|annually|annual|per annum|salary|yearly)\b", re.I)


def _value(whole, cents, k):
    v = float(whole.replace(",", "") + (f".{cents}" if cents else ""))
    return v * 1000 if k else v


def _period(after, low):
    if _HOURLY.search(after):
        return "HOUR"
    if _YEARLY.search(after):
        return "YEAR"
    return "HOUR" if low < 300 else "YEAR"


def _plausible(low, high, period):
    if high < low:
        return False
    if period == "HOUR":
        return 10 <= low <= 500 and high <= 500
    return 20_000 <= low <= 1_000_000 and high <= 1_500_000


def parse_pay(text):
    """Return {"min", "max", "period", "text"} for the first believable pay range, else None."""
    if not text:
        return None
    for m in _RANGE.finditer(text):
        low = _value(*m.group(1, 2, 3))
        high = _value(*m.group(4, 5, 6))
        if m.group(3) and not m.group(6):
            high *= 1000  # "$95K - 120" style is rare, but "$95 - $120K" means thousands on both
        elif m.group(6) and not m.group(3) and low < 1000:
            low *= 1000
        after = text[m.end():m.end() + 40]
        period = _period(after, low)
        if _plausible(low, high, period):
            return {"min": low, "max": high, "period": period, "text": _snippet(text, m.start(), m.end() + 25)}
    # A single figure only counts when it says what it's per ("$32.50 per hour").
    for m in _SINGLE.finditer(text):
        after = text[m.end():m.end() + 25]
        if not (_HOURLY.search(after) or _YEARLY.search(after)):
            continue
        value = _value(*m.group(1, 2, 3))
        period = _period(after, value)
        if _plausible(value, value, period):
            return {"min": value, "max": value, "period": period, "text": _snippet(text, m.start(), m.end() + 25)}
    return None


def _snippet(text, start, end):
    return " ".join(text[start:end].split()).rstrip(" .,;")


def format_pay(low, high, period):
    """'$112.8k – $180.2k /yr' or '$30.20 – $48.20 /hr'."""
    if not low and not high:
        return None

    def fmt(v):
        if period == "HOUR" or v < 1000:
            return f"${v:,.2f}"
        return f"${v / 1000:,.1f}k".replace(".0k", "k")

    rng = fmt(low) if not high or high == low else f"{fmt(low)} – {fmt(high)}"
    suffix = {"HOUR": " /hr", "YEAR": " /yr", "MONTH": " /mo"}.get(period or "", "")
    return rng + suffix
