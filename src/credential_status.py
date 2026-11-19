"""凭证摘要页共用的状态分类规则。"""

from typing import Any, Dict, Iterable, Set


def cooldown_families(model_names: Iterable[str]) -> Set[str]:
    families = set()
    for model_name in model_names:
        name = str(model_name or "").lower()
        if "claude" in name:
            families.add("claude")
        if "gemini" in name:
            families.add("gemini")
    return families


def credential_status_flags(
    disabled: bool,
    error_codes: Any,
    active_cooldowns: Dict[str, Any],
    mode: str,
) -> Dict[str, bool]:
    families = cooldown_families(active_cooldowns)
    abnormal = bool(disabled or error_codes or active_cooldowns)
    unscoped_abnormal = bool(disabled or (error_codes and not families))

    return {
        "abnormal": abnormal,
        "claude_abnormal": (
            "claude" in families
            or (mode == "antigravity" and unscoped_abnormal)
        ),
        "gemini_abnormal": (
            "gemini" in families
            or (mode == "geminicli" and abnormal)
            or (mode == "antigravity" and unscoped_abnormal)
        ),
    }


def matches_status_filter(status_filter: str, flags: Dict[str, bool]) -> bool:
    if status_filter == "normal":
        return not flags["abnormal"]
    if status_filter in {"abnormal", "claude_abnormal", "gemini_abnormal"}:
        return flags[status_filter]
    return True


def matches_cooldown_filter(
    cooldown_filter: str,
    active_cooldowns: Dict[str, Any],
) -> bool:
    families = cooldown_families(active_cooldowns)
    if cooldown_filter == "in_cooldown":
        return bool(active_cooldowns)
    if cooldown_filter == "claude_cooldown":
        return "claude" in families
    if cooldown_filter == "gemini_cooldown":
        return "gemini" in families
    if cooldown_filter == "no_cooldown":
        return not active_cooldowns
    return True


def new_status_stats() -> Dict[str, int]:
    return {
        "total": 0,
        "normal": 0,
        "abnormal": 0,
        "claude_abnormal": 0,
        "gemini_abnormal": 0,
    }


def add_status_stats(stats: Dict[str, int], flags: Dict[str, bool]) -> None:
    stats["total"] += 1
    stats["abnormal" if flags["abnormal"] else "normal"] += 1
    if flags["claude_abnormal"]:
        stats["claude_abnormal"] += 1
    if flags["gemini_abnormal"]:
        stats["gemini_abnormal"] += 1
