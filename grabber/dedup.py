"""Cross-source near-duplicate detection: normalized URLs + a pure-Python SimHash.

No embeddings endpoint or numeric libs are available, so similarity here is deterministic
and cheap: an exact match on a cleaned URL catches the same article reshared under a
different (source, item_id), and a 64-bit SimHash over the title+text catches same-language
reformats. Semantic near-dups that slip past both (e.g. the same story in RU vs EN) are left
for the one LLM clustering pass in the pipeline.
"""

import hashlib
import re
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .fetchers import Item

# simhashes closer than this in Hamming distance are treated as the same content.
# Calibrated on realistic title+body pairs: verbatim copies land at 0, a one-word edit or
# appended RSS footer at 4-8, a reshare with a different title + footer ~10, moderate
# same-story rewordings ~19, and genuinely distinct stories (even same-subtopic, shared
# vocabulary) ~29+. 12 catches near-identical reshares with a wide margin before distinct
# content; heavily reworded same-story posts are left for the LLM clustering pass.
HAMMING_THRESHOLD = 12
# below this many tokens a simhash is noise ("v4.2.0" vs "v4.3.0" would collide), so we
# fall back to URL-only matching for such short items
MIN_TOKENS_FOR_SIMHASH = 5

# query keys that never change which article a URL points to
_TRACKING_KEYS = {
    "fbclid", "gclid", "yclid", "msclkid", "dclid", "igshid",
    "ref", "ref_src", "ref_source", "referrer", "source", "src",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "spm", "at_medium", "at_campaign",
    "cmpid", "campaign_id", "trk", "trkCampaign",
}

_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+")

# tiny high-frequency stopword set (EN + RU) so common glue words don't dominate the hash
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "are", "was", "you", "your",
    "have", "has", "had", "not", "but", "from", "how", "why", "its", "into",
    "не", "что", "как", "это", "для", "или", "они", "она",
    "то", "об", "от", "по", "за", "из", "же", "бы", "уже", "все",
}


def normalize_url(url: str) -> str:
    """Cleaned URL for exact cross-source matching, or "" when there's nothing usable.

    Lowercases the host, drops www/fragment/default ports/trailing slash and tracking
    query params, and sorts the remaining query so the same article always maps to the
    same string. Best-effort: t.me reshares, Reddit-as-RSS and HN item pages point at
    themselves, so they won't cross-match here — the simhash and LLM pass cover those.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""  # relative / malformed — no stable identity

    host = parts.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    # keep a non-default port if present
    if parts.port and parts.port not in (80, 443):
        netloc = f"{host}:{parts.port}"

    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=False)
            if k.lower() not in _TRACKING_KEYS and not k.lower().startswith("utm_")
        )
    )

    path = parts.path.rstrip("/")
    return urlunsplit(("https", netloc, path, query, ""))


def tokenize(title: str, text: str) -> Counter:
    """Word unigrams + adjacent bigrams over title+text, lowercased, mixed RU/EN.

    Bigrams give the simhash more resolution so two unrelated items that merely share a
    vocabulary don't collapse together.
    """
    words = [w for w in _TOKEN_RE.findall(f"{title} {text}".lower()) if len(w) >= 3 and w not in _STOPWORDS]
    counts: Counter = Counter(words)
    for a, b in zip(words, words[1:]):
        counts[f"{a} {b}"] += 1
    return counts


def _stable_hash(token: str) -> int:
    """Process-stable 64-bit token hash. Must NOT be builtin hash() — PYTHONHASHSEED
    randomizes that per process, so persisted simhashes would never match on a later run."""
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def simhash(title: str, text: str) -> int:
    """64-bit weighted SimHash, or 0 when there's too little text to be a reliable signal.

    A 0 return is treated by hamming_close() as "no signal" and never matches anything.
    """
    counts = tokenize(title, text)
    if len(counts) < MIN_TOKENS_FOR_SIMHASH:
        return 0
    bits = [0] * 64
    for token, weight in counts.items():
        h = _stable_hash(token)
        for i in range(64):
            bits[i] += weight if (h >> i) & 1 else -weight
    result = 0
    for i in range(64):
        if bits[i] > 0:
            result |= 1 << i
    return result


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def hamming_close(a: int, b: int) -> bool:
    """True when two simhashes are near-duplicates. 0 means 'no signal' and never matches."""
    if not a or not b:
        return False
    return hamming(a, b) <= HAMMING_THRESHOLD


def content_key(item: Item) -> tuple[str, int]:
    return normalize_url(item.url), simhash(item.title, item.text)
