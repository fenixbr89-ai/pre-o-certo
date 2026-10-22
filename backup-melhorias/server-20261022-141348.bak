"""Preço Certo — API (FastAPI + MongoDB + Stripe + Resend).

O front (public/index.html) chama estes endpoints, sempre sob /api.
Variáveis de ambiente: veja .env.example e o README.md.
"""
import asyncio
import base64
import hashlib
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
import stripe
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
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")  # opcional
PRICE_CENTS = int(os.environ.get("PRICE_CENTS", "1990"))  # R$ 19,90

if not MONGO_URL:
    raise RuntimeError("Defina MONGO_URL.")
if len(JWT_SECRET) < 32:
    raise RuntimeError("Defina JWT_SECRET com pelo menos 32 caracteres.")
stripe.api_key = STRIPE_SECRET_KEY

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
    await db.users.create_index("stripe_customer_id", sparse=True)
    await db.stores.create_index("user_id", unique=True)
    await db.resets.create_index("token_hash", unique=True)
    await db.resets.create_index("expires_at", expireAfterSeconds=0)
    yield


app = FastAPI(title="Preço Certo API", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[SITE_URL],
    allow_methods=["GET", "POST", "PUT"],
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
    return jwt.encode({"sub": uid, "exp": exp}, JWT_SECRET, algorithm="HS256")


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
    if trial_expired(user):
        path = request.url.path
        if path not in ("/api/store", "/api/mercadopago/checkout"):
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
    if str(u.get("plan", "free")).lower() == "pro":
        expiry = u.get("plan_expires_at")
        if expiry:
            if isinstance(expiry, str):
                try:
                    expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    return True
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= expiry
        return False
    started = trial_ms(u)
    return not started or time.time() * 1000 >= started + TRIAL_DAYS * 86400000


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
    await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": await asyncio.to_thread(hash_pw, pw)}})
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
    return data


@app.put("/api/store")
async def put_store(request: Request):
    user = await current_user(request)
    rate_limit(f"store:{user['id']}", 120, 60)
    data = await body_of(request)
    for k in ("plan", "trial", "since", "photo"):
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


# ---------------------------------------------------------------- pagamento (Stripe)
def _need_stripe():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Pagamento ainda não configurado.")


@app.post("/api/checkout")
async def checkout(request: Request):
    _need_stripe()
    user = await current_user(request)
    rate_limit(f"checkout:{user['id']}", 10, 600)
    if user.get("plan") == "pro":
        raise HTTPException(400, "Você já tem o plano Completo.")
    cust = user.get("stripe_customer_id")
    try:
        if not cust:
            c = await asyncio.to_thread(
                stripe.Customer.create, email=user["email"], name=user.get("name") or None,
                metadata={"user_id": user["id"]},
            )
            cust = c["id"]
            await db.users.update_one({"id": user["id"]}, {"$set": {"stripe_customer_id": cust}})
        line = (
            {"price": STRIPE_PRICE_ID, "quantity": 1}
            if STRIPE_PRICE_ID
            else {
                "quantity": 1,
                "price_data": {
                    "currency": "brl", "unit_amount": PRICE_CENTS,
                    "recurring": {"interval": "month"},
                    "product_data": {"name": "Preço Certo — Plano Completo"},
                },
            }
        )
        s = await asyncio.to_thread(
            stripe.checkout.Session.create,
            mode="subscription", customer=cust, client_reference_id=user["id"],
            line_items=[line], allow_promotion_codes=True, locale="pt-BR",
            subscription_data={"metadata": {"user_id": user["id"]}},
            success_url=f"{SITE_URL}/?paid=1", cancel_url=f"{SITE_URL}/?canceled=1",
        )
    except HTTPException:
        raise
    except Exception:
        log.exception("Stripe checkout")
        raise HTTPException(502, "Não foi possível abrir o pagamento agora. Tente de novo.")
    return {"url": s["url"]}


@app.post("/api/portal")
async def portal(request: Request):
    _need_stripe()
    user = await current_user(request)
    cust = user.get("stripe_customer_id")
    if not cust:
        raise HTTPException(400, "Nenhuma assinatura encontrada.")
    try:
        s = await asyncio.to_thread(
            stripe.billing_portal.Session.create, customer=cust, return_url=f"{SITE_URL}/"
        )
    except Exception:
        log.exception("Stripe portal")
        raise HTTPException(502, "Não foi possível abrir o portal agora. Tente de novo.")
    return {"url": s["url"]}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhook não configurado.")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(400, "Assinatura inválida.")
    kind, obj = event["type"], event["data"]["object"]

    if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
        uid = obj.get("client_reference_id")
        if uid and obj.get("payment_status") in ("paid", "no_payment_required"):
            await db.users.update_one(
                {"id": uid},
                {"$set": {"plan": "pro", "stripe_customer_id": obj.get("customer"),
                          "stripe_subscription_id": obj.get("subscription")}},
            )
    elif kind in ("customer.subscription.created", "customer.subscription.updated",
                  "customer.subscription.deleted"):
        status, sub_id, cust = obj.get("status"), obj.get("id"), obj.get("customer")
        uid = (obj.get("metadata") or {}).get("user_id")
        user = (await db.users.find_one({"stripe_customer_id": cust}, {"_id": 0})) if cust else None
        if not user and uid:
            user = await db.users.find_one({"id": uid}, {"_id": 0})
        if user:
            current = user.get("stripe_subscription_id")
            # Ignora eventos de uma assinatura antiga já substituída por outra.
            if not (current and current != sub_id and kind == "customer.subscription.deleted"):
                active = kind != "customer.subscription.deleted" and status in ("active", "trialing")
                await db.users.update_one(
                    {"id": user["id"]},
                    {"$set": {"plan": "pro" if active else "free",
                              "stripe_subscription_id": sub_id, "stripe_status": status}},
                )
    return {"received": True}
