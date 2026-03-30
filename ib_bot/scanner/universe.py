"""Universe definitions for the IB scanner.

Contains S&P 500 constituents, ETF universe, futures universe,
and sector mappings for the top stocks by market cap.
"""

SP500_SYMBOLS: list[str] = [
    # Information Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "CSCO", "ACN", "ADBE",
    "IBM", "INTU", "TXN", "QCOM", "AMAT", "NOW", "PANW", "ADI", "LRCX", "MU",
    "KLAC", "SNPS", "CDNS", "CRWD", "MCHP", "TEL", "FTNT", "NXPI", "ON", "HPQ",
    "KEYS", "CDW", "MPWR", "FSLR", "TYL", "ZBRA", "ANSS", "TER", "TRMB", "PTC",
    "SWKS", "NTAP", "GEN", "EPAM", "JNPR", "FFIV", "AKAM",
    # Health Care
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "TMO", "ABT", "DHR", "AMGN", "PFE",
    "ISRG", "ELV", "SYK", "BSX", "GILD", "VRTX", "MDT", "CI", "REGN", "BDX",
    "ZTS", "HCA", "MCK", "EW", "IDXX", "IQV", "A", "DXCM", "MTD", "BAX",
    "ALGN", "HOLX", "TECH", "RMD", "PODD", "RVTY", "COO", "WAT", "BIO", "HSIC",
    "VTRS", "CRL", "CTLT", "DVA", "XRAY", "INCY", "MOH",
    # Financials
    "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "SPGI", "BLK",
    "AXP", "MMC", "PGR", "CB", "SCHW", "CME", "ICE", "AON", "MCO", "MET",
    "AIG", "AFL", "TRV", "ALL", "AJG", "MSCI", "FIS", "NDAQ", "TROW", "CINF",
    "FITB", "STT", "HBAN", "RF", "CFG", "KEY", "NTRS", "SBNY", "MTB", "DFS",
    "WRB", "RE", "BRO", "GL", "L", "CBOE", "RJF",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "TJX", "BKNG", "MAR",
    "ORLY", "AZO", "ROST", "DHI", "LEN", "GM", "F", "YUM", "CMG", "HLT",
    "DKNG", "EBAY", "APTV", "BBY", "GRMN", "POOL", "DRI", "ULTA", "WYNN", "LVS",
    "MGM", "CZR", "EXPE", "RCL", "CCL", "NVR", "PHM", "TPR", "RL", "HAS",
    "BWA", "MHK", "WHR", "ETSY", "GNRC", "KMX", "GPC",
    # Communication Services
    "META", "GOOGL", "GOOG", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR",
    "EA", "TTWO", "WBD", "MTCH", "LYV", "FOXA", "FOX", "OMC", "IPG", "NWSA",
    "NWS", "PARA", "DISH",
    # Industrials
    "GE", "CAT", "UNP", "HON", "UPS", "RTX", "BA", "DE", "LMT", "ADP",
    "MMM", "GD", "ITW", "WM", "ETN", "EMR", "NOC", "FDX", "CSX", "NSC",
    "TT", "PH", "CTAS", "CARR", "PCAR", "FAST", "OTIS", "AME", "ROK", "VRSK",
    "IR", "XYL", "DOV", "GWW", "SWK", "IEX", "WAB", "CPRT", "EXPD", "DAL",
    "LUV", "UAL", "JBHT", "CHRW", "TDG", "HWM", "PWR", "RSG", "PAYC", "LDOS",
    "J", "AOS", "NDSN", "ALLE",
    # Consumer Staples
    "PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "MDLZ", "CL", "EL",
    "ADM", "GIS", "KMB", "SYY", "HSY", "KHC", "K", "MKC", "CHD", "STZ",
    "SJM", "CAG", "CLX", "TSN", "HRL", "CPB", "BG", "TAP", "LW", "KR",
    "WBA",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PXD", "PSX", "VLO", "OXY",
    "HES", "WMB", "KMI", "HAL", "DVN", "FANG", "BKR", "CTRA", "OKE", "TRGP",
    "MRO", "APA",
    # Utilities
    "NEE", "DUK", "SO", "D", "AEP", "SRE", "EXC", "XEL", "ED", "WEC",
    "AWK", "DTE", "ETR", "ES", "PPL", "FE", "CMS", "AEE", "EVRG", "ATO",
    "CNP", "NI", "PNW", "NRG", "LNT", "CEG",
    # Real Estate
    "PLD", "AMT", "CCI", "EQIX", "PSA", "SPG", "O", "DLR", "WELL", "VICI",
    "AVB", "EQR", "ARE", "MAA", "ESS", "UDR", "VTR", "HST", "PEAK", "CPT",
    "REG", "KIM", "BXP", "SLG", "FRT", "INVH", "IRM",
    # Materials
    "LIN", "APD", "SHW", "ECL", "FCX", "NUE", "VMC", "MLM", "DOW", "DD",
    "PPG", "NEM", "CTVA", "CF", "IFF", "CE", "ALB", "FMC", "EMN", "MOS",
    "PKG", "IP", "AVY", "SEE", "WRK", "BALL", "AMCR",
]

ETF_UNIVERSE: list[str] = [
    "SPY", "QQQ", "IWM", "DIA", "MDY", "VTI",
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLP", "XLU", "XLY", "XLB", "XLRE", "XLC",
    "TLT", "IEF", "AGG", "LQD", "HYG", "BIL",
    "GLD", "SLV", "USO", "VNQ",
    "EEM", "EFA", "VWO",
    "ARKK", "SOXX", "SMH", "XBI", "KRE", "XHB",
]

FUTURES_UNIVERSE: list[str] = [
    # Equity index futures
    "ES", "NQ", "MES", "MNQ", "YM",
    # Metals
    "GC", "SI",
    # Energy
    "CL", "NG",
    # Bonds
    "ZB", "ZN",
]

# GICS sector mapping for top 50+ stocks by market cap
STOCK_SECTORS: dict[str, str] = {
    # Information Technology
    "AAPL": "Information Technology",
    "MSFT": "Information Technology",
    "NVDA": "Information Technology",
    "AVGO": "Information Technology",
    "ORCL": "Information Technology",
    "CRM": "Information Technology",
    "AMD": "Information Technology",
    "CSCO": "Information Technology",
    "ACN": "Information Technology",
    "ADBE": "Information Technology",
    "IBM": "Information Technology",
    "INTU": "Information Technology",
    "TXN": "Information Technology",
    "QCOM": "Information Technology",
    "AMAT": "Information Technology",
    "NOW": "Information Technology",
    "PANW": "Information Technology",
    "ADI": "Information Technology",
    "LRCX": "Information Technology",
    "MU": "Information Technology",
    # Health Care
    "UNH": "Health Care",
    "LLY": "Health Care",
    "JNJ": "Health Care",
    "ABBV": "Health Care",
    "MRK": "Health Care",
    "TMO": "Health Care",
    "ABT": "Health Care",
    "DHR": "Health Care",
    "AMGN": "Health Care",
    "PFE": "Health Care",
    # Financials
    "BRK-B": "Financials",
    "JPM": "Financials",
    "V": "Financials",
    "MA": "Financials",
    "BAC": "Financials",
    "WFC": "Financials",
    "GS": "Financials",
    "MS": "Financials",
    "SPGI": "Financials",
    "BLK": "Financials",
    # Consumer Discretionary
    "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary",
    "LOW": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary",
    "TJX": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary",
    # Communication Services
    "META": "Communication Services",
    "GOOGL": "Communication Services",
    "GOOG": "Communication Services",
    "NFLX": "Communication Services",
    "DIS": "Communication Services",
    "CMCSA": "Communication Services",
    "TMUS": "Communication Services",
    "VZ": "Communication Services",
    "T": "Communication Services",
    # Industrials
    "GE": "Industrials",
    "CAT": "Industrials",
    "UNP": "Industrials",
    "HON": "Industrials",
    "UPS": "Industrials",
    "RTX": "Industrials",
    "BA": "Industrials",
    "DE": "Industrials",
    "LMT": "Industrials",
    # Consumer Staples
    "PG": "Consumer Staples",
    "KO": "Consumer Staples",
    "PEP": "Consumer Staples",
    "COST": "Consumer Staples",
    "WMT": "Consumer Staples",
    "PM": "Consumer Staples",
    # Energy
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "SLB": "Energy",
    "EOG": "Energy",
    # Utilities
    "NEE": "Utilities",
    "DUK": "Utilities",
    "SO": "Utilities",
    "D": "Utilities",
    # Real Estate
    "PLD": "Real Estate",
    "AMT": "Real Estate",
    "CCI": "Real Estate",
    "EQIX": "Real Estate",
    # Materials
    "LIN": "Materials",
    "APD": "Materials",
    "SHW": "Materials",
    "ECL": "Materials",
    "FCX": "Materials",
    "NUE": "Materials",
}

# Sector mapping for futures (used by correlation filter)
FUTURES_SECTORS: dict[str, str] = {
    # Equity Indices
    "ES": "Equity Indices",
    "NQ": "Equity Indices",
    "MES": "Equity Indices",
    "MNQ": "Equity Indices",
    "YM": "Equity Indices",
    # Metals
    "GC": "Metals",
    "SI": "Metals",
    # Energy
    "CL": "Energy Commodities",
    "NG": "Energy Commodities",
    # Bonds
    "ZB": "Bonds",
    "ZN": "Bonds",
}


def get_universe(asset_class: str) -> list[str]:
    """Return the list of symbols for a given asset class.

    Args:
        asset_class: One of "stocks", "etf", "futures", "all".

    Returns:
        List of ticker symbols.

    Raises:
        ValueError: If asset_class is not recognized.
    """
    match asset_class.lower():
        case "stocks":
            return SP500_SYMBOLS.copy()
        case "etf":
            return ETF_UNIVERSE.copy()
        case "futures":
            return FUTURES_UNIVERSE.copy()
        case "all":
            return SP500_SYMBOLS + ETF_UNIVERSE + FUTURES_UNIVERSE
        case _:
            raise ValueError(
                f"Unknown asset_class '{asset_class}'. "
                "Use 'stocks', 'etf', 'futures', or 'all'."
            )
