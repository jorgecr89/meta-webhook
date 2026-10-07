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
  predeterminado `meta/llama-guard-4-12b`.
- `NVIDIA_MODEL` (opcional): identificador del modelo conversacional.
- `NVIDIA_MAX_TOKENS` (opcional): límite de tokens de la respuesta; predeterminado
  `2048`.
- `WHATSAPP_TOKEN` (requerida): token de acceso de WhatsApp Cloud API.
