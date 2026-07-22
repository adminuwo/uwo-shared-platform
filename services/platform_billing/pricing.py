"""Deterministic integer-only billing calculations."""

from __future__ import annotations

from packages.contracts import ChargeCalculation, Product, RateCard, TOKEN_RATE_UNIT, UsageDimensions, utc_now

from .errors import InvalidRequest, ResourceNotFound


def round_up_ratio(quantity: int, rate_microunits: int, denominator: int = TOKEN_RATE_UNIT) -> int:
    """Round a non-negative integer ratio toward positive infinity."""

    for value, name in ((quantity, "quantity"), (rate_microunits, "rate_microunits"), (denominator, "denominator")):
        if not isinstance(value, int) or isinstance(value, bool) or value < (1 if name == "denominator" else 0):
            raise InvalidRequest("invalid_usage", f"{name} must be a non-negative integer")
    numerator = quantity * rate_microunits
    return (numerator + denominator - 1) // denominator


def calculate_charge(
    rate_card: RateCard,
    product: Product,
    model: str,
    provider_id: str,
    region: str,
    dimensions: UsageDimensions,
    calculated_at: str | None = None,
) -> ChargeCalculation:
    key = (product.value, model, provider_id, region)
    price = next((item for item in rate_card.prices if item.key == key), None)
    if price is None:
        raise ResourceNotFound("unknown_rate", "no price exists for the requested product, model, provider, and region")
    input_charge = round_up_ratio(dimensions.input_tokens, price.input_token_rate_microunits)
    output_charge = round_up_ratio(dimensions.output_tokens, price.output_token_rate_microunits)
    return ChargeCalculation(
        rate_card.rate_card_id,
        rate_card.version,
        input_charge,
        output_charge,
        price.fixed_request_microunits,
        input_charge + output_charge + price.fixed_request_microunits,
        calculated_at or utc_now(),
    )
