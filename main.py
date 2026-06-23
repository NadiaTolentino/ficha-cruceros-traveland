import os
import re
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger("uvicorn.error")

KOMMO_TOKEN = os.environ.get("KOMMO_TOKEN", "")
KOMMO_BASE  = "https://travelandrd.kommo.com/api/v4"
SERVICE_URL = os.environ.get("SERVICE_URL", "https://ficha-cruceros-traveland-production.up.railway.app")

# ── IDs pipeline EMBUDO CRUCEROS ─────────────────────────────────────────────
PIPELINE_CRUCEROS_ID  = 11573556
INCOMING_LEADS_ID     = 88880268   # Etapa inicial — donde cae el lead al llenar el form
COTIZACION_ID         = 88880272   # Etapa destino — cuando el agente guarda la ficha interna

# ── Campos de contacto en Kommo ──────────────────────────────────────────────
PHONE_FIELD_ID = 343300
EMAIL_FIELD_ID = 343302

WHATSAPP_MSG = (
    "¡Hola! 🚢 Hemos recibido tu solicitud de crucero. "
    "Una de nuestras asesoras te estará contactando con la propuesta "
    "para empezar tu próxima aventura en el mar. ¡Pronto te escribimos!"
)

def _clean_phone(phone: str) -> str:
    """Devuelve número limpio con código de país para RD (+1809/829/849)."""
    digits = re.sub(r"\D", "", phone)
    # Si tiene 10 dígitos y empieza con 8 → RD (NANP +1)
    if len(digits) == 10 and digits[0] == "8":
        digits = "1" + digits
    # Si ya tiene 11 dígitos con 1 al inicio → ok
    return "+" + digits


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Modelo del formulario público ─────────────────────────────────────────────
class FichaCruceroBody(BaseModel):
    nombre:      str
    whatsapp:    str
    correo:      Optional[str] = ""
    crucero:     str
    fecha_viaje: str
    adultos:     int
    ninos:       int = 0


@app.post("/ficha")
async def crear_lead_crucero(body: FichaCruceroBody):
    """
    Recibe el formulario público, crea contacto + lead en Kommo
    en el pipeline EMBUDO CRUCEROS > Incoming leads.
    """
    headers = {"Authorization": f"Bearer {KOMMO_TOKEN}"}

    ninos_text = f" · {body.ninos} niño(s)" if body.ninos > 0 else ""
    nota_texto = (
        f"🚢 Solicitud de crucero recibida\n"
        f"Crucero: {body.crucero}\n"
        f"Fecha deseada: {body.fecha_viaje}\n"
        f"Pasajeros: {body.adultos} adulto(s){ninos_text}\n"
        f"WhatsApp: {body.whatsapp}"
        + (f"\nCorreo: {body.correo}" if body.correo else "")
    )

    async with httpx.AsyncClient(timeout=15) as client:

        # 1. Crear contacto
        contact_payload = [{
            "name": body.nombre,
            "custom_fields_values": [
                {"field_id": PHONE_FIELD_ID, "values": [{"value": body.whatsapp, "enum_code": "WORK"}]},
            ]
        }]
        if body.correo:
            contact_payload[0]["custom_fields_values"].append(
                {"field_id": EMAIL_FIELD_ID, "values": [{"value": body.correo, "enum_code": "WORK"}]}
            )

        contact_id = None
        try:
            r = await client.post(f"{KOMMO_BASE}/contacts", headers=headers, json=contact_payload)
            contacts = r.json().get("_embedded", {}).get("contacts", [])
            if contacts:
                contact_id = contacts[0]["id"]
                logger.info(f"Contacto creado: {contact_id}")
        except Exception as e:
            logger.error(f"Error creando contacto: {e}")

        # 2. Crear lead en EMBUDO CRUCEROS > Cotización directamente
        lead_payload = [{
            "name": f"{body.crucero} — {body.nombre}",
            "pipeline_id": PIPELINE_CRUCEROS_ID,
            "status_id":   COTIZACION_ID,
        }]
        if contact_id:
            lead_payload[0]["_embedded"] = {"contacts": [{"id": contact_id}]}

        lead_id = None
        try:
            r = await client.post(f"{KOMMO_BASE}/leads", headers=headers, json=lead_payload)
            leads = r.json().get("_embedded", {}).get("leads", [])
            if leads:
                lead_id = leads[0]["id"]
                logger.info(f"Lead creado: {lead_id}")
        except Exception as e:
            logger.error(f"Error creando lead: {e}")

        # 3. Agregar nota al lead
        if lead_id:
            try:
                await client.post(
                    f"{KOMMO_BASE}/leads/notes",
                    headers=headers,
                    json=[{
                        "entity_id": lead_id,
                        "note_type": "common",
                        "params": {"text": nota_texto}
                    }]
                )
            except Exception as e:
                logger.error(f"Error creando nota: {e}")

        # 4. Enviar WhatsApp de confirmación al cliente vía Kommo
        try:
            phone = _clean_phone(body.whatsapp)
            r = await client.post(
                f"{KOMMO_BASE}/chats/messages/unsolicited",
                headers=headers,
                json={"phone": phone, "message": WHATSAPP_MSG}
            )
            logger.info(f"WhatsApp enviado a {phone}: {r.status_code} {r.text[:200]}")
        except Exception as e:
            logger.error(f"Error enviando WhatsApp: {e}")

    return {"success": True, "lead_id": lead_id, "contact_id": contact_id}


# ── Webhook de Kommo (para futuras automatizaciones) ──────────────────────────
@app.post("/webhook/cruceros")
async def webhook_cruceros(request: Request):
    """
    Webhook para disparar cuando un lead llega a una etapa específica.
    Configurar en Kommo: Settings > Webhooks > URL = {SERVICE_URL}/webhook/cruceros
    """
    try:
        form = await request.form()
        data = dict(form)
        logger.info(f"Webhook cruceros: {data}")

        lead_id   = None
        status_id = None
        for key, value in data.items():
            if key == "leads[status][0][id]":
                lead_id = int(value)
            elif key == "leads[status][0][status_id]":
                status_id = int(value)

        if not lead_id:
            return {"status": "ignored"}

        logger.info(f"Lead {lead_id} llegó a etapa {status_id}")
        # Aquí puedes agregar lógica por etapa, ej:
        # if status_id == COTIZACION_ID: crear tarea, enviar WhatsApp, etc.

        return {"status": "ok", "lead_id": lead_id, "status_id": status_id}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        return {"status": "error", "detail": str(e)}
