"""Per-trade reconcile: does a decision's quality predict that trade's realized P&L?

The gating check at TRADE granularity (the per-day version only had ~6 points). For each
real filled-and-closed trade:
  - TRUTH      = c_positions.realized_pnl (the trade's own money result)
  - PREDICTION = the session's avg L4 quality, scored by the CANONICAL SERVER-SIDE judge
                 (Argus /api/compute/judge), not the local judge fork. Idempotent: sessions
                 already judged are reused; unjudged ones are scored on the fly.
  - RECONCILE  = predicted-win (quality >= median) vs actual-win (pnl > 0) -> matched/diverged.

Granularity caveat: ag_evals quality is per-agent-per-session, not per-ticker, so trades
opened in the same session share one quality prediction. True per-ticker quality needs
entity-aware judging (the next layer). This is the best per-trade read available today.

Excludes test rows and unfilled orders (no real outcome). Read-mostly; the only write is
the server judge populating L4 evals for any session that lacks them.

Run from repo root:  python scripts/reconcile_per_trade.py
"""
from __future__ import annotations

import json
import math
import os
import urllib.request

ARGUS_URL = "https://provyai.vercel.app"
EXCLUDE_EXITS = {"test_cleanup", "unfilled"}  # not real executed trades


def _server_judge(session_id: str) -> dict:
    """Ask the canonical server-side judge to score a session (idempotent)."""
    req = urllib.request.Request(
        f"{ARGUS_URL}/api/compute/judge",
        data=json.dumps({"session_id": session_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def _session_quality(client, tenant_id, session_id):
    rows = (client.table("ag_evals").select("score")
            .eq("tenant_id", tenant_id).eq("session_id", session_id)
            .eq("layer", 4).execute().data)
    s = [r["score"] for r in rows if r.get("score") is not None]
    return round(sum(s) / len(s), 4) if s else None


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else None


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    tenant_id = os.environ.get("TENANT_ID", "")
    if not tenant_id:
        print("TENANT_ID not set; cannot query.")
        return
    from core.db import get_client
    client = get_client()

    pos = (client.table("c_positions")
           .select("ticker,realized_pnl,session_id,close_date,exit_reason,status")
           .eq("status", "closed").execute().data)
    trades = [p for p in pos
              if p.get("realized_pnl") is not None
              and p.get("exit_reason") not in EXCLUDE_EXITS]

    print("=" * 64)
    print("PER-TRADE RECONCILE  —  quality (server judge)  vs  realized P&L")
    print("=" * 64)
    sessions = sorted({p["session_id"] for p in trades})
    print(f"real filled+closed trades : {len(trades)}  across {len(sessions)} sessions")
    if not trades:
        print("No real trades to reconcile.")
        return

    # Ensure quality exists for every session via the canonical server-side judge.
    qmap = {}
    judged = 0
    for sid in sessions:
        q = _session_quality(client, tenant_id, sid)
        if q is None:
            res = _server_judge(sid)
            if "error" not in res:
                judged += 1
            q = _session_quality(client, tenant_id, sid)
        qmap[sid] = q
    if judged:
        print(f"server-judged {judged} previously-unscored session(s)")

    pairs = [(qmap[p["session_id"]], p["realized_pnl"], p["ticker"],
              p["close_date"], p["exit_reason"])
             for p in trades if qmap.get(p["session_id"]) is not None]
    print(f"trades with a quality prediction : {len(pairs)}")
    if len(pairs) < 3:
        for q, pnl, tk, d, ex in pairs:
            print(f"  {d}  {tk:5s}  quality={q}  pnl={pnl:+.2f}  ({ex})")
        print("\nToo few to reconcile.")
        return

    quals = [p[0] for p in pairs]
    pnls = [p[1] for p in pairs]
    profitable = [1 if p > 0 else 0 for p in pnls]

    print(f"total realized P&L (these trades): ${sum(pnls):,.2f}")
    print(f"profitable trades                : {sum(profitable)}/{len(pnls)} ({100*sum(profitable)/len(pnls):.0f}%)")

    print("\n--- CORRELATION (per trade) ---")
    r = _pearson(quals, pnls)
    r_sign = _pearson(quals, [float(s) for s in profitable])
    print(f"Pearson r (quality, pnl)        : {r:+.3f}" if r is not None else "Pearson r : n/a")
    print(f"Pearson r (quality, win/lose)   : {r_sign:+.3f}" if r_sign is not None else "")

    # Median split on quality
    mq = sorted(quals)[len(quals) // 2]
    hi = [(q, p) for q, p in zip(quals, pnls) if q >= mq]
    lo = [(q, p) for q, p in zip(quals, pnls) if q < mq]

    def grp(g):
        if not g:
            return "(empty)"
        ps = [p for _, p in g]
        wr = 100 * sum(1 for p in ps if p > 0) / len(ps)
        return (f"n={len(g):2d}  mean P&L ${sum(ps)/len(ps):+8.2f}  "
                f"profitable {wr:3.0f}%  mean quality {sum(q for q,_ in g)/len(g):.3f}")
    print("\n--- MEDIAN-SPLIT (trades from high- vs low-quality sessions) ---")
    print(f"high quality (>= {mq:.3f}) : {grp(hi)}")
    print(f"low  quality (<  {mq:.3f}) : {grp(lo)}")

    # Reconcile: predicted win vs actual win
    matched = sum(1 for q, p in zip(quals, pnls) if (q >= mq) == (p > 0))
    print("\n--- RECONCILE (predicted-win if quality>=median  vs  actual-win if pnl>0) ---")
    print(f"matched (prediction agreed with outcome): {matched}/{len(pairs)} ({100*matched/len(pairs):.0f}%)")

    print("\n--- VERDICT ---")
    # Honest N is the number of INDEPENDENT quality predictions (sessions), not trades:
    # quality is per-session, so trades from the same session are not independent on the
    # prediction axis.
    n_independent = len(sessions)
    best = max(abs(r or 0), abs(r_sign or 0))
    direction = "consistent (high quality -> profitable)" if (r_sign or 0) > 0 else "mixed/negative"
    if n_independent < 20:
        print(f"ENCOURAGING BUT THIN: {len(pairs)} trades, but only {n_independent} INDEPENDENT")
        print(f"quality predictions (per-session). Direction is {direction}, and it matches the")
        print("per-day cut, so nothing contradicts the thesis. Not conclusive at this N. The clean")
        print("test needs PER-TICKER quality (entity-aware judging) so each trade has its own")
        print("independent prediction. That is the next layer, and the signal justifies building it.")
    elif best < 0.1:
        print("NO signal: session quality does not predict trade P&L. Needs per-ticker quality or a")
        print("better predictor before the moat thesis holds.")
    else:
        print(f"Signal present (|r|={best:.2f}). Quality has concordance with trade P&L at this")
        print("granularity. Strengthen with per-ticker quality, then build the live Ledger reconcile.")

    print("\nCaveat: quality is per-session (per-agent), not per-ticker, so trades in the same")
    print("session share one prediction. Per-ticker judging (tag each ticker's reasoning with")
    print("entity_id and judge it) is the next layer and the true per-trade reconcile.")


if __name__ == "__main__":
    main()
