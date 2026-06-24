"""Gating analysis: does trace-based L4 quality predict realized P&L?

Read-only. Pulls ag_outcomes (realized_pnl + snapshotted avg L4 quality per session)
for the trading-C tenant and measures whether the quality signal has any concordance
with the money result. This is the check that decides whether Outcome Assurance is a
moat or just a dashboard.

Run from repo root:  python scripts/analyze_quality_vs_pnl.py
"""
from __future__ import annotations

import os
import math


def _load_env() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    return os.environ.get("TENANT_ID", "")


def _live_quality(client, tenant_id, session_id):
    """Avg L4 (layer=4) eval score for a session, read live from ag_evals.

    Preferred over ag_outcomes.quality_score, which is a snapshot frozen at outcome-write
    time and is stale/None for any session scored later by a backfill.
    """
    rows = (
        client.table("ag_evals")
        .select("score")
        .eq("tenant_id", tenant_id)
        .eq("session_id", session_id)
        .eq("layer", 4)
        .execute()
        .data
    )
    scores = [r["score"] for r in rows if r.get("score") is not None]
    return round(sum(scores) / len(scores), 4) if scores else None


def _pull(client, tenant_id):
    """Return {session_id: {pnl, quality, win_rate, date}} for sessions with a realized_pnl row."""
    rows = (
        client.table("ag_outcomes")
        .select("session_id, metric_name, metric_value, quality_score, period_date")
        .eq("tenant_id", tenant_id)
        .in_("metric_name", ["realized_pnl", "win_rate", "trades_total"])
        .execute()
        .data
    )
    by_session: dict[str, dict] = {}
    for r in rows:
        sid = r["session_id"]
        s = by_session.setdefault(sid, {"pnl": None, "quality": None, "win_rate": None,
                                        "trades": None, "date": r.get("period_date")})
        name, val = r["metric_name"], r["metric_value"]
        if name == "realized_pnl":
            s["pnl"] = val
        elif name == "win_rate":
            s["win_rate"] = val
        elif name == "trades_total":
            s["trades"] = val
    # Quality read live from ag_evals (not the frozen ag_outcomes snapshot).
    for sid, s in by_session.items():
        s["quality"] = _live_quality(client, tenant_id, sid)
    return by_session


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _spearman(xs, ys):
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(vals):
            j = i
            while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    return _pearson(ranks(xs), ys and ranks(ys))


def main():
    tenant_id = _load_env()
    if not tenant_id:
        print("TENANT_ID not set in env; cannot query.")
        return
    from core.db import get_client
    client = get_client()

    by_session = _pull(client, tenant_id)
    pairs = [(s["quality"], s["pnl"], s["win_rate"], s["trades"], s["date"])
             for s in by_session.values()
             if s["pnl"] is not None and s["quality"] is not None]
    pairs.sort(key=lambda p: (p[4] or ""))

    print("=" * 64)
    print("QUALITY  vs  REALIZED P&L  —  trading-agent-C")
    print("=" * 64)
    print(f"sessions with realized_pnl row : {len(by_session)}")
    print(f"sessions with BOTH quality+pnl : {len(pairs)}")
    if len(pairs) < 3:
        print("\nNot enough paired data to correlate. Need the EOD outcome writer to have")
        print("run on more sessions (Phase 0 may have few EOD rows). Stopping.")
        # still show what we have
        for q, p, w, t, d in pairs:
            print(f"  {d}  quality={q}  pnl={p}  win_rate={w}  trades={t}")
        return

    quals = [p[0] for p in pairs]
    pnls = [p[1] for p in pairs]
    dates = [p[4] for p in pairs]

    profitable = [1 if p > 0 else 0 for p in pnls]
    total_pnl = sum(pnls)
    pct_profit = 100 * sum(profitable) / len(profitable)

    print(f"date range                     : {dates[0]}  ->  {dates[-1]}")
    print(f"total realized P&L             : ${total_pnl:,.2f}")
    print(f"profitable sessions            : {sum(profitable)}/{len(profitable)} ({pct_profit:.0f}%)")
    print(f"quality range                  : {min(quals):.3f} .. {max(quals):.3f}  (mean {sum(quals)/len(quals):.3f})")

    print("\n--- CORRELATION (does higher quality -> higher P&L?) ---")
    r = _pearson(quals, pnls)
    rho = _spearman(quals, pnls)
    r_sign = _pearson(quals, [float(s) for s in profitable])
    print(f"Pearson  r (quality, pnl)       : {r:+.3f}" if r is not None else "Pearson  r : n/a")
    print(f"Spearman rho (quality, pnl)     : {rho:+.3f}" if rho is not None else "Spearman rho : n/a")
    print(f"Pearson  r (quality, win/lose)  : {r_sign:+.3f}" if r_sign is not None else "")

    # Median split: do high-quality sessions actually do better?
    mq = sorted(quals)[len(quals) // 2]
    hi = [(q, p) for q, p in zip(quals, pnls) if q >= mq]
    lo = [(q, p) for q, p in zip(quals, pnls) if q < mq]
    def grp(g):
        if not g:
            return "  (empty)"
        ps = [p for _, p in g]
        wr = 100 * sum(1 for p in ps if p > 0) / len(ps)
        return f"n={len(g):2d}  mean P&L ${sum(ps)/len(ps):+8.2f}  profitable {wr:3.0f}%  mean quality {sum(q for q,_ in g)/len(g):.3f}"
    print("\n--- MEDIAN-SPLIT (high-quality vs low-quality sessions) ---")
    print(f"high quality (>= {mq:.3f}) : {grp(hi)}")
    print(f"low  quality (<  {mq:.3f}) : {grp(lo)}")

    # Discrimination the other way: do winners have higher quality than losers?
    win_q = [q for q, p in zip(quals, pnls) if p > 0]
    lose_q = [q for q, p in zip(quals, pnls) if p <= 0]
    if win_q and lose_q:
        print("\n--- DISCRIMINATION (quality of winners vs losers) ---")
        print(f"mean quality | profitable sessions : {sum(win_q)/len(win_q):.3f}  (n={len(win_q)})")
        print(f"mean quality | losing sessions     : {sum(lose_q)/len(lose_q):.3f}  (n={len(lose_q)})")
        print(f"separation                         : {sum(win_q)/len(win_q) - sum(lose_q)/len(lose_q):+.3f}")

    print("\n--- VERDICT ---")
    MIN_N = 20  # below this, any correlation is noise, not signal
    best = max([abs(x) for x in [r or 0, rho or 0, r_sign or 0]])
    if len(pairs) < MIN_N:
        print(f"INSUFFICIENT DATA. Only {len(pairs)} paired sessions (need ~{MIN_N}+ for any read).")
        print("The correlation numbers above are noise at this sample size. The gating question")
        print("CANNOT be answered yet. This is itself the finding: the data flywheel has barely")
        print("turned. To answer it, accumulate more EOD sessions, backfill quality onto the P&L")
        print("rows that are missing it, or switch to a per-trade test (entry decision -> that")
        print("trade's exit P&L) which yields far more data points than one-per-day.")
    elif best < 0.1:
        print("NO signal. Quality does not predict P&L on this data. Outcome Assurance would")
        print("be a dashboard, not a moat, unless a better prediction signal than avg L4 quality is found.")
    elif best < 0.3:
        print("WEAK signal. Some association but not strong. Worth a better predictor (per-decision,")
        print("not avg session quality) before betting the roadmap.")
    else:
        print("MEANINGFUL signal. Trace-based quality has real concordance with P&L. The moat thesis")
        print("has empirical support on this tenant. Proceed to build the reconcile/calibrate loop.")
    print("\nCaveat: realized_pnl is the day's portfolio P&L, which includes positions opened on")
    print("earlier days (attribution confound). A cleaner test links the entry-decision session to")
    print("that specific trade's exit P&L via a work-item key. This is the first-pass concordance check.")


if __name__ == "__main__":
    main()
