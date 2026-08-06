"""Hidden-position matching: wildcard rules, exact legs, and labels.

The blacklist is display-only, so the bar for these tests is that a rule hides
exactly what the user meant and nothing more — an over-broad match makes a live
position vanish from the Close and Roll tabs.
"""

from options_scanner import position_filters as pf


def _leg(ticker="UBER", opt="C", strike=120.0, exp="2026-06-18",
         qty=2, direction="short"):
    return {"underlying": ticker, "option_type": opt, "strike": strike,
            "expiration": exp, "quantity": qty, "direction": direction}


# ── matches ──────────────────────────────────────────────────────────────────

def test_ticker_only_rule_hides_every_leg_on_that_underlying():
    rule = {"ticker": "UBER"}
    assert pf.matches(rule, _leg())
    assert pf.matches(rule, _leg(opt="P", strike=90, exp="2027-01-15"))
    assert not pf.matches(rule, _leg(ticker="AMD"))


def test_exact_leg_rule_matches_only_that_leg():
    rule = {"ticker": "UBER", "option_type": "C", "strike": 120.0,
            "expiration": "2026-06-18"}
    assert pf.matches(rule, _leg())
    assert not pf.matches(rule, _leg(strike=125.0))
    assert not pf.matches(rule, _leg(exp="2026-07-17"))
    assert not pf.matches(rule, _leg(opt="P"))


def test_partial_rule_narrows_by_type_only():
    rule = {"ticker": "UBER", "option_type": "P"}
    assert pf.matches(rule, _leg(opt="P", strike=90))
    assert not pf.matches(rule, _leg(opt="C"))


def test_ticker_and_case_are_normalized():
    rule = {"ticker": " uber ", "option_type": "c"}
    assert pf.matches(rule, _leg(ticker="uber", opt="c"))


def test_strike_compared_with_float_tolerance():
    # Broker strikes round-trip through floats — 120.0 must still match a
    # 120.00000000001 coming back from JSON.
    rule = {"ticker": "UBER", "strike": 120.0}
    assert pf.matches(rule, _leg(strike=120.000000000001))
    assert not pf.matches(rule, _leg(strike=120.01))


def test_blank_ticker_never_becomes_a_wildcard():
    # The dangerous failure mode: a rule with no ticker hiding the whole account.
    for rule in ({}, {"ticker": ""}, {"ticker": None}, {"option_type": "C"}):
        assert not pf.matches(rule, _leg())


def test_unusable_values_do_not_match():
    assert not pf.matches({"ticker": "UBER", "strike": "abc"}, _leg())
    assert not pf.matches({"ticker": "UBER"}, {"underlying": None})
    assert not pf.matches("UBER", _leg())


# ── split_hidden / is_hidden ─────────────────────────────────────────────────

def test_split_hidden_partitions_and_preserves_order():
    legs = [_leg(ticker="AMD"), _leg(), _leg(ticker="WPC", opt="P")]
    visible, hidden = pf.split_hidden(legs, [{"ticker": "UBER"}])
    assert [l["underlying"] for l in visible] == ["AMD", "WPC"]
    assert [l["underlying"] for l in hidden] == ["UBER"]


def test_no_rules_hides_nothing():
    legs = [_leg(), _leg(ticker="AMD")]
    for rules in (None, []):
        visible, hidden = pf.split_hidden(legs, rules)
        assert visible == legs and hidden == []


def test_empty_legs_is_safe():
    assert pf.split_hidden([], [{"ticker": "UBER"}]) == ([], [])
    assert pf.split_hidden(None, None) == ([], [])


def test_overlapping_rules_hide_a_leg_once():
    legs = [_leg()]
    _, hidden = pf.split_hidden(
        legs, [{"ticker": "UBER"}, pf.rule_from_leg(_leg())])
    assert len(hidden) == 1


def test_is_hidden():
    assert pf.is_hidden(_leg(), [{"ticker": "UBER"}])
    assert not pf.is_hidden(_leg(), [{"ticker": "AMD"}])


# ── stale rules ──────────────────────────────────────────────────────────────

def test_unmatched_rules_flags_rules_for_closed_positions():
    legs = [_leg()]
    rules = [{"ticker": "UBER"}, {"ticker": "AMD", "strike": 200.0}]
    stale = pf.unmatched_rules(rules, legs)
    assert [r["ticker"] for r in stale] == ["AMD"]


def test_every_rule_is_stale_when_nothing_is_held():
    rules = [{"ticker": "UBER"}]
    assert pf.unmatched_rules(rules, []) == rules


# ── dialog rule classification ───────────────────────────────────────────────
# The Settings dialog reconciles its checkboxes into a single write, so any rule
# it does NOT render has to be reported as "carry" — otherwise opening the dialog
# would silently delete it.

def test_ticker_wide_detection():
    assert pf.is_ticker_wide({"ticker": "WPC"})
    assert pf.is_ticker_wide({"ticker": "WPC", "note": "elsewhere"})
    assert not pf.is_ticker_wide({"ticker": "WPC", "option_type": "P"})
    assert not pf.is_ticker_wide({"ticker": "WPC", "strike": 60.0})


def test_split_rules_for_ui_renders_ticker_wide_rules_for_held_tickers():
    # The dialog hides whole underlyings, so only ticker-wide rules get a tick.
    checkbox, carry = pf.split_rules_for_ui([{"ticker": "UBER"}], [_leg()])
    assert checkbox == [{"ticker": "UBER"}] and carry == []


def test_split_rules_for_ui_carries_leg_level_rules():
    # Leg-level rules have no checkbox of their own — they must be carried, not
    # reconciled away, whether or not the leg is still held.
    rules = [pf.rule_from_leg(_leg()), pf.rule_from_leg(_leg(strike=999.0))]
    checkbox, carry = pf.split_rules_for_ui(rules, [_leg()])
    assert checkbox == [] and carry == rules


def test_split_rules_for_ui_carries_rules_for_unheld_tickers():
    checkbox, carry = pf.split_rules_for_ui([{"ticker": "WPC"}], [_leg()])
    assert checkbox == [] and carry == [{"ticker": "WPC"}]


def test_split_rules_for_ui_carries_hand_edited_partial_rules():
    # A type-wide rule has no checkbox on the dialog, even for a held ticker.
    rules = [{"ticker": "UBER", "option_type": "C"}]
    checkbox, carry = pf.split_rules_for_ui(rules, [_leg()])
    assert checkbox == [] and carry == rules


def test_split_rules_for_ui_carries_everything_when_positions_unavailable():
    # Schwab unreachable → no legs → the dialog can render nothing, so every
    # rule must be carried rather than reconciled away.
    rules = [{"ticker": "UBER"}, {"ticker": "AMD"}]
    for legs in ([], None):
        checkbox, carry = pf.split_rules_for_ui(rules, legs)
        assert checkbox == [] and carry == rules


# ── keys and labels ──────────────────────────────────────────────────────────

def test_rule_key_is_stable_across_equivalent_spellings():
    a = pf.rule_key({"ticker": "uber", "option_type": "c", "strike": 120,
                     "expiration": "2026-06-18"})
    b = pf.rule_key({"ticker": "UBER", "option_type": "C", "strike": 120.0,
                     "expiration": "2026-06-18"})
    assert a == b


def test_rule_key_distinguishes_ticker_wide_from_exact_leg():
    assert (pf.rule_key({"ticker": "UBER"})
            != pf.rule_key(pf.rule_from_leg(_leg())))


def test_leg_key_matches_its_own_exact_rule():
    leg = _leg()
    assert pf.leg_key(leg) == pf.rule_key(pf.rule_from_leg(leg))


def test_rule_from_leg_round_trips_into_a_match():
    leg = _leg()
    rule = pf.rule_from_leg(leg, note="managed elsewhere")
    assert pf.matches(rule, leg)
    assert rule["note"] == "managed elsewhere"
    assert not pf.matches(rule, _leg(strike=125.0))


def test_rule_label_reads_as_what_it_covers():
    assert pf.rule_label({"ticker": "WPC"}) == "WPC — all legs"
    assert pf.rule_label(pf.rule_from_leg(_leg())) == "UBER 2026-06-18 $120 CALL"
    # A partial rule must not read like a single leg.
    assert "all matching legs" in pf.rule_label({"ticker": "UBER",
                                                 "option_type": "P"})


def test_labels_spell_the_right_out():
    # A bare "C"/"P" beside a strike is easy to misread when deciding what to
    # hide, so both labels use the whole word.
    assert pf.leg_label(_leg(opt="C")).split()[3] == "CALL"
    assert pf.leg_label(_leg(opt="P")).split()[3] == "PUT"
    assert pf.rule_label({"ticker": "UBER", "option_type": "P"}).count("PUT") == 1


def test_leg_label_includes_quantity_and_direction():
    assert pf.leg_label(_leg()) == "UBER 2026-06-18 $120 CALL ×2 short"
