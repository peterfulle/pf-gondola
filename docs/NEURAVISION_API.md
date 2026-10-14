# NeuraVision — API REST para integración de frontend

Backend FastAPI que analiza fotos de góndolas de supermercado y documentos (OCR) usando
Claude (Anthropic), con datos multi-tenant (una organización por cliente). Este documento
es la referencia para integrar un frontend externo contra esta API.

- **Documentación interactiva (Swagger):** `GET /docs` sobre la URL del backend.
- **Esquema OpenAPI:** `GET /openapi.json`.

## 1. Autenticación

Hay dos mecanismos, pensados para casos distintos:

| Mecanismo | Uso recomendado | Cómo se obtiene |
|---|---|---|
| **Cookie de sesión** | Un frontend que sirve login/signup a usuarios finales (admins, analistas, reponedores) | `POST /api/auth/login` o `/api/auth/signup` — la API responde con una cookie `neuravision_session` (httpOnly, 30 días) |
| **API Key** (header `X-API-Key`) | Integraciones server-to-server: un backend propio del equipo de frontend que consume NeuraVision sin manejar sesiones de usuario | Un administrador de la organización la genera desde `POST /api/admin/api-keys` |

Ambos mecanismos resuelven a un "actor" con `org_id` — **todos los datos están aislados por
organización**, nunca se filtran entre clientes.

### Niveles de acceso (roles)

- `superusuario` / `admin`: gestión de usuarios, API keys, presupuesto, todo lo demás.
- `analista`: lectura/escritura de góndolas, OCR, reportería. No gestiona usuarios ni API keys.
- `reponedor`: solo los puntos de venta que se le asignen.
- Las **API keys** siempre actúan con permisos equivalentes a `analista` — nunca pueden
  gestionar usuarios, API keys ni presupuesto, aunque se use la key de un superusuario.

### Ejemplo — login y uso de la cookie

```bash
curl -c cookies.txt -X POST https://<host>/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@cliente.cl", "password": "..."}'

curl -b cookies.txt https://<host>/api/points
```

### Ejemplo — generar y usar una API key

```bash
# Como admin autenticado por cookie:
curl -b cookies.txt -X POST https://<host>/api/admin/api-keys \
  -H "Content-Type: application/json" \
  -d '{"label": "frontend-equipo-x"}'
# -> { "api_key": "nv_...", ... }  — el valor completo SOLO se muestra esta vez, guárdenlo ya.

curl -H "X-API-Key: nv_..." https://<host>/api/points
```

Si se pierde o se compromete una API key, revóquenla con
`DELETE /api/admin/api-keys/{key_id}` y generen una nueva — no hay forma de recuperar el
valor original.

## 2. CORS

Si el frontend llama a esta API **desde el navegador con cookie de sesión**, su origen debe
estar en la variable de entorno `ALLOWED_ORIGINS` del backend (coordinar con el equipo de
backend para agregarlo). Si el frontend llama **server-to-server con `X-API-Key`** (su propio
backend hace la llamada, no el navegador del usuario final), CORS no aplica.

## 3. Rate limiting

- Endpoints generales: 60 req/min por IP o por API key.
- Endpoints que llaman a Claude (`/analyze`, `/ocr/voucher`, `/ocr/client-form`): 20 req/min.
- Al exceder el límite: `429` con header `Retry-After`.

## 4. Endpoints principales

### Góndolas y análisis con IA

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/points/{point_id}/analyze` | Sube 1-6 fotos (`images`, multipart), analiza con Claude (góndola / exhibición adicional / vitrina a granel vía `reading_type`), persiste la lectura |
| `GET` | `/api/points` | Lista puntos de venta con su última lectura |
| `GET` | `/api/points/{point_id}` | Detalle de un punto con historial de lecturas |
| `GET` | `/api/points/{point_id}/daily-metrics` | Métricas diarias agregadas |
| `GET` | `/api/points/{point_id}/replenishment` | Señales de quiebre de stock recurrente |
| `GET` | `/api/analytics` | Tabla de hechos + resumen por punto, para dashboards |
| `GET` | `/api/export.csv` | Export CSV, una fila por producto por lectura |

`POST /api/points/{point_id}/analyze` responde `422` si la foto no corresponde al tipo
esperado (ej. no es una góndola), `413` si una imagen supera el límite de tamaño (15 MB por
defecto), y **`402`** si la organización superó su presupuesto mensual de IA (ver sección 6).

### OCR (comprobantes y fichas de cliente)

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/ocr/voucher` | Lee un comprobante de depósito/transferencia |
| `POST` | `/api/ocr/client-form` | Prellena datos de un prospecto desde un documento |
| `GET` | `/api/ocr/{voucher\|client-form}/history` | Historial de extracciones |

### Uso, costo y presupuesto (ver sección 6 para el detalle)

| Método | Ruta | Rol mínimo |
|---|---|---|
| `GET` | `/api/usage/summary` | `analista` / API key |
| `GET` | `/api/org` | `admin` — incluye `current_month_spend_usd` |
| `PUT` | `/api/admin/org/budget` | `admin` |
| `GET`/`POST`/`DELETE` | `/api/admin/api-keys` | `admin` |

### Otros módulos (ver `/docs` para el detalle completo)

Góndolas/planogramas, acuerdos comerciales, finanzas (conciliación de comprobantes),
clientes, administración de usuarios/organización — todos bajo `/api/...`, documentados en
Swagger.

## 5. Formato de errores

Todas las respuestas de error son JSON `{"detail": "mensaje en español"}` con el código HTTP
correspondiente: `400` validación, `401` sin autenticación, `402` presupuesto agotado, `403`
sin permiso, `404` no encontrado, `413` imagen muy grande, `422` imagen no válida para el tipo
de análisis, `429` rate limit, `502` error llamando a Claude.

## 6. Costo y consumo de IA (tokens)

Cada llamada a Claude (análisis de góndola u OCR) registra modelo, tokens de entrada/salida y
costo estimado en USD, por usuario/API key y por organización.

```bash
curl -H "X-API-Key: nv_..." "https://<host>/api/usage/summary?date_from=2026-10-01"
```

```json
{
  "total_calls": 42,
  "total_cost_usd": 1.8734,
  "total_input_tokens": 530000,
  "total_output_tokens": 95000,
  "monthly_budget_usd": 25.0,
  "current_month_spend_usd": 1.8734,
  "by_user": [...],
  "by_endpoint": [...],
  "by_day": [...]
}
```

Un administrador puede fijar un **tope de gasto mensual duro** por organización
(`PUT /api/admin/org/budget`, `{"monthly_budget_usd": 25.0}`). Al alcanzarlo, toda nueva
llamada a `/analyze` o `/ocr/*` responde `402 Payment Required` hasta que empiece el mes
siguiente o un admin suba el tope. `monthly_budget_usd: null` significa sin tope.

## 7. Modelo de IA y precios (referencia)

El modelo configurado (`ANTHROPIC_MODEL`, por defecto `claude-sonnet-5`) cuesta $2 USD por
millón de tokens de entrada y $10 USD por millón de tokens de salida. Una lectura típica de
góndola (1-3 fotos) consume aproximadamente 2,000-6,000 tokens de entrada y 500-2,000 de
salida — del orden de $0.01-$0.03 USD por lectura.

## 8. Seguridad

- Las imágenes subidas (`/uploads/{org_id}/...`) requieren la misma autenticación que el resto
  de la API (cookie o `X-API-Key`) y verifican que el solicitante pertenezca a esa
  organización — no son archivos públicos. Si el frontend las muestra con `<img src="...">`
  en el mismo dominio, la cookie de sesión viaja automáticamente; si se consumen
  server-to-server, hay que pasar `X-API-Key` también al pedir la imagen, no solo al llamar
  a `/api/...`.
- Todas las claves (Anthropic, sesión) viven solo en variables de entorno del backend; nunca
  se exponen en respuestas de la API.
- El rate limiting actual es en memoria del proceso: válido para una sola instancia. Si el
  backend se escala horizontalmente, debe migrarse a un store compartido (Redis).
