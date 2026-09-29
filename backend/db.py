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
) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO readings (org_id, point_id, created_at, total_facings, shelf_levels_detected, "
            "empty_space_pct, price_visibility_score, exhibition_score, organization_score, "
            "products_json, categories_json, notes, image_paths_json, linear_meters) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        reading_id = cur.lastrowid
        row = conn.execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()
    return _reading_dict(row, list_own_brands(org_id))


def _reading_dict(row: sqlite3.Row, own_brands: list = None) -> dict:
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

    return {
        "id": row["id"],
        "point_id": row["point_id"],
        "created_at": row["created_at"],
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
        **_score_summary(availability_score, row["price_visibility_score"], row["exhibition_score"], row["organization_score"]),
    }


def list_points_with_latest(org_id: str) -> list:
    own_brands = list_own_brands(org_id)
    with get_conn() as conn:
        points = conn.execute(
            "SELECT * FROM points WHERE org_id = ? ORDER BY id", (org_id,)
        ).fetchall()
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
                    "latest": _reading_dict(latest_row, own_brands) if latest_row else None,
                    "recent_facings": recent_facings,
                    "recent_own_share": recent_own_share,
                }
            )
    return result


def get_point(org_id: str, point_id: str):
    own_brands = list_own_brands(org_id)
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
        "readings": [_reading_dict(r, own_brands) for r in readings],
    }


def delete_point(org_id: str, point_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM readings WHERE org_id = ? AND point_id = ?", (org_id, point_id))
        conn.execute("DELETE FROM points WHERE org_id = ? AND id = ?", (org_id, point_id))


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


def get_analytics(org_id: str) -> dict:
    """Tabla de hechos (una fila por lectura) + resumen por punto, para el módulo de Reportería."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT r.id, r.point_id, p.name AS point_name, p.region AS region, p.comuna AS comuna, "
            "r.created_at, r.empty_space_pct, r.price_visibility_score, r.exhibition_score, "
            "r.organization_score FROM readings r JOIN points p "
            "ON p.org_id = r.org_id AND p.id = r.point_id WHERE r.org_id = ? ORDER BY r.created_at",
            (org_id,),
        ).fetchall()

    readings = []
    by_point = {}
    for r in rows:
        availability_score = round(100 - r["empty_space_pct"], 1) if r["empty_space_pct"] is not None else None
        scores = _score_summary(availability_score, r["price_visibility_score"], r["exhibition_score"], r["organization_score"])
        entry = {
            "reading_id": r["id"],
            "point_id": r["point_id"],
            "point_name": r["point_name"],
            "region": r["region"],
            "comuna": r["comuna"],
            "created_at": r["created_at"],
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

    inserted = 0
    with get_conn() as conn:
        for p in points:
            bias = rng.uniform(-8, 10)
            for (y, m) in months:
                for _ in range(rng.choice([1, 1, 2, 3])):
                    day = rng.randint(1, 28)
                    hour = rng.randint(8, 20)
                    created_at = f"{y:04d}-{m:02d}-{day:02d}T{hour:02d}:00:00+00:00"
                    empty_space_pct = round(max(0, min(100, rng.uniform(3, 22) - bias / 2)), 1)
                    price_vis = round(max(0, min(100, rng.uniform(65, 98) + bias)))
                    exhibition = round(max(0, min(100, rng.uniform(60, 97) + bias)))
                    organization_score = round(max(0, min(100, rng.uniform(55, 96) + bias)))
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
