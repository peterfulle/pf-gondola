import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "data.db"

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
        try:
            conn.execute("ALTER TABLE readings ADD COLUMN image_paths_json TEXT")
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


def add_reading(point_id: str, analysis: dict, image_paths: list) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO readings (point_id, created_at, total_facings, shelf_levels_detected, "
            "empty_space_pct, products_json, categories_json, notes, image_paths_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
