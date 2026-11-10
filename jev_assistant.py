"""
jev_assistant.py - turn a plain-language Bicing request (Catalan, Spanish or English) into
form values, using TypeSafe's Jev model in one system_one call.

Code lists the candidate phrases (every short run of words) and time expressions found in the
request; Jev only *chooses* among them, so it cannot invent a place or a time. Code then
normalises the choices: time -> 24 h clock, walking score -> metres.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

NONE = "none of these"
MAX_SPAN_WORDS = 6
MAX_SPAN_OPTIONS = 250          # Choice allows at most 255 options (one is NONE)
UNSURE_BELOW = 0.6              # Choice confidence under which a filled field is flagged
WALK_RADII_M = [200, 350, 500, 800, 1100]   # one per level of the walk Score

# Words that never start or end a place phrase (Catalan, Spanish, English).
_EDGE_WORDS = {
    # prepositions / conjunctions
    "de", "d'", "des", "del", "dels", "a", "al", "als", "fins", "per", "pel", "pels", "amb", "i",
    "o", "que", "en", "prop", "cap", "desde", "hasta", "para", "por", "con", "y", "cerca", "hacia",
    "from", "to", "at", "in", "on", "near", "by", "for", "with", "and", "or", "of", "towards",
    "around", "between",
    # time words
    "demà", "mañana", "tomorrow", "avui", "hoy", "today", "ara", "ahora", "now", "abans", "antes",
    "before", "després", "después", "after", "tarda", "tarde", "vespre", "nit", "noche", "matí",
    "morning", "evening", "night", "afternoon", "les", "las",
    # bikes, people, verbs
    "bici", "bicis", "bicicleta", "bicicletes", "bicicletas", "bike", "bikes", "e-bike", "ebike",
    "elèctrica", "eléctrica", "electric", "mecànica", "mecánica", "normal", "vull", "quiero",
    "want", "need", "necessito", "necesito", "anar", "ir", "go", "get", "take", "agafar", "coger",
    "deixar", "dejar", "leave", "return", "tornar", "volver", "llegar", "arribar", "arrive", "be",
    "i'd", "i'm", "em", "me", "my", "meu", "mi", "please", "si", "us", "plau", "favor",
}
_NOT_LAST = {"la", "el", "els", "los", "l'", "the", "un", "una", "an", "c/", "av.", "pg.", "pl."}

_TOKEN_RE = re.compile(r"[^\s,;:!?¿¡()\"]+")
_TIME_RE = re.compile(
    r"(?<![\d:.,/])(\d{1,2})(?:[:.h](\d{2}))?(?:\s*(am|pm|a\.m\.|p\.m\.|h))?(?![\d:])", re.I)


@dataclass
class JevFill:
    intent: str                        # plan_trip | find_nearby | other
    origin: str | None = None
    destination: str | None = None
    nearby_place: str | None = None
    nearby_need: str = "bikes"         # bikes | docks
    bike_type: str = "either"          # either | ebike | mechanical
    uphill: float = 0.0                # Noul probability
    walk_m: int = 500
    when: str = "now"                  # now | depart_at | arrive_by
    day: str = "today"                 # today | tomorrow
    time_hm: tuple[int, int] | None = None
    unsure: list[str] = field(default_factory=list)   # field ids: origin, destination, ...
    tokens: int = 0


# --- candidates (code) -----------------------------------------------------

def _tokens(text: str):
    """(word, start, end) with sentence-final dots removed, abbreviations like 'Av.' kept."""
    out = []
    for m in _TOKEN_RE.finditer(text):
        word, start, end = m.group(0), m.start(), m.end()
        while word.endswith(".") and len(word.rstrip(".")) > 3:
            word, end = word[:-1], end - 1
        if word:
            out.append((word, start, end))
    return out


def candidate_spans(text: str) -> list[str]:
    """Every run of 1..MAX_SPAN_WORDS words that could be a place, copied exactly from text."""
    toks = _tokens(text)
    spans = []
    for i in range(len(toks)):
        for j in range(i, min(i + MAX_SPAN_WORDS, len(toks))):
            first, last = toks[i][0].casefold(), toks[j][0].casefold()
            if first in _EDGE_WORDS or last in _EDGE_WORDS or last in _NOT_LAST:
                continue
            spans.append((j - i, toks[i][1], text[toks[i][1]: toks[j][2]]))
    if len(spans) > MAX_SPAN_OPTIONS:               # keep the shorter phrases
        spans = sorted(spans)[:MAX_SPAN_OPTIONS]
    seen, out = set(), []
    for _, _, s in sorted(spans, key=lambda x: (x[1], x[0])):   # document order
        if s not in seen and s != NONE:
            seen.add(s)
            out.append(s)
    return out


def candidate_times(text: str) -> dict[str, tuple[int, int, str | None]]:
    """{choice key: (hour, minute, suffix)}; the key shows the time with two words before it."""
    out = {}
    for m in _TIME_RE.finditer(text):
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        suffix = (m.group(3) or "").lower().replace(".", "").strip() or None
        if mi > 59 or h > 23 or (suffix in ("am", "pm") and not 1 <= h <= 12):
            continue
        before = text[: m.start()].split()[-2:]
        key = " ".join(before + [m.group(0).strip()])
        while key in out:
            key += " (again)"
        out[key] = (h, mi, suffix)
    return out


# --- questions (Jev) -------------------------------------------------------

_WALK_LEVELS = [
    "They must walk as little as possible: carrying luggage or shopping, mobility problems, or in a big hurry.",
    "They prefer a short walk.",
    "Nothing is said about walking.",
    "They are happy to walk a bit more to get a better station.",
    "They explicitly don't mind walking a long way.",
]


def build_questions(spans: list[str], times: dict) -> dict:
    from typesafe_sdk import Choice, Noul, Score

    q = {
        "intent": Choice(
            instructions="What does the person writing `request` want to do with Barcelona's Bicing bike-share?",
            criteria={
                "plan_trip": "Ride a bike from one place to another (a trip with a destination, with or without a named start).",
                "find_nearby": "See Bicing stations, available bikes or free docks around one single place, without describing a trip.",
                "other": "Something that is not about finding or using Bicing stations.",
            }),
        "bike_type": Choice(
            instructions="Which kind of Bicing bike does the person ask for in `request`?",
            criteria={
                "ebike": "An electric bike (e-bike, bici elèctrica, bici eléctrica).",
                "mechanical": "A normal, mechanical, non-electric bike.",
                "either": "No bike type is requested.",
            }),
        "nearby_need": Choice(
            instructions="If the person in `request` is looking for stations around a place, what do they need there?",
            criteria={
                "bikes": "To take a bike: available bikes.",
                "docks": "To leave, park or return a bike: free docks.",
            }),
        "uphill": Noul(instructions=(
            "The trip in `request` goes significantly uphill in Barcelona, for example towards "
            "Montjuïc, Park Güell, Tibidabo, Collserola, Vallcarca, Horta or the upper parts of "
            "Gràcia, Sarrià or Sant Gervasi.")),
        "walk": Score(
            instructions="How far is the person in `request` willing to walk between a bike station and their start or destination?",
            criteria=_WALK_LEVELS),
        "day": Choice(
            instructions="On which day is the trip in `request`? `now` is the current date and time in Barcelona.",
            criteria={"today": "Today, or no day is mentioned.", "tomorrow": "Tomorrow (demà, mañana)."}),
        "time_meaning": Choice(
            instructions="How does `request` use a clock time?",
            criteria={
                "depart_at": "The person wants to leave at that time.",
                "arrive_by": "The person needs to arrive at the destination by that time.",
                "no_time": "No clock time is given (they leave now).",
            }),
    }
    if spans:
        opts = {s: None for s in spans} | {NONE: "None of these phrases names that place."}
        q["origin"] = Choice(
            instructions=("Which phrase copied from `request` names the place where the bike trip STARTS "
                          "(where the rider picks up a bike)? If the start is not named, or is only "
                          "'here' or 'my location', choose the none option."),
            criteria=opts)
        q["destination"] = Choice(
            instructions=("Which phrase copied from `request` names the place where the bike trip ENDS "
                          "(where the rider wants to go)? If no destination is named, choose the none option."),
            criteria=opts)
        q["nearby_place"] = Choice(
            instructions=("Which phrase copied from `request` names the single place around which the "
                          "person wants to find Bicing stations? If none is named, choose the none option."),
            criteria=opts)
    if times:
        q["time"] = Choice(
            instructions=("Which time expression copied from `request` sets the timing of the bike trip: "
                          "either when the person wants to leave, or the time by which they need to arrive?"),
            criteria={k: None for k in times} | {NONE: "None of these is a departure or arrival time."})
        q["pm"] = Noul(instructions=(
            "The trip time in `request` is in the afternoon, evening or night (PM) rather than in the morning."))
    return q


# --- interpretation (code) -------------------------------------------------

def _walk_radius(score: float) -> int:
    s = max(0.0, min(float(len(WALK_RADII_M) - 1), score))
    i = int(s)
    if i >= len(WALK_RADII_M) - 1:
        return WALK_RADII_M[-1]
    v = WALK_RADII_M[i] + (s - i) * (WALK_RADII_M[i + 1] - WALK_RADII_M[i])
    return int(round(v / 50) * 50)


def _to_24h(h: int, suffix: str | None, pm_prob: float) -> int:
    if suffix == "pm" and h < 12:
        return h + 12
    if suffix == "am" and h == 12:
        return 0
    if suffix in (None, "h") and 1 <= h <= 11 and pm_prob > 0.5:   # 'a les 9 del vespre'
        return h + 12
    return h


def interpret(answers, times: dict) -> JevFill:
    unsure: list[str] = []

    def pick(qid: str):
        ans = answers.get(qid)
        if ans is None or ans.choice == NONE:
            return None
        if ans.confidence is not None and ans.confidence < UNSURE_BELOW:
            unsure.append(qid)
        return ans.choice

    fill = JevFill(intent=answers["intent"].choice, unsure=unsure)
    if answers["intent"].confidence is not None and answers["intent"].confidence < UNSURE_BELOW:
        unsure.append("intent")
    if fill.intent == "find_nearby":
        fill.nearby_place = pick("nearby_place")
        fill.nearby_need = answers["nearby_need"].choice
    else:
        fill.origin = pick("origin")
        fill.destination = pick("destination")
    fill.bike_type = pick("bike_type") or "either"
    fill.uphill = answers["uphill"].noul
    fill.walk_m = _walk_radius(answers["walk"].score)
    fill.day = answers["day"].choice
    time_key = pick("time") if times else None
    if time_key:
        h, mi, suffix = times[time_key]
        fill.time_hm = (_to_24h(h, suffix, answers["pm"].noul), mi)
        meaning = answers["time_meaning"].choice
        fill.when = meaning if meaning in ("depart_at", "arrive_by") else "depart_at"
    return fill


def parse(text: str, api_key: str, now: datetime | None = None) -> JevFill:
    """One Jev call; `now` should be Barcelona local time."""
    from typesafe_sdk import TypeSafeClient

    now = now or datetime.now()
    spans, times = candidate_spans(text), candidate_times(text)
    state = {"request": text, "city": "Barcelona (Bicing public bike-share)",
             "now": now.strftime("%A %d %B %Y, %H:%M")}
    with TypeSafeClient(api_key=api_key) as client:
        resp = client.system_one(state=state, questions=build_questions(spans, times))
    fill = interpret(resp.answers, times)
    usage = getattr(resp, "usage", None)
    fill.tokens = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    return fill
