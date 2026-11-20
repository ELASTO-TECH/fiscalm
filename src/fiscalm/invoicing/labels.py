"""Shared invoice labels for the app and printable documents."""

from __future__ import annotations

NON_EU_SERVICES_REASON = "Neimpozabil în România conform art. 278 alin. (2)"

UNITS = [
    dict(code=c, label=label)
    for c, label in [
        ("H87", "buc"),
        ("HUR", "oră"),
        ("DAY", "zi"),
        ("MON", "lună"),
        ("KGM", "kg"),
        ("MTR", "m"),
        ("LTR", "litru"),
    ]
]
VAT = dict(
    rates=["21", "11", "0"],
    categories=[
        dict(code=c, label=label)
        for c, label in [
            ("S", "Cotă standard sau redusă"),
            ("Z", "Cotă zero"),
            ("E", "Scutit de TVA"),
            ("AE", "Taxare inversă"),
            ("K", "Livrare intracomunitară"),
            ("G", "Export de bunuri"),
            ("O", "În afara sferei TVA"),
        ]
    ],
    exemptions=[
        dict(code=c, label=label)
        for c, label in [
            ("VATEX-EU-AE", "Taxare inversă"),
            ("VATEX-EU-IC", "Livrare intracomunitară"),
            ("VATEX-EU-G", "Export de bunuri"),
            ("art. 310", "Scutire conform art. 310"),
        ]
    ],
)


def vat_choices(seller: dict | None) -> dict:
    """The app renders choices; tax defaults and required reasons belong to the engine."""
    choices = [
        dict(key=key, label=label, category=cat, rate=rate, exemption=reason, needs_delivery_date=cat == "K")
        for key, label, cat, rate, reason in [
            ("S21", "TVA 21%", "S", "21", ""),
            ("S11", "TVA 11%", "S", "11", ""),
            ("Z", "Cotă zero", "Z", "0", ""),
            ("E", "Scutit de TVA conform art. 292", "E", "0", "Scutit de TVA conform art. 292"),
            ("AE", "Taxare inversă", "AE", "0", "VATEX-EU-AE"),
            ("K", "Livrare intracomunitară", "K", "0", "VATEX-EU-IC"),
            ("G", "Export de bunuri", "G", "0", "VATEX-EU-G"),
            # Art. 278(2): general B2B services rule, subject to its exceptions.
            # https://static.anaf.ro/static/10/Anaf/legislatie/Cod_fiscal_norme_2023.htm
            ("O278", "Prestare de servicii în afara UE (neimpozabilă în România)", "O", "0", NON_EU_SERVICES_REASON),
            ("O310", "Scutire conform art. 310", "O", "0", "Scutire conform art. 310"),
        ]
    ]
    # Art. 310: only non-payers use the special exemption scheme.
    choices = choices[-1:] if not seller or not seller["vat_payer"] else [c for c in choices if c["key"] != "O310"]
    return VAT | dict(choices=choices, default=choices[0]["key"])


COUNTY_NAMES = dict(
    zip(
        [
            "AB",
            "AG",
            "AR",
            "B",
            "BC",
            "BH",
            "BN",
            "BR",
            "BT",
            "BV",
            "BZ",
            "CJ",
            "CL",
            "CS",
            "CT",
            "CV",
            "DB",
            "DJ",
            "GJ",
            "GL",
            "GR",
            "HD",
            "HR",
            "IF",
            "IL",
            "IS",
            "MH",
            "MM",
            "MS",
            "NT",
            "OT",
            "PH",
            "SB",
            "SJ",
            "SM",
            "SV",
            "TL",
            "TM",
            "TR",
            "VL",
            "VN",
            "VS",
        ],
        [
            "Alba",
            "Argeș",
            "Arad",
            "București",
            "Bacău",
            "Bihor",
            "Bistrița-Năsăud",
            "Brăila",
            "Botoșani",
            "Brașov",
            "Buzău",
            "Cluj",
            "Călărași",
            "Caraș-Severin",
            "Constanța",
            "Covasna",
            "Dâmbovița",
            "Dolj",
            "Gorj",
            "Galați",
            "Giurgiu",
            "Hunedoara",
            "Harghita",
            "Ilfov",
            "Ialomița",
            "Iași",
            "Mehedinți",
            "Maramureș",
            "Mureș",
            "Neamț",
            "Olt",
            "Prahova",
            "Sibiu",
            "Sălaj",
            "Satu Mare",
            "Suceava",
            "Tulcea",
            "Timiș",
            "Teleorman",
            "Vâlcea",
            "Vrancea",
            "Vaslui",
        ],
        strict=True,
    )
)
