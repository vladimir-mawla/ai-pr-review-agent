"""Per-model USD pricing for LLM cost accounting.

Owns exactly one thing: ``MODEL_PRICES``, a table of USD-per-million-token
input/output prices for every model this project's LLM client is configured
to call. Kept in this single dedicated module (not inlined in
``backend.economics.budget`` or ``backend.tools.llm_client``) so there is
exactly one place a human updates when Anthropic's published pricing
changes -- scattering a second inline literal anywhere else in the codebase
would be exactly the kind of silent-drift risk
``backend.models.enums.SEVERITY_RANK``'s docstring already warns about, for
a different pair of numbers.

MAINTENANCE NOTE -- PRICES WILL GO STALE: the figures below are a
point-in-time snapshot of Anthropic's published first-party API pricing.
They are not fetched live and nothing in this codebase re-validates them
against Anthropic's actual current rates. Review this table (and only this
table -- never hand-adjust ``compute_cost_usd``'s formula to compensate for
a stale price) before trusting ``BudgetGuard``'s dollar figures for a real
budget decision, and whenever ``Settings.anthropic_model`` is repointed at a
model not yet listed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPrice:
    """USD price per 1,000,000 tokens, input and output priced separately.

    Attributes:
        input_usd_per_million_tokens: Cost per 1M input (prompt) tokens.
        output_usd_per_million_tokens: Cost per 1M output (completion)
            tokens -- always priced higher than input for every Anthropic
            model to date, reflecting generation being the more expensive
            direction.
    """

    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal


# Anthropic first-party API pricing, USD per 1,000,000 tokens. See this
# module's docstring's MAINTENANCE NOTE -- these numbers will go stale.
MODEL_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(
        input_usd_per_million_tokens=Decimal("1.00"),
        output_usd_per_million_tokens=Decimal("5.00"),
    ),
    # M13: the LLM-as-judge model (backend.evaluation.judge.JUDGE_MODEL) --
    # deliberately a different, stronger tier than the haiku-tier
    # specialists above, never the model being judged. Priced here so
    # AnthropicLLMClient.complete's cost accounting (and therefore
    # BudgetGuard, which the judge shares a daily cap with -- see
    # backend.evaluation.judge.judge_settings) works for real judge calls
    # instead of raising UnknownModelPriceError.
    "claude-sonnet-5": ModelPrice(
        input_usd_per_million_tokens=Decimal("2.00"),
        output_usd_per_million_tokens=Decimal("10.00"),
    ),
}

# cost_usd is stored as NUMERIC(10, 6) in agent_events (see
# backend.database.models.AgentEvent.cost_usd) -- quantizing to the same
# precision here means the value this module computes is exactly the value
# that eventually lands in the database, with no silent further rounding
# happening downstream.
_COST_QUANTUM = Decimal("0.000001")


class UnknownModelPriceError(Exception):
    """Raised when ``compute_cost_usd`` is asked to price a model with no table entry.

    Deliberately fails loud rather than falling back to a guessed price or
    silently returning ``$0.00`` -- either of those would let a real LLM
    call's cost vanish from ``BudgetGuard``'s accounting, defeating the
    entire point of a hard budget cap.
    """

    def __init__(self, model: str) -> None:
        super().__init__(
            f"no price table entry for model {model!r} -- add one to "
            "backend.economics.pricing.MODEL_PRICES before using it"
        )
        self.model = model


def compute_cost_usd(model: str, *, tokens_in: int, tokens_out: int) -> Decimal:
    """Compute one call's USD cost from its token counts, per ``MODEL_PRICES``.

    Raises ``UnknownModelPriceError`` for a model with no table entry --
    see that exception's docstring for why silently defaulting would be
    worse than failing loudly here.
    """
    price = MODEL_PRICES.get(model)
    if price is None:
        raise UnknownModelPriceError(model)
    million = Decimal(1_000_000)
    cost = (Decimal(tokens_in) / million) * price.input_usd_per_million_tokens + (
        Decimal(tokens_out) / million
    ) * price.output_usd_per_million_tokens
    return cost.quantize(_COST_QUANTUM)
