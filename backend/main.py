import csv
import io
import shutil
import uuid
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import db
from vision import MAX_IMAGES, analyze_shelf

load_dotenv()
db.init_db()

app = FastAPI(title="PF - Ejecución de Punto de Venta")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
UPLOADS_DIR = db.DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")

EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
}


def save_upload(point_id: str, image_bytes: bytes, content_type: str) -> str:
    ext = EXTENSION_BY_CONTENT_TYPE.get(content_type, "jpg")
    point_dir = UPLOADS_DIR / point_id
    point_dir.mkdir(exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    (point_dir / filename).write_bytes(image_bytes)
    return f"{point_id}/{filename}"


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/own-brands")
def api_list_own_brands():
    return db.list_own_brands()


@app.post("/api/own-brands")
def api_add_own_brand(name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre de la marca es obligatorio")
    db.add_own_brand(name)
    return db.list_own_brands()


@app.delete("/api/own-brands/{name}")
def api_delete_own_brand(name: str):
    db.delete_own_brand(name)
    return db.list_own_brands()


def _rows_to_csv(rows: list) -> str:
    fieldnames = [
        "point_id", "point_name", "reading_id", "created_at",
        "product", "brand", "category", "facings", "shelf_level", "position_index",
        "out_of_stock", "is_own_brand",
        "reading_total_facings", "reading_empty_space_pct", "reading_shelf_levels", "reading_linear_meters",
        "reading_depth_units", "reading_total_units_estimate",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


@app.get("/api/export.csv")
def api_export_all_csv():
    csv_text = _rows_to_csv(db.export_rows())
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pf-gondola-export.csv"},
    )


@app.get("/api/points")
def api_list_points():
    return db.list_points_with_latest()


@app.post("/api/points")
def api_create_point(point_id: Optional[str] = Form(default=None), name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre del punto es obligatorio")

    pid = (point_id or "").strip() or db.next_point_id()
    if db.point_exists(pid):
        raise HTTPException(status_code=409, detail=f"El punto {pid} ya existe")

    db.create_point(pid, name)
    return db.get_point(pid)


@app.get("/api/points/{point_id}")
def api_get_point(point_id: str):
    point = db.get_point(point_id)
    if not point:
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return point


@app.get("/api/points/{point_id}/export.csv")
def api_export_point_csv(point_id: str):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    csv_text = _rows_to_csv(db.export_rows(point_id))
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=pf-{point_id}-export.csv"},
    )


@app.get("/api/points/{point_id}/daily-metrics")
def api_daily_metrics(point_id: str):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.daily_metrics(point_id)


@app.get("/api/points/{point_id}/replenishment")
def api_replenishment(point_id: str):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.replenishment_signals(point_id)


@app.delete("/api/points/{point_id}")
def api_delete_point(point_id: str):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")

    db.delete_point(point_id)
    shutil.rmtree(UPLOADS_DIR / point_id, ignore_errors=True)
    return {"deleted": point_id}


@app.post("/api/points/{point_id}/analyze")
async def api_analyze(
    point_id: str,
    images: List[UploadFile] = File(...),
    linear_meters: Optional[float] = Form(default=None),
    depth_units: Optional[int] = Form(default=None),
):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")

    if not images:
        raise HTTPException(status_code=400, detail="Debes subir al menos una foto")
    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_IMAGES} fotos por lectura")

    loaded = []
    for image in images:
        if not image.content_type or not image.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Todos los archivos deben ser imágenes")
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="Una de las imágenes está vacía")
        loaded.append((image_bytes, image.content_type))

    try:
        analysis = analyze_shelf(loaded)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al analizar la imagen: {exc}") from exc

    if not analysis.get("is_supermarket_shelf", True):
        reason = analysis.get("rejection_reason") or "La imagen no parece ser una góndola de supermercado."
        raise HTTPException(status_code=422, detail=reason)

    image_paths = [
        save_upload(point_id, image_bytes, content_type) for image_bytes, content_type in loaded
    ]
    return db.add_reading(point_id, analysis, image_paths, linear_meters, depth_units)
