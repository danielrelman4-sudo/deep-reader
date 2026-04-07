def run(
    entity_count: int,
    claim_count: int,
    chunk_token_estimate: int,
    threads_updated: int,
    threads_created: int,
    current_multiplier: float = 1.0,
) -> float:
    """Calculate next chunk's size multiplier based on density metrics.

    No LLM call — pure heuristic.

    High density / high disruption → smaller chunks (lower multiplier).
    Low density → larger chunks (higher multiplier).

    Returns new multiplier clamped to [0.5, 2.0].
    """
    # Density: entities + claims per 1K tokens
    tokens_k = max(chunk_token_estimate / 1000, 0.1)
    density = (entity_count + claim_count) / tokens_k

    # Disruption: thread activity
    disruption = threads_updated + threads_created

    # Compute adjustment
    adjustment = 0.0

    if density > 8:
        adjustment -= 0.2
    elif density > 5:
        adjustment -= 0.1
    elif density < 1:
        adjustment += 0.2
    elif density < 2:
        adjustment += 0.1

    if disruption > 4:
        adjustment -= 0.15
    elif disruption > 2:
        adjustment -= 0.05

    # Smooth: cap change per step
    adjustment = max(-0.3, min(0.3, adjustment))

    new_multiplier = current_multiplier + adjustment
    return max(0.5, min(2.0, round(new_multiplier, 2)))
