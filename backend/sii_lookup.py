from __future__ import annotations

import os
import re

import httpx

SIMPLEAPI_RUT_URL = "https://rut.simpleapi.cl/v2"
TIMEOUT_SECONDS = 5.0


class SiiNotConfigured(Exception):
    pass


def _normalize_rut(rut: str) -> str:
    """SimpleAPI espera el RUT sin puntos, con guión y dígito verificador: 76192083-9."""
    cleaned = re.sub(r"[^0-9kK]", "", rut or "")
    if len(cleaned) < 2:
        raise ValueError("RUT inválido")
    return f"{cleaned[:-1]}-{cleaned[-1].upper()}"


def lookup_rut(rut: str) -> dict | None:
    """Consulta la situación tributaria de un RUT vía SimpleAPI.cl.

    Devuelve None si el RUT no existe en el SII o si el proveedor falla/no responde a
    tiempo — nunca una excepción por esos casos, para que el llamador (el wizard de alta
    de clientes) se degrade en silencio sin bloquear al vendedor. Solo lanza
    SiiNotConfigured cuando no hay API key, que el endpoint traduce a un 503 explícito.
    """
    api_key = os.environ.get("SII_API_KEY")
    if not api_key:
        raise SiiNotConfigured("SII_API_KEY no está configurada")

    normalized = _normalize_rut(rut)
    try:
        resp = httpx.get(
            f"{SIMPLEAPI_RUT_URL}/{normalized}",
            headers={"Authorization": api_key},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    actividades = data.get("actividadesEconomicas") or []
    domicilios = data.get("domicilios") or []
    domicilio = domicilios[0] if domicilios else {}
    direccion = ", ".join(p for p in (domicilio.get("direccion", "").strip(), domicilio.get("comuna")) if p) or None

    return {
        "rut": data.get("rut") or normalized,
        "razon_social": data.get("razonSocial"),
        "giro": actividades[0].get("descripcion") if actividades else None,
        "direccion": direccion,
    }
