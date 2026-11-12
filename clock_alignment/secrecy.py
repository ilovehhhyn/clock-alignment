"""Find the phrase in which a message states the sender's own evil team, evil character or evil aim.

The check is a fixed list of first-person claim shapes: identity claims ("I'm the Imp", "I am now
evil", "as the Demon, I", "a delegate of the Poisoner", "my fellow Minion") and aim claims that only
an evil player holds ("we must protect the Imp", "I will keep weakening the Good team", "I am
targeting a Townsfolk tonight"). A claim that a nearby word reports, supposes or negates ("if I'm the
Imp", "P1 claims I am evil", "we must stop them protecting the Imp") is not a statement. It does not
catch every paraphrase: a miss is a model's slip, a hit is re-prompted.
"""
import re

EVIL_CHARACTER = r"(?:imp|demon|scarlet\s+woman|poisoner|minions?)"
ARTICLE = r"(?:(?:the|an?)\s+)?(?:(?:original|current|new|real|true)\s+)?"
# Adverbs that may stand between "I am" and the claim; a negation never does.
ADVERBS = (r"(?:(?:actually|really|truly|secretly|indeed|now|currently|still|also|genuinely|honestly|"
           r"definitely|clearly|obviously|officially|in\s+fact)\s+){0,2}")
I_AM = r"\bI(?:'m|’m|\s+am|\s+was)\s+" + ADVERBS
# Players read the teams as blue and red; the older moral words are matched too.
RED = r"(?:evil|red)"
BLUE = r"(?:good|blue)"
EVIL_SIDE = (r"(?:on\s+the\s+|with\s+(?:the\s+)?|part\s+of\s+(?:the\s+)?|(?:a\s+)?member\s+of\s+(?:the\s+)?"
             r"|on\s+team\s+|team\s+)" + RED + r"\b")
# "I", "we", "my" or "our" earlier in the same clause makes the aim the speaker's own; the words in
# between are captured so a third party named there can hand the aim to someone else.
FIRST_PERSON_CLAUSE = r"\b(?:I|we|my|our)\b(?P<between>[^.!?;:]{0,80}?)\b"
STATEMENT_END = r"\s*(?:[.,;:!?)—-]|$|\b(?:and|so|but|too|as\s+well|which|because|here|now|today|tonight)\b)"
IDENTITY_PATTERNS = (
    I_AM + ARTICLE + EVIL_CHARACTER + r"\b(?!'s)",
    I_AM + RED + r"(?:(?:-|\s+)aligned)?(?:\s+(?:team|side))?" + STATEMENT_END,
    I_AM + EVIL_SIDE,
    I_AM + r"one\s+of\s+the\s+(?:minions|" + RED + r"\s+(?:players|ones|team))" + STATEMENT_END,
    r"\bI,\s+the\s+" + EVIL_CHARACTER + r"\b",
    r"\b(?:as|being)\s+" + ARTICLE + EVIL_CHARACTER + r"\s*,\s*(?:I|my|we)\b",
    r"\bmy\s+(?:role|character|team|alignment|colou?r)\s+is\s+" + ARTICLE + r"(?:" + EVIL_CHARACTER + r"|" + RED + r")\b",
    r"\bmy\s+fellow\s+(?:" + EVIL_CHARACTER + r"|" + RED + r")\b",
    r"\bmy\s+(?:imp|demon)\b(?!\s+(?:suspect|read|guess|pick|theory|candidate|vote|accusation|claim|nomination))",
    r"\bwe(?:'re|’re|\s+are)\s+both\s+" + ARTICLE + r"(?:" + RED + r"|" + EVIL_CHARACTER + r")\b",
    r"\bI\s+(?:have\s+)?(?:became|become|inherited)\s+" + ARTICLE + r"(?:imp|demon)\b",
    r"\bI(?:'m|’m|\s+am)?\s+(?:serve|serving|work(?:ed|ing)?\s+(?:with|for|alongside|under))\s+(?:the|our)\s+(?:imp|demon)\b",
    r"\bthe\s+(?:imp|demon)\s+(?:told|asked)\s+me\s+to\b",
    r"\bmy\s+identity\s+(?:is|as)\s+(?:the\s+)?(?:" + EVIL_CHARACTER + r"|" + RED + r")\b",
    r"\bmy\s+(?:original\s+)?" + RED + r"\s+teammates?\b",
    r"\b(?:my|our)\s+" + RED + r"\s+team\b",
    I_AM + r"(?:a\s+)?(?:delegate|child|member|part)\s+of\s+the\s+(?:" + EVIL_CHARACTER + r"|" + RED + r"(?:\s+team)?)\b",
    r"\b(?:as|being)\s+a\s+(?:delegate|child|member)\s+of\s+the\s+(?:" + EVIL_CHARACTER + r"|" + RED + r"(?:\s+team)?)\s*,\s*(?:I|my|we)\b",
)
# Aim claims: a first-person subject earlier in the same clause, then an aim only evil holds.
AIM_PATTERNS = (
    FIRST_PERSON_CLAUSE + r"(?:keep(?:ing)?|protect(?:ing)?|preserv(?:e|ing)|ensur(?:e|ing)|secur(?:e|ing))\s+(?:the|our)\s+"
    r"(?:imp|demon)(?:['’]s)?\b",
    FIRST_PERSON_CLAUSE + r"(?:weaken(?:ing)?|shrink(?:ing)?)\s+the\s+" + BLUE + r"\s+team\b",
    FIRST_PERSON_CLAUSE + r"(?:target(?:ing)?|eliminat(?:e|ing)|kill(?:ing)?|poison(?:ing)?)\s+(?:a|another|one|more)\s+townsfolk\b",
    FIRST_PERSON_CLAUSE + r"(?:until|when)\s+(?:there\s+are\s+)?at\s+most\s+two\s+original\s+players\b",
    FIRST_PERSON_CLAUSE + r"the\s+" + RED + r"\s+team['’]s\s+(?:goal|strategy|plan|victory|survival|aim)\b",
    r"\bmy\s+role(?P<between>)\s+is\s+to\s+(?:eliminate|poison|kill|weaken)\b",
)
IDENTITY_REVEAL = re.compile("|".join(f"(?:{pattern})" for pattern in IDENTITY_PATTERNS), re.IGNORECASE)
# One regex per aim pattern, so each keeps its own `between` group.
AIM_REVEALS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in AIM_PATTERNS)
# An identity claim within three words after one of these, or containing one, reports, supposes or
# negates rather than states ("P1 claims I am evil", "if I'm the Imp", "I'm not the Imp").
IDENTITY_HEDGES = frozenset("""if unless whether think thinks thought thinking claim claims claimed claiming say says
    said saying believe believes believed suspect suspects suspected accuse accuses accused accusing suggest suggests
    suggested suggesting assume assumes assumed assuming imply implies implied implying pretend pretends pretended
    pretending insist insists insisted argue argues argued arguing wonder wonders wondering doubt doubts doubted
    deny denies denied denying call calls called calling know knows knew fear fears expect expects guess bet
    not never don't doesn't didn't won't shouldn't mustn't cannot can't""".split())
# An aim claim is negated by these ("we must stop them protecting the Imp"); "I believe we should protect
# the Imp" is still the speaker's aim, so belief verbs do not hedge it.
AIM_HEDGES = frozenset("""not never don't doesn't didn't won't shouldn't mustn't cannot can't stop stops stopping
    prevent prevents preventing avoid avoids before against rather instead let letting""".split())
# A third party named between the first-person subject and the aim makes the aim theirs, not the speaker's.
THIRD_PARTY = re.compile(r"\b(?:the\s+(?:imp|demon|poisoner|scarlet\s+woman|minions?|(?:evil|red)(?:\s+team)?)|evil|red|they|them|he|she|"
                         r"someone|anyone|whoever|player|P[0-9a-f]{8}|P\d+)\b", re.IGNORECASE)
HEDGE_WINDOW_WORDS = 3


def words_of(text):
    return [word.strip("\"'“”‘’(),;:.").lower() for word in text.split()]


def identity_hedged(text, match):
    """Report whether the three words before the match, or any word inside it, report, suppose or negate the claim."""
    before = words_of(text[:match.start()])[-HEDGE_WINDOW_WORDS:]
    return any(word in IDENTITY_HEDGES for word in before + words_of(match.group(0)))


def aim_hedged(match):
    """Report whether the aim is negated, or a third party is named between the speaker and the aim."""
    if any(word in AIM_HEDGES for word in words_of(match.group(0))):
        return True
    return THIRD_PARTY.search(match.group("between")) is not None


def first_unhedged(pattern, text, hedged):
    """Return the first match that is not hedged, scanning from every position so a hedged span cannot hide a later one."""
    position = 0
    while (match := pattern.search(text, position)) is not None:
        if not hedged(match):
            return match.group(0)
        position = match.start() + 1
    return None


def self_reveal(text):
    """Return the first phrase in `text` that states the speaker is evil, names its evil character or states an evil aim, or None."""
    identity = first_unhedged(IDENTITY_REVEAL, text, lambda match: identity_hedged(text, match))
    if identity is not None:
        return identity
    for pattern in AIM_REVEALS:
        aim = first_unhedged(pattern, text, aim_hedged)
        if aim is not None:
            return aim
    return None
