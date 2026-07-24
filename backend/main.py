import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db
from vision import analyze_shelf

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


@app.post("/api/points/{point_id}/analyze")
async def api_analyze(point_id: str, image: UploadFile = File(...)):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")

    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="El archivo debe ser una imagen")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Imagen vacía")

    try:
        analysis = analyze_shelf(image_bytes, image.content_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al analizar la imagen: {exc}") from exc

    image_path = save_upload(point_id, image_bytes, image.content_type)
    return db.add_reading(point_id, analysis, image_path)
