import os
import re
import time
import xmlrpc.client
from ddgs import DDGS
from google import genai
from google.genai import types
from pydantic import BaseModel

# Configuración desde los Secretos de GitHub
ODOO_URL = os.environ["ODOO_URL"]
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

ID_ETAPA_CHEQUEADO = 7  # Etapa destino
MAX_LEADS_POR_EJECUCION = 40  # Lote seguro para cuidar los límites gratuitos

client_ai = genai.Client(api_key=GEMINI_API_KEY)

class ResultadoValidacion(BaseModel):
    email_corregido_o_encontrado: str
    celular_corregido_o_encontrado: str
    datos_validos_y_verificados: bool
    motivo: str

def conectar_odoo():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

def limpiar_formato_celular(numero):
    if not numero:
        return ""
    solo_digitos = re.sub(r'\D', '', str(numero))
    if solo_digitos.startswith("549") and len(solo_digitos) == 13:
        return f"+{solo_digitos}"
    if solo_digitos.startswith("54") and len(solo_digitos) == 12:
        return f"+549{solo_digitos[2:]}"
    if solo_digitos.startswith("0") and len(solo_digitos) == 11:
        return f"+549{solo_digitos[1:]}"
    if len(solo_digitos) == 10:
        return f"+549{solo_digitos}"
    return ""

def buscar_en_internet_gratis(query):
    try:
        resultados = DDGS().text(query, region="ar-es", max_results=6)
        if not resultados:
            return "Sin resultados de búsqueda."
        snippets = []
        for item in resultados:
            snippets.append(f"Título: {item.get('title')} | Desc: {item.get('body')} | Link: {item.get('href')}")
        return "\n".join(snippets)
    except Exception as e:
        return f"Error al buscar en internet: {str(e)}"

def analizar_lead_con_ia(lead, info_internet):
    prompt = f"""
    Eres un auditor de datos CRM experto en Argentina. Analiza este lead de Odoo y compáralo con los resultados de búsqueda en internet (en Córdoba / Argentina).

    DATOS ACTUALES EN ODOO:
    - Nombre del Lead: {lead.get('name')}
    - Empresa / Contacto: {lead.get('partner_name') or lead.get('contact_name') or 'No especificado'}
    - Ciudad / País: {lead.get('city') or 'Córdoba'} {lead.get('country_id') if lead.get('country_id') else 'Argentina'}
    - Web: {lead.get('website') or 'No especificado'}
    - Email actual: {lead.get('email_from') or 'VACÍO'}
    - Teléfono actual: {lead.get('phone') or 'VACÍO'}

    RESULTADOS EN INTERNET:
    {info_internet}

    REGLAS ESTRICTAS DE VALIDACIÓN:
    1. TELÉFONOS (SOLO CELULARES):
       - IGNORA Y DESCARTA POR COMPLETO cualquier teléfono fijo (por ejemplo, números de Córdoba que empiezan con 0351 4..., 03573 4..., o números de línea fija sin 15/9). Nunca devuelvas un número fijo.
       - Busca únicamente teléfonos CELULARES o WhatsApp (números con 15, +54 9, o celulares de Argentina).
       - Si encuentras un celular o si el teléfono actual es un celular válido, devuélvelo SIEMPRE en el formato exacto sin espacios ni guiones: +549 + código de área sin 0 + número sin 15 (Ejemplo exacto: +5493573412724 o +5493511234567).
       - Si el teléfono actual en Odoo es un número fijo y no encuentras un celular en internet, devuelve "" en celular_corregido_o_encontrado.
    2. EMAILS:
       - Si falta el email, búscalo en los resultados de internet y complétalo. Si no aparece con certeza, déjalo vacío ("").
       - Si ya tiene email, verifica que pertenezca a esa empresa/persona y corrígelo si hay un error evidente.
    3. 'datos_validos_y_verificados' debe ser TRUE únicamente si:
       - Lograste confirmar en internet que los datos presentes (celular y/o email) son reales y correctos.
       - Si faltaba un dato y no lo encontraste, pero el dato que SÍ tenía (celular válido o email) pudo ser verificado como correcto en internet, también pon TRUE.
    4. 'datos_validos_y_verificados' debe ser FALSE si:
       - No pudiste confirmar ninguno de los datos en internet.
       - Detectaste que el dato cargado es incorrecto o es solo un teléfono fijo sin email válido.
       - El lead quedó sin email y sin celular.
    """

    response = client_ai.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ResultadoValidacion,
            temperature=0.1,
        ),
    )
    return response.parsed

def ejecutar_agente():
    print("Conectando a Odoo...", flush=True)
    uid, models = conectar_odoo()

    etapas = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.stage', 'search_read',
        [[['name', 'ilike', 'Prospectos']]], {'fields': ['id', 'name'], 'limit': 1}
    )
    
    if not etapas:
        print("Error: No se encontró ninguna etapa llamada 'Prospectos'.", flush=True)
        return

    id_prospectos = etapas[0]['id']
    print(f"Etapa encontrada: {etapas[0]['name']} (ID: {id_prospectos})", flush=True)

    campos = ['id', 'name', 'partner_name', 'contact_name', 'email_from', 'phone', 'website', 'city', 'country_id']
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
        [[['stage_id', '=', id_prospectos]]], {'fields': campos, 'limit': MAX_LEADS_POR_EJECUCION}
    )

    print(f"Revisando un lote de {len(leads)} leads en Prospectos...", flush=True)

    for lead in leads:
        print(f"\n--- Revisando Lead ID {lead['id']}: {lead['name']} ---", flush=True)
        ciudad = lead.get('city') or 'Cordoba Argentina'
        termino_busqueda = f"{lead.get('partner_name') or lead.get('name')} {ciudad} whatsapp celular contacto email"
        
        info_internet = buscar_en_internet_gratis(termino_busqueda)
        
        try:
            resultado = analizar_lead_con_ia(lead, info_internet)
        except Exception as e:
            print(f"Error con Gemini en este lead: {e}", flush=True)
            time.sleep(5)
            continue

        celular_formateado = limpiar_formato_celular(resultado.celular_corregido_o_encontrado)
        print(f"Resultado IA: Válido={resultado.datos_validos_y_verificados} | Celular={celular_formateado} | Motivo: {resultado.motivo}", flush=True)

        valores_actualizar = {}

        if resultado.email_corregido_o_encontrado and resultado.email_corregido_o_encontrado != lead.get('email_from'):
            valores_actualizar['email_from'] = resultado.email_corregido_o_encontrado

        if celular_formateado and celular_formateado != lead.get('phone'):
            valores_actualizar['phone'] = celular_formateado

        if resultado.datos_validos_y_verificados:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO
            print(f"-> Lead aprobado. Moviendo a etapa ID {ID_ETAPA_CHEQUEADO}.", flush=True)
        else:
            print("-> Lead NO aprobado o incompleto. Se mantiene en Prospectos.", flush=True)

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])
        
        mensaje_nota = f"<b>Agente de Verificación:</b> {resultado.motivo}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [lead['id']], {'body': mensaje_nota})

        time.sleep(4)

if __name__ == "__main__":
    ejecutar_agente()
