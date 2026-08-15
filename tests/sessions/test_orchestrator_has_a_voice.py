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


PROPOSALS = {"proposals": [{"ticker": "NVDA"}, {"ticker": "AVGO"}, {"ticker": "PANW"}]}
VERDICTS = {"verdicts": [
    {"ticker": "NVDA", "verdict": "APPROVED", "entry_price": 178.2, "position_size": 12, "confidence": 0.81},
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
    assert "entry 178.2" in say and "size 12" in say and "confidence 0.81" in say
    assert "intraday_entries_placed" in say


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


def test_it_does_not_invent_fields_the_verdict_did_not_carry():
    """A missing size or confidence is omitted, never guessed."""
    thin = [{"ticker": "NVDA", "verdict": "APPROVED"}]
    say = intraday._entry_rationale({"proposals": [{"ticker": "NVDA"}]},
                                    {"verdicts": thin}, thin, 1, "intraday_entries_placed")
    assert "Approved: NVDA." in say
    assert "size" not in say and "confidence" not in say
