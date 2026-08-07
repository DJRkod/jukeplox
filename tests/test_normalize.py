"""Shared-contract vectors for app/normalize.py (2026-06-10 plan U1).

These input/expected pairs ARE the contract between the Python
implementation and the mirrored JS normalize() in static/browse/index.js
(verified via the harness — no JS test runner). Change semantics in either
implementation and these vectors must move in lockstep.
"""

from app.normalize import compile_rules, normalize, valid_rules

APOSTROPHES = ["'", "’"]
AMPERSAND = ["&", "and"]
E_DIACRITICS = ["e", "ë", "è", "é", "ê"]


def _n(s, rules):
    return normalize(s, compile_rules(rules))


def test_apostrophe_variants_equal():
    rules = [APOSTROPHES]
    assert _n("Don’t Stop", rules) == _n("don't stop", rules)


def test_ampersand_and_equal():
    rules = [AMPERSAND]
    assert _n("Belle & Sebastian", rules) == _n("belle and sebastian", rules)


def test_diacritics_normalize_to_plain_e():
    rules = [E_DIACRITICS]
    assert _n("Étienne", rules).startswith("e")
    assert _n("Étienne", rules) == _n("Etienne", rules)


def test_multi_occurrence_multiple_rules():
    rules = [AMPERSAND, E_DIACRITICS]
    assert _n("Tëst & Tèst", rules) == _n("test and test", rules)


def test_substring_semantics_documented():
    # Accepted, documented behavior: "and" inside "android" rewrites.
    rules = [AMPERSAND]
    assert _n("android", rules) == _n("&roid", rules)


def test_inert_rule_has_no_effect():
    # One filled + one empty field → inert (AE5): identical to no rules.
    rules = [["&", ""]]
    assert _n("Belle & Sebastian", rules) == "belle & sebastian"
    assert valid_rules(rules) == []


def test_whitespace_only_fields_dropped():
    assert valid_rules([["&", "   ", "and"]]) == [["&", "and"]]
    assert valid_rules([["   ", "x"]]) == []


def test_no_rules_is_identity_lowercase():
    assert normalize("Belle & Sebastian", compile_rules([])) == "belle & sebastian"
    assert normalize(None, compile_rules(None)) == ""


def test_longer_members_replace_first_within_rule():
    # Within one rule, the longer member must rewrite before a shorter
    # member that is its substring, or the longer form never matches.
    rules = [["x", "ab", "abc"]]
    assert _n("abc", rules) == "x"


def test_case_insensitive_members():
    rules = [["&", "AND"]]
    assert _n("Belle And Sebastian", rules) == _n("belle & sebastian", rules)


# ── query variants (Plex-facing expansion; plan U2) ──────────────────────────

from app.normalize import query_variants


def test_variants_original_first_and_substituted():
    vs = query_variants("belle and", [AMPERSAND])
    assert vs[0] == "belle and"
    assert "belle &" in vs


def test_variants_no_matching_rule_member_is_just_original():
    assert query_variants("zeppelin", [AMPERSAND]) == ["zeppelin"]


def test_variants_cap_bounds_explosion():
    vs = query_variants("e e e", [E_DIACRITICS], cap=8)
    assert len(vs) <= 8
    assert vs[0] == "e e e"


def test_variants_inert_rules_ignored():
    assert query_variants("belle and", [["and", ""]]) == ["belle and"]


def test_variants_case_insensitive_member_match():
    vs = query_variants("Belle AND Sebastian", [AMPERSAND])
    assert any("&" in v for v in vs)


# ── default rules + directional variant guard (2026-06-10 follow-up) ─────────

from app.normalize import DEFAULT_PATTERN_RULES


def test_default_rules_all_valid_and_ascii_canonical():
    """Every shipped default is a working rule whose canonical (first)
    member is the plain ASCII form."""
    assert valid_rules(DEFAULT_PATTERN_RULES) == DEFAULT_PATTERN_RULES
    for rule in DEFAULT_PATTERN_RULES:
        assert rule[0].isascii(), rule


def test_default_rules_cover_user_examples():
    rules = DEFAULT_PATTERN_RULES
    c = compile_rules(rules)
    assert normalize("Belle & Sebastian", c) == normalize("belle and sebastian", c)
    assert normalize("Don`t", c) == normalize("don't", c)  # backtick per request
    assert normalize("Beyoncé", c) == normalize("beyonce", c)
    assert normalize("Motörhead", c) == normalize("motorhead", c)
    assert normalize("Sigur Rós", c) == normalize("sigur ros", c)


def test_variants_never_replace_plain_ascii_letter():
    """Directional guard: 'etienne' must NOT explode into accented junk
    variants (every query contains common letters) — but the useful
    direction, accented → plain, still expands."""
    assert query_variants("etienne", DEFAULT_PATTERN_RULES) == ["etienne"]
    vs = query_variants("étienne", DEFAULT_PATTERN_RULES)
    assert "etienne" in vs


def test_default_ampersand_rule_does_not_mangle_names_containing_and():
    """The shipped &/and default is SPACED ([" & ", " and "]) so the
    word-level swap works without substring-rewriting names that merely
    contain "and" — 'Andrew Bird' must not become '&rew bird' (which
    bucketed him under '#' in the Browse rail)."""
    c = compile_rules(DEFAULT_PATTERN_RULES)
    assert normalize("Andrew Bird", c) == "andrew bird"
    assert normalize("Band of Horses", c) == "band of horses"
    assert normalize("Sandy Denny", c) == "sandy denny"


def test_variants_ampersand_still_expands_both_ways():
    vs = query_variants("belle and sebastian", DEFAULT_PATTERN_RULES)
    assert "belle & sebastian" in vs
    vs2 = query_variants("belle & sebastian", DEFAULT_PATTERN_RULES)
    assert "belle and sebastian" in vs2
