import time
from typing import Any, Dict, Optional


def model_cooldown_group(model_name: Optional[str]) -> Optional[str]:
    model = str(model_name or "").lower()
    if "gemini" in model:
        return "gemini"
    if "claude" in model:
        return "claude"
    return None


def has_active_model_cooldown(
    model_cooldowns: Optional[Dict[str, Any]],
    model_name: Optional[str],
    current_time: Optional[float] = None,
) -> bool:
    if not model_name or not isinstance(model_cooldowns, dict):
        return False

    now = time.time() if current_time is None else current_time
    target_group = model_cooldown_group(model_name)

    for cooldown_model, cooldown_until in model_cooldowns.items():
        if cooldown_model != model_name and (
            not target_group or model_cooldown_group(cooldown_model) != target_group
        ):
            continue

        try:
            if float(cooldown_until) > now:
                return True
        except (TypeError, ValueError):
            return True

    return False
