"""Distance-based "near home" checks for job locations.

Job locations come as free text in many shapes ("Columbus, OH", "Dublin, Ohio, United
States", "OH-Columbus-Easton", "USA-NY-New York, 1325 Ave of the Americas",
"Worcester, MA, USA; Remote"). parse_points() pulls out every US city/state it can find
and looks it up in the Census Bureau's place list (data/us_places.tsv, bundled, no
network). Distances are straight-line miles, which is close enough for "about an hour".
"""

import math
import os
import re
from functools import lru_cache

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "us_places.tsv")

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
_STATE_BY_NAME = {name.lower(): abbr for abbr, name in STATES.items()}

# Text that puts a job outside the US.
_FOREIGN = re.compile(
    r"\b(india|ind|uk|gbr|united kingdom|england|scotland|ireland|irl|canada|can|mexico|mex|philippines|phl|"
    r"poland|pol|germany|deu|france|fra|australia|aus|brazil|bra|costa rica|portugal|spain|singapore|japan|"
    r"china|chn|emea|apac|europe|latam|argentina|colombia|netherlands|romania|hungary|malaysia|israel|isr|"
    r"korea|egypt|egy|karnataka|telangana|maharashtra|tamil nadu|ontario|quebec|british columbia|alberta|"
    r"national capital region|distrito|international|munster|leinster|bogota|warsaw poland|poland)\b", re.I)


def _norm(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


@lru_cache(maxsize=1)
def _places():
    """{(state, normalized name): (lat, lon)} and {normalized name: [(state, lat, lon)]}."""
    by_state, by_name = {}, {}
    with open(DATA_PATH, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            state, name, lat, lon = line.rstrip("\n").split("\t")
            point = (float(lat), float(lon))
            # Consolidated cities are listed as "Nashville-Davidson" or "Louisville/Jefferson County";
            # job sites just say "Nashville", so index the short name too.
            for alias in {name, re.split(r"[-/]", name)[0]}:
                key = _norm(alias)
                if (state, key) in by_state:
                    continue
                by_state[(state, key)] = point
                by_name.setdefault(key, []).append((state, *point))
    return by_state, by_name


def lookup(city, state):
    """Coordinates for a city in a state (abbreviation or name), or None."""
    st = _state_abbr(state)
    if not st:
        return None
    key = _norm(city)
    key = re.sub(r"^(st|ste)\b", "saint", key) if key.startswith(("st ", "ste ")) else key
    by_state, _ = _places()
    return by_state.get((st, key)) or by_state.get((st, _norm(city)))


def _state_abbr(text):
    t = (text or "").strip()
    if t.upper() in STATES:
        return t.upper()
    return _STATE_BY_NAME.get(t.lower())


def miles(a, b):
    """Great-circle distance in miles between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


_STATE_TOKEN = r"(?:" + "|".join(sorted([re.escape(n) for n in STATES.values()] + list(STATES), key=len, reverse=True)) + r")"
# "Columbus, OH" / "Dublin, Ohio" / "New Albany OH"
_CITY_STATE = re.compile(r"([A-Za-z][A-Za-z .'-]{1,40}?)\s*,?\s+(" + _STATE_TOKEN + r")\b")
# "OH-Columbus-Easton" / "US-OH-Columbus" / "USA-NY-New York, ..."
_STATE_CITY = re.compile(r"(?:\bUSA?-)?\b(" + "|".join(STATES) + r")-([A-Za-z .'-]{2,40}?)(?:-|,|$)")


def parse_points(text):
    """All US (lat, lon) points named in a location string."""
    points = []
    if not text:
        return points
    # Some sites glue fields together: "Dublin, OHOH" -> "Dublin, OH".
    text = re.sub(r"\b([A-Z]{2})\1\b", r"\1", text)
    for segment in re.split(r"[;|\n]+", text):
        found = False
        for st, city in _STATE_CITY.findall(segment):
            p = lookup(city.strip(), st)
            if p:
                points.append(p)
                found = True
        if found:
            continue
        for m in _CITY_STATE.finditer(segment):
            city, st = m.group(1).strip(), m.group(2)
            # Try the last 1-3 words before the state, longest first ("New Albany", "Grove City").
            words = city.replace(",", " ").split()
            for n in (3, 2, 1):
                if len(words) >= n:
                    p = lookup(" ".join(words[-n:]), st)
                    if p:
                        points.append(p)
                        break
    return points


def city_only_points(text, home, radius):
    """For a bare city name with no state ("Columbus"), the matching places near home, if any."""
    _, by_name = _places()
    hits = by_name.get(_norm(text)) or []
    return [(lat, lon) for _, lat, lon in hits if miles(home, (lat, lon)) <= radius]


def is_foreign(text):
    return bool(_FOREIGN.search(text or ""))


def nearby_place_names(home, radius):
    """Normalized names of all places within the radius (used to pick careers-site location filters)."""
    by_state, _ = _places()
    return {name for (_, name), p in by_state.items() if miles(home, p) <= radius}


def classify(text, home, radius):
    """'near', 'far', or 'unknown' for a location string."""
    points = parse_points(text)
    if points:
        return "near" if any(miles(home, p) <= radius for p in points) else "far"
    if is_foreign(text):
        return "far"
    # "City, Region" where the region isn't a US state ("Cork, Munster", "Taguig, NCR"): outside the US.
    if text and "," in text and not re.search(r"\b" + _STATE_TOKEN + r"\b", text) \
            and not re.search(r"\b(united states|usa|us|remote|multiple|locations?)\b", text, re.I):
        return "far"
    # A bare city name ("Columbus") with no state: near if a place by that name is near home.
    if text and re.fullmatch(r"[A-Za-z .'-]{2,40}", text.strip()) and not _state_abbr(text.strip()):
        _, by_name = _places()
        if _norm(text) in by_name:
            return "near" if city_only_points(text, home, radius) else "far"
    return "unknown"
