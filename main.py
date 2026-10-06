import os
import logging
import httpx
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

# Configuración básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp Meta Webhook", version="1.0.1")

# Token de verificación secreto (configurado en Dokploy)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mi_token_secreto_super_seguro")

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/muse-glimmer-30b")
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_API_KEY = NVIDIA_API_KEY.strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
for _name, _val in (("NVIDIA_API_KEY", NVIDIA_API_KEY), ("WHATSAPP_TOKEN", WHATSAPP_TOKEN)):
    if not _val:
        logger.error(f"La variable de entorno {_name} está vacía o no definida")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v21.0")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Eres un asistente útil. Responde de forma breve y clara en el idioma del usuario.",
)


async def ask_nvidia(text: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            NVIDIA_URL,
            headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
            json={
                "model": NVIDIA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.6,
                "max_tokens": 512,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


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


async def reply_with_ai(phone_number_id: str, to: str, text: str) -> None:
    try:
        answer = await ask_nvidia(text)
        await send_whatsapp(phone_number_id, to, answer)
        logger.info(f"Respuesta enviada a {to}")
    except Exception as e:
        logger.error(f"Error respondiendo a {to}: {e}")


@app.get("/")
def health_check():
    return {"status": "ok", "service": "whatsapp-webhook"}

@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """
    Endpoint para la verificación inicial de Meta.
    """
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("Webhook verificado exitosamente por Meta.")
        # Meta requiere que se devuelva el challenge en texto plano
        return PlainTextResponse(content=hub_challenge, status_code=200)
    
    logger.warning("Fallo en la verificación del Webhook.")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, 
        detail="Token de verificación inválido"
    )

@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint para recibir mensajes y notificaciones de estado desde WhatsApp.
    """
    try:
        body = await request.json()
        
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
