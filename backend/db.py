import json
import os
import random
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "data.db"

# Altura promedio asumida por nivel de estante (metros). Es un supuesto de referencia,
# no una medición: la altura real de cada góndola varía por retailer y categoría.
LEVEL_HEIGHT_M = 0.35

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  role TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (org_id) REFERENCES organizations(id)
);
CREATE TABLE IF NOT EXISTS points (
  org_id TEXT NOT NULL,
  id TEXT NOT NULL,
  name TEXT NOT NULL,
  region TEXT,
  comuna TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (org_id, id)
);
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id TEXT NOT NULL,
  point_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  total_facings INTEGER,
  shelf_levels_detected INTEGER,
  empty_space_pct REAL,
  price_visibility_score REAL,
  exhibition_score REAL,
  organization_score REAL,
  products_json TEXT,
  categories_json TEXT,
  notes TEXT,
  image_paths_json TEXT,
  linear_meters REAL
);
CREATE TABLE IF NOT EXISTS own_brands (
  org_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (org_id, name)
);
CREATE TABLE IF NOT EXISTS shelves (
  org_id TEXT NOT NULL,
  id TEXT NOT NULL,
  point_id TEXT NOT NULL,
  name TEXT NOT NULL,
  shelf_type TEXT,
  description TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (org_id, id)
);
CREATE TABLE IF NOT EXISTS planogram_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id TEXT NOT NULL,
  shelf_id TEXT NOT NULL,
  product TEXT NOT NULL,
  brand TEXT,
  category TEXT,
  expected_facings INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id TEXT NOT NULL,
  reading_id INTEGER NOT NULL,
  product_label TEXT,
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  corrected_by TEXT NOT NULL,
  corrected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS point_assignments (
  org_id TEXT NOT NULL,
  point_id TEXT NOT NULL,
  email TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (org_id, point_id, email)
);
"""

VALID_ROLES = ("superusuario", "admin", "analista", "reponedor")
ADMIN_ROLES = ("superusuario", "admin")
ANALYST_ROLES = ("superusuario", "admin", "analista")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _has_column(conn, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def init_db():
    with get_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        # El esquema single-tenant anterior (users.username como PK, sin org_id)
        # es incompatible con el modelo multi-tenant: no se puede migrar celda a
        # celda porque faltan columnas estructurales (org_id, email como PK).
        # No hay datos reales dependiendo de ese esquema, así que en vez de una
        # migración parcial que rompería a mitad de camino, se reemplaza limpio.
        if "users" in tables and not _has_column(conn, "users", "org_id"):
            for legacy_table in ("readings", "points", "users", "own_brands"):
                conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")

        conn.executescript(SCHEMA)
        # Columnas agregadas después del primer despliegue multi-tenant: ADD COLUMN
        # es un no-op seguro cuando la columna ya existe.
        for statement in (
            "ALTER TABLE points ADD COLUMN region TEXT",
            "ALTER TABLE points ADD COLUMN comuna TEXT",
            "ALTER TABLE readings ADD COLUMN price_visibility_score REAL",
            "ALTER TABLE readings ADD COLUMN exhibition_score REAL",
            "ALTER TABLE readings ADD COLUMN organization_score REAL",
            "ALTER TABLE readings ADD COLUMN shelf_id TEXT",
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def slugify_org_name(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "empresa"
    return base[:40]


def _unique_org_id(conn, base_slug: str) -> str:
    candidate = base_slug
    while conn.execute("SELECT 1 FROM organizations WHERE id = ?", (candidate,)).fetchone():
        candidate = f"{base_slug}-{secrets.token_hex(3)}"
    return candidate


def create_organization(name: str) -> dict:
    with get_conn() as conn:
        org_id = _unique_org_id(conn, slugify_org_name(name))
        created_at = now_iso()
        conn.execute(
            "INSERT INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
            (org_id, name, created_at),
        )
    return {"id": org_id, "name": name, "created_at": created_at}


def get_organization(org_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    return dict(row) if row else None


def rename_organization(org_id: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE organizations SET name = ? WHERE id = ?", (name, org_id))


# ---------- usuarios ----------

def email_exists(email: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    return row is not None


def create_user(org_id: str, email: str, password_hash: str, salt: str, role: str) -> dict:
    with get_conn() as conn:
        created_at = now_iso()
        cur = conn.execute(
            "INSERT INTO users (org_id, email, password_hash, salt, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (org_id, email, password_hash, salt, role, created_at),
        )
    return {"id": cur.lastrowid, "org_id": org_id, "email": email, "role": role, "created_at": created_at}


def get_user_by_email(email: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def list_users(org_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT email, role, created_at FROM users WHERE org_id = ? ORDER BY created_at", (org_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_role(org_id: str, roles: tuple) -> int:
    placeholders = ",".join("?" for _ in roles)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM users WHERE org_id = ? AND role IN ({placeholders})",
            (org_id, *roles),
        ).fetchone()
    return row["c"]


def update_user_role(org_id: str, email: str, role: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET role = ? WHERE org_id = ? AND email = ?", (role, org_id, email))


def delete_user(org_id: str, email: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE org_id = ? AND email = ?", (org_id, email))


def user_in_org(org_id: str, email: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE org_id = ? AND email = ?", (org_id, email)
        ).fetchone()
    return dict(row) if row else None


# ---------- marcas propias ----------

def list_own_brands(org_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM own_brands WHERE org_id = ? ORDER BY name", (org_id,)
        ).fetchall()
    return [r["name"] for r in rows]


def add_own_brand(org_id: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO own_brands (org_id, name, created_at) VALUES (?, ?, ?)",
            (org_id, name, now_iso()),
        )


def delete_own_brand(org_id: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM own_brands WHERE org_id = ? AND name = ?", (org_id, name))


def _is_own_brand(product: dict, own_brands: list) -> bool:
    haystack = f"{product.get('brand', '')} {product.get('product', '')}".lower()
    return any(b.lower() in haystack for b in own_brands if b.strip())


def _tag_products(products: list, own_brands: list) -> list:
    if not own_brands:
        return [{**p, "is_own_brand": False} for p in products]
    return [{**p, "is_own_brand": _is_own_brand(p, own_brands)} for p in products]


def performance_label(score) -> str:
    if score is None:
        return None
    if score >= 85:
        return "Excelente"
    if score >= 65:
        return "Bueno"
    return "Necesita atención"


def _score_summary(availability_score, price_visibility_score, exhibition_score, organization_score) -> dict:
    parts = [
        v for v in (availability_score, price_visibility_score, exhibition_score, organization_score)
        if v is not None
    ]
    score_total = round(sum(parts) / len(parts), 1) if parts else None
    return {
        "availability_score": availability_score,
        "price_visibility_score": price_visibility_score,
        "exhibition_score": exhibition_score,
        "organization_score": organization_score,
        "score_total": score_total,
        "performance_label": performance_label(score_total),
    }


def _benchmark_summary(products: list) -> dict:
    own_facings = sum(p.get("facings", 0) or 0 for p in products if p.get("is_own_brand"))
    competitor_facings = sum(p.get("facings", 0) or 0 for p in products if not p.get("is_own_brand"))
    total = own_facings + competitor_facings
    return {
        "own_facings": own_facings,
        "competitor_facings": competitor_facings,
        "own_share_pct": round(own_facings / total * 100, 1) if total else None,
        "own_product_count": sum(1 for p in products if p.get("is_own_brand")),
        "competitor_product_count": sum(1 for p in products if not p.get("is_own_brand")),
    }


# ---------- góndolas (estantes nombrados dentro de un PDV) ----------

def next_shelf_id(org_id: str, point_id: str) -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM shelves WHERE org_id = ? AND point_id = ?", (org_id, point_id)
        ).fetchall()
    max_n = 0
    for row in rows:
        try:
            n = int(row["id"].split("-G")[-1])
        except ValueError:
            continue
        max_n = max(max_n, n)
    return f"{point_id}-G{max_n + 1:02d}"


def create_shelf(org_id: str, point_id: str, name: str, shelf_type: str = None, description: str = None) -> dict:
    shelf_id = next_shelf_id(org_id, point_id)
    created_at = now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO shelves (org_id, id, point_id, name, shelf_type, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (org_id, shelf_id, point_id, name, shelf_type, description, created_at),
        )
    return {
        "id": shelf_id, "point_id": point_id, "name": name,
        "shelf_type": shelf_type, "description": description, "created_at": created_at,
    }


def list_shelves(org_id: str, point_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM shelves WHERE org_id = ? AND point_id = ? ORDER BY created_at", (org_id, point_id)
        ).fetchall()
    return [dict(r) for r in rows]


def get_shelf(org_id: str, shelf_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM shelves WHERE org_id = ? AND id = ?", (org_id, shelf_id)
        ).fetchone()
    return dict(row) if row else None


def shelf_exists(org_id: str, point_id: str, shelf_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM shelves WHERE org_id = ? AND point_id = ? AND id = ?", (org_id, point_id, shelf_id)
        ).fetchone()
    return row is not None


def delete_shelf(org_id: str, shelf_id: str) -> None:
    # Las lecturas históricas de esta góndola se conservan; solo pierden la etiqueta
    # de góndola, para no destruir el historial de levantamientos ya hechos.
    with get_conn() as conn:
        conn.execute("UPDATE readings SET shelf_id = NULL WHERE org_id = ? AND shelf_id = ?", (org_id, shelf_id))
        conn.execute("DELETE FROM planogram_items WHERE org_id = ? AND shelf_id = ?", (org_id, shelf_id))
        conn.execute("DELETE FROM shelves WHERE org_id = ? AND id = ?", (org_id, shelf_id))


def _shelf_names_by_id(org_id: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, shelf_type FROM shelves WHERE org_id = ?", (org_id,)).fetchall()
    return {r["id"]: {"name": r["name"], "shelf_type": r["shelf_type"]} for r in rows}


# ---------- planograma (productos esperados por góndola) ----------

def add_planogram_item(org_id: str, shelf_id: str, product: str, brand: str = None,
                        category: str = None, expected_facings: int = None) -> dict:
    created_at = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO planogram_items (org_id, shelf_id, product, brand, category, expected_facings, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (org_id, shelf_id, product, brand, category, expected_facings, created_at),
        )
    return {
        "id": cur.lastrowid, "shelf_id": shelf_id, "product": product, "brand": brand,
        "category": category, "expected_facings": expected_facings, "created_at": created_at,
    }


def list_planogram_items(org_id: str, shelf_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM planogram_items WHERE org_id = ? AND shelf_id = ? ORDER BY created_at",
            (org_id, shelf_id),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_planogram_item(org_id: str, item_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM planogram_items WHERE org_id = ? AND id = ?", (org_id, item_id))


def _planogram_items_by_shelf(org_id: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM planogram_items WHERE org_id = ?", (org_id,)).fetchall()
    by_shelf = {}
    for r in rows:
        by_shelf.setdefault(r["shelf_id"], []).append(dict(r))
    return by_shelf


def _match_product(item: dict, products: list):
    needle = (item.get("product") or "").strip().lower()
    brand_needle = (item.get("brand") or "").strip().lower()
    for p in products:
        haystack = (p.get("product") or "").strip().lower()
        brand_hay = (p.get("brand") or "").strip().lower()
        name_match = needle and (needle in haystack or haystack in needle)
        brand_match = (not brand_needle) or (brand_needle in brand_hay or brand_hay in brand_needle)
        if name_match and brand_match:
            return p
    return None


def _planogram_gaps(items: list, products: list) -> list:
    """Compara el planograma (lo que debería estar) contra lo detectado en la foto.
    No es un match exacto de SKU: es texto libre de producto/marca, así que puede haber
    falsos positivos/negativos si la IA describe el producto distinto al planograma."""
    gaps = []
    for item in items:
        match = _match_product(item, products)
        expected = item.get("expected_facings")
        if not match:
            gaps.append({
                "product": item.get("product"), "brand": item.get("brand"), "category": item.get("category"),
                "expected_facings": expected, "detected_facings": 0, "status": "no_detectado",
            })
        elif match.get("out_of_stock"):
            gaps.append({
                "product": item.get("product"), "brand": item.get("brand"), "category": item.get("category"),
                "expected_facings": expected, "detected_facings": 0, "status": "quiebre_de_stock",
            })
        elif expected and (match.get("facings") or 0) < expected:
            gaps.append({
                "product": item.get("product"), "brand": item.get("brand"), "category": item.get("category"),
                "expected_facings": expected, "detected_facings": match.get("facings") or 0, "status": "bajo_lo_esperado",
            })
    return gaps


# ---------- puntos de venta ----------

def point_exists(org_id: str, point_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM points WHERE org_id = ? AND id = ?", (org_id, point_id)
        ).fetchone()
    return row is not None


def next_point_id(org_id: str) -> str:
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM points WHERE org_id = ?", (org_id,)).fetchall()
    max_n = 0
    for row in rows:
        try:
            n = int(row["id"].split("-")[-1])
        except ValueError:
            continue
        max_n = max(max_n, n)
    return f"PDV-{max_n + 1:03d}"


def create_point(org_id: str, point_id: str, name: str, region: str = None, comuna: str = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO points (org_id, id, name, region, comuna, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (org_id, point_id, name, region, comuna, now_iso()),
        )


def add_reading(
    org_id: str,
    point_id: str,
    analysis: dict,
    image_paths: list,
    linear_meters: float = None,
    shelf_id: str = None,
) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO readings (org_id, point_id, created_at, total_facings, shelf_levels_detected, "
            "empty_space_pct, price_visibility_score, exhibition_score, organization_score, "
            "products_json, categories_json, notes, image_paths_json, linear_meters, shelf_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                org_id,
                point_id,
                now_iso(),
                analysis.get("total_facings"),
                analysis.get("shelf_levels_detected"),
                analysis.get("empty_space_pct"),
                analysis.get("price_visibility_score"),
                analysis.get("exhibition_score"),
                analysis.get("organization_score"),
                json.dumps(analysis.get("products", [])),
                json.dumps(analysis.get("categories", [])),
                analysis.get("notes", ""),
                json.dumps(image_paths or []),
                linear_meters,
                shelf_id,
            ),
        )
        reading_id = cur.lastrowid
        row = conn.execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()
    shelves = _shelf_names_by_id(org_id)
    planograms = _planogram_items_by_shelf(org_id)
    return _reading_dict(row, list_own_brands(org_id), shelves, planograms)


def _reading_dict(row: sqlite3.Row, own_brands: list = None, shelves: dict = None, planograms: dict = None) -> dict:
    paths_json = row["image_paths_json"]
    paths = json.loads(paths_json) if paths_json else []

    products = _tag_products(json.loads(row["products_json"] or "[]"), own_brands or [])
    linear_meters = row["linear_meters"]
    total_facings = row["total_facings"] or 0
    levels = row["shelf_levels_detected"] or 0

    vertical_meters = round(levels * LEVEL_HEIGHT_M, 2) if levels else None
    display_area_m2 = round(linear_meters * vertical_meters, 2) if linear_meters and vertical_meters else None
    total_units_estimate = sum(
        (p.get("facings") or 0) * (p.get("estimated_depth") or 1) for p in products
    ) if products else None
    empty_space_pct = row["empty_space_pct"]
    availability_score = round(100 - empty_space_pct, 1) if empty_space_pct is not None else None

    shelf_id = row["shelf_id"] if "shelf_id" in row.keys() else None
    shelf_meta = (shelves or {}).get(shelf_id) if shelf_id else None
    planogram_items = (planograms or {}).get(shelf_id, []) if shelf_id else []
    planogram_gaps = _planogram_gaps(planogram_items, products) if planogram_items else []

    return {
        "id": row["id"],
        "point_id": row["point_id"],
        "created_at": row["created_at"],
        "shelf_id": shelf_id,
        "shelf_name": shelf_meta["name"] if shelf_meta else None,
        "shelf_type": shelf_meta["shelf_type"] if shelf_meta else None,
        "total_facings": row["total_facings"],
        "shelf_levels_detected": row["shelf_levels_detected"],
        "empty_space_pct": empty_space_pct,
        "products": products,
        "categories": json.loads(row["categories_json"] or "[]"),
        "notes": row["notes"],
        "image_urls": [f"/uploads/{p}" for p in paths],
        "benchmark": _benchmark_summary(products),
        "linear_meters": linear_meters,
        "facings_per_linear_meter": round(total_facings / linear_meters, 1) if linear_meters else None,
        "vertical_meters": vertical_meters,
        "display_area_m2": display_area_m2,
        "total_units_estimate": total_units_estimate,
        "has_planogram": bool(planogram_items),
        "planogram_gaps": planogram_gaps,
        **_score_summary(availability_score, row["price_visibility_score"], row["exhibition_score"], row["organization_score"]),
    }


def list_points_with_latest(org_id: str, allowed_point_ids: set = None) -> list:
    own_brands = list_own_brands(org_id)
    shelves = _shelf_names_by_id(org_id)
    planograms = _planogram_items_by_shelf(org_id)
    with get_conn() as conn:
        points = conn.execute(
            "SELECT * FROM points WHERE org_id = ? ORDER BY id", (org_id,)
        ).fetchall()
        if allowed_point_ids is not None:
            points = [p for p in points if p["id"] in allowed_point_ids]
        result = []
        for p in points:
            latest_row = conn.execute(
                "SELECT * FROM readings WHERE org_id = ? AND point_id = ? ORDER BY id DESC LIMIT 1",
                (org_id, p["id"]),
            ).fetchone()
            count_row = conn.execute(
                "SELECT COUNT(*) AS c FROM readings WHERE org_id = ? AND point_id = ?",
                (org_id, p["id"]),
            ).fetchone()
            recent_rows = conn.execute(
                "SELECT total_facings, products_json FROM readings WHERE org_id = ? AND point_id = ? "
                "ORDER BY id DESC LIMIT 8",
                (org_id, p["id"]),
            ).fetchall()
            recent_rows = list(reversed(recent_rows))
            recent_facings = [r["total_facings"] for r in recent_rows]
            recent_own_share = [
                _benchmark_summary(_tag_products(json.loads(r["products_json"] or "[]"), own_brands))["own_share_pct"]
                for r in recent_rows
            ]
            result.append(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "region": p["region"],
                    "comuna": p["comuna"],
                    "created_at": p["created_at"],
                    "readings_count": count_row["c"],
                    "latest": _reading_dict(latest_row, own_brands, shelves, planograms) if latest_row else None,
                    "recent_facings": recent_facings,
                    "recent_own_share": recent_own_share,
                }
            )
    return result


def get_point(org_id: str, point_id: str):
    own_brands = list_own_brands(org_id)
    shelves = _shelf_names_by_id(org_id)
    planograms = _planogram_items_by_shelf(org_id)
    with get_conn() as conn:
        p = conn.execute(
            "SELECT * FROM points WHERE org_id = ? AND id = ?", (org_id, point_id)
        ).fetchone()
        if not p:
            return None
        readings = conn.execute(
            "SELECT * FROM readings WHERE org_id = ? AND point_id = ? ORDER BY id DESC",
            (org_id, point_id),
        ).fetchall()
    return {
        "id": p["id"],
        "name": p["name"],
        "region": p["region"],
        "comuna": p["comuna"],
        "created_at": p["created_at"],
        "shelves": list_shelves(org_id, point_id),
        "readings": [_reading_dict(r, own_brands, shelves, planograms) for r in readings],
    }


def delete_point(org_id: str, point_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM readings WHERE org_id = ? AND point_id = ?", (org_id, point_id))
        conn.execute("DELETE FROM points WHERE org_id = ? AND id = ?", (org_id, point_id))
        conn.execute("DELETE FROM shelves WHERE org_id = ? AND point_id = ?", (org_id, point_id))
        conn.execute("DELETE FROM point_assignments WHERE org_id = ? AND point_id = ?", (org_id, point_id))


# ---------- corrección manual de productos (con auditoría) ----------

CORRECTABLE_FIELDS = ("product", "brand", "category", "facings", "out_of_stock")


def get_reading_for_point(org_id: str, point_id: str, reading_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM readings WHERE org_id = ? AND point_id = ? AND id = ?",
            (org_id, point_id, reading_id),
        ).fetchone()
    return row


def correct_product(org_id: str, point_id: str, reading_id: int, product_index: int,
                     updates: dict, corrected_by: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM readings WHERE org_id = ? AND point_id = ? AND id = ?",
            (org_id, point_id, reading_id),
        ).fetchone()
        if not row:
            return None
        products = json.loads(row["products_json"] or "[]")
        if product_index < 0 or product_index >= len(products):
            return None

        product = products[product_index]
        label = f"{product.get('brand', '')} {product.get('product', '')}".strip() or f"producto #{product_index}"
        corrected_at = now_iso()
        changed = False
        for field in CORRECTABLE_FIELDS:
            if field not in updates:
                continue
            old_value = product.get(field)
            new_value = updates[field]
            if old_value == new_value:
                continue
            changed = True
            product[field] = new_value
            conn.execute(
                "INSERT INTO product_corrections (org_id, reading_id, product_label, field, old_value, "
                "new_value, corrected_by, corrected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (org_id, reading_id, label, field, json.dumps(old_value), json.dumps(new_value),
                 corrected_by, corrected_at),
            )

        if changed:
            products[product_index] = product
            conn.execute(
                "UPDATE readings SET products_json = ? WHERE org_id = ? AND id = ?",
                (json.dumps(products), org_id, reading_id),
            )
            row = conn.execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()

    shelves = _shelf_names_by_id(org_id)
    planograms = _planogram_items_by_shelf(org_id)
    return _reading_dict(row, list_own_brands(org_id), shelves, planograms)


def list_corrections(org_id: str, reading_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM product_corrections WHERE org_id = ? AND reading_id = ? ORDER BY corrected_at DESC",
            (org_id, reading_id),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["old_value"] = json.loads(d["old_value"]) if d["old_value"] is not None else None
        d["new_value"] = json.loads(d["new_value"]) if d["new_value"] is not None else None
        out.append(d)
    return out


# ---------- asignación de puntos de venta a usuarios ----------

def set_point_assignments(org_id: str, email: str, point_ids: list) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM point_assignments WHERE org_id = ? AND email = ?", (org_id, email))
        created_at = now_iso()
        for pid in point_ids:
            conn.execute(
                "INSERT OR IGNORE INTO point_assignments (org_id, point_id, email, created_at) VALUES (?, ?, ?, ?)",
                (org_id, pid, email, created_at),
            )


def list_assigned_point_ids(org_id: str, email: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT point_id FROM point_assignments WHERE org_id = ? AND email = ?", (org_id, email)
        ).fetchall()
    return [r["point_id"] for r in rows]


def allowed_point_ids_for_user(org_id: str, user: dict):
    """None = sin restricción (ve todos los puntos). Un set = restringido a esos IDs.
    Un reponedor/analista sin asignaciones todavía ve todo, para no romper cuentas
    existentes que nunca configuraron asignaciones."""
    if user["role"] in ADMIN_ROLES:
        return None
    assigned = list_assigned_point_ids(org_id, user["email"])
    if not assigned:
        return None
    return set(assigned)


def export_rows(org_id: str, point_id: str = None) -> list:
    """Una fila por producto por lectura, para exportar a Excel/Power BI/Looker."""
    own_brands = list_own_brands(org_id)
    with get_conn() as conn:
        if point_id:
            readings = conn.execute(
                "SELECT r.*, p.name AS point_name FROM readings r JOIN points p "
                "ON p.org_id = r.org_id AND p.id = r.point_id "
                "WHERE r.org_id = ? AND r.point_id = ? ORDER BY r.id",
                (org_id, point_id),
            ).fetchall()
        else:
            readings = conn.execute(
                "SELECT r.*, p.name AS point_name FROM readings r JOIN points p "
                "ON p.org_id = r.org_id AND p.id = r.point_id "
                "WHERE r.org_id = ? ORDER BY r.id",
                (org_id,),
            ).fetchall()

    rows = []
    for r in readings:
        products = _tag_products(json.loads(r["products_json"] or "[]"), own_brands)
        if not products:
            continue
        for p in products:
            estimated_depth = p.get("estimated_depth") or 1
            rows.append(
                {
                    "point_id": r["point_id"],
                    "point_name": r["point_name"],
                    "reading_id": r["id"],
                    "created_at": r["created_at"],
                    "product": p.get("product"),
                    "brand": p.get("brand"),
                    "category": p.get("category"),
                    "facings": p.get("facings"),
                    "shelf_level": p.get("shelf_level"),
                    "position_index": p.get("position_index"),
                    "out_of_stock": p.get("out_of_stock"),
                    "is_own_brand": p.get("is_own_brand"),
                    "estimated_depth": estimated_depth,
                    "units_estimate": (p.get("facings") or 0) * estimated_depth,
                    "reading_total_facings": r["total_facings"],
                    "reading_empty_space_pct": r["empty_space_pct"],
                    "reading_shelf_levels": r["shelf_levels_detected"],
                    "reading_linear_meters": r["linear_meters"],
                }
            )
    return rows


def daily_metrics(org_id: str, point_id: str) -> list:
    own_brands = list_own_brands(org_id)
    with get_conn() as conn:
        readings = conn.execute(
            "SELECT * FROM readings WHERE org_id = ? AND point_id = ? ORDER BY id", (org_id, point_id)
        ).fetchall()

    by_day = {}
    for r in readings:
        day = r["created_at"][:10]
        products = _tag_products(json.loads(r["products_json"] or "[]"), own_brands)
        bench = _benchmark_summary(products)
        bucket = by_day.setdefault(
            day, {"date": day, "readings": 0, "facings_sum": 0, "empty_space_sum": 0.0,
                  "empty_space_n": 0, "own_share_sum": 0.0, "own_share_n": 0}
        )
        bucket["readings"] += 1
        bucket["facings_sum"] += r["total_facings"] or 0
        if r["empty_space_pct"] is not None:
            bucket["empty_space_sum"] += r["empty_space_pct"]
            bucket["empty_space_n"] += 1
        if bench["own_share_pct"] is not None:
            bucket["own_share_sum"] += bench["own_share_pct"]
            bucket["own_share_n"] += 1

    result = []
    for day, b in sorted(by_day.items()):
        result.append(
            {
                "date": day,
                "readings": b["readings"],
                "avg_total_facings": round(b["facings_sum"] / b["readings"], 1) if b["readings"] else None,
                "avg_empty_space_pct": round(b["empty_space_sum"] / b["empty_space_n"], 1) if b["empty_space_n"] else None,
                "avg_own_share_pct": round(b["own_share_sum"] / b["own_share_n"], 1) if b["own_share_n"] else None,
            }
        )
    return result


def replenishment_signals(org_id: str, point_id: str, lookback: int = 10) -> dict:
    own_brands = list_own_brands(org_id)
    with get_conn() as conn:
        readings = conn.execute(
            "SELECT * FROM readings WHERE org_id = ? AND point_id = ? ORDER BY id DESC LIMIT ?",
            (org_id, point_id, lookback),
        ).fetchall()
    readings = list(reversed(readings))

    empty_space_trend = [
        {"date": r["created_at"][:10], "created_at": r["created_at"], "empty_space_pct": r["empty_space_pct"]}
        for r in readings
    ]
    empty_vals = [r["empty_space_pct"] for r in readings if r["empty_space_pct"] is not None]
    avg_empty = round(sum(empty_vals) / len(empty_vals), 1) if empty_vals else None
    trend_delta = None
    if len(empty_vals) >= 2:
        trend_delta = round(empty_vals[-1] - empty_vals[0], 1)

    stockout_counts = {}
    seen_counts = {}
    product_meta = {}
    for r in readings:
        products = _tag_products(json.loads(r["products_json"] or "[]"), own_brands)
        for p in products:
            key = (p.get("brand") or "").strip().lower() + "|" + (p.get("product") or "").strip().lower()
            seen_counts[key] = seen_counts.get(key, 0) + 1
            product_meta[key] = p
            if p.get("out_of_stock"):
                stockout_counts[key] = stockout_counts.get(key, 0) + 1

    recurring = []
    for key, times_out in stockout_counts.items():
        times_seen = seen_counts.get(key, 1)
        meta = product_meta[key]
        recurring.append(
            {
                "product": meta.get("product"),
                "brand": meta.get("brand"),
                "category": meta.get("category"),
                "is_own_brand": meta.get("is_own_brand"),
                "times_out_of_stock": times_out,
                "times_seen": times_seen,
                "urgency_pct": round(times_out / times_seen * 100, 0),
            }
        )
    recurring.sort(key=lambda x: (-x["urgency_pct"], -x["times_out_of_stock"]))

    return {
        "empty_space_trend": empty_space_trend,
        "avg_empty_space_pct": avg_empty,
        "empty_space_trend_delta": trend_delta,
        "recurring_stockouts": recurring,
    }


def get_analytics(org_id: str, allowed_point_ids: set = None) -> dict:
    """Tabla de hechos (una fila por lectura) + resumen por punto, para el módulo de Reportería."""
    shelves = _shelf_names_by_id(org_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT r.id, r.point_id, p.name AS point_name, p.region AS region, p.comuna AS comuna, "
            "r.created_at, r.empty_space_pct, r.price_visibility_score, r.exhibition_score, "
            "r.organization_score, r.shelf_id FROM readings r JOIN points p "
            "ON p.org_id = r.org_id AND p.id = r.point_id WHERE r.org_id = ? ORDER BY r.created_at",
            (org_id,),
        ).fetchall()
    if allowed_point_ids is not None:
        rows = [r for r in rows if r["point_id"] in allowed_point_ids]

    readings = []
    by_point = {}
    for r in rows:
        availability_score = round(100 - r["empty_space_pct"], 1) if r["empty_space_pct"] is not None else None
        scores = _score_summary(availability_score, r["price_visibility_score"], r["exhibition_score"], r["organization_score"])
        shelf_meta = shelves.get(r["shelf_id"]) if r["shelf_id"] else None
        entry = {
            "reading_id": r["id"],
            "point_id": r["point_id"],
            "point_name": r["point_name"],
            "region": r["region"],
            "comuna": r["comuna"],
            "created_at": r["created_at"],
            "shelf_id": r["shelf_id"],
            "shelf_name": shelf_meta["name"] if shelf_meta else None,
            **scores,
        }
        readings.append(entry)

        bucket = by_point.setdefault(
            r["point_id"],
            {"id": r["point_id"], "name": r["point_name"], "region": r["region"], "comuna": r["comuna"],
             "readings_count": 0, "score_sum": 0.0, "score_n": 0, "last_scan": None},
        )
        bucket["readings_count"] += 1
        if scores["score_total"] is not None:
            bucket["score_sum"] += scores["score_total"]
            bucket["score_n"] += 1
        if not bucket["last_scan"] or r["created_at"] > bucket["last_scan"]:
            bucket["last_scan"] = r["created_at"]

    points_summary = []
    for bucket in by_point.values():
        avg_score = round(bucket["score_sum"] / bucket["score_n"], 1) if bucket["score_n"] else None
        points_summary.append(
            {
                "id": bucket["id"],
                "name": bucket["name"],
                "region": bucket["region"],
                "comuna": bucket["comuna"],
                "readings_count": bucket["readings_count"],
                "last_scan": bucket["last_scan"],
                "score_total": avg_score,
                "performance_label": performance_label(avg_score),
            }
        )
    points_summary.sort(key=lambda p: (p["score_total"] is None, -(p["score_total"] or 0)))

    return {"readings": readings, "points": points_summary}


_DEMO_POINTS = [
    ("Sucursal Providencia", "Metropolitana", "Providencia"),
    ("Sucursal Ñuñoa", "Metropolitana", "Ñuñoa"),
    ("Sucursal Viña del Mar", "Valparaíso", "Viña del Mar"),
    ("Sucursal Talca", "Maule", "Talca"),
    ("Sucursal Concepción", "Biobío", "Concepción"),
]

# (score al inicio del rango, score al final, volatilidad por lectura) — cada punto
# demo sigue una historia distinta (mejora, decae, estable) en vez de ruido plano,
# para que la tendencia y el ranking se vean como datos reales, no aleatorios.
_DEMO_PERSONAS = [
    (62, 92, 7),
    (84, 90, 5),
    (90, 66, 8),
    (70, 81, 15),
    (55, 68, 7),
]


def seed_demo_data(org_id: str) -> dict:
    """Crea puntos y lecturas sintéticas (marcadas como demo en sus notas) para que
    una organización pueda explorar Reportería antes de tener datos propios."""
    with get_conn() as conn:
        existing = [dict(p) for p in conn.execute(
            "SELECT id, name, region, comuna FROM points WHERE org_id = ?", (org_id,)
        ).fetchall()]

    points = list(existing)
    existing_names = {p["name"] for p in points}
    for name, region, comuna in _DEMO_POINTS:
        if name in existing_names:
            continue
        pid = next_point_id(org_id)
        create_point(org_id, pid, name, region, comuna)
        points.append({"id": pid, "name": name, "region": region, "comuna": comuna})

    rng = random.Random(42)
    today = datetime.now(timezone.utc)
    months = []
    for i in range(8, -1, -1):
        m, y = today.month - i, today.year
        while m <= 0:
            m += 12
            y -= 1
        months.append((y, m))
    n_months = len(months)

    def around(center, spread):
        return round(max(0, min(100, center + rng.uniform(-spread, spread))))

    inserted = 0
    with get_conn() as conn:
        # Re-generar es idempotente: se limpia el lote demo anterior antes de
        # insertar el nuevo, para no mezclar dos historias sintéticas distintas.
        conn.execute(
            "DELETE FROM readings WHERE org_id = ? AND notes = ?",
            (org_id, "Lectura de demostración"),
        )
        for idx, p in enumerate(points):
            start, end, volatility = _DEMO_PERSONAS[idx % len(_DEMO_PERSONAS)]
            for month_idx, (y, m) in enumerate(months):
                progress = month_idx / max(1, n_months - 1)
                base = start + (end - start) * progress
                for _ in range(rng.choice([1, 1, 2, 3])):
                    day = rng.randint(1, 28)
                    hour = rng.randint(8, 20)
                    created_at = f"{y:04d}-{m:02d}-{day:02d}T{hour:02d}:00:00+00:00"
                    center = max(15, min(98, base + rng.uniform(-volatility, volatility)))
                    availability_target = around(center, 10)
                    empty_space_pct = round(max(0, min(100, 100 - availability_target)), 1)
                    price_vis = around(center, 8)
                    exhibition = around(center, 10)
                    organization_score = around(center, 9)
                    total_facings = rng.randint(25, 90)
                    conn.execute(
                        "INSERT INTO readings (org_id, point_id, created_at, total_facings, shelf_levels_detected, "
                        "empty_space_pct, price_visibility_score, exhibition_score, organization_score, "
                        "products_json, categories_json, notes, image_paths_json, linear_meters) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (org_id, p["id"], created_at, total_facings, 4, empty_space_pct, price_vis, exhibition,
                         organization_score, "[]", "[]", "Lectura de demostración", "[]", 2.5),
                    )
                    inserted += 1

    return {"points_created": len(points) - len(existing), "readings_created": inserted}
