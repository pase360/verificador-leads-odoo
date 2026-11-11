import os
import time
import xmlrpc.client
from duckduckgo_search import DDGS
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
MAX_LEADS_POR_EJECUCION = 40  # Lote seguro para no exceder límites gratuitos en una sola corrida

client_ai = genai.Client(api_key=GEMINI_API_KEY)

class ResultadoValidacion(BaseModel):
    email_corregido_o_encontrado: str
    telefono_corregido_o_encontrado: str
    datos_validos_y_verificados: bool
    motivo: str

def conectar_odoo():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

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
    Eres un auditor de datos CRM experto. Analiza este lead de Odoo y compáralo con los resultados de búsqueda en internet (en Córdoba / Argentina).

    DATOS ACTUALES EN ODOO:
    - Nombre del Lead: {lead.get('name')}
    - Empresa / Contacto: {lead.get('partner_name') or lead.get('contact_name') or 'No especificado'}
    - Ciudad / País: {lead.get('city') or 'Córdoba'} {lead.get('country_id') if lead.get('country_id') else 'Argentina'}
    - Web: {lead.get('website') or 'No especificado'}
    - Email actual: {lead.get('email_from') or 'VACÍO'}
    - Teléfono actual: {lead.get('phone') or lead.get('mobile') or 'VACÍO'}

    RESULTADOS EN INTERNET:
    {info_internet}

    REGLAS ESTRICTAS DE VALIDACIÓN:
    1. Si falta el email o el teléfono, búscalos en los resultados de internet y complétalos. Si no aparecen con certeza, déjalos vacíos ("").
    2. Si ya tiene email o teléfono, verifica en los resultados que pertenezcan realmente a esa empresa/persona y no tengan errores de tipeo ni sean falsos. Corrígelos si ves un error evidente en internet.
    3. 'datos_validos_y_verificados' debe ser TRUE únicamente si:
       - Lograste confirmar en internet que los datos presentes son reales y correctos.
       - Si faltaba un dato y no lo encontraste, pero el dato que SÍ tenía pudo ser verificado como correcto en internet, también pon TRUE.
    4. 'datos_validos_y_verificados' debe ser FALSE si:
       - No pudiste confirmar ninguno de los datos en internet.
       - Detectaste que el teléfono o email cargado es incorrecto/inválido y no pudiste encontrar el real para reemplazarlo.
       - El lead quedó sin email y sin teléfono.
    """

    response = client_ai.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ResultadoValidacion,
            temperature=0.1,
        ),
    )
    return response.parsed

def ejecutar_agente():
    print("Conectando a Odoo...")
    uid, models = conectar_odoo()

    # 1. Buscar el ID de la etapa "Prospectos"
    etapas = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.stage', 'search_read',
        [[['name', 'ilike', 'Prospectos']]], {'fields': ['id', 'name'], 'limit': 1}
    )
    
    if not etapas:
        print("Error: No se encontró ninguna etapa llamada 'Prospectos'.")
        return

    id_prospectos = etapas[0]['id']
    print(f"Etapa encontrada: {etapas[0]['name']} (ID: {id_prospectos})")

    # 2. Traer leads en Prospectos (de a lotes para cuidar el límite gratuito)
    campos = ['id', 'name', 'partner_name', 'contact_name', 'email_from', 'phone', 'mobile', 'website', 'city', 'country_id']
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
        [[['stage_id', '=', id_prospectos]]], {'fields': campos, 'limit': MAX_LEADS_POR_EJECUCION}
    )

    print(f"Revisando un lote de {len(leads)} leads en Prospectos...")

    for lead in leads:
        print(f"\n--- Revisando Lead ID {lead['id']}: {lead['name']} ---")
        ciudad = lead.get('city') or 'Cordoba Argentina'
        termino_busqueda = f"{lead.get('partner_name') or lead.get('name')} {ciudad} contacto telefono email"
        
        info_internet = buscar_en_internet_gratis(termino_busqueda)
        
        try:
            resultado = analizar_lead_con_ia(lead, info_internet)
        except Exception as e:
            print(f"Error con Gemini en este lead (se reintentará luego): {e}")
            time.sleep(10)
            continue

        print(f"Resultado IA: Válido={resultado.datos_validos_y_verificados} | Motivo: {resultado.motivo}")

        valores_actualizar = {}

        # Actualizar email si se encontró o corrigió
        if resultado.email_corregido_o_encontrado and resultado.email_corregido_o_encontrado != lead.get('email_from'):
            valores_actualizar['email_from'] = resultado.email_corregido_o_encontrado

        # Actualizar teléfono si se encontró o corrigió
        telefono_actual = lead.get('phone') or lead.get('mobile')
        if resultado.telefono_corregido_o_encontrado and resultado.telefono_corregido_o_encontrado != telefono_actual:
            valores_actualizar['phone'] = resultado.telefono_corregido_o_encontrado

        # Mover de etapa solo si pasó la verificación
        if resultado.datos_validos_y_verificados:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO
            print(f"-> Lead aprobado. Moviendo a etapa ID {ID_ETAPA_CHEQUEADO}.")
        else:
            print("-> Lead NO aprobado o incompleto. Se mantiene en Prospectos para seguir chequeando.")

        # Guardar cambios en Odoo
        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])
        
        # Dejar nota interna en el historial del lead
        mensaje_nota = f"<b>Agente de Verificación:</b> {resultado.motivo}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [lead['id']], {'body': mensaje_nota})

        # Pausa de 5 segundos para respetar el límite gratuito de velocidad de Gemini
        time.sleep(5)

if __name__ == "__main__":
    ejecutar_agente()
