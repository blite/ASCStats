"""App Store territories: App Store Connect names them by ISO 3166-1 alpha-3 code (``CAN``), the
public store by the alpha-2 code in its paths (``apps.apple.com/ca/...``)."""

ALPHA2 = {
    "AFG": "af", "AGO": "ao", "AIA": "ai", "ALB": "al", "ARE": "ae", "ARG": "ar", "ARM": "am",
    "ATG": "ag", "AUS": "au", "AUT": "at", "AZE": "az", "BEL": "be", "BEN": "bj", "BFA": "bf",
    "BGR": "bg", "BHR": "bh", "BHS": "bs", "BIH": "ba", "BLR": "by", "BLZ": "bz", "BMU": "bm",
    "BOL": "bo", "BRA": "br", "BRB": "bb", "BRN": "bn", "BTN": "bt", "BWA": "bw", "CAF": "cf",
    "CAN": "ca", "CHE": "ch", "CHL": "cl", "CHN": "cn", "CIV": "ci", "CMR": "cm", "COD": "cd",
    "COG": "cg", "COL": "co", "CPV": "cv", "CRI": "cr", "CYM": "ky", "CYP": "cy", "CZE": "cz",
    "DEU": "de", "DMA": "dm", "DNK": "dk", "DOM": "do", "DZA": "dz", "ECU": "ec", "EGY": "eg",
    "ESP": "es", "EST": "ee", "FIN": "fi", "FJI": "fj", "FRA": "fr", "FSM": "fm", "GAB": "ga",
    "GBR": "gb", "GEO": "ge", "GHA": "gh", "GMB": "gm", "GNB": "gw", "GRC": "gr", "GRD": "gd",
    "GTM": "gt", "GUY": "gy", "HKG": "hk", "HND": "hn", "HRV": "hr", "HUN": "hu", "IDN": "id",
    "IND": "in", "IRL": "ie", "IRQ": "iq", "ISL": "is", "ISR": "il", "ITA": "it", "JAM": "jm",
    "JOR": "jo", "JPN": "jp", "KAZ": "kz", "KEN": "ke", "KGZ": "kg", "KHM": "kh", "KNA": "kn",
    "KOR": "kr", "KWT": "kw", "LAO": "la", "LBN": "lb", "LBR": "lr", "LBY": "ly", "LCA": "lc",
    "LKA": "lk", "LTU": "lt", "LUX": "lu", "LVA": "lv", "MAC": "mo", "MAR": "ma", "MDA": "md",
    "MDG": "mg", "MDV": "mv", "MEX": "mx", "MKD": "mk", "MLI": "ml", "MLT": "mt", "MMR": "mm",
    "MNE": "me", "MNG": "mn", "MOZ": "mz", "MRT": "mr", "MSR": "ms", "MUS": "mu", "MWI": "mw",
    "MYS": "my", "NAM": "na", "NER": "ne", "NGA": "ng", "NIC": "ni", "NLD": "nl", "NOR": "no",
    "NPL": "np", "NRU": "nr", "NZL": "nz", "OMN": "om", "PAK": "pk", "PAN": "pa", "PER": "pe",
    "PHL": "ph", "PLW": "pw", "PNG": "pg", "POL": "pl", "PRT": "pt", "PRY": "py", "QAT": "qa",
    "ROU": "ro", "RUS": "ru", "RWA": "rw", "SAU": "sa", "SEN": "sn", "SGP": "sg", "SLB": "sb",
    "SLE": "sl", "SLV": "sv", "SRB": "rs", "STP": "st", "SUR": "sr", "SVK": "sk", "SVN": "si",
    "SWE": "se", "SWZ": "sz", "SYC": "sc", "TCA": "tc", "TCD": "td", "THA": "th", "TJK": "tj",
    "TKM": "tm", "TON": "to", "TTO": "tt", "TUN": "tn", "TUR": "tr", "TWN": "tw", "TZA": "tz",
    "UGA": "ug", "UKR": "ua", "URY": "uy", "USA": "us", "UZB": "uz", "VCT": "vc", "VEN": "ve",
    "VGB": "vg", "VNM": "vn", "VUT": "vu", "XKS": "xk", "YEM": "ye", "ZAF": "za", "ZMB": "zm",
    "ZWE": "zw",
}  # fmt: skip


def store_page(apple_id: int, territory: str) -> str:
    """The app's public App Store page in the review's storefront (the US one if the territory
    is unknown)."""
    return f"https://apps.apple.com/{ALPHA2.get(territory, 'us')}/app/id{apple_id}"
