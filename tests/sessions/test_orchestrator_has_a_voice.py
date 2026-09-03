"""
argus#579: the orchestrator must be quality-scoreable from the session that actually decides.

⛔ THE HOLE THIS CLOSES. The orchestrator's only `log_agent_message` lived in the premarket synthesis
call, and premarket has deliberately deferred entry to the open since 27 Jul (the IEX feed has no
premarket quotes, so research gates skip every candidate). So the agent routed every entry the fleet
made while `orchestrator_synthesis_completeness` had nothing to read. Last score: 3 Aug 2026.

⚠️ IT REPORTS, IT DOES NOT NARRATE. Intraday is a deterministic router, not an LLM synthesiser. The
criterion asks for "approved trades with entry price, position size and confidence, plus a clear
terminal_reason", which is exactly what this path decides, so honesty and scoreability agree here.
"""
import inspect

from sessions import intraday


# ⛔ THESE ARE THE SHAPES THE AGENTS ACTUALLY EMIT, AND THAT IS THE WHOLE POINT (argus#726).
#
# This file used to put entry_price, position_size and confidence on the VERDICT, which the risk
# agent has never returned — its contract is {ticker, verdict, reason} (agents/risk_agent.py) and
# every other verdict fixture in the suite says so. The fabricated shape made a green test out of a
# summary that omitted all three on all 128 production runs. It also had position_size as 12, a
# share count, where research emits $3,000 of notional.
#
# Price, size and confidence come from the RESEARCH proposal (agents/research_agent.py).
PROPOSALS = {"proposals": [
    {"ticker": "NVDA", "entry_price": 178.2, "position_size": 3000, "confidence": "HIGH"},
    {"ticker": "AVGO", "entry_price": 291.4, "position_size": 3000, "confidence": "MEDIUM"},
    {"ticker": "PANW", "entry_price": 402.9, "position_size": 3000, "confidence": "LOW"},
]}
VERDICTS = {"verdicts": [
    {"ticker": "NVDA", "verdict": "APPROVED"},
    {"ticker": "AVGO", "verdict": "REJECTED", "reason": "ATR above limit"},
    {"ticker": "PANW", "verdict": "REJECTED", "reason": "correlation with open position"},
]}
APPROVED = [v for v in VERDICTS["verdicts"] if v["verdict"] == "APPROVED"]


def test_the_intraday_orchestrator_emits_reasoning():
    """A decision log is an operational record. It is not a voice."""
    src = inspect.getsource(intraday)
    assert 'tracer.log_agent_message("orchestrator"' in src, (
        "the orchestrator must emit reasoning from the session that decides entries, "
        "or it cannot be quality scored at all"
    )


def test_it_answers_the_criterion_on_its_own_terms():
    """entry price, position size, confidence, and a terminal_reason."""
    say = intraday._entry_rationale(PROPOSALS, VERDICTS, APPROVED, 1, "intraday_entries_placed")
    assert "NVDA" in say
    assert "entry 178.2" in say and "size 3000" in say and "confidence HIGH" in say
    assert "intraday_entries_placed" in say


def test_it_reads_price_size_and_confidence_off_the_proposal():
    """
    ⛔ THE SHIPPED BUG, VERBATIM. Read from the verdict, all three are absent from every real
    session, and the summary says only "Approved: NVDA." — which is what the judge saw 128 times.
    """
    say = intraday._entry_rationale(PROPOSALS, VERDICTS, APPROVED, 1, "intraday_entries_placed")
    assert "Approved: NVDA, entry 178.2, size 3000, confidence HIGH." in say


def test_a_verdict_that_carries_a_field_overrides_the_proposal():
    """If risk ever starts returning its own sizing, its number is the one that ran."""
    verds = [{"ticker": "NVDA", "verdict": "APPROVED", "position_size": 1500}]
    say = intraday._entry_rationale(PROPOSALS, {"verdicts": verds}, verds, 1, "intraday_entries_placed")
    assert "size 1500" in say and "size 3000" not in say


def test_it_reconciles_its_own_counts():
    say = intraday._entry_rationale(PROPOSALS, VERDICTS, APPROVED, 1, "intraday_entries_placed")
    assert "proposed 3" in say and "approved 1 of 3" in say and "1 entered" in say


def test_it_names_the_gate_when_risk_said_yes_and_nothing_entered():
    """
    The distinction the 31 Jul fix exists for: blaming risk for what the order gate did cost two
    days of chasing the wrong agent. The reasoning must not reintroduce that confusion.
    """
    say = intraday._entry_rationale(PROPOSALS, VERDICTS, APPROVED, 0, "intraday_entry_gate_skipped")
    assert "the refusal was the gate rather than the risk assessment" in say


def test_it_does_not_invent_fields_the_run_did_not_produce():
    """
    A size or confidence absent from BOTH the proposal and the verdict is omitted, never guessed.
    Reporting a value the session did not produce is the dishonesty this whole path avoids, and it
    stays the behaviour now that there is a second place to look.
    """
    thin = [{"ticker": "NVDA", "verdict": "APPROVED"}]
    say = intraday._entry_rationale({"proposals": [{"ticker": "NVDA"}]},
                                    {"verdicts": thin}, thin, 1, "intraday_entries_placed")
    assert "Approved: NVDA." in say
    assert "size" not in say and "confidence" not in say
