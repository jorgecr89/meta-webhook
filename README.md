# meta-webhook
Webhook META

## Protección con Llama Guard

Antes de enviar una pregunta al modelo conversacional, el webhook la clasifica con
Llama Guard. También clasifica la respuesta generada antes de enviarla a WhatsApp.
Si el contenido se clasifica como `UNSAFE`, se bloquea y se envía un mensaje fijo
al usuario. Si NVIDIA falla o devuelve una clasificación inesperada, no se envía
el contenido.

Configura estas variables de entorno en el despliegue:

- `NVIDIA_API_KEY` (requerida): clave de NVIDIA, utilizada para ambos modelos.
- `LLAMA_GUARD_MODEL` (opcional): identificador del modelo de Llama Guard;
  predeterminado `nvidia/llama-3.1-nemotron-safety-guard-8b-v3` (también acepta modelos Llama Guard con respuesta `safe`/`unsafe`).
- `NVIDIA_MODEL` (opcional): identificador del modelo conversacional.
- `NVIDIA_MAX_TOKENS` (opcional): límite de tokens de la respuesta; predeterminado
  `2048`.
- `NVIDIA_TIMEOUT_SECONDS` (opcional): tiempo máximo de espera por llamada a NVIDIA en segundos; predeterminado `120`. Si se agota, no se omite la moderación y se informa al usuario para que reintente.
- `LLAMA_GUARD_TIMEOUT_SECONDS` (opcional): tiempo máximo para la llamada de moderación; predeterminado `60` segundos. Es independiente del timeout del modelo conversacional.
- `LLAMA_GUARD_FAIL_OPEN` (opcional, predeterminado `false`): si es `true`, cuando el guard no responda (timeout o error de red) el mensaje se procesa sin moderación y se registra una advertencia. Menos seguro; por defecto el bot no responde si no puede validar. Los veredictos inesperados y errores HTTP siempre bloquean.
- `WHATSAPP_TOKEN` (requerida): token de acceso de WhatsApp Cloud API.

## Protección contra peticiones maliciosas

- Solo existen las rutas `/` y `/webhook`; cualquier otra (`.env`, `*.php`, `.git`, etc.)
  responde 404 sin procesamiento y cuenta como infracción. `/docs` y `/openapi.json`
  están desactivadas.
- Tras `SECURITY_MAX_STRIKES` infracciones (predeterminado 5) en
  `SECURITY_STRIKE_WINDOW` segundos (600), la IP se bloquea `SECURITY_BAN_SECONDS`
  segundos (3600). Las IP privadas nunca se bloquean. El estado está en memoria y es
  independiente por worker.
- La IP real se toma de `X-Forwarded-For`; `TRUSTED_PROXY_HOPS` (predeterminado 1)
  indica cuántos proxies de confianza hay delante.
- `APP_SECRET` (recomendada): secreto de la app de Meta. Si se define, los POST a
  `/webhook` deben traer una firma `X-Hub-Signature-256` válida.
- `MAX_BODY_BYTES` (predeterminado 262144): tamaño máximo del cuerpo.
