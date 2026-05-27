# Testing Strategy — Trading Agent C

Every file written has tests written alongside it. No file ships without passing tests.
This doc defines the full test architecture: what to test, how to mock it, test file layout,
and the TypeScript test setup for the News Analyst.

---

## Guiding Principles

1. **Test behavior, not implementation.** Test what goes in and what comes out, not how the agent thinks.
2. **Never mock the DB with in-memory fakes.** Use a test Supabase project (separate from dev/prod) with real queries.
3. **Mock the Anthropic API.** Claude responses are non-deterministic and expensive — test the parsing/validation layer with fixture JSON.
4. **Mock external data APIs.** yfinance, Alpaca, alternative.me — deterministic fixtures only.
5. **Fail fast.** If a fixture fails to parse, the test fails with a clear message, not a silent miss.
6. **One test file per source file.** `agents/market_agent.py` → `tests/agents/test_market_agent.py`.

---

## Test Layout

```
trading-agent-c/
├── tests/
│   ├── conftest.py                   — shared fixtures: db client, mock anthropic, test session_id
│   ├── fixtures/
│   │   ├── market_agent/
│   │   │   ├── response_go.json      — sample Claude response: GO decision
│   │   │   ├── response_caution.json — CAUTION decision
│   │   │   ├── response_skip.json    — SKIP decision
│   │   │   └── tool_outputs/
│   │   │       ├── get_vix.json
│   │   │       ├── get_futures.json
│   │   │       ├── get_fear_greed.json
│   │   │       └── get_sector_rotation.json
│   │   ├── research_agent/
│   │   │   ├── response_3_proposals.json
│   │   │   ├── response_0_proposals.json
│   │   │   ├── response_caution_day.json
│   │   │   └── tool_outputs/
│   │   │       ├── get_candidates.json
│   │   │       ├── get_news_clean.json
│   │   │       ├── get_news_blackout.json
│   │   │       ├── get_live_price.json
│   │   │       ├── get_intraday_signals.json
│   │   │       ├── get_atr.json
│   │   │       └── get_position_history.json
│   │   ├── risk_agent/
│   │   │   ├── response_all_approved.json
│   │   │   ├── response_partial_rejection.json
│   │   │   ├── response_loss_limit.json
│   │   │   ├── response_concentration_reject.json
│   │   │   └── tool_outputs/
│   │   │       ├── get_open_positions.json
│   │   │       ├── get_today_pnl.json
│   │   │       ├── get_buying_power.json
│   │   │       └── get_portfolio_exposure.json
│   │   ├── orchestrator/
│   │   │   ├── response_converged.json
│   │   │   ├── response_retry_needed.json
│   │   │   ├── response_structural_block.json
│   │   │   └── response_skip_propagated.json
│   │   ├── learning_agent/
│   │   │   ├── response_adjustment_made.json
│   │   │   ├── response_observation_only.json
│   │   │   └── tool_outputs/
│   │   │       ├── read_today_trades_3_wins.json
│   │   │       ├── read_today_trades_2_losses.json
│   │   │       ├── read_strategy_params.json
│   │   │       └── read_recent_learnings.json
│   │   └── news_analyst/
│   │       ├── output_all_neutral.json
│   │       ├── output_one_blackout.json
│   │       └── output_positive_signal.json
│   ├── agents/
│   │   ├── test_market_agent.py
│   │   ├── test_research_agent.py
│   │   ├── test_risk_agent.py
│   │   ├── test_learning_agent.py
│   │   └── test_news_analyst_integration.py  — tests subprocess call from Python
│   ├── sessions/
│   │   ├── test_premarket.py
│   │   ├── test_intraday.py
│   │   └── test_eod.py
│   ├── core/
│   │   ├── test_protection.py
│   │   ├── test_goals.py
│   │   └── test_params.py
│   ├── trace/
│   │   ├── test_logger.py
│   │   └── test_normalizer.py
│   └── tools/
│       ├── test_market_tools.py
│       ├── test_research_tools.py
│       ├── test_risk_tools.py
│       └── test_learning_tools.py
└── agents/ts/
    └── tests/
        ├── news_analyst.test.ts
        ├── otel_spans.test.ts
        └── fixtures/
            ├── ticker_news_aapl.json
            └── ticker_news_blackout.json
```

---

## Mock Strategy

### Mocking the Anthropic API (Python)

All agent tests use a fixture-driven mock. The mock intercepts `anthropic.messages.create`
and returns a pre-built response object matching the `anthropic.types.Message` structure.

```python
# tests/conftest.py

import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import anthropic

FIXTURES = Path(__file__).parent / "fixtures"

def load_fixture(path: str) -> dict:
    return json.loads((FIXTURES / path).read_text())

def make_mock_response(content_blocks: list, stop_reason: str = "end_turn") -> MagicMock:
    """Build a mock anthropic.Message with the given content blocks."""
    response = MagicMock(spec=anthropic.types.Message)
    response.stop_reason = stop_reason
    response.content = []
    response.usage = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50

    for block in content_blocks:
        if block["type"] == "text":
            b = MagicMock(spec=anthropic.types.TextBlock)
            b.type = "text"
            b.text = block["text"]
            response.content.append(b)
        elif block["type"] == "tool_use":
            b = MagicMock(spec=anthropic.types.ToolUseBlock)
            b.type = "tool_use"
            b.id = block["id"]
            b.name = block["name"]
            b.input = block["input"]
            response.content.append(b)
    return response

@pytest.fixture
def mock_anthropic():
    with patch("anthropic.Anthropic") as mock_client_class:
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        yield mock_client
```

### Mocking Tool Implementations

Each tool implementation is tested independently in `tests/tools/`. Agent tests mock
the tool dispatch layer, not the raw API calls.

```python
# tests/agents/test_market_agent.py

from unittest.mock import patch
from agents.market_agent import run_market_agent
from tests.conftest import load_fixture, make_mock_response

def test_market_agent_returns_go_decision(mock_anthropic, test_session_id, tracer):
    # Simulate: 4 tool calls then end_turn with GO verdict
    tool_call_response = make_mock_response([
        {"type": "tool_use", "id": "t1", "name": "get_vix",            "input": {}},
        {"type": "tool_use", "id": "t2", "name": "get_futures",        "input": {}},
        {"type": "tool_use", "id": "t3", "name": "get_fear_greed",     "input": {}},
        {"type": "tool_use", "id": "t4", "name": "get_sector_rotation","input": {}},
    ], stop_reason="tool_use")

    final_response = make_mock_response([
        {"type": "text", "text": json.dumps(load_fixture("market_agent/response_go.json"))}
    ], stop_reason="end_turn")

    mock_anthropic.messages.create.side_effect = [tool_call_response, final_response]

    with patch("agents.tools.market_tools.get_vix_impl",
               return_value=load_fixture("market_agent/tool_outputs/get_vix.json")):
        result = run_market_agent(test_session_id, tracer)

    assert result["decision"] == "GO"
    assert result["max_positions"] > 0
    assert "summary" in result
```

---

## Test Coverage by Module

### agents/test_market_agent.py

| Test | What it validates |
|---|---|
| `test_go_decision` | Returns `decision=GO`, `max_positions>0`, `summary` present |
| `test_caution_decision` | Returns `decision=CAUTION`, `max_positions` reduced by caution multiplier |
| `test_skip_decision` | Returns `decision=SKIP`, session driver should stop |
| `test_requires_all_4_tool_calls` | Fails validation if any tool not called |
| `test_vix_failure_falls_back` | `get_vix` fails → assumes VIX=25, still produces output |
| `test_futures_failure_falls_back` | `get_futures` fails → assumes 0.0% change |
| `test_parse_error_raises` | Malformed Claude JSON → raises `MarketAgentError` |
| `test_vix_scaling_table` | VIX=22 → max_positions=10, VIX=31 → max_positions=3 |

### agents/test_research_agent.py

| Test | What it validates |
|---|---|
| `test_produces_proposals` | Returns `proposals` list with required fields |
| `test_caution_day_requires_score_7` | CAUTION market → score<7 tickers dropped |
| `test_blackout_ticker_skipped` | `get_news` returns `blackout=true` → ticker dropped |
| `test_atr_too_wide_skipped` | `atr_pct>5.0` → ticker gets `skipped_atr` outcome |
| `test_price_moved_too_far` | Price diff >5% → ticker dropped with `skipped_price_moved` |
| `test_tool_call_cap_respected` | MAX_TOOL_CALLS=25 enforced — agent cannot exceed |
| `test_get_candidates_called_once` | Tool implementation errors on 2nd call |
| `test_retry_context_excludes_rejected` | Retry pass excludes previously rejected tickers |
| `test_zero_proposals_valid_response` | Empty proposals list is valid, not an error |

### agents/test_risk_agent.py

| Test | What it validates |
|---|---|
| `test_all_approved` | All constraints satisfied → all APPROVED |
| `test_loss_limit_rejects_all` | `limit_hit=true` → all proposals rejected |
| `test_duplicate_position_rejected` | Ticker already in open_positions → rejected |
| `test_position_count_ceiling` | `approved_so_far + positions_open >= 15` → rejected |
| `test_buying_power_insufficient` | Capital needed > buying_power → rejected |
| `test_sector_concentration` | Sector would exceed 35% → rejected |
| `test_sequential_capital_deduction` | 1st approved trade reduces buying_power for 2nd |
| `test_get_open_positions_failure` | Tool fails → all rejected with "risk check unavailable" |
| `test_get_buying_power_failure` | Tool fails → all rejected |
| `test_get_today_pnl_failure` | Tool fails → assume limit_hit=false, continue |
| `test_get_exposure_failure` | Tool fails → skip constraint 5, apply 1-4 only |
| `test_map_rejection_vocabulary` | All rejection reasons map to controlled outcome vocab |

### agents/test_learning_agent.py

| Test | What it validates |
|---|---|
| `test_observation_below_sample_threshold` | < 3 trades → observation only, no param change |
| `test_adjustment_applied_within_bounds` | 3+ trades, valid bounds → adjust_param called |
| `test_adjustment_rejected_out_of_bounds` | Proposed value > max_bound → not applied, requires_human_review=true |
| `test_cooldown_prevents_adjustment` | cooldown_until in future → observation only |
| `test_max_2_adjustments_per_day` | 3rd adjustment attempt → written as observation |
| `test_false_positive_raises_threshold` | Prior false_positive → requires 5+ trades |
| `test_goal_recommendation_written` | 5-session winning pattern → recommend_goal called |
| `test_no_trades_returns_early` | trades_count=0 → agent not invoked |
| `test_context_for_tomorrow_in_output` | `context_for_tomorrow` field present and non-empty |

### sessions/test_premarket.py

| Test | What it validates |
|---|---|
| `test_full_session_converged` | All agents succeed → trades list returned |
| `test_skip_propagated` | Market Agent SKIP → empty trades, terminal_reason=skip_propagated |
| `test_retry_triggered_and_succeeds` | First pass rejected → retry → approved on round 2 |
| `test_retry_not_triggered_on_structural` | Loss limit rejection → no retry |
| `test_time_limit_before_research` | Clock past 10:20 → terminal_reason=time_limit |
| `test_tool_cap_stops_session` | Counter >= 40 → terminal_reason=tool_cap |
| `test_market_parse_error` | Market Agent returns bad JSON → terminal_reason=market_parse_error |
| `test_synthesis_parse_error` | Synthesis returns bad JSON → terminal_reason=synthesis_parse_error |
| `test_session_row_written` | c_sessions row exists after session |
| `test_trace_rows_written` | c_traces has rows for each agent step |
| `test_c_strategy_params_loaded` | Session uses DB params, not hardcoded values |

### sessions/test_intraday.py

| Test | What it validates |
|---|---|
| `test_skips_on_non_trading_day` | Saturday → early exit, no DB writes |
| `test_skips_outside_window` | Time < 9:15 AM → early exit |
| `test_skips_when_suspended` | protection.suspended=true → exit |
| `test_records_bracket_exit` | Position closed in Alpaca → c_trades row written |
| `test_lock_in_mode_no_new_entries` | daily_pnl >= target → no research agent call |
| `test_pnl_floor_no_new_entries` | daily_pnl <= floor → no research agent call |
| `test_intraday_entries_disabled` | enable_intraday_entries=false → no research call |
| `test_past_entry_window_no_entries` | Time >= 13:00 → no entries |
| `test_no_capacity_no_entries` | open_positions >= max_positions → no entries |
| `test_intraday_research_higher_score` | Intraday pass requires min_score + bonus |

### sessions/test_eod.py

| Test | What it validates |
|---|---|
| `test_force_closes_open_positions` | Open positions → market sell orders placed |
| `test_reconcile_fills_missing_exits` | Position closed between polls → exit recorded |
| `test_protection_tier_1_triggered` | daily_pnl < -$500 → tier 1 event written |
| `test_protection_tier_4_triggered` | drawdown > 10% → suspend flag set |
| `test_goal_snapshot_written` | c_goal_snapshots row for today written |
| `test_learning_agent_called_if_trades` | trades_count > 0 → Learning Agent invoked |
| `test_learning_agent_skipped_if_no_trades` | trades_count = 0 → Learning Agent not invoked |
| `test_daily_performance_row_written` | c_daily_performance row exists after EOD |
| `test_daily_summary_alert_sent` | Alert function called with P&L summary |

### core/test_protection.py

| Test | What it validates |
|---|---|
| `test_tier1_daily_soft_floor` | pnl < -$200 → reduces max_new_entries |
| `test_tier2_daily_hard_stop` | pnl < -$500 → suspended=True for today |
| `test_tier3_rolling_3day` | 3-day sum < -$1000 → max_positions * 0.5 for 2 days |
| `test_tier4_drawdown_10pct` | drawdown > 10% → suspended 24h |
| `test_tier5_drawdown_20pct` | drawdown > 20% → suspended 7 days, human_unlock_required |
| `test_tier6_consecutive_losses` | 5 consecutive losing days → suspended |
| `test_protection_event_written` | All tiers write to c_protection_events |
| `test_learning_agent_cannot_modify` | adjust_param for loss_limit → rejected |

### core/test_goals.py

| Test | What it validates |
|---|---|
| `test_lock_in_triggers_at_target` | pnl = target → lock_in_mode=True |
| `test_lock_in_not_triggered_below` | pnl < target → lock_in_mode=False |
| `test_floor_gate_triggers` | pnl = floor → pnl_floor_hit=True |
| `test_goal_snapshot_update` | evaluate_goals writes c_goal_snapshots |
| `test_recommended_goal_pending` | recommend_goal → status=pending_approval |
| `test_no_goals_configured` | No rows in c_goals → goals return empty status, no lock-in |

### core/test_params.py

| Test | What it validates |
|---|---|
| `test_loads_from_db` | c_strategy_params row → correct value returned |
| `test_falls_back_to_default` | Missing row → default value used |
| `test_adjust_within_bounds` | New value in [min, max] → applied |
| `test_adjust_out_of_bounds` | New value > max_bound → rejected |
| `test_cooldown_blocks_adjustment` | cooldown_until in future → rejected |
| `test_cooldown_set_after_adjustment` | Successful adjust → cooldown_until set |

### trace/test_logger.py

| Test | What it validates |
|---|---|
| `test_tool_call_row_written` | log_tool_call → c_traces row with correct fields |
| `test_agent_message_row_written` | log_agent_message → c_traces row |
| `test_decision_row_written` | log_decision → c_traces row |
| `test_session_row_written` | log_session → c_sessions row |
| `test_span_hierarchy_correct` | tool call parent_span_id = agent span_id |
| `test_sequence_increments` | Sequence numbers increment per session |

### trace/test_normalizer.py

| Test | What it validates |
|---|---|
| `test_otel_span_to_c_traces` | OTel span JSON → c_traces row format |
| `test_otel_missing_attributes` | Partial OTel span → normalizer fills defaults |
| `test_structured_log_to_c_traces` | Log line → c_traces row |
| `test_trace_id_to_session_id` | OTel traceId maps to correct session_id |
| `test_unknown_format_rejected` | Unrecognized format → logged as error, not crash |

---

## TypeScript Tests (Jest)

```typescript
// agents/ts/tests/news_analyst.test.ts

import { runNewsAnalyst } from "../news_analyst";
import * as fs from "fs";

jest.mock("child_process", () => ({
  execSync: jest.fn(),
}));

const { execSync } = require("child_process");

describe("News Analyst", () => {
  beforeEach(() => jest.clearAllMocks());

  test("classifies neutral signal for non-news ticker", async () => {
    const fixture = JSON.parse(
      fs.readFileSync("tests/fixtures/news_analyst/ticker_news_aapl.json", "utf-8")
    );
    execSync.mockReturnValue(JSON.stringify(fixture));

    const output = await runNewsAnalyst({
      tickers: ["AAPL"],
      date: "2026-05-27",
      session_id: "test-session-id",
    });

    expect(output.news_signals[0].ticker).toBe("AAPL");
    expect(output.news_signals[0].blackout).toBe(false);
    expect(["positive", "negative", "neutral", "null"]).toContain(
      output.news_signals[0].signal
    );
  });

  test("sets blackout true for earnings announcement", async () => {
    const fixture = JSON.parse(
      fs.readFileSync("tests/fixtures/news_analyst/ticker_news_blackout.json", "utf-8")
    );
    execSync.mockReturnValue(JSON.stringify(fixture));

    const output = await runNewsAnalyst({
      tickers: ["CRWD"],
      date: "2026-05-27",
      session_id: "test-session-id",
    });

    expect(output.news_signals[0].blackout).toBe(true);
  });

  test("handles fetch error gracefully", async () => {
    execSync.mockImplementation(() => {
      throw new Error("yfinance fetch failed");
    });

    const output = await runNewsAnalyst({
      tickers: ["AAPL"],
      date: "2026-05-27",
      session_id: "test-session-id",
    });

    expect(output.news_signals[0].signal).toBe("null");
    expect(output.news_signals[0].blackout).toBe(false);
  });

  test("emits OTEL_SPAN lines to stdout for each tool call", async () => {
    const writeSpy = jest.spyOn(process.stdout, "write");
    execSync.mockReturnValue(JSON.stringify({ headlines: [], count: 0, error: null }));

    await runNewsAnalyst({
      tickers: ["AAPL"],
      date: "2026-05-27",
      session_id: "test-session-id",
    });

    const otelLines = (writeSpy.mock.calls as [string][])
      .map((args) => args[0])
      .filter((line) => line.startsWith("OTEL_SPAN:"));
    expect(otelLines.length).toBeGreaterThanOrEqual(1);
    const span = JSON.parse(otelLines[0].replace("OTEL_SPAN: ", ""));
    expect(span.attributes["agent.name"]).toBe("news_analyst");
    expect(span.attributes["agent.language"]).toBe("typescript");
  });

  test("all input tickers present in output", async () => {
    execSync.mockReturnValue(JSON.stringify({ headlines: [], count: 0, error: null }));

    const tickers = ["AAPL", "NVDA", "AMD"];
    const output = await runNewsAnalyst({
      tickers,
      date: "2026-05-27",
      session_id: "test-session-id",
    });

    const outputTickers = output.news_signals.map((s: { ticker: string }) => s.ticker);
    expect(outputTickers.sort()).toEqual(tickers.sort());
  });
});
```

---

## Fixture File Standards

Each fixture JSON must represent a realistic response — not a minimal stub.

For agent response fixtures:
- Include all required output fields
- Include realistic values (not 0/null everywhere)
- One fixture per distinct scenario (e.g., GO vs CAUTION vs SKIP)

For tool output fixtures:
- Match the exact schema in the agent design doc
- Include edge cases: empty lists, error fields, boundary values

Naming convention: `{fixture_dir}/{scenario_name}.json`
Scenarios: `response_go`, `response_all_approved`, `output_one_blackout` etc.

---

## Running Tests

```bash
# All Python tests
pytest tests/ -v

# Single module
pytest tests/agents/test_market_agent.py -v

# TypeScript tests (from agents/ts/)
cd agents/ts && npm test

# All tests (CI)
pytest tests/ && cd agents/ts && npm test
```

No tests skip, no tests are marked xfail by default. If a test is not ready, the
corresponding production code is not merged.

---

## CI Integration (.github/workflows/tests.yml)

```yaml
name: Tests
on: [push, pull_request]

jobs:
  python-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: pytest tests/ -v
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL_C_TEST }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY_C_TEST }}

  ts-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: cd agents/ts && npm ci
      - run: cd agents/ts && npm test
```

Tests use a dedicated `_TEST` Supabase project — separate from dev and prod.
All test DB writes are rolled back via `supabase.rpc("rollback_test_session")` in teardown,
or use a dedicated `c_traces_test` table that is truncated before each test run.
