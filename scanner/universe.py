from __future__ import annotations

# Curated universe of ~120 liquid S&P 500 stocks for Strategy C.
# Criteria: avg volume > 2M, price $10-$500, clear sector classification.
# Reviewed 2026-05-27.

UNIVERSE: list[tuple[str, str]] = [
    # Technology
    ("AAPL",  "Technology"), ("MSFT",  "Technology"), ("NVDA",  "Technology"),
    ("AMD",   "Technology"), ("INTC",  "Technology"), ("QCOM",  "Technology"),
    ("AVGO",  "Technology"), ("TXN",   "Technology"), ("MU",    "Technology"),
    ("AMAT",  "Technology"), ("LRCX",  "Technology"), ("KLAC",  "Technology"),
    ("ADI",   "Technology"), ("MRVL",  "Technology"), ("SWKS",  "Technology"),
    ("CRM",   "Technology"), ("NOW",   "Technology"), ("SNOW",  "Technology"),
    ("PLTR",  "Technology"), ("CRWD",  "Technology"), ("ZS",    "Technology"),
    ("PANW",  "Technology"), ("FTNT",  "Technology"), ("NET",   "Technology"),
    ("DDOG",  "Technology"), ("MDB",   "Technology"), ("TTD",   "Technology"),

    # Consumer Discretionary
    ("AMZN",  "Consumer Discretionary"), ("TSLA",  "Consumer Discretionary"),
    ("HD",    "Consumer Discretionary"), ("LOW",   "Consumer Discretionary"),
    ("NKE",   "Consumer Discretionary"), ("TGT",   "Consumer Discretionary"),
    ("BKNG",  "Consumer Discretionary"), ("MAR",   "Consumer Discretionary"),
    ("GM",    "Consumer Discretionary"), ("F",     "Consumer Discretionary"),
    ("RIVN",  "Consumer Discretionary"), ("UBER",  "Consumer Discretionary"),
    ("LYFT",  "Consumer Discretionary"), ("ABNB",  "Consumer Discretionary"),

    # Financials
    ("JPM",   "Financials"), ("BAC",   "Financials"), ("WFC",   "Financials"),
    ("GS",    "Financials"), ("MS",    "Financials"),  ("C",     "Financials"),
    ("AXP",   "Financials"), ("V",     "Financials"),  ("MA",    "Financials"),
    ("BLK",   "Financials"), ("SCHW",  "Financials"), ("COF",   "Financials"),
    ("MET",   "Financials"), ("PRU",   "Financials"), ("USB",   "Financials"),

    # Healthcare
    ("JNJ",   "Healthcare"), ("UNH",   "Healthcare"), ("ABBV",  "Healthcare"),
    ("LLY",   "Healthcare"), ("MRK",   "Healthcare"), ("PFE",   "Healthcare"),
    ("BMY",   "Healthcare"), ("AMGN",  "Healthcare"), ("GILD",  "Healthcare"),
    ("REGN",  "Healthcare"), ("VRTX",  "Healthcare"), ("ISRG",  "Healthcare"),
    ("TMO",   "Healthcare"), ("DHR",   "Healthcare"), ("BSX",   "Healthcare"),
    ("HUM",   "Healthcare"), ("CVS",   "Healthcare"), ("CI",    "Healthcare"),

    # Industrials
    ("CAT",   "Industrials"), ("BA",   "Industrials"), ("RTX",  "Industrials"),
    ("HON",   "Industrials"), ("UPS",  "Industrials"), ("FDX",  "Industrials"),
    ("GE",    "Industrials"), ("LMT",  "Industrials"), ("NOC",  "Industrials"),
    ("DE",    "Industrials"), ("MMM",  "Industrials"), ("EMR",  "Industrials"),
    ("ETN",   "Industrials"), ("PH",   "Industrials"),

    # Energy
    ("XOM",   "Energy"), ("CVX",   "Energy"), ("COP",   "Energy"),
    ("SLB",   "Energy"), ("OXY",   "Energy"), ("PSX",   "Energy"),
    ("MPC",   "Energy"), ("VLO",   "Energy"), ("HAL",   "Energy"),

    # Communication Services
    ("GOOGL", "Communication Services"), ("META",  "Communication Services"),
    ("NFLX",  "Communication Services"), ("DIS",   "Communication Services"),
    ("CMCSA", "Communication Services"), ("T",     "Communication Services"),
    ("VZ",    "Communication Services"), ("SNAP",  "Communication Services"),

    # Consumer Staples
    ("PG",    "Consumer Staples"), ("KO",    "Consumer Staples"),
    ("PEP",   "Consumer Staples"), ("WMT",   "Consumer Staples"),
    ("COST",  "Consumer Staples"), ("PM",    "Consumer Staples"),
    ("MO",    "Consumer Staples"), ("CL",    "Consumer Staples"),

    # Materials
    ("LIN",   "Materials"), ("APD",   "Materials"), ("FCX",   "Materials"),
    ("NEM",   "Materials"), ("NUE",   "Materials"), ("ALB",   "Materials"),

    # Real Estate & Utilities (smaller allocation — lower momentum)
    ("AMT",   "Real Estate"), ("PLD",  "Real Estate"),
    ("NEE",   "Utilities"),   ("DUK",  "Utilities"),
]

SECTOR_MAP: dict[str, str] = {ticker: sector for ticker, sector in UNIVERSE}


def get_tickers() -> list[str]:
    return [t for t, _ in UNIVERSE]


def get_sector(ticker: str) -> str:
    return SECTOR_MAP.get(ticker, "Other")
