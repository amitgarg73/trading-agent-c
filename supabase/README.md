# Supabase Setup — Trading Agent C

## Projects to create

| Project name | Purpose | SQL to run |
|---|---|---|
| `trading-agent-c` | Production | `schema.sql` then `seed.sql` |
| `trading-agent-c-test` | CI tests | `schema.sql` only |

Create both at supabase.com → New project. Leave the GitHub integration blank.

---

## Step 1 — Run schema.sql

In the Supabase dashboard for **trading-agent-c**:

1. Go to **SQL Editor**
2. Paste the contents of `schema.sql`
3. Click **Run**

Repeat for **trading-agent-c-test** (schema only — no seed).

---

## Step 2 — Run seed.sql (prod only)

In the SQL Editor for **trading-agent-c**:

1. Paste the contents of `seed.sql`
2. Click **Run**

This inserts the 8 strategy parameters, 11 agent config flags, and 2 starter goals.

---

## Step 3 — Get your API keys

For each project go to **Settings → API**:

- **Project URL** → `SUPABASE_URL_C` / `SUPABASE_URL_C_TEST`
- **service_role** key (under Project API keys) → `SUPABASE_KEY_C` / `SUPABASE_KEY_C_TEST`

Use `service_role`, not `anon` — the agent needs insert/update/delete permissions.

---

## Step 4 — Add to GitHub Secrets

In the trading-agent-c GitHub repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `SUPABASE_URL_C` | Project URL from trading-agent-c |
| `SUPABASE_KEY_C` | service_role key from trading-agent-c |
| `SUPABASE_URL_C_TEST` | Project URL from trading-agent-c-test |
| `SUPABASE_KEY_C_TEST` | service_role key from trading-agent-c-test |

---

## Step 5 — Add to core/db.py env

The `core/db.py` module reads `SUPABASE_URL` and `SUPABASE_KEY` from the environment.
For local dev, create a `.env` file (already in `.gitignore`):

```
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=your-service-role-key
```

---

## Phase 0 note: c_scan_results

The Research Agent calls `get_candidates()` which queries `c_scan_results` for today's
date. In Phase 0 there is no live scanner, so you need to seed this table manually
before running a premarket session.

Sample insert (run in SQL Editor on a trading day morning):

```sql
INSERT INTO c_scan_results (date, ticker, score, price, sector) VALUES
  (CURRENT_DATE, 'AAPL',  6, 185.40, 'Technology'),
  (CURRENT_DATE, 'NVDA',  7, 875.20, 'Technology'),
  (CURRENT_DATE, 'MSFT',  5, 415.60, 'Technology'),
  (CURRENT_DATE, 'TSLA',  6, 182.30, 'Consumer Discretionary'),
  (CURRENT_DATE, 'META',  7, 512.80, 'Communication Services'),
  (CURRENT_DATE, 'AMZN',  5, 185.10, 'Consumer Discretionary'),
  (CURRENT_DATE, 'GOOGL', 6, 172.40, 'Communication Services'),
  (CURRENT_DATE, 'AMD',   7, 156.90, 'Technology'),
  (CURRENT_DATE, 'CRM',   5, 285.50, 'Technology'),
  (CURRENT_DATE, 'CRWD',  6, 342.10, 'Technology');
```

A proper scanner feeding this table is a Phase 1 task.
