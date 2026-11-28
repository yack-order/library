"""Keep archived preview labels consistent with the Catalog's Discover policy."""
import re

# Equivalent to REGEX_DISCOVER in src/domain/officiality.ts; preserve its full-card
# marketing exclusions rather than treating every use of "discover" as a preview.
DISCOVER_DESCRIPTION = re.compile(
    r'(\b(?:check out this|this is a|listen to (?:a|this)) preview\b|Discover(?!: The full card| ten| fascinating| the science)|(: A preview)|'
    r'(: The first)|(: Prologue)|(Chapter 1)|(Chapter one)|'
    r'[0-9]+ track[s]? Full Card: [0-9]+ tracks. Available: Until)', re.I)


def is_demo_archive(filename='', description='', title=''):
    return bool(re.search(r'\[DEMO\]', str(filename), re.I)
                or DISCOVER_DESCRIPTION.search(str(description or ''))
                or re.search(r'Discover:\s*\d+\s+tracks?', str(title or ''), re.I))
