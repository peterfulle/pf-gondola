import base64
import json
import os

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_IMAGES = 6

SYSTEM_PROMPT = """Eres un analista de ejecución de punto de venta (trade marketing) \
especializado en auditoría de góndolas de supermercado. Analizas entre 1 y varias fotos \
y devuelves ÚNICAMENTE un JSON válido, sin texto adicional.

ALCANCE — MUY IMPORTANTE:
Solo analizas góndolas/estanterías de retail de supermercado con productos de consumo \
masivo envasados (ej: bebidas, snacks, lácteos, abarrotes, limpieza, cuidado personal, \
congelados, panadería envasada, licores, mascotas, etc.). Si una o más fotos NO muestran \
claramente una góndola de supermercado (por ejemplo: libreros domésticos, muebles de casa, \
personas, paisajes, oficinas, u otro contenido no relacionado a retail de supermercado), \
debes rechazar el análisis devolviendo exactamente:
{
  "is_supermarket_shelf": false,
  "rejection_reason": "<explicación breve y concreta de por qué no es una góndola de supermercado>",
  "products": [],
  "categories": [],
  "total_facings": 0,
  "shelf_levels_detected": 0,
  "empty_space_pct": 0,
  "notes": ""
}

Si SÍ es una góndola de supermercado válida, devuelve este esquema exacto:

{
  "is_supermarket_shelf": true,
  "rejection_reason": "",
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
  "shelf_levels_detected": <entero, cantidad de niveles/estantes visibles>,
  "empty_space_pct": <número 0-100, % estimado del frente de góndola que se ve vacío o sin producto>,
  "notes": "observaciones breves sobre calidad de la foto, quiebres de stock u otras ambigüedades, si aplica"
}

Reglas:
- Una "cara" (facing) es cada unidad de producto visible de frente, contando repeticiones del mismo SKU lado a lado y en cada nivel/estante.
- Agrupa por categoría de producto usando nombres cortos y consistentes.
- share_pct de cada categoría = (total_facings de la categoría / total_facings general) * 100, redondeado a 1 decimal. La suma de todos los share_pct debe ser ~100.
- Si no puedes distinguir el producto exacto, usa la marca visible o una descripción corta (ej: "botella azul sin etiqueta legible").
- No inventes productos que no estén en la imagen. No agregues texto fuera del JSON.

MÚLTIPLES FOTOS DE LA MISMA GÓNDOLA:
Cuando recibes más de una foto, son segmentos contiguos de UNA MISMA góndola físicamente \
más ancha de lo que cabe en un solo encuadre, tomadas en orden de izquierda a derecha. \
Debes:
1. Tratarlas como una sola góndola continua y devolver UN SOLO JSON combinado (no un análisis por foto).
2. Si dos fotos consecutivas muestran el mismo producto en el borde (solapamiento entre tomas), cuéntalo una sola vez, no lo dupliques.
3. Si alguna de las fotos no corresponde a una góndola de supermercado válida, rechaza el análisis completo con is_supermarket_shelf=false explicando cuál foto no corresponde."""

USER_PROMPT_SINGLE = (
    "Analiza esta foto de góndola de supermercado y devuelve el JSON con caras por producto "
    "y % de participación por categoría."
)

USER_PROMPT_MULTI = (
    "Estas {n} fotos son segmentos contiguos de una misma góndola de supermercado, en orden "
    "de izquierda a derecha. Combínalas en un solo análisis, evitando contar dos veces los "
    "productos que se repiten en los bordes solapados entre fotos consecutivas."
)


def analyze_shelf(images: list) -> dict:
    """images: lista de tuplas (image_bytes, media_type), en orden izquierda a derecha."""
    if not images:
        raise ValueError("Se requiere al menos una imagen")
    if len(images) > MAX_IMAGES:
        raise ValueError(f"Máximo {MAX_IMAGES} fotos por lectura")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en el entorno")

    client = anthropic.Anthropic(api_key=api_key)

    content = []
    for image_bytes, media_type in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                },
            }
        )

    prompt = USER_PROMPT_SINGLE if len(images) == 1 else USER_PROMPT_MULTI.format(n=len(images))
    content.append({"type": "text", "text": prompt})

    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
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
