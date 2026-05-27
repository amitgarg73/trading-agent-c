# CLAUDE.md — Trading Agent C (Agentic)

## Status: DESIGN COMPLETE — Implementation in progress

This project is a ground-up redesign of the trading agent as a genuine agentic
orchestration system. Strategy A and B are single-Claude-call pipelines. Strategy C
is multi-agent with tool use, agent-to-agent communication, and trace observability.

## Why This Exists

Strategy A and B use Claude for one decision per day (trade selection), surrounded
by deterministic Python agents (risk, sector, guardrails). That is a Claude-powered
pipeline, not agentic orchestration.

Strategy C inverts this: Claude agents control their own information gathering via
tools, specialized agents communicate through a shared context, and the Orchestrator
Agent (also Claude) coordinates the loop and makes the final call.

This also serves as the primary proof-of-concept for the AI Agent Reliability /
observability initiative — built from day one with full trace logging.

## Design Principles

1. Tool use over pre-packaging — agents decide what to look up, not the orchestrator
2. Specialization — each agent has one job and a minimal tool set
3. Hard loop termination — financial system; no unbounded agent loops
4. Trace-first — every tool call, agent message, and decision persisted from day one
5. Shadow before live — run alongside A for 2 weeks in shadow mode before paper capital

## Architecture

6 agents across 2 languages, 3 daily sessions, adaptive parameter system:

```
PREMARKET (7:15 AM ET):
  Market Agent (Python/Haiku) → News Analyst (TypeScript/Haiku) →
  Research Agent (Python/Sonnet) → Risk Agent (Python/Haiku) →
  Orchestrator synthesis (Python/Sonnet) → trade execution

INTRADAY (every 15 min, 9:15 AM - 3:50 PM ET):
  Position sync → goal gates → optional new entries (if enabled)

EOD (3:55 PM ET):
  Force-close → reconcile → performance → Learning Agent (Python/Sonnet) → alert
```

Multi-language is intentional: Python agents emit Custom JSON traces, TypeScript agents
emit OTel spans — proves the AI Agent Reliability product handles heterogeneous trace formats.

## Design Documents

All in design/. Key files:
- architecture.md — full system design with folder structure
- testing.md — test strategy, fixture approach, CI setup
- schema.md — all DB tables with DDL and seed data
- trace-formats.md — 3 trace formats + normalization spec
- agents/*.md — per-agent design docs (6 agents)
- sessions/*.md — intraday and EOD session design

## Infrastructure

- Separate Alpaca paper account (separate email — not shared with A/B)
- Separate Supabase project (3 environments: prod / dev / test)
- GitHub repo: trading-agent-c
- 3 GitHub Actions workflows + 1 test workflow
- Trading days: Mon-Fri (configurable via c_agent_config)
- Current phase: Phase 0 (simulation)

## Key Files

| File | Purpose |
|---|---|
| design/architecture.md | Full system design — agents, tools, message flow |
| design/agent-prompts.md | System prompt design for each agent |
| design/tool-schemas.md | Tool definitions and return schemas |
| design/trace-schema.md | Observability trace table design |
| design/open-questions.md | Unresolved design decisions |
| design/risks.md | Known risks and mitigations |

## Relationship to Other Projects

- Observability initiative: Strategy C is the primary POC — built trace-first
- Strategy A: Shadow comparison target — run C alongside A before going live
- Strategy B: Pool model stays separate; C uses A's universe approach

## What NOT to Do

- Do not start implementation until all design docs are complete and reviewed
- Do not share the Alpaca paper account with A/B — get a separate one
- Do not skip trace logging as "we'll add it later" — it is a core requirement
- Do not remove the hard loop termination limit for any reason
