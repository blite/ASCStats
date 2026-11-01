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


# English names by alpha-2 code, short forms where the official one is long
NAMES = {
    "ae": "United Arab Emirates", "af": "Afghanistan", "ag": "Antigua and Barbuda",
    "ai": "Anguilla", "al": "Albania", "am": "Armenia", "ao": "Angola", "ar": "Argentina",
    "at": "Austria",
    "au": "Australia", "az": "Azerbaijan", "ba": "Bosnia and Herzegovina", "bb": "Barbados",
    "be": "Belgium", "bf": "Burkina Faso", "bg": "Bulgaria", "bh": "Bahrain", "bj": "Benin",
    "bm": "Bermuda", "bn": "Brunei", "bo": "Bolivia", "br": "Brazil", "bs": "Bahamas",
    "bt": "Bhutan", "bw": "Botswana", "by": "Belarus", "bz": "Belize", "ca": "Canada",
    "cd": "DR Congo", "cf": "Central African Republic", "cg": "Congo", "ch": "Switzerland",
    "ci": "Côte d'Ivoire", "cl": "Chile", "cm": "Cameroon", "cn": "China", "co": "Colombia",
    "cr": "Costa Rica", "cv": "Cape Verde", "cy": "Cyprus", "cz": "Czechia", "de": "Germany",
    "dk": "Denmark", "dm": "Dominica", "do": "Dominican Republic", "dz": "Algeria",
    "ec": "Ecuador", "ee": "Estonia", "eg": "Egypt", "es": "Spain", "fi": "Finland", "fj": "Fiji",
    "fm": "Micronesia", "fr": "France", "ga": "Gabon", "gb": "United Kingdom", "gd": "Grenada",
    "ge": "Georgia", "gh": "Ghana", "gm": "Gambia", "gr": "Greece", "gt": "Guatemala",
    "gw": "Guinea-Bissau", "gy": "Guyana", "hk": "Hong Kong", "hn": "Honduras", "hr": "Croatia",
    "hu": "Hungary", "id": "Indonesia", "ie": "Ireland", "il": "Israel", "in": "India",
    "iq": "Iraq", "is": "Iceland", "it": "Italy", "jm": "Jamaica", "jo": "Jordan", "jp": "Japan",
    "ke": "Kenya", "kg": "Kyrgyzstan", "kh": "Cambodia", "kn": "Saint Kitts and Nevis",
    "kr": "South Korea", "kw": "Kuwait", "ky": "Cayman Islands", "kz": "Kazakhstan", "la": "Laos",
    "lb": "Lebanon", "lc": "Saint Lucia", "lk": "Sri Lanka", "lr": "Liberia", "lt": "Lithuania",
    "lu": "Luxembourg", "lv": "Latvia", "ly": "Libya", "ma": "Morocco", "md": "Moldova",
    "me": "Montenegro", "mg": "Madagascar", "mk": "North Macedonia", "ml": "Mali",
    "mm": "Myanmar", "mn": "Mongolia", "mo": "Macao", "mr": "Mauritania", "ms": "Montserrat",
    "mt": "Malta", "mu": "Mauritius", "mv": "Maldives", "mw": "Malawi", "mx": "Mexico",
    "my": "Malaysia", "mz": "Mozambique", "na": "Namibia", "ne": "Niger", "ng": "Nigeria",
    "ni": "Nicaragua", "nl": "Netherlands", "no": "Norway", "np": "Nepal", "nr": "Nauru",
    "nz": "New Zealand", "om": "Oman", "pa": "Panama", "pe": "Peru", "pg": "Papua New Guinea",
    "ph": "Philippines", "pk": "Pakistan", "pl": "Poland", "pt": "Portugal", "pw": "Palau",
    "py": "Paraguay", "qa": "Qatar", "ro": "Romania", "rs": "Serbia", "ru": "Russia",
    "rw": "Rwanda", "sa": "Saudi Arabia", "sb": "Solomon Islands", "sc": "Seychelles",
    "se": "Sweden", "sg": "Singapore", "si": "Slovenia", "sk": "Slovakia", "sl": "Sierra Leone",
    "sn": "Senegal", "sr": "Suriname", "st": "São Tomé and Príncipe", "sv": "El Salvador",
    "sz": "Eswatini", "tc": "Turks and Caicos Islands", "td": "Chad", "th": "Thailand",
    "tj": "Tajikistan", "tm": "Turkmenistan", "tn": "Tunisia", "to": "Tonga", "tr": "Türkiye",
    "tt": "Trinidad and Tobago", "tw": "Taiwan", "tz": "Tanzania", "ua": "Ukraine",
    "ug": "Uganda", "us": "United States", "uy": "Uruguay", "uz": "Uzbekistan",
    "vc": "Saint Vincent and the Grenadines", "ve": "Venezuela", "vg": "British Virgin Islands",
    "vn": "Vietnam", "vu": "Vanuatu", "xk": "Kosovo", "ye": "Yemen", "za": "South Africa",
    "zm": "Zambia", "zw": "Zimbabwe",
}  # fmt: skip


def name(alpha2: str) -> str:
    return NAMES.get(alpha2.lower(), alpha2.upper())


def flag(alpha2: str) -> str:
    """The flag emoji: the code's two letters as regional indicator symbols."""
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in alpha2.lower())


def store_page(apple_id: int, territory: str) -> str:
    """The app's public App Store page in the review's storefront (the US one if the territory
    is unknown)."""
    return f"https://apps.apple.com/{ALPHA2.get(territory, 'us')}/app/id{apple_id}"
