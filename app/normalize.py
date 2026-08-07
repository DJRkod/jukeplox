"""Admin-defined pattern-matching rules: validation and normalization.

2026-06-10 pattern-rules plan U1. A rule is a list of plain strings the
admin declares interchangeable. `normalize()` is the ONE Python semantic
for every server-side name comparison (identity merging, local search
matching, roster grouping); `static/browse/index.js` carries a mirrored JS
implementation for sorting/rail bucketing. The two are kept honest by the
shared test vectors in tests/test_normalize.py — change semantics here and
those vectors (and the JS twin) must move with you.

Semantics: lowercase the input, then for each VALID rule in saved order,
replace all (case-insensitive, substring-level) occurrences of every
non-canonical member with the rule's first surviving string (the
canonical), longer members first within a rule. Substring application is
deliberate and documented — a ["&", "and"] rule maps "android" → "&roid";
both sides of every comparison pass through the same function, so
matching stays consistent even when intermediate forms look odd.
"""

from __future__ import annotations

# Default rule set (2026-06-10 follow-up): shipped as DATA, not behavior —
# database.get_pattern_rules() returns a copy of these only while the
# setting has never been saved. The Setup editor shows them pre-populated;
# any Save (including an emptied list) persists the admin's choice and the
# defaults never come back on their own.
#
# Ordering matters for search variant expansion (cap-bounded): high-value
# substitutions first (&/and, apostrophes), diacritics last. The first
# string in each rule is the canonical form.
# The &/and default is SPACED (" & " / " and ") on purpose: rules apply at
# substring level, so an unspaced ["&", "and"] rewrites "Andrew Bird" to
# "&rew Bird" (rail-bucketed under '#'). The spaced form only swaps the
# word-level conjunction; admins who want the aggressive substring form can
# still create it themselves.
DEFAULT_PATTERN_RULES: list[list[str]] = [
    [" & ", " and "],
    ["'", "’", "`", "´"],          # ' ’ ` ´
    ['"', "“", "”"],                # " “ ”
    ["-", "–", "—"],                # - – —
    ["...", "…"],                        # ... …
    ["ae", "æ"],                         # ae æ
    ["oe", "œ"],                         # oe œ
    ["ss", "ß"],                         # ss ß
    ["a", "à", "á", "â", "ã", "ä", "å"],  # à á â ã ä å
    ["c", "ç"],                          # ç
    ["e", "è", "é", "ê", "ë"],                      # è é ê ë
    ["i", "ì", "í", "î", "ï"],                      # ì í î ï
    ["n", "ñ"],                          # ñ
    ["o", "ò", "ó", "ô", "õ", "ö", "ø"],  # ò ó ô õ ö ø
    ["u", "ù", "ú", "û", "ü"],                      # ù ú û ü
    ["y", "ý", "ÿ"],                # ý ÿ
]


def valid_rules(rules: list[list[str]] | None) -> list[list[str]]:
    """Filter to rules that take effect: ≥2 non-empty, non-whitespace
    members after dropping blank fields (R5). Member text is preserved as
    typed (no trimming — internal/edge spaces may be meaningful)."""
    out: list[list[str]] = []
    for rule in rules or []:
        members = [m for m in rule if isinstance(m, str) and m.strip()]
        if len(members) >= 2:
            out.append(members)
    return out


def compile_rules(rules: list[list[str]] | None) -> list[tuple[str, list[str]]]:
    """Precompile valid rules to (canonical_lower, [other members,
    longest-first, lowered]) so per-request normalization isn't re-deriving
    order. Lowercasing members here pairs with lowercasing the input in
    normalize() for case-insensitive replacement."""
    compiled = []
    for members in valid_rules(rules):
        canonical = members[0].lower()
        others = sorted((m.lower() for m in members[1:]), key=len, reverse=True)
        compiled.append((canonical, others))
    return compiled


def _replace_ci(s: str, needle_lower: str, replacement: str) -> str:
    """Replace all case-insensitive occurrences of needle_lower in s."""
    out = []
    i = 0
    sl = s.lower()
    n = len(needle_lower)
    while True:
        j = sl.find(needle_lower, i)
        if j < 0:
            out.append(s[i:])
            return "".join(out)
        out.append(s[i:j])
        out.append(replacement)
        i = j + n


def query_variants(q: str, rules: list[list[str]] | None, cap: int = 8) -> list[str]:
    """Capped query variants for search engines we cannot normalize (Plex
    does its own text matching — plan U2). For each valid rule member found
    in an existing variant, add the variant with that member swapped for
    each sibling member (replace-all per substitution). Original query is
    always first; the cap bounds Plex fan-out."""
    variants = [q]
    seen = {q.lower()}
    for members in valid_rules(rules):
        lowered = [m.lower() for m in members]
        for v in list(variants):
            vl = v.lower()
            for i, m1 in enumerate(lowered):
                if not m1 or m1 not in vl:
                    continue
                # Directional guard (default-rules follow-up): never use a
                # plain single ASCII letter as the REPLACED side — with the
                # default diacritic rules, every query containing "e" would
                # otherwise burn the cap on junk like "étiénné" and multiply
                # Plex calls. The useful direction (é→e) still expands.
                if len(m1) == 1 and m1.isascii() and m1.isalpha():
                    continue
                for j, m2 in enumerate(members):
                    if i == j:
                        continue
                    nv = _replace_ci(v, m1, m2)
                    if nv.lower() not in seen:
                        seen.add(nv.lower())
                        variants.append(nv)
                        if len(variants) >= cap:
                            return variants
    return variants


def normalize(s: str | None, compiled: list[tuple[str, list[str]]]) -> str:
    """Normalized comparison form of `s` under the compiled rules.

    Comparison-only — NEVER displayed (R4). With no rules this is plain
    lowercase, matching the pre-feature `.lower().strip()` behavior of the
    callers (callers keep their own strip())."""
    out = (s or "").lower()
    for canonical, others in compiled:
        for member in others:
            if member in out:
                out = out.replace(member, canonical)
    return out
