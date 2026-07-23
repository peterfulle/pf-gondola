import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data.db"

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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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


def add_reading(point_id: str, analysis: dict, image_path: str = None) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO readings (point_id, created_at, total_facings, shelf_levels_detected, "
            "empty_space_pct, products_json, categories_json, notes, image_path) "
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
                image_path,
            ),
        )
        reading_id = cur.lastrowid
        row = conn.execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()
    return _reading_dict(row)


def _reading_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "point_id": row["point_id"],
        "created_at": row["created_at"],
        "total_facings": row["total_facings"],
        "shelf_levels_detected": row["shelf_levels_detected"],
        "empty_space_pct": row["empty_space_pct"],
        "products": json.loads(row["products_json"] or "[]"),
        "categories": json.loads(row["categories_json"] or "[]"),
        "notes": row["notes"],
        "image_url": f"/uploads/{row['image_path']}" if row["image_path"] else None,
    }


def list_points_with_latest() -> list:
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
            result.append(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "created_at": p["created_at"],
                    "readings_count": count_row["c"],
                    "latest": _reading_dict(latest_row) if latest_row else None,
                }
            )
    return result


def get_point(point_id: str):
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
        "readings": [_reading_dict(r) for r in readings],
    }
