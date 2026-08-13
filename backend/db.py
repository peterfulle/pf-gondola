import json
import os
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
CREATE TABLE IF NOT EXISTS points (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  point_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  total_facings INTEGER,
  shelf_levels_detected INTEGER,
  empty_space_pct REAL,
  products_json TEXT,
  categories_json TEXT,
  notes TEXT,
  image_path TEXT,
  FOREIGN KEY (point_id) REFERENCES points(id)
);
CREATE TABLE IF NOT EXISTS own_brands (
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for statement in (
            "ALTER TABLE readings ADD COLUMN image_paths_json TEXT",
            "ALTER TABLE readings ADD COLUMN linear_meters REAL",
            "ALTER TABLE readings ADD COLUMN depth_units INTEGER",
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def list_own_brands() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM own_brands ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def add_own_brand(name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO own_brands (name, created_at) VALUES (?, ?)",
            (name, now_iso()),
        )


def delete_own_brand(name: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM own_brands WHERE name = ?", (name,))


def _is_own_brand(product: dict, own_brands: list) -> bool:
    haystack = f"{product.get('brand', '')} {product.get('product', '')}".lower()
    return any(b.lower() in haystack for b in own_brands if b.strip())


def _tag_products(products: list, own_brands: list) -> list:
    if not own_brands:
        return [{**p, "is_own_brand": False} for p in products]
    return [{**p, "is_own_brand": _is_own_brand(p, own_brands)} for p in products]


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


def point_exists(point_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM points WHERE id = ?", (point_id,)).fetchone()
    return row is not None


def next_point_id() -> str:
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM points").fetchall()
    max_n = 0
    for row in rows:
        try:
            n = int(row["id"].split("-")[-1])
        except ValueError:
            continue
        max_n = max(max_n, n)
    return f"PDV-{max_n + 1:03d}"


def create_point(point_id: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO points (id, name, created_at) VALUES (?, ?, ?)",
            (point_id, name, now_iso()),
        )


def add_reading(
    point_id: str,
    analysis: dict,
    image_paths: list,
    linear_meters: float = None,
    depth_units: int = None,
) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO readings (point_id, created_at, total_facings, shelf_levels_detected, "
            "empty_space_pct, products_json, categories_json, notes, image_paths_json, linear_meters, depth_units) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                point_id,
                now_iso(),
                analysis.get("total_facings"),
                analysis.get("shelf_levels_detected"),
                analysis.get("empty_space_pct"),
                json.dumps(analysis.get("products", [])),
                json.dumps(analysis.get("categories", [])),
                analysis.get("notes", ""),
                json.dumps(image_paths or []),
                linear_meters,
                depth_units,
            ),
        )
        reading_id = cur.lastrowid
        row = conn.execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()
    return _reading_dict(row, list_own_brands())


def _reading_dict(row: sqlite3.Row, own_brands: list = None) -> dict:
    keys = row.keys()
    paths_json = row["image_paths_json"] if "image_paths_json" in keys else None
    if paths_json:
        paths = json.loads(paths_json)
    elif "image_path" in keys and row["image_path"]:
        paths = [row["image_path"]]
    else:
        paths = []

    products = _tag_products(json.loads(row["products_json"] or "[]"), own_brands or [])
    linear_meters = row["linear_meters"] if "linear_meters" in keys else None
    depth_units = row["depth_units"] if "depth_units" in keys else None
    total_facings = row["total_facings"] or 0
    levels = row["shelf_levels_detected"] or 0

    vertical_meters = round(levels * LEVEL_HEIGHT_M, 2) if levels else None
    display_area_m2 = round(linear_meters * vertical_meters, 2) if linear_meters and vertical_meters else None
    total_units_estimate = total_facings * depth_units if depth_units else None

    return {
        "id": row["id"],
        "point_id": row["point_id"],
        "created_at": row["created_at"],
        "total_facings": row["total_facings"],
        "shelf_levels_detected": row["shelf_levels_detected"],
        "empty_space_pct": row["empty_space_pct"],
        "products": products,
        "categories": json.loads(row["categories_json"] or "[]"),
        "notes": row["notes"],
        "image_urls": [f"/uploads/{p}" for p in paths],
        "benchmark": _benchmark_summary(products),
        "linear_meters": linear_meters,
        "facings_per_linear_meter": round(total_facings / linear_meters, 1) if linear_meters else None,
        "vertical_meters": vertical_meters,
        "display_area_m2": display_area_m2,
        "depth_units": depth_units,
        "total_units_estimate": total_units_estimate,
    }


def list_points_with_latest() -> list:
    own_brands = list_own_brands()
    with get_conn() as conn:
        points = conn.execute("SELECT * FROM points ORDER BY id").fetchall()
        result = []
        for p in points:
            latest_row = conn.execute(
                "SELECT * FROM readings WHERE point_id = ? ORDER BY id DESC LIMIT 1",
                (p["id"],),
            ).fetchone()
            count_row = conn.execute(
                "SELECT COUNT(*) AS c FROM readings WHERE point_id = ?", (p["id"],)
            ).fetchone()
            recent_rows = conn.execute(
                "SELECT total_facings, products_json FROM readings WHERE point_id = ? ORDER BY id DESC LIMIT 8",
                (p["id"],),
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
                    "created_at": p["created_at"],
                    "readings_count": count_row["c"],
                    "latest": _reading_dict(latest_row, own_brands) if latest_row else None,
                    "recent_facings": recent_facings,
                    "recent_own_share": recent_own_share,
                }
            )
    return result


def get_point(point_id: str):
    own_brands = list_own_brands()
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM points WHERE id = ?", (point_id,)).fetchone()
        if not p:
            return None
        readings = conn.execute(
            "SELECT * FROM readings WHERE point_id = ? ORDER BY id DESC", (point_id,)
        ).fetchall()
    return {
        "id": p["id"],
        "name": p["name"],
        "created_at": p["created_at"],
        "readings": [_reading_dict(r, own_brands) for r in readings],
    }


def delete_point(point_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM readings WHERE point_id = ?", (point_id,))
        conn.execute("DELETE FROM points WHERE id = ?", (point_id,))


def export_rows(point_id: str = None) -> list:
    """Una fila por producto por lectura, para exportar a Excel/Power BI/Looker."""
    own_brands = list_own_brands()
    with get_conn() as conn:
        if point_id:
            readings = conn.execute(
                "SELECT r.*, p.name AS point_name FROM readings r JOIN points p ON p.id = r.point_id "
                "WHERE r.point_id = ? ORDER BY r.id",
                (point_id,),
            ).fetchall()
        else:
            readings = conn.execute(
                "SELECT r.*, p.name AS point_name FROM readings r JOIN points p ON p.id = r.point_id ORDER BY r.id"
            ).fetchall()

    rows = []
    for r in readings:
        products = _tag_products(json.loads(r["products_json"] or "[]"), own_brands)
        if not products:
            continue
        for p in products:
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
                    "reading_total_facings": r["total_facings"],
                    "reading_empty_space_pct": r["empty_space_pct"],
                    "reading_shelf_levels": r["shelf_levels_detected"],
                    "reading_linear_meters": r["linear_meters"] if "linear_meters" in r.keys() else None,
                    "reading_depth_units": r["depth_units"] if "depth_units" in r.keys() else None,
                    "reading_total_units_estimate": (
                        (r["total_facings"] or 0) * r["depth_units"]
                        if "depth_units" in r.keys() and r["depth_units"]
                        else None
                    ),
                }
            )
    return rows


def daily_metrics(point_id: str) -> list:
    own_brands = list_own_brands()
    with get_conn() as conn:
        readings = conn.execute(
            "SELECT * FROM readings WHERE point_id = ? ORDER BY id", (point_id,)
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


def replenishment_signals(point_id: str, lookback: int = 10) -> dict:
    own_brands = list_own_brands()
    with get_conn() as conn:
        readings = conn.execute(
            "SELECT * FROM readings WHERE point_id = ? ORDER BY id DESC LIMIT ?",
            (point_id, lookback),
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
