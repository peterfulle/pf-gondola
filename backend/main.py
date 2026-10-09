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
from ocr_client_form import extract_client_form
from ocr_voucher import extract_voucher
from vision import MAX_IMAGES, analyze_additional_display, analyze_bulk_display, analyze_shelf

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


class CreateShelfPayload(BaseModel):
    name: str
    shelf_type: Optional[str] = None
    description: Optional[str] = None


class PlanogramItemPayload(BaseModel):
    product: str
    brand: Optional[str] = None
    category: Optional[str] = None
    expected_facings: Optional[int] = None


class ProductCorrectionPayload(BaseModel):
    product: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    facings: Optional[int] = None
    out_of_stock: Optional[bool] = None


class AssignPointsPayload(BaseModel):
    point_ids: List[str]


class CreateAgreementPayload(BaseModel):
    point_id: Optional[str] = None
    display_type: str
    brand: str
    description: Optional[str] = None
    committed_from: Optional[str] = None
    committed_to: Optional[str] = None


class ReconcilePayload(BaseModel):
    reconciled: bool


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


def _write_upload(subpath_parts: list, image_bytes: bytes, content_type: str) -> str:
    ext = EXTENSION_BY_CONTENT_TYPE.get(content_type, "jpg")
    target_dir = UPLOADS_DIR.joinpath(*subpath_parts)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    (target_dir / filename).write_bytes(image_bytes)
    return "/".join([*subpath_parts, filename])


def save_upload(org_id: str, point_id: str, image_bytes: bytes, content_type: str) -> str:
    return _write_upload([org_id, point_id], image_bytes, content_type)


def save_ocr_upload(org_id: str, kind: str, image_bytes: bytes, content_type: str) -> str:
    return _write_upload([org_id, "ocr", kind], image_bytes, content_type)


def _check_point_access(user: dict, point_id: str) -> None:
    allowed = db.allowed_point_ids_for_user(user["org_id"], user)
    if allowed is not None and point_id not in allowed:
        raise HTTPException(status_code=404, detail="Punto no encontrado")


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


@app.get("/api/admin/users/{email}/points")
def api_get_user_points(email: str, actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    email = email.strip().lower()
    if not db.user_in_org(actor["org_id"], email):
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return db.list_assigned_point_ids(actor["org_id"], email)


@app.put("/api/admin/users/{email}/points")
def api_set_user_points(email: str, payload: AssignPointsPayload, actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    email = email.strip().lower()
    if not db.user_in_org(actor["org_id"], email):
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    valid_ids = {p["id"] for p in db.list_points_with_latest(actor["org_id"])}
    point_ids = [pid for pid in payload.point_ids if pid in valid_ids]
    db.set_point_assignments(actor["org_id"], email, point_ids)
    return {"email": email, "point_ids": point_ids}


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
        "price_clp", "price_confidence",
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
    allowed = db.allowed_point_ids_for_user(actor["org_id"], actor)
    return db.get_analytics(actor["org_id"], allowed)


@app.post("/api/admin/seed-demo-data")
def api_seed_demo_data(actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    """Genera puntos y lecturas de ejemplo en la organización del actor, para
    explorar Reportería antes de tener datos propios."""
    return db.seed_demo_data(actor["org_id"])


@app.get("/api/points")
def api_list_points(user: dict = Depends(require_auth)):
    allowed = db.allowed_point_ids_for_user(user["org_id"], user)
    return db.list_points_with_latest(user["org_id"], allowed)


@app.post("/api/points/import")
async def api_import_points(file: UploadFile = File(...), user: dict = Depends(require_auth)):
    """Carga masiva de puntos de venta desde un CSV con columnas: name (obligatoria),
    point_id, region, comuna (opcionales). Para Excel, exportar primero como CSV."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(status_code=400, detail="El CSV debe tener una columna 'name' con el nombre del punto")

    org_id = user["org_id"]
    created, skipped = [], []
    for i, raw_row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        name = row.get("name", "")
        if not name:
            skipped.append({"row": i, "reason": "falta el nombre"})
            continue
        pid = row.get("point_id") or db.next_point_id(org_id)
        if db.point_exists(org_id, pid):
            skipped.append({"row": i, "reason": f"el punto {pid} ya existe"})
            continue
        db.create_point(org_id, pid, name, row.get("region") or None, row.get("comuna") or None)
        created.append({"id": pid, "name": name})

    return {"created": created, "skipped": skipped, "created_count": len(created), "skipped_count": len(skipped)}


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
    _check_point_access(user, point_id)
    point = db.get_point(user["org_id"], point_id)
    if not point:
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return point


@app.get("/api/points/{point_id}/export.csv")
def api_export_point_csv(point_id: str, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    _check_point_access(actor, point_id)
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
    _check_point_access(actor, point_id)
    if not db.point_exists(actor["org_id"], point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.daily_metrics(actor["org_id"], point_id)


@app.get("/api/points/{point_id}/replenishment")
def api_replenishment(point_id: str, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    _check_point_access(actor, point_id)
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


@app.get("/api/points/{point_id}/shelves")
def api_list_shelves(point_id: str, user: dict = Depends(require_auth)):
    _check_point_access(user, point_id)
    if not db.point_exists(user["org_id"], point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.list_shelves(user["org_id"], point_id)


@app.post("/api/points/{point_id}/shelves")
def api_create_shelf(point_id: str, payload: CreateShelfPayload, user: dict = Depends(require_auth)):
    _check_point_access(user, point_id)
    if not db.point_exists(user["org_id"], point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre de la góndola es obligatorio")
    return db.create_shelf(user["org_id"], point_id, name, (payload.shelf_type or "").strip() or None,
                            (payload.description or "").strip() or None)


@app.delete("/api/points/{point_id}/shelves/{shelf_id}")
def api_delete_shelf(point_id: str, shelf_id: str, actor: dict = Depends(require_role(*db.ADMIN_ROLES))):
    if not db.shelf_exists(actor["org_id"], point_id, shelf_id):
        raise HTTPException(status_code=404, detail="Góndola no encontrada")
    db.delete_shelf(actor["org_id"], shelf_id)
    return {"deleted": shelf_id}


@app.get("/api/shelves/{shelf_id}/planogram")
def api_get_planogram(shelf_id: str, user: dict = Depends(require_auth)):
    shelf = db.get_shelf(user["org_id"], shelf_id)
    if not shelf:
        raise HTTPException(status_code=404, detail="Góndola no encontrada")
    _check_point_access(user, shelf["point_id"])
    return db.list_planogram_items(user["org_id"], shelf_id)


@app.post("/api/shelves/{shelf_id}/planogram")
def api_add_planogram_item(shelf_id: str, payload: PlanogramItemPayload, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    shelf = db.get_shelf(actor["org_id"], shelf_id)
    if not shelf:
        raise HTTPException(status_code=404, detail="Góndola no encontrada")
    product = payload.product.strip()
    if not product:
        raise HTTPException(status_code=400, detail="El nombre del producto esperado es obligatorio")
    return db.add_planogram_item(actor["org_id"], shelf_id, product, (payload.brand or "").strip() or None,
                                  (payload.category or "").strip() or None, payload.expected_facings)


@app.delete("/api/shelves/{shelf_id}/planogram/{item_id}")
def api_delete_planogram_item(shelf_id: str, item_id: int, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    shelf = db.get_shelf(actor["org_id"], shelf_id)
    if not shelf:
        raise HTTPException(status_code=404, detail="Góndola no encontrada")
    db.delete_planogram_item(actor["org_id"], item_id)
    return {"deleted": item_id}


@app.get("/api/points/{point_id}/readings/{reading_id}/corrections")
def api_list_corrections(point_id: str, reading_id: int, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    _check_point_access(actor, point_id)
    if not db.get_reading_for_point(actor["org_id"], point_id, reading_id):
        raise HTTPException(status_code=404, detail="Lectura no encontrada")
    return db.list_corrections(actor["org_id"], reading_id)


@app.put("/api/points/{point_id}/readings/{reading_id}/products/{product_index}")
def api_correct_product(
    point_id: str,
    reading_id: int,
    product_index: int,
    payload: ProductCorrectionPayload,
    actor: dict = Depends(require_role(*db.ANALYST_ROLES)),
):
    _check_point_access(actor, point_id)
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No se recibió ningún campo para corregir")
    result = db.correct_product(actor["org_id"], point_id, reading_id, product_index, updates, actor["email"])
    if not result:
        raise HTTPException(status_code=404, detail="Lectura o producto no encontrado")
    return result


READING_TYPES = ("gondola", "exhibicion_adicional", "vitrina_granel")
_VALIDITY_FIELD_BY_TYPE = {
    "gondola": ("is_supermarket_shelf", "La imagen no parece ser una góndola de supermercado."),
    "exhibicion_adicional": ("is_valid_display", "La imagen no parece ser una exhibición adicional (cabecera, isla, exhibidor o mueble de marca)."),
    "vitrina_granel": ("is_valid_bulk_display", "La imagen no parece ser una vitrina o exhibición a granel."),
}


@app.post("/api/points/{point_id}/analyze")
async def api_analyze(
    point_id: str,
    images: List[UploadFile] = File(...),
    linear_meters: Optional[float] = Form(default=None),
    shelf_id: Optional[str] = Form(default=None),
    reading_type: str = Form(default="gondola"),
    user: dict = Depends(require_auth),
):
    org_id = user["org_id"]
    _check_point_access(user, point_id)
    if not db.point_exists(org_id, point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")

    reading_type = (reading_type or "gondola").strip()
    if reading_type not in READING_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de lectura inválido")

    shelf_id = (shelf_id or "").strip() or None
    if shelf_id and not db.shelf_exists(org_id, point_id, shelf_id):
        raise HTTPException(status_code=404, detail="Góndola no encontrada")
    if shelf_id and reading_type != "gondola":
        shelf_id = None  # el planograma solo aplica a la góndola principal

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

    analyze_fn = {
        "gondola": analyze_shelf,
        "exhibicion_adicional": analyze_additional_display,
        "vitrina_granel": analyze_bulk_display,
    }[reading_type]

    try:
        analysis = analyze_fn(loaded)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al analizar la imagen: {exc}") from exc

    validity_field, default_reason = _VALIDITY_FIELD_BY_TYPE[reading_type]
    if not analysis.get(validity_field, True):
        reason = analysis.get("rejection_reason") or default_reason
        raise HTTPException(status_code=422, detail=reason)

    image_paths = [
        save_upload(org_id, point_id, image_bytes, content_type) for image_bytes, content_type in loaded
    ]
    return db.add_reading(org_id, point_id, analysis, image_paths, linear_meters, shelf_id, reading_type)


@app.get("/api/commercial-agreements")
def api_list_agreements(point_id: Optional[str] = None, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    return db.list_commercial_agreements(actor["org_id"], point_id)


@app.post("/api/commercial-agreements")
def api_create_agreement(payload: CreateAgreementPayload, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    brand = payload.brand.strip()
    if not brand:
        raise HTTPException(status_code=400, detail="La marca es obligatoria")
    if payload.display_type not in db.AGREEMENT_DISPLAY_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de exhibición inválido")
    point_id = (payload.point_id or "").strip() or None
    if point_id and not db.point_exists(actor["org_id"], point_id):
        raise HTTPException(status_code=404, detail="Punto no encontrado")
    return db.create_commercial_agreement(
        actor["org_id"], point_id, payload.display_type, brand,
        (payload.description or "").strip() or None,
        (payload.committed_from or "").strip() or None,
        (payload.committed_to or "").strip() or None,
    )


@app.delete("/api/commercial-agreements/{agreement_id}")
def api_delete_agreement(agreement_id: int, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    db.delete_commercial_agreement(actor["org_id"], agreement_id)
    return {"deleted": agreement_id}


# ---------- PoC: extracción OCR (persiste resultados por organización para mantener un historial) ----------

OCR_KINDS = {"voucher": "voucher", "client-form": "client_form"}
OCR_EXTRACT_FN = {"voucher": extract_voucher, "client-form": extract_client_form}
OCR_VALID_FIELD = {"voucher": "is_valid_voucher", "client-form": "is_valid_document"}
OCR_ERROR_LABEL = {"voucher": "el comprobante", "client-form": "el documento"}


async def _load_ocr_images(images: List[UploadFile]) -> list:
    if not images:
        raise HTTPException(status_code=400, detail="Debes subir al menos una foto")
    if len(images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_IMAGES} fotos")
    loaded = []
    for image in images:
        if not image.content_type or not image.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Todos los archivos deben ser imágenes")
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="Una de las imágenes está vacía")
        loaded.append((image_bytes, image.content_type))
    return loaded


async def _api_ocr_process(url_kind: str, images: List[UploadFile], user: dict) -> dict:
    kind = OCR_KINDS[url_kind]
    loaded = await _load_ocr_images(images)
    try:
        result = OCR_EXTRACT_FN[url_kind](loaded)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al leer {OCR_ERROR_LABEL[url_kind]}: {exc}") from exc

    image_paths = [
        save_ocr_upload(user["org_id"], kind, image_bytes, content_type) for image_bytes, content_type in loaded
    ]
    return db.add_ocr_extraction(user["org_id"], kind, result, image_paths, user["email"])


@app.post("/api/ocr/voucher")
async def api_ocr_voucher(images: List[UploadFile] = File(...), user: dict = Depends(require_auth)):
    return await _api_ocr_process("voucher", images, user)


@app.post("/api/ocr/client-form")
async def api_ocr_client_form(images: List[UploadFile] = File(...), user: dict = Depends(require_auth)):
    return await _api_ocr_process("client-form", images, user)


@app.get("/api/ocr/{url_kind}/history")
def api_ocr_history(url_kind: str, user: dict = Depends(require_auth)):
    if url_kind not in OCR_KINDS:
        raise HTTPException(status_code=404, detail="Tipo de extracción inválido")
    return db.list_ocr_extractions(user["org_id"], OCR_KINDS[url_kind])


@app.delete("/api/ocr/{url_kind}/{extraction_id}")
def api_ocr_delete(url_kind: str, extraction_id: int, actor: dict = Depends(require_role(*db.ANALYST_ROLES))):
    if url_kind not in OCR_KINDS:
        raise HTTPException(status_code=404, detail="Tipo de extracción inválido")
    db.delete_ocr_extraction(actor["org_id"], OCR_KINDS[url_kind], extraction_id)
    return {"deleted": extraction_id}


@app.patch("/api/ocr/voucher/{extraction_id}/reconcile")
def api_ocr_voucher_reconcile(
    extraction_id: int, payload: ReconcilePayload, actor: dict = Depends(require_role(*db.ANALYST_ROLES))
):
    updated = db.set_ocr_reconciled(actor["org_id"], "voucher", extraction_id, payload.reconciled, actor["email"])
    if not updated:
        raise HTTPException(status_code=404, detail="Comprobante no encontrado")
    return updated


# ---------- Finanzas: dashboard, reportería y exportación sobre comprobantes de depósito ----------

@app.get("/api/finance/summary")
def api_finance_summary(user: dict = Depends(require_auth)):
    return db.finance_summary(user["org_id"])


def _finance_voucher_filters(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    bank: Optional[str] = None, reconciled: Optional[bool] = None,
):
    return {"date_from": date_from, "date_to": date_to, "bank": bank, "reconciled": reconciled}


@app.get("/api/finance/vouchers")
def api_finance_vouchers(
    user: dict = Depends(require_auth), filters: dict = Depends(_finance_voucher_filters),
):
    return db.list_finance_vouchers(user["org_id"], **filters)


def _finance_vouchers_csv(rows: list) -> str:
    fieldnames = [
        "id", "created_at", "banco_o_servicio", "monto_clp", "fecha", "hora",
        "folio_o_referencia", "cuenta_depositada", "tipo_deposito", "needs_review",
        "reconciled", "reconciled_at", "reconciled_by", "created_by",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


@app.get("/api/finance/vouchers/export.csv")
def api_finance_vouchers_export(
    actor: dict = Depends(require_role(*db.ANALYST_ROLES)), filters: dict = Depends(_finance_voucher_filters),
):
    rows = db.list_finance_vouchers(actor["org_id"], **filters)
    csv_text = _finance_vouchers_csv(rows)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=neuravision-finanzas-comprobantes.csv"},
    )
