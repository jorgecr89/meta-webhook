# Documentación Técnica (Desarrolladores)

## 1. Guía de instalación y configuración

### 1.1 Requisitos
- Python 3.11
- Docker (opcional, para empaquetar/desplegar)
- Cuenta de Meta for Developers con WhatsApp Cloud API, clave de NVIDIA y (opcional) cuenta de servicio de Google

### 1.2 Entorno local
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Alternativa: crear un archivo .env en la raíz con KEY=valor (se carga con python-dotenv;
# las variables del sistema tienen prioridad sobre el .env)
$env:VERIFY_TOKEN = "mi_token"
$env:NVIDIA_API_KEY = "nvapi-..."
$env:WHATSAPP_TOKEN = "EAA..."
$env:APP_SECRET = "secreto_de_la_app_meta"   # recomendado
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
Para recibir webhooks de Meta en local expón el puerto con un túnel HTTPS (p. ej. ngrok) y registra
`https://<túnel>/webhook` en Meta con el `VERIFY_TOKEN`.

### 1.3 Docker
```bash
docker build -t meta-webhook .
docker run -p 8000:8000 --env-file .env meta-webhook
```
La imagen ejecuta `uvicorn main:app --workers 2`. Como hay 2 workers, el estado de agendado se
comparte vía SQLite (`BOOKING_DB_PATH`); el estado de bloqueos de IP es por worker.

### 1.4 Variables de entorno
| Variable | Req. | Predeterminado | Descripción |
|----------|:----:|----------------|-------------|
| `VERIFY_TOKEN` | Sí | valor de ejemplo (**cámbialo**) | Token de verificación del webhook con Meta. |
| `WHATSAPP_TOKEN` | Sí | – | Token de acceso de WhatsApp Cloud API (usuario del sistema, permisos `whatsapp_business_messaging`/`management`). |
| `NVIDIA_API_KEY` | Sí | – | Clave para LLM y Llama Guard. |
| `APP_SECRET` | Rec. | – | Secreto de la app Meta; habilita la verificación de `X-Hub-Signature-256`. |
| `NVIDIA_MODEL` | No | `meta/muse-glimmer-30b` | Modelo conversacional. |
| `LLAMA_GUARD_MODEL` | No | `nvidia/llama-3.1-nemotron-safety-guard-8b-v3` | Modelo de moderación. |
| `NVIDIA_MAX_TOKENS` | No | `2048` | Límite de tokens de respuesta. |
| `NVIDIA_TIMEOUT_SECONDS` | No | `120` | Timeout del LLM. |
| `LLAMA_GUARD_TIMEOUT_SECONDS` | No | `60` | Timeout del guard. |
| `LLAMA_GUARD_FAIL_OPEN` | No | `false` | Si `true`, omite moderación ante timeout/red. |
| `GRAPH_API_VERSION` | No | `v21.0` | Versión de Graph API. |
| `KNOWLEDGE_DIR` | No | `./knowledge` | Carpeta con la base de conocimiento (`.md`/`.txt`). |
| `SYSTEM_PROMPT` | No | prompt del taller | Instrucciones del asistente. |
| `MAX_BODY_BYTES` | No | `262144` | Tamaño máximo del cuerpo. |
| `SECURITY_MAX_STRIKES` | No | `5` | Infracciones antes de bloquear una IP. |
| `SECURITY_STRIKE_WINDOW` | No | `600` | Ventana (s) de infracciones. |
| `SECURITY_BAN_SECONDS` | No | `3600` | Duración del bloqueo. |
| `TRUSTED_PROXY_HOPS` | No | `1` | Proxies de confianza delante (`X-Forwarded-For`). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | No* | – | JSON de la cuenta de servicio (o en base64). *Sin ella el agendado se desactiva. |
| `GOOGLE_CALENDAR_ID` | Sí* | – | ID del calendario. Se lee de `.env` o del entorno. *Sin ella el agendado se desactiva. |
| `CALENDAR_TIMEZONE` | No | `America/Mexico_City` | Zona horaria. |
| `APPOINTMENT_MINUTES` | No | `60` | Duración de cada cita. |
| `APPOINTMENT_CAPACITY` | No | `1` | Citas simultáneas permitidas. |
| `APPOINTMENT_MAX_DAYS_AHEAD` | No | `60` | Anticipación máxima. |
| `APPOINTMENT_MIN_LEAD_MINUTES` | No | `60` | Anticipación mínima. |
| `BOOKING_DB_PATH` | No | `/tmp/booking_sessions.db` | Archivo SQLite de sesiones. |

Constantes en código (`booking.py`): `BUSINESS_HOURS` (L-V 8-18, sáb 9-14, dom cerrado),
`SLOT_STEP_MINUTES=30`, `SESSION_TTL_SECONDS=1800`. En `main.py`: aviso de progreso cada 15 s.

### 1.5 Configuración de Google Calendar
1. Crear proyecto en Google Cloud, habilitar **Google Calendar API**, crear cuenta de servicio y clave JSON.
2. Compartir el calendario del taller con el correo de la cuenta de servicio (*Realizar cambios en eventos*).
3. Definir `GOOGLE_SERVICE_ACCOUNT_JSON` y `GOOGLE_CALENDAR_ID` (ambas requeridas para activar el agendado).

### 1.6 Verificación rápida
```bash
curl http://localhost:8000/
# {"status":"ok","service":"whatsapp-webhook"}

curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=mi_token&hub.challenge=123"
# 123
```
Al arrancar revisa en el log: `Config guard: ... WHATSAPP_TOKEN len=N fin=XXXX` y el estado del agendado.

### 1.7 Solución de problemas
| Síntoma | Causa probable |
|---------|----------------|
| `Graph API 401 ... code 190` al responder | `WHATSAPP_TOKEN` expirado, de otra app o sin acceso al `phone_number_id`. Recibir mensajes no usa este token. |
| `403 Firma inválida` en POST | `APP_SECRET` incorrecto. |
| «Llama Guard no disponible» | Timeout/red con NVIDIA; con `LLAMA_GUARD_FAIL_OPEN` solo se advierte. |
| Agendado desactivado | Falta `GOOGLE_SERVICE_ACCOUNT_JSON`. |

## 2. Documentación de API / Endpoints

La documentación automática (`/docs`, `/redoc`, `/openapi.json`) está **desactivada** por seguridad.
Cualquier otra ruta responde `404` y cuenta como infracción.

### 2.1 Endpoints expuestos
#### `GET /` — Health check
- Respuesta `200`: `{"status":"ok","service":"whatsapp-webhook"}`

#### `GET /webhook` — Verificación de Meta
| Parámetro (query) | Descripción |
|-------------------|-------------|
| `hub.mode` | Debe ser `subscribe`. |
| `hub.verify_token` | Debe coincidir con `VERIFY_TOKEN` (comparación en tiempo constante). |
| `hub.challenge` | Valor a devolver. |

- `200` `text/plain`: el `hub.challenge`.
- `403`: token/modo inválido (suma una infracción a la IP).

#### `POST /webhook` — Eventos de WhatsApp
- Encabezado `X-Hub-Signature-256: sha256=<hmac>` (obligatorio si `APP_SECRET` está definido).
- Cuerpo: JSON de Meta, máximo `MAX_BODY_BYTES`.
```json
{
  "object": "whatsapp_business_account",
  "entry": [{
    "changes": [{
      "value": {
        "metadata": {"phone_number_id": "1350064664848272"},
        "messages": [{"from": "5217226275556", "type": "text", "text": {"body": "Hola"}}]
      }
    }]
  }]
}
```
| Código | Significado |
|--------|-------------|
| `200 {"status":"success"}` | Procesado; la respuesta se envía en segundo plano. |
| `200 {"status":"ignored"}` | Cuerpo no es un objeto o `object` distinto de `whatsapp_business_account`. |
| `200 {"status":"error"}` | Error interno (se devuelve 200 para evitar reintentos de Meta). |
| `403` | Firma inválida. |
| `413` | Cuerpo demasiado grande. |

Solo se procesan mensajes `type == "text"`; las actualizaciones `statuses` solo se registran en el log.
Otros códigos del middleware: `403` IP bloqueada, `404` ruta no permitida, `405` método no permitido.

### 2.2 APIs externas consumidas
| API | Llamada | Función en código |
|-----|---------|-------------------|
| Meta Graph | `POST https://graph.facebook.com/{ver}/{phone_number_id}/messages` con `Authorization: Bearer {WHATSAPP_TOKEN}`; cuerpo `{"messaging_product":"whatsapp","to":..,"type":"text","text":{"body":..}}` (máx. 4096 caracteres) | `send_whatsapp` |
| NVIDIA | `POST https://integrate.api.nvidia.com/v1/chat/completions` (LLM, con `tools` si el agendado está activo) | `ask_nvidia` |
| NVIDIA | Misma URL con `LLAMA_GUARD_MODEL`; veredicto `safe`/`unsafe` o JSON `User Safety`/`Response Safety` | `is_safe_with_llama_guard` |
| Google Calendar v3 | `events` (consulta y creación) con token OAuth de cuenta de servicio | `booking._calendar_request` |

Nota: los números móviles mexicanos `521XXXXXXXXXX` se normalizan a `52XXXXXXXXXX` antes de enviar (`normalize_mx`).

### 2.3 Herramienta `agendar_cita` (function calling)
| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `servicio` | string | Servicio solicitado. |
| `fecha` | string | `YYYY-MM-DD`. |
| `hora` | string | `HH:MM` 24 h. |
| `nombre` | string | Nombre del cliente. |

Todos son opcionales; solo se incluyen los datos que dijo el cliente. Lo que falte lo pregunta el flujo guiado.

## 3. Flujo interno de `reply_with_ai`

1. Si el cliente tiene sesión de agendado activa → `booking.handle_message` y fin.
2. Si el mensaje expresa intención de agendar (`booking.wants_booking`): se envía el aviso de inicio y arranca
   la tarea de progreso, que envía «Procesando...» cada 15 s.
3. Llama Guard valida la pregunta; si es `UNSAFE` → mensaje fijo y fin.
4. `ask_nvidia`: el LLM responde o pide `agendar_cita`. Al terminar se detiene la tarea de progreso.
5. Si hubo `tool_call` → `booking.start_from_tool`. Si no, y hay intención de agendar, se usa el flujo guiado.
6. Llama Guard valida la respuesta; se envía o se bloquea.
7. Ante timeout o excepción se detiene la tarea de progreso y se intenta el flujo guiado de citas.

## 4. Comentarios en el código

Criterio del proyecto: comentar solo la lógica no evidente. Puntos ya documentados en el código:
- `client_ip` / `register_strike`: por qué se usa la IP del final de `X-Forwarded-For` y por qué no se bloquean IP privadas.
- `guard_allows`: solo fallos de red pueden omitirse con `LLAMA_GUARD_FAIL_OPEN`.
- `is_safe_with_llama_guard`: formatos de veredicto soportados (JSON de Nemotron / texto de Llama Guard).
- `booking._advance`: revalidación del horario al confirmar por posibles carreras.
- `booking.start_from_tool`: los datos del LLM pasan por las mismas validaciones del flujo guiado.

Al agregar lógica, documenta el *porqué* (reglas de negocio, trampas de la API de Meta/NVIDIA), no el *qué*.

## 5. Consideraciones de seguridad
- Nunca registrar ni versionar tokens (`WHATSAPP_TOKEN`, `NVIDIA_API_KEY`, JSON de Google). El log solo muestra longitud y últimos 4 caracteres.
- Cambia el valor predeterminado de `VERIFY_TOKEN` y define `APP_SECRET`.
- `GOOGLE_CALENDAR_ID` ya no tiene valor en el código: se carga de `.env` (con `python-dotenv`) o del entorno.
  `.env` está en `.dockerignore` y no debe versionarse; en Docker/Dokploy define las variables en el entorno.
