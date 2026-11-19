"""Canonical subscription labels shared by OAuth and the control panel."""

SUBSCRIPTION_TIERS = ("free", "pro", "ultra", "ultra_lite")


def normalize_subscription_tier(raw_tier: str | None) -> str | None:
    if not isinstance(raw_tier, str) or not raw_tier.strip():
        return None
    tier = raw_tier.strip().lower().replace("_", "-").replace(" ", "-")
    if "ultra" in tier:
        return "ultra_lite" if "lite" in tier else "ultra"
    return {
        "pro": "pro", "g1-pro-tier": "pro", "helium-tier": "pro",
        "standard-tier": "pro", "free": "free", "free-tier": "free",
    }.get(tier)
