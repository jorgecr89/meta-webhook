import os
import hmac
import hashlib
import ipaddress
import json
import logging
import time
from collections import deque
from pathlib import Path
import httpx
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, Response

# Configuración básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Se desactivan la documentación y el esquema OpenAPI para no exponer rutas innecesarias
app = FastAPI(title="WhatsApp Meta Webhook", version="1.0.1", docs_url=None, redoc_url=None, openapi_url=None)

# --- Protección contra escaneos y peticiones maliciosas ---
APP_SECRET = os.getenv("APP_SECRET", "").strip()
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "262144"))
SECURITY_MAX_STRIKES = int(os.getenv("SECURITY_MAX_STRIKES", "5"))
SECURITY_STRIKE_WINDOW = int(os.getenv("SECURITY_STRIKE_WINDOW", "600"))
SECURITY_BAN_SECONDS = int(os.getenv("SECURITY_BAN_SECONDS", "3600"))
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))
ALLOWED_ROUTES = {
    "/": {"GET", "HEAD"},
    "/webhook": {"GET", "POST"},
}
_strikes: dict[str, deque] = {}
_bans: dict[str, float] = {}
_MAX_TRACKED_IPS = 10000

if not APP_SECRET:
    logger.warning("APP_SECRET no definido: no se verificará la firma X-Hub-Signature-256 de Meta")


def client_ip(request: Request) -> str:
    # Detrás del proxy, la IP real es la que éste añadió al final de X-Forwarded-For
    forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(forwarded) >= TRUSTED_PROXY_HOPS > 0:
        return forwarded[-TRUSTED_PROXY_HOPS]
    return request.client.host if request.client else "unknown"


def is_banned(ip: str) -> bool:
    until = _bans.get(ip)
    if until is None:
        return False
    if until <= time.monotonic():
        _bans.pop(ip, None)
        return False
    return True


def register_strike(ip: str, reason: str) -> None:
    now = time.monotonic()
    if len(_strikes) > _MAX_TRACKED_IPS:
        _strikes.clear()
    hits = _strikes.setdefault(ip, deque())
    hits.append(now)
    while hits and now - hits[0] > SECURITY_STRIKE_WINDOW:
        hits.popleft()
    logger.warning(f"Petición sospechosa de {ip}: {reason} ({len(hits)}/{SECURITY_MAX_STRIKES})")
    if len(hits) < SECURITY_MAX_STRIKES:
        return
    try:
        if not ipaddress.ip_address(ip).is_global:
            # IP interna (p. ej. el proxy): bloquearla dejaría sin servicio a todos los clientes
            return
    except ValueError:
        return
    _bans[ip] = now + SECURITY_BAN_SECONDS
    _strikes.pop(ip, None)
    logger.error(f"IP {ip} bloqueada por {SECURITY_BAN_SECONDS}s")


def valid_meta_signature(raw_body: bytes, header: str | None) -> bool:
    if not APP_SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


@app.middleware("http")
async def security_guard(request: Request, call_next):
    ip = client_ip(request)
    if is_banned(ip):
        return Response(status_code=403)

    allowed_methods = ALLOWED_ROUTES.get(request.url.path)
    if allowed_methods is None:
        register_strike(ip, f"ruta no permitida {request.method} {request.url.path[:100]}")
        return Response(status_code=404)
    if request.method not in allowed_methods:
        register_strike(ip, f"método no permitido {request.method} {request.url.path}")
        return Response(status_code=405)

    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        register_strike(ip, "cuerpo demasiado grande")
        return Response(status_code=413)

    return await call_next(request)

# Token de verificación secreto (configurado en Dokploy)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mi_token_secreto_super_seguro")

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/muse-glimmer-30b")
LLAMA_GUARD_MODEL = os.getenv("LLAMA_GUARD_MODEL", "nvidia/llama-3.1-nemotron-safety-guard-8b-v3")
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_TIMEOUT_SECONDS = float(os.getenv("NVIDIA_TIMEOUT_SECONDS", "120"))
LLAMA_GUARD_TIMEOUT_SECONDS = float(os.getenv("LLAMA_GUARD_TIMEOUT_SECONDS", "60"))
LLAMA_GUARD_FAIL_OPEN = os.getenv("LLAMA_GUARD_FAIL_OPEN", "false").strip().lower() in ("1", "true", "yes")
NVIDIA_API_KEY = NVIDIA_API_KEY.strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
for _name, _val in (("NVIDIA_API_KEY", NVIDIA_API_KEY), ("WHATSAPP_TOKEN", WHATSAPP_TOKEN)):
    if not _val:
        logger.error(f"La variable de entorno {_name} está vacía o no definida")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v21.0")
KNOWLEDGE_DIR = Path(os.getenv("KNOWLEDGE_DIR", Path(__file__).parent / "knowledge"))


def load_knowledge() -> str:
    # Documento pequeño: se inyecta completo en el prompt, sin fragmentar.
    parts = [
        f.read_text(encoding="utf-8")
        for f in sorted(KNOWLEDGE_DIR.glob("*"))
        if f.suffix.lower() in (".md", ".txt")
    ]
    return "\n\n".join(parts)


KNOWLEDGE = load_knowledge()
logger.info(f"Base de conocimiento cargada: {len(KNOWLEDGE)} caracteres")

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Eres el asistente virtual del Taller Mecánico AutoMotor Pro. Atiende a los clientes por WhatsApp "
    "de forma amable, profesional y breve. Responde ÚNICAMENTE con la información del contexto. "
    "Si la respuesta no está en el contexto, indica amablemente que el cliente debe comunicarse "
    "directamente con el taller. No inventes precios, horarios ni políticas.",
)


async def ask_nvidia(text: str) -> str:
    async with httpx.AsyncClient(timeout=NVIDIA_TIMEOUT_SECONDS) as client:
        r = await client.post(
            NVIDIA_URL,
            headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
            json={
                "model": NVIDIA_MODEL,
                "messages": [
                    {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nContexto oficial del taller:\n{KNOWLEDGE}"},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.6,
                "max_tokens": int(os.getenv("NVIDIA_MAX_TOKENS", "2048")),
            },
        )
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        if not content:
            logger.error(f"Respuesta vacía de NVIDIA (finish_reason={choice.get('finish_reason')}): {data}")
            return "Disculpa, no pude generar una respuesta en este momento. Intenta de nuevo, por favor."
        return content


async def is_safe_with_llama_guard(question: str, answer: str | None = None) -> bool:
    # Llama Guard evalúa el último turno: la pregunta del usuario o, si se indica, la respuesta del asistente.
    if not NVIDIA_API_KEY:
        raise RuntimeError("La variable de entorno NVIDIA_API_KEY está vacía o no definida")

    messages = [{"role": "user", "content": question}]
    if answer is not None:
        messages.append({"role": "assistant", "content": answer})

    async with httpx.AsyncClient(timeout=LLAMA_GUARD_TIMEOUT_SECONDS) as client:
        response = await client.post(
            NVIDIA_URL,
            headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
            json={
                "model": LLAMA_GUARD_MODEL,
                "messages": messages,
                "temperature": 0.2,
                "top_p": 0.7,
                "max_tokens": 100,
                "stream": False,
            },
        )
        if response.is_error:
            logger.error(
                "Llama Guard respondió HTTP %s: %s",
                response.status_code,
                response.text[:1000],
            )
        response.raise_for_status()
        data = response.json()

    result = (data["choices"][0]["message"].get("content") or "").strip()
    verdict = ""
    try:
        # Nemotron Safety Guard responde JSON: {"User Safety": "safe", "Response Safety": "unsafe", ...}
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            key = "Response Safety" if answer is not None else "User Safety"
            verdict = str(parsed.get(key, "")).strip().upper()
            if answer is not None and not verdict:
                verdict = str(parsed.get("User Safety", "")).strip().upper()
    except ValueError:
        # Llama Guard responde texto plano: "safe" o "unsafe\nS<n>"
        verdict = result.splitlines()[0].strip().upper() if result else ""
    if verdict == "SAFE":
        return True
    if verdict == "UNSAFE":
        return False
    raise RuntimeError(f"Respuesta de clasificación inesperada del guard: {result[:200]!r}")


def normalize_mx(number: str) -> str:
    # Meta entrega los móviles mexicanos como 521XXXXXXXXXX pero la API espera 52XXXXXXXXXX
    if number.startswith("521") and len(number) == 13:
        return "52" + number[3:]
    return number


async def send_whatsapp(phone_number_id: str, to: str, text: str) -> None:
    to = normalize_mx(to)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text[:4096]},
            },
        )
        if r.is_error:
            logger.error(f"Graph API {r.status_code}: {r.text}")
        r.raise_for_status()


async def guard_allows(question: str, answer: str | None = None) -> bool:
    try:
        return await is_safe_with_llama_guard(question, answer)
    except httpx.TransportError:
        # Solo timeouts y fallos de red; veredictos inesperados o errores HTTP siguen bloqueando.
        if not LLAMA_GUARD_FAIL_OPEN:
            raise
        logger.warning("Llama Guard no disponible; LLAMA_GUARD_FAIL_OPEN activo, se omite la moderación")
        return True


async def reply_with_ai(phone_number_id: str, to: str, text: str) -> None:
    try:
        if not await guard_allows(text):
            await send_whatsapp(
                phone_number_id,
                to,
                "No puedo ayudar con esa solicitud. Si necesitas información del taller, dime qué servicio buscas.",
            )
            logger.info(f"Pregunta bloqueada por Llama Guard para {to}")
            return

        answer = await ask_nvidia(text)
        if not await guard_allows(text, answer):
            await send_whatsapp(
                phone_number_id,
                to,
                "No pude generar una respuesta segura. Reformula tu pregunta o comunícate directamente con el taller.",
            )
            logger.warning(f"Respuesta bloqueada por Llama Guard para {to}")
            return

        await send_whatsapp(phone_number_id, to, answer)
        logger.info(f"Respuesta enviada a {to}")
    except httpx.TimeoutException:
        logger.exception(f"Timeout de NVIDIA al procesar el mensaje de {to}")
        try:
            await send_whatsapp(
                phone_number_id,
                to,
                "El servicio está tardando más de lo esperado. No pude validar tu mensaje de forma segura; intenta de nuevo en unos minutos.",
            )
        except Exception:
            logger.exception(f"No se pudo enviar el aviso de timeout a {to}")
    except Exception:
        logger.exception(f"Error respondiendo a {to}")


@app.get("/")
def health_check():
    return {"status": "ok", "service": "whatsapp-webhook"}

@app.get("/webhook")
async def verify_webhook(
    request: Request,
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """
    Endpoint para la verificación inicial de Meta.
    """
    if hub_mode == "subscribe" and hub_challenge and hmac.compare_digest(
        (hub_verify_token or "").encode(), VERIFY_TOKEN.encode()
    ):
        logger.info("Webhook verificado exitosamente por Meta.")
        # Meta requiere que se devuelva el challenge en texto plano
        return PlainTextResponse(content=hub_challenge, status_code=200)

    register_strike(client_ip(request), "verificación de webhook fallida")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, 
        detail="Token de verificación inválido"
    )

@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint para recibir mensajes y notificaciones de estado desde WhatsApp.
    """
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        register_strike(client_ip(request), "cuerpo demasiado grande")
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    if not valid_meta_signature(raw_body, request.headers.get("x-hub-signature-256")):
        register_strike(client_ip(request), "firma X-Hub-Signature-256 inválida")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Firma inválida")

    try:
        body = json.loads(raw_body)
        if not isinstance(body, dict):
            return {"status": "ignored"}
        
        # Validación básica de la estructura de Meta
        if body.get("object") == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    # Identificar si es un mensaje entrante
                    if "messages" in value:
                        messages = value["messages"]
                        for msg in messages:
                            logger.info(f"Nuevo mensaje recibido de {msg.get('from')}: {msg.get('text', {}).get('body')}")
                            text = msg.get("text", {}).get("body")
                            phone_number_id = value.get("metadata", {}).get("phone_number_id")
                            if msg.get("type") == "text" and text and phone_number_id:
                                background_tasks.add_task(reply_with_ai, phone_number_id, msg["from"], text)

                    # Identificar si es una actualización de estado (enviado, entregado, leído)
                    elif "statuses" in value:
                        statuses = value["statuses"]
                        for status_update in statuses:
                            logger.info(f"Actualización de estado: {status_update.get('status')} para mensaje {status_update.get('id')}")

            # Meta requiere un 200 OK rápido para no reenviar el payload
            return {"status": "success"}
        
        return {"status": "ignored"}
        
    except Exception as e:
        logger.error(f"Error procesando el webhook: {str(e)}")
        # Siempre devuelve 200 a Meta para evitar retries infinitos, a menos que el servicio esté caído.
        return {"status": "error"}
