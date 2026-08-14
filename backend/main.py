import csv
import io
import shutil
import uuid
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
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


class AuthPayload(BaseModel):
    username: str
    password: str


def require_auth(request: Request) -> str:
    token = request.cookies.get(auth.SESSION_COOKIE)
    username = auth.verify_session_token(token) if token else None
    if not username or not db.user_exists(username):
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
    return username


def _set_session_cookie(response: Response, username: str, request: Request) -> None:
    token = auth.create_session_token(username)
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
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
    return FileResponse(
        FRONTEND_DIR / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.post("/api/auth/register")
def api_register(payload: AuthPayload, request: Request, response: Response):
    username = payload.username.strip().lower()
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="El usuario debe tener al menos 3 caracteres")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")
    if db.user_exists(username):
        raise HTTPException(status_code=409, detail="Ese usuario ya existe")

    password_hash, salt = auth.hash_password(payload.password)
    db.create_user(username, password_hash, salt)
    _set_session_cookie(response, username, request)
    return {"username": username}


@app.post("/api/auth/login")
def api_login(payload: AuthPayload, request: Request, response: Response):
    username = payload.username.strip().lower()
    user = db.get_user(username)
    if not user or not auth.verify_password(payload.password, user["password_hash"], user["salt"]):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    _set_session_cookie(response, username, request)
    return {"username": username}


@app.post("/api/auth/logout")
def api_logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(username: str = Depends(require_auth)):
    return {"username": username}


@app.get("/api/own-brands")
def api_list_own_brands(_user: str = Depends(require_auth)):
    return db.list_own_brands()


@app.post("/api/own-brands")
def api_add_own_brand(name: str = Form(...), _user: str = Depends(require_auth)):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre de la marca es obligatorio")
    db.add_own_brand(name)
    return db.list_own_brands()


@app.delete("/api/own-brands/{name}")
def api_delete_own_brand(name: str, _user: str = Depends(require_auth)):
    db.delete_own_brand(name)
    return db.list_own_brands()


def _rows_to_csv(rows: list) -> str:
    fieldnames = [
        "point_id", "point_name", "reading_id", "created_at",
        "product", "brand", "category", "facings", "shelf_level", "position_index",
        "out_of_stock", "is_own_brand", "estimated_depth", "units_estimate",
        "reading_total_facings", "reading_empty_space_pct", "reading_shelf_levels", "reading_linear_meters",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


@app.get("/api/export.csv")
def api_export_all_csv(_user: str = Depends(require_auth)):
    csv_text = _rows_to_csv(db.export_rows())
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pf-gondola-export.csv"},
    )


@app.get("/api/points")
def api_list_points(_user: str = Depends(require_auth)):
    return db.list_points_with_latest()


@app.post("/api/points")
def api_create_point(
    point_id: Optional[str] = Form(default=None),
    name: str = Form(...),
    _user: str = Depends(require_auth),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre del punto es obligatorio")

    pid = (point_id or "").strip() or db.next_point_id()
    if db.point_exists(pid):
        raise HTTPException(status_code=409, detail=f"El punto {pid} ya existe")

    db.create_point(pid, name)
    return db.get_point(pid)


@app.get("/api/points/{point_id}")
def api_get_point(point_id: str, _user: str = Depends(require_auth)):
    point = db.get_point(point_id)
    if not point:
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return point


@app.get("/api/points/{point_id}/export.csv")
def api_export_point_csv(point_id: str, _user: str = Depends(require_auth)):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    csv_text = _rows_to_csv(db.export_rows(point_id))
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=pf-{point_id}-export.csv"},
    )


@app.get("/api/points/{point_id}/daily-metrics")
def api_daily_metrics(point_id: str, _user: str = Depends(require_auth)):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.daily_metrics(point_id)


@app.get("/api/points/{point_id}/replenishment")
def api_replenishment(point_id: str, _user: str = Depends(require_auth)):
    if not db.point_exists(point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.replenishment_signals(point_id)


@app.delete("/api/points/{point_id}")
def api_delete_point(point_id: str, _user: str = Depends(require_auth)):
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
    _user: str = Depends(require_auth),
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
    return db.add_reading(point_id, analysis, image_paths, linear_meters)
