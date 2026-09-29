import csv
import io
import os
import re
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

app = FastAPI(title="Neuravision — Visión artificial para ejecución de punto de venta")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupPayload(BaseModel):
    company_name: str
    email: str
    password: str


class LoginPayload(BaseModel):
    email: str
    password: str


class CreateUserPayload(BaseModel):
    email: str
    password: str
    role: str


class UpdateRolePayload(BaseModel):
    role: str


class RenameOrgPayload(BaseModel):
    name: str


ROLE_LABELS = {
    "superusuario": "Superusuario",
    "admin": "Administrador",
    "analista": "Analista",
    "reponedor": "Reponedor",
}


def require_auth(request: Request) -> dict:
    token = request.cookies.get(auth.SESSION_COOKIE)
    email = auth.verify_session_token(token) if token else None
    user = db.get_user_by_email(email) if email else None
    if not user:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
    return user


def require_role(*roles: str):
    def _checker(request: Request) -> dict:
        user = require_auth(request)
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="No tienes permiso para realizar esta acción")
        return user

    return _checker


def _set_session_cookie(response: Response, email: str, request: Request) -> None:
    token = auth.create_session_token(email)
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


def save_upload(org_id: str, point_id: str, image_bytes: bytes, content_type: str) -> str:
    ext = EXTENSION_BY_CONTENT_TYPE.get(content_type, "jpg")
    point_dir = UPLOADS_DIR / org_id / point_id
    point_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    (point_dir / filename).write_bytes(image_bytes)
    return f"{org_id}/{point_id}/{filename}"


def _user_out(user: dict) -> dict:
    org = db.get_organization(user["org_id"])
    return {
        "email": user["email"],
        "role": user["role"],
        "role_label": ROLE_LABELS.get(user["role"], user["role"]),
        "org_id": user["org_id"],
        "org_name": org["name"] if org else user["org_id"],
    }


@app.get("/")
def index():
    return FileResponse(
        FRONTEND_DIR / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.post("/api/auth/signup")
def api_signup(payload: SignupPayload, request: Request, response: Response):
    company_name = payload.company_name.strip()
    email = payload.email.strip().lower()
    if len(company_name) < 2:
        raise HTTPException(status_code=400, detail="El nombre de la empresa es obligatorio")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Ingresa un correo válido")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")
    if db.email_exists(email):
        raise HTTPException(status_code=409, detail="Ese correo ya está registrado")

    org = db.create_organization(company_name)
    password_hash, salt = auth.hash_password(payload.password)
    # El primer usuario de una organización nueva queda como superusuario: dueño
    # de su propio espacio, sin depender de Neuramorphic para autogestionarse.
    user = db.create_user(org["id"], email, password_hash, salt, "superusuario")
    _set_session_cookie(response, email, request)
    return _user_out(user)


@app.post("/api/auth/login")
def api_login(payload: LoginPayload, request: Request, response: Response):
    email = payload.email.strip().lower()
    user = db.get_user_by_email(email)
    if not user or not auth.verify_password(payload.password, user["password_hash"], user["salt"]):
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos")

    _set_session_cookie(response, email, request)
    return _user_out(user)


@app.post("/api/auth/logout")
def api_logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user: dict = Depends(require_auth)):
    return _user_out(user)


@app.get("/api/org")
def api_get_org(actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    org = db.get_organization(actor["org_id"])
    return {**org, "member_count": len(db.list_users(actor["org_id"]))}


@app.put("/api/org")
def api_rename_org(payload: RenameOrgPayload, actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    name = payload.name.strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="El nombre de la empresa es obligatorio")
    db.rename_organization(actor["org_id"], name)
    return db.get_organization(actor["org_id"])


@app.get("/api/admin/users")
def api_list_users(actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    return [
        {**u, "role_label": ROLE_LABELS.get(u["role"], u["role"])}
        for u in db.list_users(actor["org_id"])
    ]


@app.post("/api/admin/users")
def api_create_user(payload: CreateUserPayload, actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    email = payload.email.strip().lower()
    role = payload.role.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Ingresa un correo válido")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")
    if role not in db.VALID_ROLES:
        raise HTTPException(status_code=400, detail="Rol inválido")
    if role == "superusuario" and actor["role"] != "superusuario":
        raise HTTPException(status_code=403, detail="Solo un superusuario puede crear otro superusuario")
    if db.email_exists(email):
        raise HTTPException(status_code=409, detail="Ese correo ya está registrado")

    password_hash, salt = auth.hash_password(payload.password)
    user = db.create_user(actor["org_id"], email, password_hash, salt, role)
    return {**user, "role_label": ROLE_LABELS.get(role, role)}


@app.put("/api/admin/users/{email}/role")
def api_update_user_role(email: str, payload: UpdateRolePayload, actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    email = email.strip().lower()
    role = payload.role.strip().lower()
    if role not in db.VALID_ROLES:
        raise HTTPException(status_code=400, detail="Rol inválido")
    target = db.user_in_org(actor["org_id"], email)
    if not target:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if role == "superusuario" and actor["role"] != "superusuario":
        raise HTTPException(status_code=403, detail="Solo un superusuario puede otorgar ese rol")
    if target["role"] == "superusuario" and role != "superusuario" and db.count_role(actor["org_id"], ("superusuario",)) <= 1:
        raise HTTPException(status_code=400, detail="Debe quedar al menos un superusuario en la organización")
    db.update_user_role(actor["org_id"], email, role)
    return {"email": email, "role": role, "role_label": ROLE_LABELS.get(role, role)}


@app.delete("/api/admin/users/{email}")
def api_delete_user(email: str, actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    email = email.strip().lower()
    if email == actor["email"]:
        raise HTTPException(status_code=400, detail="No puedes eliminar tu propia cuenta")
    target = db.user_in_org(actor["org_id"], email)
    if not target:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if target["role"] == "superusuario" and db.count_role(actor["org_id"], ("superusuario",)) <= 1:
        raise HTTPException(status_code=400, detail="Debe quedar al menos un superusuario en la organización")
    db.delete_user(actor["org_id"], email)
    return {"deleted": email}


@app.get("/api/own-brands")
def api_list_own_brands(actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    return db.list_own_brands(actor["org_id"])


@app.post("/api/own-brands")
def api_add_own_brand(name: str = Form(...), actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre de la marca es obligatorio")
    db.add_own_brand(actor["org_id"], name)
    return db.list_own_brands(actor["org_id"])


@app.delete("/api/own-brands/{name}")
def api_delete_own_brand(name: str, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    db.delete_own_brand(actor["org_id"], name)
    return db.list_own_brands(actor["org_id"])


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
def api_export_all_csv(actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    csv_text = _rows_to_csv(db.export_rows(actor["org_id"]))
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=neuravision-export.csv"},
    )


@app.get("/api/analytics")
def api_analytics(actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    return db.get_analytics(actor["org_id"])


@app.post("/api/admin/seed-demo-data")
def api_seed_demo_data(actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    """Genera puntos y lecturas de ejemplo en la organización del actor, para
    explorar Reportería antes de tener datos propios."""
    return db.seed_demo_data(actor["org_id"])


@app.get("/api/points")
def api_list_points(user: dict = Depends(require_auth)):
    return db.list_points_with_latest(user["org_id"])


@app.post("/api/points")
def api_create_point(
    point_id: Optional[str] = Form(default=None),
    name: str = Form(...),
    region: Optional[str] = Form(default=None),
    comuna: Optional[str] = Form(default=None),
    user: dict = Depends(require_auth),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre del punto es obligatorio")

    org_id = user["org_id"]
    pid = (point_id or "").strip() or db.next_point_id(org_id)
    if db.point_exists(org_id, pid):
        raise HTTPException(status_code=409, detail=f"El punto {pid} ya existe")

    db.create_point(org_id, pid, name, (region or "").strip() or None, (comuna or "").strip() or None)
    return db.get_point(org_id, pid)


@app.get("/api/points/{point_id}")
def api_get_point(point_id: str, user: dict = Depends(require_auth)):
    point = db.get_point(user["org_id"], point_id)
    if not point:
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return point


@app.get("/api/points/{point_id}/export.csv")
def api_export_point_csv(point_id: str, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    if not db.point_exists(actor["org_id"], point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    csv_text = _rows_to_csv(db.export_rows(actor["org_id"], point_id))
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=neuravision-{point_id}-export.csv"},
    )


@app.get("/api/points/{point_id}/daily-metrics")
def api_daily_metrics(point_id: str, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    if not db.point_exists(actor["org_id"], point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.daily_metrics(actor["org_id"], point_id)


@app.get("/api/points/{point_id}/replenishment")
def api_replenishment(point_id: str, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    if not db.point_exists(actor["org_id"], point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.replenishment_signals(actor["org_id"], point_id)


@app.delete("/api/points/{point_id}")
def api_delete_point(point_id: str, actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    if not db.point_exists(actor["org_id"], point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")

    db.delete_point(actor["org_id"], point_id)
    shutil.rmtree(UPLOADS_DIR / actor["org_id"] / point_id, ignore_errors=True)
    return {"deleted": point_id}


@app.post("/api/points/{point_id}/analyze")
async def api_analyze(
    point_id: str,
    images: List[UploadFile] = File(...),
    linear_meters: Optional[float] = Form(default=None),
    user: dict = Depends(require_auth),
):
    org_id = user["org_id"]
    if not db.point_exists(org_id, point_id):
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
        save_upload(org_id, point_id, image_bytes, content_type) for image_bytes, content_type in loaded
    ]
    return db.add_reading(org_id, point_id, analysis, image_paths, linear_meters)
