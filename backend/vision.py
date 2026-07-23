import base64
import json
import os

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """Eres un analista de ejecución de punto de venta (trade marketing) \
especializado en auditoría de góndolas de retail. Analizas una foto de una góndola \
y devuelves ÚNICAMENTE un JSON válido, sin texto adicional, con este esquema exacto:

{
  "products": [
    {
      "product": "nombre del producto",
      "brand": "marca visible",
      "category": "categoría del producto",
      "facings": <entero, caras visibles>,
      "shelf_level": <entero, nivel de estante contado desde arriba empezando en 1>,
      "out_of_stock": <true/false, true si hay un hueco vacío evidente donde debería ir ese producto>
    }
  ],
  "categories": [
    {
      "category": "nombre categoría",
      "total_facings": <entero>,
      "share_pct": <número, % de participación en la góndola>,
      "product_count": <entero, cantidad de productos distintos de esa categoría>,
      "brand_count": <entero, cantidad de marcas distintas de esa categoría>
    }
  ],
  "total_facings": <entero, suma de todas las caras de todos los productos>,
  "shelf_levels_detected": <entero, cantidad de niveles/estantes visibles en la foto>,
  "empty_space_pct": <número 0-100, % estimado del frente de góndola que se ve vacío o sin producto>,
  "notes": "observaciones breves sobre calidad de la foto, quiebres de stock u otras ambigüedades, si aplica"
}

Reglas:
- Una "cara" (facing) es cada unidad de producto visible de frente en el frente de la góndola, contando repeticiones del mismo SKU lado a lado y en cada nivel/estante.
- Agrupa por categoría de producto (ej: gaseosas, snacks, lácteos, cuidado personal, etc.) según lo que veas, usando nombres de categoría cortos y consistentes.
- share_pct de cada categoría = (total_facings de la categoría / total_facings general) * 100, redondeado a 1 decimal. La suma de todos los share_pct debe ser ~100.
- Si no puedes distinguir el producto exacto, usa la marca visible o una descripción corta (ej: "botella azul sin etiqueta legible") y deja "brand" igual a esa descripción si tampoco se distingue la marca.
- No inventes productos que no estén en la imagen. No agregues texto fuera del JSON."""

USER_PROMPT = (
    "Analiza esta foto de góndola y devuelve el JSON con caras por producto "
    "y % de participación por categoría."
)


def analyze_shelf(image_bytes: bytes, media_type: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en el entorno")

    client = anthropic.Anthropic(api_key=api_key)
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    message = client.messages.create(
        model=MODEL,
        max_tokens=3072,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_image,
                        },
                    },
                    {"type": "text", "text": USER_PROMPT},
                ],
            }
        ],
    )

    raw_text = "".join(block.text for block in message.content if block.type == "text")
    return _parse_json(raw_text)


def _parse_json(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())
