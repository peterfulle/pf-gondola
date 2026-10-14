"""Costo y control de presupuesto para las llamadas a Claude (visión y OCR).

Precios en USD por millón de tokens (tarifa estándar de la API de Anthropic,
actualizada 2026-10). Si se usa un modelo no listado aquí, se aplica una
estimación conservadora (DEFAULT_PRICING) en vez de fallar: preferimos un
costo aproximado a perder el registro del gasto.
"""

import db

PRICING = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
DEFAULT_PRICING = {"input": 3.00, "output": 15.00}


class BudgetExceeded(Exception):
    pass


def price_for_model(model: str) -> dict:
    return PRICING.get(model, DEFAULT_PRICING)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price = price_for_model(model)
    cost = (input_tokens / 1_000_000) * price["input"] + (output_tokens / 1_000_000) * price["output"]
    return round(cost, 6)


def check_budget(org_id: str) -> None:
    """Lanza BudgetExceeded si la organización ya alcanzó su tope mensual configurado.
    Sin tope configurado (monthly_budget_usd es None), no hay restricción."""
    org = db.get_organization(org_id)
    budget = org.get("monthly_budget_usd") if org else None
    if not budget:
        return
    spent = db.get_org_spend_current_month(org_id)
    if spent >= budget:
        raise BudgetExceeded(
            f"La organización alcanzó su límite de gasto mensual (${budget:.2f} USD) en análisis con IA. "
            "Contacta a un administrador para ajustar el presupuesto."
        )


def record_call(org_id: str, user_email: str, endpoint: str, model: str,
                 input_tokens: int, output_tokens: int) -> dict:
    cost_usd = estimate_cost_usd(model, input_tokens, output_tokens)
    db.record_usage(org_id, user_email, endpoint, model, input_tokens, output_tokens, cost_usd)
    return {
        "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost_usd,
    }
