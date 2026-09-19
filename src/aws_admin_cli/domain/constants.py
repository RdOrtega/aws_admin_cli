"""Cross-service domain constants: friendly display names for AWS region codes.

``AWS_REGION_NAMES`` is the single source of truth every TUI screen (the
Governance Grid header, the Region Selector menu) draws its region labels
from, so the two never drift out of sync with each other.
"""

from typing import Final

__all__ = ["AWS_REGION_NAMES", "get_region_display_name"]

AWS_REGION_NAMES: Final[dict[str, str]] = {
    "us-east-1": "N. Virginia",
    "us-west-2": "Oregon",
    "eu-west-1": "Ireland",
    "sa-east-1": "São Paulo",
    "ap-northeast-1": "Tokyo",
}


def get_region_display_name(code: str) -> str:
    """``"<code> (<friendly name>)"`` for a known region, else just ``<code>``.

    Falls back to the bare code for a region this CLI doesn't have a
    friendly name for yet -- never raises, since ``settings.region`` can be
    any string a user's profile/config sets.
    """
    friendly_name = AWS_REGION_NAMES.get(code)
    if friendly_name is None:
        return code
    return f"{code} ({friendly_name})"
