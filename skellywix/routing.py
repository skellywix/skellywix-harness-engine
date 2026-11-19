"""Pure routing policy. The proposer receives training rows, never check labels."""
from collections import defaultdict
import re

WORD = re.compile(r'[a-z][a-z0-9-]{0,31}\Z', re.ASCII)
MAX_GUIDES = 128
MAX_CASES = 2048
MAX_TAGS = 8


def token(value):
    if not isinstance(value, str) or not WORD.fullmatch(value):
        raise ValueError('Use a lowercase task tag or guide ID, up to 32 letters, digits or hyphens.')
    return value


def tags(values):
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= MAX_TAGS:
        raise ValueError('Supply one to eight task tags, not prompts or logs.')
    return sorted({token(v) for v in values})


def guide(value):
    if not isinstance(value, dict) or value.keys() != {'id', 'title', 'tags', 'advice'}:
        raise ValueError('A guide needs exactly id, title, tags and advice.')
    key = token(value['id'])
    if key == 'none':
        raise ValueError('The guide ID none is reserved for a checked abstention.')
    for field, limit in [('title', 96), ('advice', 1200)]:
        text = value[field]
        if not isinstance(text, str) or not text.strip() or len(text) > limit or not text.isprintable():
            raise ValueError(f'{field} must be a nonempty line of printable text, up to {limit} characters.')
    return {**value, 'id': key, 'tags': tags(value['tags'])}


def index(guides, aliases):
    routes = {key: {target} for key, target in aliases.items()}
    for item in guides.values():
        for key in item['tags']:
            routes.setdefault(key, set()).add(item['id'])
    return routes


def match(routes, query):
    scores = defaultdict(int)
    for key in query:
        for target in routes.get(key, ()):
            scores[target] += 1
    if not scores:
        return None
    best = max(scores.values())
    winners = [target for target, count in scores.items() if count == best]
    return winners[0] if len(winners) == 1 else None


def propose(guides, training):
    native = index(guides, {})
    owners = defaultdict(set)
    for words, expected in training:
        for key in words:
            if key not in native:
                owners[key].add(expected)
    return {key: next(iter(ids)) for key, ids in owners.items() if len(ids) == 1}


def grade(routes, checks):
    return [match(routes, words) == expected for words, expected in checks]
