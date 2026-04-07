COST_PER_MILLION = {
    "claude-opus-4-6-20250605": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_create": 18.75},
    "claude-opus-4-5-20251101": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_create": 18.75},
    "claude-sonnet-4-5-20250514": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_create": 3.75},
    "claude-sonnet-4-6-20250620": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_create": 3.75},
    "claude-haiku-3-5-20241022": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_create": 1.0},
}

# Default rates for unknown models (use Sonnet pricing as reasonable middle ground)
DEFAULT_RATES = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_create": 3.75}


def _get_rates(model_name):
    """Find pricing rates for a model, matching by prefix if exact match not found."""
    if model_name in COST_PER_MILLION:
        return COST_PER_MILLION[model_name]
    # Try prefix matching
    for key, rates in COST_PER_MILLION.items():
        if model_name.startswith(key.rsplit("-", 1)[0]):
            return rates
    # Match by family name
    if "opus" in model_name:
        return COST_PER_MILLION.get("claude-opus-4-6-20250605", DEFAULT_RATES)
    if "sonnet" in model_name:
        return COST_PER_MILLION.get("claude-sonnet-4-5-20250514", DEFAULT_RATES)
    if "haiku" in model_name:
        return COST_PER_MILLION.get("claude-haiku-3-5-20241022", DEFAULT_RATES)
    return DEFAULT_RATES


def estimate_cost(model, usage):
    """Estimate cost in USD for a single usage record."""
    rates = _get_rates(model)
    cost = (
        usage.get("input_tokens", 0) * rates["input"]
        + usage.get("output_tokens", 0) * rates["output"]
        + usage.get("cache_read_input_tokens", 0) * rates["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * rates["cache_create"]
    ) / 1_000_000
    return cost


def get_model_display_name(model_name):
    """Shorten model name for display."""
    if "opus" in model_name and "4-6" in model_name:
        return "Opus 4.6"
    if "opus" in model_name and "4-5" in model_name:
        return "Opus 4.5"
    if "sonnet" in model_name and "4-6" in model_name:
        return "Sonnet 4.6"
    if "sonnet" in model_name and "4-5" in model_name:
        return "Sonnet 4.5"
    if "haiku" in model_name:
        return "Haiku 3.5"
    return model_name
