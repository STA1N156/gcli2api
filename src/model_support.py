"""Match advertised model IDs without changing outgoing model names."""


def model_key(model):
    name = str(model or "").strip().lower().removeprefix("models/")
    if name in {"claude-sonnet-4-6-thinking", "claude-opus-4-6-thinking"}:
        return name.removesuffix("-thinking")
    return name


def supports_model(models, model):
    if not model:
        return True
    if not isinstance(models, list) or not models:
        return None
    return model_key(model) in {model_key(item) for item in models if isinstance(item, str)}
