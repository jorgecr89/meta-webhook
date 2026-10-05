import os
import logging
from fastapi import FastAPI, Request, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

# Configuración básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WhatsApp Meta Webhook", version="1.0.1")

# Token de verificación secreto (configurado en Dokploy)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mi_token_secreto_super_seguro")

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
async def receive_message(request: Request):
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
