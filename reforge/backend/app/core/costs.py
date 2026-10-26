"""Run cost estimates from measured token usage.

Provider prices change often and differ by account (Ollama Cloud's free tier
costs nothing; paid models bill per million tokens), so ReForge never embeds a
price list.  Set ``LLM_INPUT_PRICE_PER_MTOK`` and ``LLM_OUTPUT_PRICE_PER_MTOK``
(US dollars per one million tokens, from your provider's pricing page) and every
run, report, and benchmark summary shows an estimated cost.
"""

from __future__ import annotations

from dataclasses import dataclass

TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True, slots=True)
class Pricing:
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    @property
    def configured(self) -> bool:
        return self.input_per_mtok > 0 or self.output_per_mtok > 0


def estimate_cost_usd(tokens_in: int, tokens_out: int, pricing: Pricing) -> float:
    """Estimated spend for the given token counts, rounded to a hundredth of a cent."""
    cost = (tokens_in * pricing.input_per_mtok + tokens_out * pricing.output_per_mtok) / TOKENS_PER_PRICE_UNIT
    return round(cost, 4)
