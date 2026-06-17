from __future__ import annotations

# Curated universe of S&P 500 stocks for Strategy C.
# Criteria: avg volume > 2M, clear sector classification.
# No price cap — positions are dollar-sized, share price is irrelevant.
# Reviewed 2026-06-17.

UNIVERSE: list[tuple[str, str]] = [
    # Technology — semis, software, hardware
    ("AAPL",  "Technology"), ("MSFT",  "Technology"), ("NVDA",  "Technology"),
    ("AMD",   "Technology"), ("INTC",  "Technology"), ("QCOM",  "Technology"),
    ("AVGO",  "Technology"), ("TXN",   "Technology"), ("MU",    "Technology"),
    ("AMAT",  "Technology"), ("LRCX",  "Technology"), ("KLAC",  "Technology"),
    ("ADI",   "Technology"), ("MRVL",  "Technology"), ("SWKS",  "Technology"),
    ("STX",   "Technology"), ("WDC",   "Technology"), ("TER",   "Technology"),
    ("SNPS",  "Technology"), ("CDNS",  "Technology"),
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
    ("FISV",  "Financials"),

    # Healthcare
    ("JNJ",   "Healthcare"), ("UNH",   "Healthcare"), ("ABBV",  "Healthcare"),
    ("LLY",   "Healthcare"), ("MRK",   "Healthcare"), ("PFE",   "Healthcare"),
    ("BMY",   "Healthcare"), ("AMGN",  "Healthcare"), ("GILD",  "Healthcare"),
    ("REGN",  "Healthcare"), ("VRTX",  "Healthcare"), ("ISRG",  "Healthcare"),
    ("TMO",   "Healthcare"), ("DHR",   "Healthcare"), ("BSX",   "Healthcare"),
    ("HUM",   "Healthcare"), ("CVS",   "Healthcare"), ("CI",    "Healthcare"),

    # Industrials — added high-momentum industrials
    ("CAT",   "Industrials"), ("BA",   "Industrials"), ("RTX",  "Industrials"),
    ("HON",   "Industrials"), ("UPS",  "Industrials"), ("FDX",  "Industrials"),
    ("GE",    "Industrials"), ("LMT",  "Industrials"), ("NOC",  "Industrials"),
    ("DE",    "Industrials"), ("MMM",  "Industrials"), ("EMR",  "Industrials"),
    ("ETN",   "Industrials"), ("PH",   "Industrials"),
    ("GEV",   "Industrials"), ("VRT",  "Industrials"), ("CMI",  "Industrials"),

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

    # Real Estate & Utilities
    ("AMT",   "Real Estate"), ("PLD",  "Real Estate"),
    ("NEE",   "Utilities"),   ("DUK",  "Utilities"),
]

SECTOR_MAP: dict[str, str] = {ticker: sector for ticker, sector in UNIVERSE}

# Maps each sector to its primary ETF for sector momentum scoring.
# Technology maps to both XLK (broad tech) and SMH (semis) — use SMH for semi stocks.
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology":             "XLK",
    "Financials":             "XLF",
    "Healthcare":             "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Materials":              "XLB",
    "Industrials":            "XLI",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}

# Semiconductor stocks in our universe — use SMH instead of XLK for sector context.
SEMI_TICKERS = frozenset({
    "NVDA", "AMD", "INTC", "QCOM", "AVGO", "MU", "AMAT", "LRCX", "KLAC",
    "ADI", "MRVL", "SWKS", "TXN", "TER", "MCHP",
})


def get_tickers() -> list[str]:
    return [t for t, _ in UNIVERSE]


def get_sector(ticker: str) -> str:
    return SECTOR_MAP.get(ticker, "Other")


def get_sector_etf(ticker: str) -> str:
    """Return the sector ETF for a ticker. Semis use SMH instead of XLK."""
    if ticker in SEMI_TICKERS:
        return "SMH"
    sector = get_sector(ticker)
    return SECTOR_ETF_MAP.get(sector, "SPY")
