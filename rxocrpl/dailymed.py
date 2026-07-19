"""DailyMed lookup — resolve a product-level NDC to its DailyMed URL.

DailyMed identifies drug labels by a UUID ``setid``, not by NDC, so we search
the ``/spls`` endpoint by NDC and read the setid off the first match. NDC
candidates are generated at the product level ("{4-5}-{3-4}", i.e.
labeler-product with no package segment) since that's the granularity
``Product.product_ndc`` is keyed at and what DailyMed's search accepts.

If no SPL match is found (delisted product, DailyMed outage, etc.) we fall
back to a DailyMed site-search URL built from the NDC, so callers always get
a usable href.
"""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import quote

import requests

__all__ = ["get_dailymed_url", "product_ndc_candidates"]

_SITE_URL = "https://dailymed.nlm.nih.gov/dailymed"
_API_URL = f"{_SITE_URL}/services/v2"
_TIMEOUT = 10

_session = requests.Session()


def product_ndc_candidates(ndc: str) -> list[str]:
    """Plausible product-level ("{4-5}-{3-4}") forms of *ndc*.

    Accepts an NDC already at the product level (hyphenated or not), or a
    package-level NDC (10/11 digits, 3 segments), from which the trailing
    package segment is dropped. The labeler/product split is unambiguous
    once the package segment is gone (4-3, 4-4, 5-3 or 5-4), except for a
    bare 10-digit input, which is ambiguous about where the package segment
    starts — all three plausible splits are returned for the caller to try.
    """
    if not ndc:
        return []
    parts = ndc.split("-")
    if len(parts) == 2:
        return [ndc]
    if len(parts) == 3:
        return [f"{parts[0]}-{parts[1]}"]

    digits = re.sub(r"\D", "", ndc)
    if len(digits) == 11:
        digits = digits[:9]  # drop the 2-digit package segment (5-4-2 split)
    if len(digits) == 10:
        return [
            f"{digits[:4]}-{digits[4:8]}",  # 4-4-2 split
            f"{digits[:5]}-{digits[5:8]}",  # 5-3-2 split
            f"{digits[:5]}-{digits[5:9]}",  # 5-4-1 split
        ]
    if len(digits) == 9:
        return [f"{digits[:5]}-{digits[5:9]}"]
    if len(digits) == 8:
        return [f"{digits[:4]}-{digits[4:8]}", f"{digits[:5]}-{digits[5:8]}"]
    if len(digits) == 7:
        return [f"{digits[:4]}-{digits[4:7]}"]
    return []


def _find_setid(product_ndc: str) -> str | None:
    """Look up the DailyMed SPL setid for a product-level *product_ndc*."""
    try:
        response = _session.get(
            f"{_API_URL}/spls.json",
            params={"ndc": product_ndc, "pagesize": 1},
            timeout=_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    results = response.json().get("data") or []
    return results[0].get("setid") if results else None


@lru_cache(maxsize=512)
def get_dailymed_url(ndc: str) -> str:
    """Return an outgoing DailyMed URL for the drug identified by *ndc*.

    Tries each product-level candidate against DailyMed's SPL search and
    links straight to the matching label page. If nothing matches, falls
    back to a DailyMed site-search URL for the normalized NDC so callers
    always get a usable href.
    """
    candidates = product_ndc_candidates(ndc)
    for candidate in candidates:
        setid = _find_setid(candidate)
        if setid:
            return f"{_SITE_URL}/drugInfo.cfm?setid={setid}"

    query = candidates[0] if candidates else ndc
    return f"{_SITE_URL}/search.cfm?query={quote(query)}&searchdb=ndc"


if __name__ == "__main__":
    print(
        get_dailymed_url("0006-3026-01")  # KEYTRUDA 200 MG/ML INJECTION, SOLUTION
    )
    assert "https://dailymed.nlm.nih.gov/dailymed/getFile.cfm?setid=9333c79b-d487-4538-a9f0-71b91a02b287&type=zip"
