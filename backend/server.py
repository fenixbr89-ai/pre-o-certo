"""Preço Certo — API (FastAPI + MongoDB + SyncPay + Resend).

O front (public/index.html) chama estes endpoints, sempre sob /api.
Variáveis de ambiente: veja .env.example e o README.md.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import bcrypt
import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger("precocerto")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------- configuração
MONGO_URL = os.environ.get("MONGO_URL", "")
DB_NAME = os.environ.get("DB_NAME", "precocerto")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
SITE_URL = os.environ.get("SITE_URL", "http://localhost:3000").rstrip("/")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "Preço Certo <onboarding@resend.dev>")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")  # recebe as sugestões (opcional)
SYNCPAY_CLIENT_ID = os.environ.get("SYNCPAY_CLIENT_ID", "")
SYNCPAY_CLIENT_SECRET = os.environ.get("SYNCPAY_CLIENT_SECRET", "")
SYNCPAY_PUBLIC_KEY = os.environ.get("SYNCPAY_PUBLIC_KEY", "")
SYNCPAY_PLAN_PIX_TOKEN = os.environ.get("SYNCPAY_PLAN_PIX_TOKEN", "")
SYNCPAY_PLAN_CARD_TOKEN = os.environ.get("SYNCPAY_PLAN_CARD_TOKEN", "")
SYNCPAY_WEBHOOK_SECRET = os.environ.get("SYNCPAY_WEBHOOK_SECRET", "")
# "log" = só registra se a assinatura não bater; "enforce" = recusa (401). Veja o README/.env.example.
SYNCPAY_SIGNATURE_MODE = os.environ.get("SYNCPAY_SIGNATURE_MODE", "log").strip().lower()
SYNCPAY_API = "https://api.syncpayments.com.br/api/partner/v1"
PRICE_CENTS = int(os.environ.get("PRICE_CENTS", "1990"))  # R$ 19,90
_syncpay_token = ""
_syncpay_token_exp = 0

if not MONGO_URL:
    raise RuntimeError("Defina MONGO_URL.")
if len(JWT_SECRET) < 32:
    raise RuntimeError("Defina JWT_SECRET com pelo menos 32 caracteres.")

client = AsyncIOMotorClient(MONGO_URL, tz_aware=True)
db = client[DB_NAME]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TOKEN_DAYS = 30
RESET_MINUTES = 60
MAX_STORE_BYTES = 1_500_000


@asynccontextmanager
async def lifespan(_app):
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id", unique=True)
    await db.users.create_index("syncpay_subscription_token", sparse=True)
    await db.syncpay_events.create_index("key", unique=True, sparse=True)
    await db.syncpay_events.create_index("received_at", expireAfterSeconds=60 * 24 * 3600)
    await db.stores.create_index("user_id", unique=True)
    await db.resets.create_index("token_hash", unique=True)
    await db.resets.create_index("expires_at", expireAfterSeconds=0)
    yield


app = FastAPI(title="Preço Certo API", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[SITE_URL],
    allow_methods=["GET", "POST", "PUT", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---------------------------------------------------------------- utilitários
def now() -> datetime:
    return datetime.now(timezone.utc)


def clean(s, n: int) -> str:
    return str(s or "").strip()[:n]


def norm_email(s) -> str:
    return clean(s, 254).lower()


def _prehash(pw: str) -> bytes:
    # bcrypt só lê 72 bytes; o pré-hash permite senhas longas sem truncar.
    return base64.b64encode(hashlib.sha256(pw.encode()).digest())


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(_prehash(pw), bcrypt.gensalt()).decode()


def check_pw(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(pw), h.encode())
    except Exception:
        return False


def make_token(uid: str) -> str:
    exp = now() + timedelta(days=TOKEN_DAYS)
    return jwt.encode({"sub": uid, "exp": exp, "iat": int(now().timestamp())}, JWT_SECRET, algorithm="HS256")


def session(u: dict) -> dict:
    return {"token": make_token(u["id"]), "user": {"name": u.get("name", ""), "email": u["email"]}}


async def body_of(request: Request) -> dict:
    try:
        d = await request.json()
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def ip_of(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))


_hits: dict[str, list[float]] = {}


def rate_limit(key: str, limit: int, seconds: int):
    t = time.time()
    arr = [x for x in _hits.get(key, []) if t - x < seconds]
    if len(arr) >= limit:
        raise HTTPException(429, "Muitas tentativas. Aguarde alguns minutos e tente de novo.")
    arr.append(t)
    _hits[key] = arr
    if len(_hits) > 5000:  # evita crescer sem limite
        for k in list(_hits)[:2500]:
            _hits.pop(k, None)


async def current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Sessão expirada.")
    try:
        data = jwt.decode(auth[7:].strip(), JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "Sessão expirada.")
    user = await db.users.find_one({"id": data.get("sub")}, {"_id": 0})
    if not user:
        raise HTTPException(401, "Sessão expirada.")
    changed = user.get("pw_changed_at")
    if changed:  # trocar a senha derruba as sessões abertas antes da troca
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
        if int(data.get("iat") or 0) < int(changed.timestamp()):
            raise HTTPException(401, "Sessão expirada.")
    if trial_expired(user):
        path = request.url.path
        if path not in ("/api/store", "/api/mercadopago/checkout", "/api/syncpay/checkout", "/api/syncpay/cancel", "/api/account/delete"):
            raise HTTPException(
                status_code=402,
                detail={"code": "TRIAL_EXPIRED", "message": "Seu teste grátis terminou. Assine por R$ 19,90 para continuar usando os recursos."}
            )
        if path == "/api/store" and request.method.upper() != "GET":
            raise HTTPException(
                status_code=402,
                detail={"code": "TRIAL_EXPIRED", "message": "Seu teste grátis terminou. Assine por R$ 19,90 para continuar usando os recursos."}
            )
        return user
    return user


async def send_mail(to: str, subject: str, html: str):
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY ausente. E-mail para %s não enviado: %s", to, subject)
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": MAIL_FROM, "to": [to], "subject": subject, "html": html},
            )
        if r.status_code >= 300:
            log.error("Resend recusou o e-mail (%s): %s", r.status_code, r.text[:300])
    except Exception:
        log.exception("Falha ao enviar e-mail")


TRIAL_DAYS = 7
# Status em que a assinatura já acabou e o cliente pode assinar de novo (inclui os nomes em português gravados antes).
DEAD_SUB = {"cancelled", "canceled", "cancelada", "suspended", "suspensa", "failed"}

def trial_ms(u: dict) -> int:
    created = u.get("created_at")
    if not created:
        return 0
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return 0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return int(created.timestamp() * 1000)


def trial_expired(u: dict) -> bool:
    if u.get("plan") == "pro":
        return False
    started = trial_ms(u)
    return not started or time.time() * 1000 >= started + TRIAL_DAYS * 24 * 60 * 60 * 1000


# ---------------------------------------------------------------- rotas básicas
@app.get("/api/health")
async def health():
    return {"ok": True}


# ---------------------------------------------------------------- contas
@app.post("/api/signup")
async def signup(request: Request):
    rate_limit(f"signup:{ip_of(request)}", 10, 3600)
    b = await body_of(request)
    email, name, pw = norm_email(b.get("email")), clean(b.get("name"), 80), str(b.get("password") or "")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "E-mail inválido.")
    if len(pw) < 8:
        raise HTTPException(400, "A senha precisa ter pelo menos 8 caracteres.")
    if len(pw) > 128:
        raise HTTPException(400, "A senha é muito longa.")
    if await db.users.find_one({"email": email}, {"_id": 1}):
        raise HTTPException(409, "Já existe uma conta com esse e-mail.")
    user = {
        "id": uuid.uuid4().hex, "email": email, "name": name,
        "password_hash": await asyncio.to_thread(hash_pw, pw),
        "plan": "free", "created_at": now(),
    }
    try:
        await db.users.insert_one(dict(user))
    except Exception:
        raise HTTPException(409, "Já existe uma conta com esse e-mail.")
    return session(user)


@app.post("/api/login")
async def login(request: Request):
    b = await body_of(request)
    email, pw = norm_email(b.get("email")), str(b.get("password") or "")
    rate_limit(f"login-ip:{ip_of(request)}", 20, 600)
    rate_limit(f"login-email:{email}", 8, 600)
    user = await db.users.find_one({"email": email}, {"_id": 0})
    ok = bool(user and user.get("password_hash")) and await asyncio.to_thread(check_pw, pw, user["password_hash"])
    if not ok:
        raise HTTPException(401, "E-mail ou senha incorretos.")
    return session(user)


@app.post("/api/forgot")
async def forgot(request: Request):
    b = await body_of(request)
    email = norm_email(b.get("email"))
    rate_limit(f"forgot-ip:{ip_of(request)}", 10, 3600)
    if EMAIL_RE.match(email):
        rate_limit(f"forgot-email:{email}", 3, 3600)
        user = await db.users.find_one({"email": email}, {"_id": 0})
        if user:
            token = secrets.token_urlsafe(32)
            await db.resets.insert_one({
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "user_id": user["id"],
                "expires_at": now() + timedelta(minutes=RESET_MINUTES),
            })
            link = f"{SITE_URL}/?reset={token}"
            html = (
                "<div style='font-family:Arial,sans-serif;max-width:480px'>"
                "<h2>Redefinir sua senha</h2>"
                "<p>Recebemos um pedido para criar uma nova senha no Preço Certo.</p>"
                f"<p><a href='{link}' style='background:#059669;color:#fff;padding:12px 20px;"
                "border-radius:10px;text-decoration:none;display:inline-block'>Criar nova senha</a></p>"
                f"<p style='color:#666;font-size:13px'>O link vale por {RESET_MINUTES} minutos. "
                "Se não foi você, ignore este e-mail.</p></div>"
            )
            asyncio.create_task(send_mail(email, "Redefinir sua senha — Preço Certo", html))
    # Sempre a mesma resposta, exista a conta ou não.
    return {"ok": True}


@app.post("/api/reset")
async def reset(request: Request):
    rate_limit(f"reset:{ip_of(request)}", 20, 3600)
    b = await body_of(request)
    token, pw = str(b.get("token") or ""), str(b.get("password") or "")
    if len(pw) < 8:
        raise HTTPException(400, "A senha precisa ter pelo menos 8 caracteres.")
    if len(pw) > 128:
        raise HTTPException(400, "A senha é muito longa.")
    th = hashlib.sha256(token.encode()).hexdigest()
    rec = await db.resets.find_one_and_delete({"token_hash": th})
    if not rec or rec["expires_at"] < now():
        raise HTTPException(400, "Link inválido ou expirado. Peça um novo.")
    user = await db.users.find_one({"id": rec["user_id"]}, {"_id": 0})
    if not user:
        raise HTTPException(400, "Link inválido ou expirado. Peça um novo.")
    await db.users.update_one({"id": user["id"]}, {"$set": {
        "password_hash": await asyncio.to_thread(hash_pw, pw), "pw_changed_at": now()}})
    await db.resets.delete_many({"user_id": user["id"]})
    return session(user)


# ---------------------------------------------------------------- dados do usuário
@app.get("/api/store")
async def get_store(request: Request):
    user = await current_user(request)
    doc = await db.stores.find_one({"user_id": user["id"]}, {"_id": 0})
    try:
        data = json.loads(doc["json"]) if doc else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # Plano e teste grátis vêm SEMPRE do servidor, nunca do que o navegador envia.
    data["plan"] = "pro" if user.get("plan") == "pro" else "free"
    data["trial"] = trial_ms(user)
    data["photo"] = user.get("photo") or ""
    st = str(user.get("syncpay_status") or "")
    data["sub"] = "" if st in DEAD_SUB else st  # "" = sem assinatura em andamento
    return data


@app.put("/api/store")
async def put_store(request: Request):
    user = await current_user(request)
    rate_limit(f"store:{user['id']}", 120, 60)
    data = await body_of(request)
    for k in ("plan", "trial", "since", "photo", "sub"):
        data.pop(k, None)
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw.encode()) > MAX_STORE_BYTES:
        raise HTTPException(413, "Dados grandes demais.")
    await db.stores.update_one(
        {"user_id": user["id"]}, {"$set": {"json": raw, "updated_at": now()}}, upsert=True
    )
    return {"ok": True}


@app.post("/api/photo")
async def set_photo(request: Request):
    import base64

    user = await current_user(request)
    rate_limit(f"photo:{user['id']}", 20, 600)
    b = await body_of(request)
    photo = str(b.get("photo") or "")
    if photo:
        head = "data:image/jpeg;base64,"
        ok = photo.startswith(head) and len(photo) <= 200_000
        if ok:
            try:
                base64.b64decode(photo[len(head):], validate=True)
            except Exception:
                ok = False
        if not ok:
            raise HTTPException(400, "Foto inválida ou grande demais.")
    await db.users.update_one({"id": user["id"]}, {"$set": {"photo": photo}})
    return {"ok": True}


@app.post("/api/account/delete")
async def delete_account(request: Request):
    """Apaga a conta e todos os dados do usuário. Exige a senha; cancela a assinatura antes."""
    user = await current_user(request)
    rate_limit(f"delete:{user['id']}", 5, 600)
    b = await body_of(request)
    pw = str(b.get("password") or "")
    ok = bool(user.get("password_hash")) and await asyncio.to_thread(check_pw, pw, user["password_hash"])
    if not ok:
        raise HTTPException(403, "Senha incorreta.")
    # Se existe assinatura na SyncPay, cancela primeiro para o cliente não continuar sendo cobrado.
    sub_token = user.get("syncpay_subscription_token")
    if sub_token and user.get("syncpay_status") not in DEAD_SUB:
        access = await syncpay_access_token()
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.patch(f"{SYNCPAY_API}/subscriptions/{sub_token}/cancel",
                    headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
                    json={"reason": "Conta apagada pelo cliente no Preço Certo"})
            if r.status_code >= 400 and r.status_code != 404:
                log.warning("SyncPay cancel (apagar conta) recusado: status=%s", r.status_code)
                raise HTTPException(502, "Não foi possível cancelar a sua assinatura agora, então a conta não foi apagada. Tente de novo em alguns minutos.")
        except HTTPException:
            raise
        except Exception:
            log.exception("SyncPay cancel (apagar conta)")
            raise HTTPException(502, "Não foi possível cancelar a sua assinatura agora, então a conta não foi apagada. Tente de novo em alguns minutos.")
    uid, email = user["id"], user["email"]
    await db.stores.delete_many({"user_id": uid})
    await db.resets.delete_many({"user_id": uid})
    await db.suggestions.delete_many({"user": email})
    await db.users.delete_one({"id": uid})
    log.info("Conta apagada: %s", uid)
    return {"ok": True}


@app.post("/api/suggest")
async def suggest(request: Request):
    rate_limit(f"suggest:{ip_of(request)}", 10, 3600)
    b = await body_of(request)
    msg = clean(b.get("m"), 2000)
    if len(msg) < 5:
        raise HTTPException(400, "Escreva pelo menos algumas palavras.")
    item = {
        "type": clean(b.get("t"), 40), "message": msg, "contact": clean(b.get("e"), 254),
        "user": clean(b.get("user"), 254), "created_at": now(),
    }
    await db.suggestions.insert_one(dict(item))
    if ADMIN_EMAIL:
        esc = lambda x: x.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = f"<p><b>{esc(item['type'])}</b></p><p>{esc(msg)}</p><p>Contato: {esc(item['contact'])} / {esc(item['user'])}</p>"
        asyncio.create_task(send_mail(ADMIN_EMAIL, "Nova sugestão — Preço Certo", html))
    return {"ok": True}


# ---------------------------------------------------------------- pagamento (SyncPay)
async def syncpay_access_token() -> str:
    """Obtém e reutiliza o token de API da SyncPay por até uma hora."""
    global _syncpay_token, _syncpay_token_exp
    if not SYNCPAY_CLIENT_ID or not SYNCPAY_CLIENT_SECRET:
        raise HTTPException(503, "SyncPay ainda não configurada. Configure as variáveis SYNCPAY_* no Render.")
    if _syncpay_token and time.time() < _syncpay_token_exp - 60:
        return _syncpay_token
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{SYNCPAY_API}/auth-token", json={
                "client_id": SYNCPAY_CLIENT_ID, "client_secret": SYNCPAY_CLIENT_SECRET
            })
        data = r.json()
        if r.status_code >= 400 or not data.get("access_token"):
            log.error("SyncPay auth falhou: status=%s", r.status_code)
            raise HTTPException(502, "A SyncPay não aceitou as credenciais. Confira client ID e client secret.")
        _syncpay_token = data["access_token"]
        _syncpay_token_exp = time.time() + int(data.get("expires_in", 3600))
        return _syncpay_token
    except HTTPException:
        raise
    except Exception:
        log.exception("SyncPay auth")
        raise HTTPException(502, "Não foi possível conectar à SyncPay agora.")


@app.get("/api/syncpay/config")
async def syncpay_config():
    # Apenas a chave pública é enviada ao navegador; nunca enviar client_secret.
    return {"public_key": SYNCPAY_PUBLIC_KEY, "pix_enabled": bool(SYNCPAY_PLAN_PIX_TOKEN),
            "card_enabled": bool(SYNCPAY_PLAN_CARD_TOKEN and SYNCPAY_PUBLIC_KEY)}


@app.post("/api/mercadopago/checkout")
@app.post("/api/syncpay/checkout")
async def syncpay_checkout(request: Request):
    user = await current_user(request)
    rate_limit(f"checkout:{user['id']}", 5, 600)
    if user.get("plan") == "pro":
        raise HTTPException(400, "Você já tem o plano Completo.")
    b = await body_of(request)
    method = clean(b.get("method") or "pix", 12).lower()
    document = re.sub(r"\D", "", str(b.get("document") or ""))
    phone = re.sub(r"\D", "", str(b.get("phone") or ""))
    card_token = clean(b.get("card_token"), 500)
    if method not in ("pix", "card"):
        raise HTTPException(400, "Método de pagamento inválido.")
    plan_token = SYNCPAY_PLAN_PIX_TOKEN if method == "pix" else SYNCPAY_PLAN_CARD_TOKEN
    if not plan_token:
        raise HTTPException(503, "Este método ainda não foi configurado no painel do Render.")
    if len(document) not in (11, 14):
        raise HTTPException(400, "Informe um CPF ou CNPJ válido para a cobrança.")
    if len(phone) < 10 or len(phone) > 13:
        raise HTTPException(400, "Informe um telefone com DDD.")
    if method == "card" and not card_token.startswith("card_token."):
        raise HTTPException(400, "Não foi possível tokenizar o cartão. Confira os dados e tente novamente.")
    # Evita duplicar assinatura se o usuário tocar duas vezes ou repetir após timeout.
    existing = await db.users.find_one({"id": user["id"], "syncpay_subscription_token": {"$exists": True}}, {"_id": 0})
    if existing and existing.get("syncpay_status") not in DEAD_SUB:
        raise HTTPException(409, "Já existe uma assinatura em andamento nesta conta. Se você desistiu do pagamento, cancele a tentativa na tela do plano e comece de novo.")
    token = await syncpay_access_token()
    payload = {"name": clean(user.get("name") or user.get("email"), 120),
               "email": user["email"], "document": document, "phone": phone}
    if method == "card":
        payload["card"] = {"token": card_token}
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(f"{SYNCPAY_API}/subscription-plans/{plan_token}/enroll", headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"
            }, json=payload)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            log.warning("SyncPay enroll recusado: status=%s method=%s", r.status_code, method)
            msg = data.get("message") or data.get("error_description") or "A SyncPay recusou a adesão. Confira se o plano e a conta estão habilitados."
            raise HTTPException(502, clean(msg, 300))
        sub = data.get("data") if isinstance(data.get("data"), dict) else data
        sub_token = sub.get("subscription_token") or sub.get("token")
        status = sub.get("status", "pending_first_payment")
        if not sub_token:
            log.error("Resposta inesperada da SyncPay no enroll")
            raise HTTPException(502, "A SyncPay respondeu sem o identificador da assinatura.")
        await db.users.update_one({"id": user["id"]}, {"$set": {
            "syncpay_subscription_token": sub_token, "syncpay_status": status,
            "syncpay_billing_method": method, "syncpay_document_last4": document[-4:],
            "syncpay_phone_last4": phone[-4:], "plan": "free"
        }})
        payment = sub.get("payment") if isinstance(sub.get("payment"), dict) else {}
        return {"ok": True, "method": method, "status": status,
                "payment_status": payment.get("status"), "pix_code": payment.get("pix_code"),
                "qr_code": payment.get("qr_code"), "mandate_id": payment.get("mandate_id"),
                "card_brand": payment.get("card_brand"), "card_last4": payment.get("card_last4")}
    except HTTPException:
        raise
    except Exception:
        log.exception("SyncPay enrollment")
        raise HTTPException(502, "Não foi possível iniciar a assinatura na SyncPay. Tente novamente.")


@app.post("/api/syncpay/cancel")
async def syncpay_cancel(request: Request):
    user = await current_user(request)
    sub_token = user.get("syncpay_subscription_token")
    if not sub_token:
        raise HTTPException(400, "Não encontrei uma assinatura SyncPay vinculada a esta conta.")
    access = await syncpay_access_token()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.patch(f"{SYNCPAY_API}/subscriptions/{sub_token}/cancel",
                headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
                json={"reason": "Cancelamento solicitado pelo cliente no Preço Certo"})
        data = r.json() if r.content else {}
        if r.status_code >= 400 and r.status_code != 404:  # 404 = a SyncPay já não conhece essa assinatura
            log.warning("SyncPay cancel recusado: status=%s", r.status_code)
            raise HTTPException(502, data.get("message") or "A SyncPay não conseguiu cancelar agora.")
        await db.users.update_one({"id": user["id"]}, {"$set": {"plan": "free", "syncpay_status": "cancelled"}})
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:
        log.exception("SyncPay cancel")
        raise HTTPException(502, "Não foi possível cancelar agora. Tente novamente.")


# ---------------------------------------------------------------- webhook SyncPay
def check_syncpay_signature(raw: bytes, header: str, secret: str, now_ts: float, tolerance: int = 300) -> str:
    # Formato documentado nos webhooks de transação: "t=<unix>,v1=<hex>", HMAC-SHA256 de "{t}.{corpo bruto}".
    # Retorna: ok, absent, malformed, expired ou mismatch.
    if not header:
        return "absent"
    t, sigs = "", []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            t = v
        elif k == "v1":
            sigs.append(v.lower())
    if not t.isdigit() or not sigs:
        return "malformed"
    if abs(now_ts - int(t)) > tolerance:
        return "expired"
    digest = hmac.new(secret.encode(), t.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return "ok" if any(hmac.compare_digest(digest, s) for s in sigs) else "mismatch"


def syncpay_sub_token(event: dict) -> str:
    nested = event.get("subscription") if isinstance(event.get("subscription"), dict) else {}
    return str(event.get("subscription_token") or nested.get("token") or "")


def syncpay_event_key(event: dict, raw: bytes) -> str:
    # Chave de idempotência: o aviso de assinatura não traz event_id, então usamos evento + assinatura + data.
    eid = str(event.get("event_id") or "")
    if eid:
        return "id:" + eid
    kind, sub, when = str(event.get("event") or ""), syncpay_sub_token(event), str(event.get("occurred_at") or "")
    if kind and sub and when:
        return "ev:" + hashlib.sha256(f"{kind}|{sub}|{when}".encode()).hexdigest()
    return "raw:" + hashlib.sha256(raw).hexdigest()


def plan_update_for_status(status: str):
    # Status documentados da assinatura: pending_first_payment, active, overdue, suspended, cancelled.
    s = (status or "").lower()
    if s == "active":
        return {"plan": "pro", "syncpay_status": "active"}
    if s == "overdue":
        # Em atraso: mantém o plano. A tolerância é a do plano na SyncPay (grace_period_days); depois ela vira suspended.
        return {"syncpay_status": "overdue"}
    if s in ("suspended", "cancelled", "canceled"):
        return {"plan": "free", "syncpay_status": "suspended" if s == "suspended" else "cancelled"}
    if s == "pending_first_payment":
        return {"syncpay_status": "pending_first_payment"}
    return None


async def syncpay_fetch_subscription(sub_token: str):
    access = await syncpay_access_token()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{SYNCPAY_API}/subscriptions/{sub_token}",
                headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
    except Exception:
        log.exception("SyncPay detalhe da assinatura")
        raise HTTPException(502, "Não foi possível consultar a SyncPay agora.")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        log.warning("SyncPay detalhe da assinatura recusado: status=%s", r.status_code)
        raise HTTPException(502, "A SyncPay recusou a consulta da assinatura.")
    data = r.json() if r.content else {}
    return data.get("data") if isinstance(data.get("data"), dict) else data


@app.post("/api/syncpay/webhook")
async def syncpay_webhook(request: Request):
    if not SYNCPAY_WEBHOOK_SECRET:
        raise HTTPException(503, "Configure SYNCPAY_WEBHOOK_SECRET no Render antes de ativar o webhook.")
    raw = await request.body()
    result = check_syncpay_signature(raw, request.headers.get("x-syncpay-signature", ""), SYNCPAY_WEBHOOK_SECRET, time.time())
    if result != "ok":
        names = sorted(k for k in request.headers.keys() if k.lower().startswith("x-syncpay"))
        log.warning("Webhook SyncPay: assinatura=%s modo=%s cabecalhos=%s", result, SYNCPAY_SIGNATURE_MODE, names)
        if SYNCPAY_SIGNATURE_MODE == "enforce":
            raise HTTPException(401, "Assinatura do webhook inválida.")
    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "JSON inválido.")
    if not isinstance(event, dict):
        raise HTTPException(400, "JSON inválido.")
    payload = event.get("data") if isinstance(event.get("data"), dict) else event
    sub_token = syncpay_sub_token(payload)
    kind = str(event.get("event") or event.get("type") or "")
    if not sub_token:
        return {"received": True}  # aviso que não é de assinatura (ex.: transação): não altera nada
    # Idempotência: reivindica a chave do evento antes de processar; repetido = ignora.
    key = syncpay_event_key(event, raw)
    try:
        res = await db.syncpay_events.update_one({"key": key}, {"$setOnInsert": {
            "key": key, "event": kind, "subscription_token": sub_token, "received_at": now()}}, upsert=True)
        if res.upserted_id is None:
            return {"received": True, "duplicate": True}
    except Exception as e:
        if "DuplicateKey" in type(e).__name__:
            return {"received": True, "duplicate": True}
        raise
    try:
        # O aviso é só o gatilho: quem decide é o status real da assinatura, consultado na SyncPay.
        sub = await syncpay_fetch_subscription(sub_token)
        if sub is None:
            log.warning("Webhook SyncPay: assinatura não encontrada na SyncPay (evento=%s)", kind)
            return {"received": True}
        plan = sub.get("plan") if isinstance(sub.get("plan"), dict) else {}
        allowed = {t for t in (SYNCPAY_PLAN_PIX_TOKEN, SYNCPAY_PLAN_CARD_TOKEN) if t}
        if plan.get("token") and allowed and plan["token"] not in allowed:
            log.warning("Webhook SyncPay: assinatura de outro plano; ignorada (evento=%s)", kind)
            return {"received": True}
        status = str(sub.get("status") or "")
        update = plan_update_for_status(status)
        if not update:
            log.warning("Webhook SyncPay: status desconhecido=%s (evento=%s); nada alterado", status, kind)
            return {"received": True}
        res = await db.users.update_one({"syncpay_subscription_token": sub_token}, {"$set": update})
        log.info("Webhook SyncPay: evento=%s status=%s usuarios=%s", kind, status, res.matched_count)
        return {"received": True}
    except BaseException:
        # Falhou no meio: libera a chave para a SyncPay poder reenviar o aviso.
        await db.syncpay_events.delete_one({"key": key})
        raise
