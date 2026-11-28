"""Explicit preview evidence, in parity with src/domain/preview.ts."""
import re

FULL_OFFER = re.compile(r'\b(?:listen to|enjoy|discover)\s+(?:(?:the|this|a)\s+)?(?:full(?:[- ]length)?|complete|entire)\s+(?:card|recording|collection|book)\b|\bdiscover\s*:\s*(?:the\s+)?full\s+card\b|\bthis\s+is\s+(?:the\s+|a\s+)?full\s+card\b|(?:^|\n)\s*à découvrir\s*:\s*(?:(?:la\s+)?carte entière|tous les chapitres)\b', re.I)
WHOLE_PACK_CARD = re.compile(r'\b(?:one|1|a)\s+card\s+from\b|\b(?:one|1|a)\s+recipe\s+from\b[^\n]{0,160}\bpack\b', re.I)
PATTERNS = [
 ('preview_terms', r'\b(?:(?:a|this|another|free|splashy)\s+preview|preview\s+(?:of|from)|(?:ten-joke|free)\s+taster|free\s+sample|(?:a|an|this)\s+(?:audio\s+)?excerpt)\b'),
 ('chapter_offer', r'\b(?:listen to|enjoy|jump into|immerse yourself in|delve into|dive into)\b[^.!?\n]{0,160}\bfirst\s+chapter\b|\bfree\s+first\s+chapter\b|\bfirst\s+chapter\b[^.!?\n]{0,100}\bfor\s+free\b'),
 ('minutes_offer', r'\b(?:listen to|enjoy|hear)\b[^.!?\n]{0,100}\bfirst\s+(?:\d+|one|two|three|four|five|ten)\s+minutes?\b'),
 ('discover_partial_offer', r'(?:^|\n)\s*discover\s*:\s*(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:tracks?|minutes?|mins?)\b|track\s+\d+\b|[^\n]{0,100}\b(?:first|1st|prologue|one|half|sneak peek|sneak peak|[1-9]\d*\s+(?:full\s+)?(?:songs?|stor(?:y|ies)|activit(?:y|ies)|draw[- ]alongs?))\b)'),
 ('french_excerpt', r'(?:^|\n)\s*à découvrir\s*:\s*(?:(?:\d+(?:er)?|un|une|le premier)\s+(?:chapitres?|pistes?|histoires?|contes?|comptines?|mots?)\b|le prologue\b)'),
 ('standalone_excerpt', r'^\s*(?:chapter\s+(?:1|one)(?:\s*:[^\n]*)?|(?:introduction|(?:a\s+)?story)\s*:\s*prologue)\s*$'),
 ('partial_track_offer', r'\b[0-9]+ tracks?\s+Full Card:\s*[0-9]+ tracks?\.\s*Available:')
]
PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in PATTERNS]


def preview_evidence(value, track_count=None):
    description = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    if WHOLE_PACK_CARD.search(description):
        return {'kind': 'full', 'rule': 'whole_card_from_pack'}
    if FULL_OFFER.search(description):
        return {'kind': 'full', 'rule': 'explicit_full_offer'}
    english_offer = re.search(r'\bDiscover\s*:\s*(\d+)\s+(tracks?|chapters?|stories|songs?|minutes?|mins?)\b', description, re.I)
    english_total = re.search(r'\bFull Card\s*:\s*(\d+)\s+(tracks?|chapters?|stories|songs?|minutes?|mins?)\b', description, re.I)
    french_offer = re.search(r'(?:^|\n)\s*à découvrir\s*:\s*(\d+)\s+(pistes?|chapitres?|histoires?|contes?|comptines?|mots?)\b', description, re.I)
    french_total = re.search(r"\bCarte sur l[’']e-shop\s*:\s*(\d+)\s+(pistes?|chapitres?|histoires?|contes?|comptines?|mots?)\b", description, re.I)
    def unit(s):
        s = s.lower()
        if s == 'stories': return 'story'
        if re.fullmatch(r'mins?|minutes?', s): return 'minute'
        return s[:-1] if s.endswith('s') else s
    for offered, total in [(english_offer, english_total), (french_offer, french_total)]:
        if offered and total and unit(offered[2]) == unit(total[2]) and (int(offered[1]) >= int(total[1]) or track_count is not None and unit(total[2]) in ('track', 'piste') and track_count >= int(total[1])):
            return {'kind': 'full', 'rule': 'complete_track_count'}
    for name, pattern in PATTERNS:
        if pattern.search(description):
            return {'kind': 'preview', 'rule': name}
    return {'kind': 'none', 'rule': None}


def is_demo_archive(filename='', description='', title='', *, user_id=None, verified_source=False, track_count=None):
    """A verified archive's own JSON can correct a legacy label; an unbound
    current description must never relabel a historical archive.
    """
    creator = str(user_id or '').strip().lower()
    marker = bool(re.search(r'\[DEMO\]', str(filename), re.I))
    if creator and creator != 'yoto':
        return marker and not verified_source
    evidence = preview_evidence(description, track_count)
    if verified_source and (creator == 'yoto' or evidence['kind'] == 'full'):
        return evidence['kind'] == 'preview'
    return marker or evidence['kind'] == 'preview'


def is_withdrawal_notice(card):
    """Require explicit provider notice names; short audio alone is legitimate."""
    def notice(part):
        duration = part.get('duration')
        return bool(re.fullmatch(r'this (?:title|content) is no longer (?:free|available)(?:\.(?:m4a|mp3))?', str(part.get('title', '')).strip(), re.I)) and (duration is None or 0 < float(duration) <= 30)
    chapters = card.get('content', {}).get('chapters', [])
    return bool(chapters) and all(notice(ch) and ch.get('tracks') and all(notice(t) for t in ch['tracks']) for ch in chapters)
