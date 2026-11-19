import time
from typing import Any, Dict, Optional


def normalize_model_key(model_name: Optional[str]) -> Optional[str]:
    model = str(model_name or "").strip().lower()
    return model or None


def has_active_model_cooldown(
    model_cooldowns: Optional[Dict[str, Any]],
    model_name: Optional[str],
    current_time: Optional[float] = None,
    mode: Optional[str] = None,
) -> bool:
    if not model_name or not isinstance(model_cooldowns, dict):
        return False

    now = time.time() if current_time is None else current_time
    target_model = normalize_model_key(model_name)

    for cooldown_model, cooldown_until in model_cooldowns.items():
        if normalize_model_key(cooldown_model) != target_model:
            continue

        try:
            if float(cooldown_until) > now:
                return True
        except (TypeError, ValueError):
            return True

    return False
