"""Source tiering (the methodology's source hierarchy) and fetch rules."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Tiers follow the WBS source standard:
# 1 government / parliament / regulator / central bank / international organisation
# 2 WBS / University of Warwick / universities / research institutions
# 3 official employer / organisation (only set for known org pages)
# 4 awards / registers / conference programmes
# 5 reputable media, and unknown sites (counted, but never alone for a CONFIRMED grade)
# 6 LinkedIn (supplementary only)
# 7 Wikipedia / Wikidata / aggregators (discovery, not proof)

WARWICK_DOMAINS = ("warwick.ac.uk", "wbs.ac.uk")
IO_DOMAINS = (
    "un.org", "undp.org", "unicef.org", "unesco.org", "unctad.org", "unep.org", "ilo.org", "fao.org",
    "wfp.org", "who.int", "wto.org", "imf.org", "worldbank.org", "ifc.org", "oecd.org", "afdb.org",
    "adb.org", "iadb.org", "ebrd.com", "eib.org", "aiib.org", "isdb.org", "bis.org", "iaea.org",
    "au.int", "europa.eu", "commonwealth.int", "thecommonwealth.org", "ecowas.int", "asean.org",
    "caricom.org", "sadc.int", "eac.int", "gcc-sg.org", "nato.int", "unhcr.org", "iom.int", "itu.int",
    "wipo.int", "ifad.org", "unido.org", "weforum.org", "gavi.org", "theglobalfund.org",
    "bankofengland.co.uk", "ecb.europa.eu", "federalreserve.gov",
)
GOV_RX = re.compile(
    r"(\.gov$|\.gov\.[a-z]{2}$|\.gob\.[a-z]{2}$|\.gouv\.[a-z]{2}$|\.gouv\.fr$|\.go\.[a-z]{2}$|\.govt\.[a-z]{2}$|"
    r"\.gv\.at$|\.admin\.ch$|\.bund\.de$|\.gc\.ca$|\.gub\.uy$|\.nic\.in$|\.mil$|parliament|senate|"
    r"assembly|presidency|statehouse|cabinet|government|\.gov\.uk$)"
)
ACADEMIC_RX = re.compile(r"(\.ac\.[a-z]{2}$|\.edu$|\.edu\.[a-z]{2}$|\.ac\.uk$|universit|\.uni-)")
AWARD_REGISTER_DOMAINS = (
    "thegazette.co.uk", "find-and-update.company-information.service.gov.uk", "nobelprize.org",
    "orcid.org", "openalex.org",
)
NEWS_DOMAINS = (
    "bbc.co.uk", "bbc.com", "ft.com", "reuters.com", "bloomberg.com", "theguardian.com", "nytimes.com",
    "economist.com", "forbes.com", "fortune.com", "time.com", "aljazeera.com", "apnews.com", "cnn.com",
    "wsj.com", "telegraph.co.uk", "independent.co.uk", "thetimes.co.uk", "cnbc.com", "washingtonpost.com",
    "premiumtimesng.com", "punchng.com", "vanguardngr.com", "thisdaylive.com", "businessday.ng",
    "guardian.ng", "nation.africa", "myjoyonline.com", "graphic.com.gh", "citinewsroom.com",
    "thehindu.com", "hindustantimes.com", "livemint.com", "indiatimes.com", "scmp.com", "straitstimes.com",
    "gulfnews.com", "thenationalnews.com", "arabnews.com", "dawn.com", "thestar.com.my", "bangkokpost.com",
    "japantimes.co.jp", "koreaherald.com", "smh.com.au", "afr.com", "theaustralian.com.au", "lemonde.fr",
    "spiegel.de", "elpais.com", "people.cn", "chinadaily.com.cn", "caixinglobal.com", "news24.com",
    "businesslive.co.za", "monitor.co.ug", "thecitizen.co.tz", "herald.co.zw",
)
LINKEDIN = ("linkedin.com",)
WIKI_AGGREGATORS = (
    "wikipedia.org", "wikidata.org", "wikiwand.com", "everybodywiki.com", "prabook.com", "peoplepill.com",
    "crunchbase.com", "theorg.com", "famousbirthdays.com", "dbpedia.org",
)
# Never fetched: terms of service prohibit scraping, or the site is a personal-data broker / social feed.
BLOCKED_DOMAINS = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com", "zoominfo.com",
    "rocketreach.co", "contactout.com", "signalhire.com", "apollo.io", "lusha.com", "spokeo.com",
    "whitepages.com", "192.com", "youtube.com", "web.archive.org", "archive.org", "books.google.com",
    "google.com", "doi.org", "jstor.org", "worldcat.org", "viaf.org",
)
# AI-written encyclopedias: text generated from other pages, so never independent evidence. Not fetched, and any
# claim already saved from them is rejected (UNSUPPORTED_SOURCE).
AI_GENERATED_DOMAINS = ("grokipedia.com",)
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".mp4", ".mp3", ".zip", ".doc", ".docx",
                   ".xls", ".xlsx", ".ppt", ".pptx")

TEMPLATE_SOURCE_TYPE = {
    "gov": "Government/regulator/international organisation",
    "warwick": "WBS/University source",
    "academic": "University/research institution",
    "org": "Employer/organisation",
    "register": "Award/appointment register",
    "news": "Reputable news/trade publication",
    "linkedin": "LinkedIn (supplementary only)",
    "other": "Other",
}


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _ends(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify(url: str) -> tuple[int, str, bool]:
    """Return (tier, kind, warwick_side)."""
    host = domain_of(url)
    if _ends(host, WARWICK_DOMAINS):
        return 2, "warwick", True
    if _ends(host, WIKI_AGGREGATORS):
        return 7, "other", False
    if _ends(host, LINKEDIN):
        return 6, "linkedin", False
    if _ends(host, IO_DOMAINS) or GOV_RX.search(host):
        return 1, "gov", False
    if ACADEMIC_RX.search(host):
        return 2, "academic", False
    if _ends(host, AWARD_REGISTER_DOMAINS):
        return 4, "register", False
    if _ends(host, NEWS_DOMAINS):
        return 5, "news", False
    return 5, "other", False


def is_ai_generated(url: str) -> bool:
    return _ends(domain_of(url), AI_GENERATED_DOMAINS)


def fetchable(url: str) -> bool:
    if not url.lower().startswith(("http://", "https://")):
        return False
    host = domain_of(url)
    if _ends(host, BLOCKED_DOMAINS + AI_GENERATED_DOMAINS) or _ends(host, ("wikipedia.org", "wikidata.org", "wikimedia.org")):
        return False
    path = urlparse(url).path.lower()
    return not path.endswith(SKIP_EXTENSIONS)
