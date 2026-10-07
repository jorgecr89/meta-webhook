"""Agendado de citas por WhatsApp en Google Calendar (flujo guiado, sin depender del LLM)."""
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

CALENDAR_ID = os.getenv(
    "GOOGLE_CALENDAR_ID",
    "8a694e63d3de42f2be715ccd967c6a8af472a386f60bc96450a4505aa5bb5e69@group.calendar.google.com",
).strip()
TZ = ZoneInfo(os.getenv("CALENDAR_TIMEZONE", "America/Mexico_City"))
APPOINTMENT_MINUTES = int(os.getenv("APPOINTMENT_MINUTES", "60"))
APPOINTMENT_CAPACITY = int(os.getenv("APPOINTMENT_CAPACITY", "1"))
MAX_DAYS_AHEAD = int(os.getenv("APPOINTMENT_MAX_DAYS_AHEAD", "60"))
MIN_LEAD_MINUTES = int(os.getenv("APPOINTMENT_MIN_LEAD_MINUTES", "60"))
SESSION_TTL_SECONDS = 30 * 60
SLOT_STEP_MINUTES = 30
DB_PATH = Path(os.getenv("BOOKING_DB_PATH", "/tmp/booking_sessions.db"))
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Horario del taller (weekday: 0=lunes). Domingo cerrado.
BUSINESS_HOURS = {
    **{d: (dtime(8, 0), dtime(18, 0)) for d in range(5)},
    5: (dtime(9, 0), dtime(14, 0)),
}

DAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
DAY_NAMES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

INTENT_RE = re.compile(r"\b(agendar|agenda|agendame|reservar|reserva|programar|apartar|cita|citas)\b")
NON_BOOKING_RE = re.compile(r"\b(cancel\w*|reprogram\w*|cambiar|politica\w*|cuanto|precio\w*|costo\w*)\b")
CANCEL_WORDS = {"cancelar", "cancela", "salir", "ya no", "olvidalo", "no gracias"}
YES_WORDS = {"si", "sii", "confirmo", "confirmar", "ok", "okay", "claro", "dale", "correcto", "listo", "va", "de acuerdo"}
NO_WORDS = {"no", "nel", "incorrecto"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w\s:/.\-]", " ", text).strip()


def _clean(text: str, limit: int) -> str:
    text = "".join(c for c in text if c.isprintable())
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _credentials_info() -> dict | None:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        if not raw.startswith("{"):
            raw = base64.b64decode(raw).decode("utf-8")
        return json.loads(raw)
    except Exception:
        logger.exception("GOOGLE_SERVICE_ACCOUNT_JSON inválido (use el JSON completo o su versión en base64)")
        return None


_CREDS_INFO = _credentials_info()
ENABLED = _CREDS_INFO is not None
_creds = None


async def _access_token() -> str:
    global _creds
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    if _creds is None:
        _creds = service_account.Credentials.from_service_account_info(_CREDS_INFO, scopes=SCOPES)
    if not _creds.valid:
        await asyncio.to_thread(_creds.refresh, Request())
    return _creds.token


async def _calendar_request(method: str, path: str, **kwargs) -> dict:
    token = await _access_token()
    url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(CALENDAR_ID, safe='')}{path}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    if r.is_error:
        logger.error("Google Calendar HTTP %s: %s", r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()


def _event_interval(event: dict) -> tuple[datetime, datetime] | None:
    if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
        return None
    start, end = event.get("start", {}), event.get("end", {})
    if "dateTime" in start and "dateTime" in end:
        return datetime.fromisoformat(start["dateTime"]).astimezone(TZ), datetime.fromisoformat(end["dateTime"]).astimezone(TZ)
    if "date" in start and "date" in end:
        s = datetime.combine(date.fromisoformat(start["date"]), dtime(0, 0), TZ)
        e = datetime.combine(date.fromisoformat(end["date"]), dtime(0, 0), TZ)
        return s, e
    return None


async def _busy_intervals(day: date) -> list[tuple[datetime, datetime]]:
    start = datetime.combine(day, dtime(0, 0), TZ)
    data = await _calendar_request(
        "GET",
        "/events",
        params={
            "timeMin": start.isoformat(),
            "timeMax": (start + timedelta(days=1)).isoformat(),
            "singleEvents": "true",
            "maxResults": 250,
        },
    )
    return [i for i in map(_event_interval, data.get("items", [])) if i]


def _slot_free(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return sum(1 for s, e in busy if s < end and e > start) < APPOINTMENT_CAPACITY


def _slot_problem(start: datetime) -> str | None:
    """Reglas que no dependen del calendario. Devuelve el motivo si el horario no es válido."""
    now = datetime.now(TZ)
    hours = BUSINESS_HOURS.get(start.weekday())
    end = start + timedelta(minutes=APPOINTMENT_MINUTES)
    if start < now + timedelta(minutes=MIN_LEAD_MINUTES):
        return f"Ese horario ya pasó o es demasiado pronto; necesitamos al menos {MIN_LEAD_MINUTES} minutos de anticipación."
    if start.date() > now.date() + timedelta(days=MAX_DAYS_AHEAD):
        return f"Solo agendamos con un máximo de {MAX_DAYS_AHEAD} días de anticipación."
    if hours is None:
        return "Los domingos el taller está cerrado."
    if start.time() < hours[0] or end.time() > hours[1] or end.date() != start.date():
        return f"El {DAY_NAMES[start.weekday()]} atendemos de {hours[0]:%H:%M} a {hours[1]:%H:%M}."
    if start.minute % SLOT_STEP_MINUTES:
        return f"Las citas se agendan en bloques de {SLOT_STEP_MINUTES} minutos (por ejemplo 10:00 o 10:30)."
    return None


async def _free_slots(day: date, limit: int = 5) -> list[str]:
    hours = BUSINESS_HOURS.get(day.weekday())
    if hours is None:
        return []
    busy = await _busy_intervals(day)
    slots, cur = [], datetime.combine(day, hours[0], TZ)
    while cur + timedelta(minutes=APPOINTMENT_MINUTES) <= datetime.combine(day, hours[1], TZ):
        end = cur + timedelta(minutes=APPOINTMENT_MINUTES)
        if _slot_problem(cur) is None and _slot_free(busy, cur, end):
            slots.append(f"{cur:%H:%M}")
            if len(slots) >= limit:
                break
        cur += timedelta(minutes=SLOT_STEP_MINUTES)
    return slots


def parse_date(text: str, today: date) -> date | None:
    t = _norm(text)
    if re.search(r"\bpasado manana\b", t):
        return today + timedelta(days=2)
    if re.search(r"\bmanana\b", t):
        return today + timedelta(days=1)
    if re.search(r"\bhoy\b", t):
        return today
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        y, mo, d = map(int, m.groups())
        return _safe_date(y, mo, d)
    m = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        return _resolve_year(d, mo, m.group(3), today)
    m = re.search(r"\b(\d{1,2})\s+(?:de\s+)?([a-z]+)(?:\s+(?:de\s+)?(\d{4}))?", t)
    if m and m.group(2) in MONTHS:
        return _resolve_year(int(m.group(1)), MONTHS[m.group(2)], m.group(3), today)
    for name, wd in DAYS.items():
        if re.search(rf"\b{name}\b", t):
            return today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    return None


def _safe_date(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _resolve_year(d: int, mo: int, year: str | None, today: date) -> date | None:
    if year:
        y = int(year)
        y += 2000 if y < 100 else 0
        return _safe_date(y, mo, d)
    result = _safe_date(today.year, mo, d)
    if result and result < today:
        result = _safe_date(today.year + 1, mo, d)
    return result


def parse_time(text: str) -> dtime | None:
    t = _norm(text)
    m = re.search(
        r"\b(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm|a m|p m|de la manana|de la tarde|de la noche|hrs|hs|horas|h)?\b", t
    )
    if not m:
        return None
    hour, minute, suffix = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "")
    if hour > 23 or minute > 59:
        return None
    pm = suffix in ("pm", "p m", "de la tarde", "de la noche")
    am = suffix in ("am", "a m", "de la manana")
    if pm and hour < 12:
        hour += 12
    elif am and hour == 12:
        hour = 0
    elif not (pm or am) and 1 <= hour <= 7:
        hour += 12  # el taller abre desde las 8: "a las 3" es 15:00
    return dtime(hour, minute)


# --- Sesiones (SQLite compartido entre workers) ---

def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (phone TEXT PRIMARY KEY, step TEXT, data TEXT, updated REAL)")
    return conn


def _load(phone: str) -> tuple[str, dict] | None:
    with _db() as conn:
        row = conn.execute("SELECT step, data, updated FROM sessions WHERE phone = ?", (phone,)).fetchone()
    if not row or time.time() - row[2] > SESSION_TTL_SECONDS:
        return None
    return row[0], json.loads(row[1])


def _save(phone: str, step: str, data: dict) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (phone, step, data, updated) VALUES (?, ?, ?, ?)",
            (phone, step, json.dumps(data), time.time()),
        )


def _clear(phone: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))


def has_session(phone: str) -> bool:
    return _load(phone) is not None


def wants_booking(text: str) -> bool:
    t = _norm(text)
    return bool(INTENT_RE.search(t)) and not NON_BOOKING_RE.search(t)


ASK_SERVICE = "¡Claro! Te ayudo a agendar tu cita. ¿Qué servicio necesitas? (por ejemplo: cambio de aceite, frenos, diagnóstico)"

def _fmt(start: datetime) -> str:
    return f"{DAY_NAMES[start.weekday()]} {start:%d/%m/%Y} a las {start:%H:%M}"


async def _create_event(phone: str, data: dict) -> None:
    start = datetime.fromisoformat(data["start"])
    end = start + timedelta(minutes=APPOINTMENT_MINUTES)
    await _calendar_request(
        "POST",
        "/events",
        json={
            "summary": f"Cita: {data['service']} - {data['name']}",
            "description": f"Cliente: {data['name']}\nWhatsApp: {phone}\nServicio: {data['service']}\n(Agendado por el asistente de WhatsApp)",
            "start": {"dateTime": start.isoformat(), "timeZone": str(TZ)},
            "end": {"dateTime": end.isoformat(), "timeZone": str(TZ)},
        },
    )


async def _check_slot(start: datetime) -> str | None:
    """Devuelve un mensaje de error si el horario no se puede usar, o None si está libre."""
    problem = _slot_problem(start)
    if problem:
        return problem
    busy = await _busy_intervals(start.date())
    if _slot_free(busy, start, start + timedelta(minutes=APPOINTMENT_MINUTES)):
        return None
    free = await _free_slots(start.date())
    if free:
        return "Ese horario ya está ocupado. Horarios libres ese día: " + ", ".join(free) + "."
    return "Ese día ya no tiene horarios libres. ¿Qué otro día te acomoda?"


async def handle_message(phone: str, text: str) -> str | None:
    """Procesa un mensaje del flujo de citas. Devuelve la respuesta, o None si no es tema de citas."""
    if not ENABLED:
        return None
    session = _load(phone)
    if session is None and not wants_booking(text):
        return None

    norm = _norm(text)
    if session and norm in CANCEL_WORDS:
        _clear(phone)
        return "Listo, cancelé el agendado de tu cita. Si quieres retomarlo, escribe «agendar cita»."

    try:
        step, data = session if session else ("service", {})
        if session is None:
            _save(phone, "service", {})
            return ASK_SERVICE
        return await _advance(phone, step, data, text, norm)
    except httpx.HTTPError:
        logger.exception("Error con Google Calendar al agendar para %s", phone)
        return "No pude consultar la agenda en este momento. Intenta de nuevo en unos minutos o comunícate directamente con el taller."


async def _advance(phone: str, step: str, data: dict, text: str, norm: str) -> str:
    today = datetime.now(TZ).date()

    if step == "service":
        data["service"] = _clean(text, 100)
        if not data["service"]:
            return "¿Qué servicio necesitas?"
        _save(phone, "date", data)
        return "Perfecto. ¿Para qué día quieres la cita? (por ejemplo: mañana, viernes o 15/03)"

    if step == "date":
        day = parse_date(text, today)
        if day is None:
            return "No entendí la fecha. Puedes escribir «mañana», «viernes» o «15/03»."
        if day < today:
            return "Esa fecha ya pasó. ¿Qué otro día te acomoda?"
        if BUSINESS_HOURS.get(day.weekday()) is None:
            return "Los domingos el taller está cerrado. ¿Qué otro día te acomoda?"
        data["date"] = day.isoformat()
        data.pop("start", None)
        free = await _free_slots(day)
        if not free:
            return "Ese día ya no tiene horarios libres. ¿Qué otro día te acomoda?"
        _save(phone, "time", data)
        return f"Para el {DAY_NAMES[day.weekday()]} {day:%d/%m/%Y}. Horarios libres: {', '.join(free)}. ¿A qué hora te gustaría?"

    if step == "time":
        hour = parse_time(text)
        if hour is None:
            return "No entendí la hora. Puedes escribir por ejemplo «10:30» o «3 pm»."
        start = datetime.combine(date.fromisoformat(data["date"]), hour, TZ)
        error = await _check_slot(start)
        if error:
            return error + " ¿Qué otra hora prefieres?"
        data["start"] = start.isoformat()
        _save(phone, "name", data)
        return "Excelente, ese horario está disponible. ¿A nombre de quién agendo la cita?"

    if step == "name":
        name = _clean(text, 80)
        if len(name) < 2:
            return "¿A nombre de quién agendo la cita?"
        data["name"] = name
        _save(phone, "confirm", data)
        start = datetime.fromisoformat(data["start"])
        return (
            f"Confirma tu cita:\n• Servicio: {data['service']}\n• Fecha: {_fmt(start)}\n"
            f"• Nombre: {name}\n\n¿Todo correcto? Responde «sí» para confirmar o «no» para cancelar."
        )

    if norm in YES_WORDS:
        start = datetime.fromisoformat(data["start"])
        # Se revalida por si alguien tomó el horario mientras se confirmaba.
        error = await _check_slot(start)
        if error:
            _save(phone, "time", data)
            return error + " ¿Qué otra hora prefieres?"
        await _create_event(phone, data)
        _clear(phone)
        logger.info("Cita creada para %s: %s", phone, data["start"])
        return f"¡Listo! Tu cita quedó agendada para el {_fmt(start)}. Si necesitas cancelar o reprogramar, avísanos con al menos 2 horas de anticipación."
    if norm in NO_WORDS:
        _clear(phone)
        return "De acuerdo, no agendé la cita. Si quieres intentarlo de nuevo, escribe «agendar cita»."
    return "Responde «sí» para confirmar la cita o «no» para cancelar."


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "agendar_cita",
            "description": (
                "Inicia el agendado de una cita en el taller. Úsala siempre que el cliente quiera agendar, "
                "reservar o apartar una cita. Incluye solo los datos que el cliente haya dicho; no inventes ninguno."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "servicio": {"type": "string", "description": "Servicio solicitado, por ejemplo cambio de aceite"},
                    "fecha": {"type": "string", "description": "Fecha de la cita en formato YYYY-MM-DD"},
                    "hora": {"type": "string", "description": "Hora de la cita en formato HH:MM de 24 horas"},
                    "nombre": {"type": "string", "description": "Nombre del cliente"},
                },
            },
        },
    }
]


def now_context() -> str:
    now = datetime.now(TZ)
    return f"Fecha y hora actuales: {DAY_NAMES[now.weekday()]} {now:%Y-%m-%d %H:%M} ({TZ})."


async def start_from_tool(phone: str, args: dict) -> str | None:
    """Precarga la sesión con los datos extraídos por el LLM; cada dato pasa por las mismas validaciones del flujo guiado."""
    if not ENABLED:
        return None
    _save(phone, "service", {})
    reply = ASK_SERVICE
    values = [("service", args.get("servicio")), ("date", args.get("fecha")), ("time", args.get("hora")), ("name", args.get("nombre"))]
    try:
        for expected, value in values:
            session = _load(phone)
            if session is None or session[0] != expected or not isinstance(value, str) or not value.strip():
                break
            reply = await _advance(phone, expected, session[1], value, _norm(value))
    except httpx.HTTPError:
        logger.exception("Error con Google Calendar al agendar para %s", phone)
        return "No pude consultar la agenda en este momento. Intenta de nuevo en unos minutos o comunícate directamente con el taller."
    return reply
