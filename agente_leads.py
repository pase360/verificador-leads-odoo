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
MAX_LEADS_POR_EJECUCION = 50  # Lote equilibrado para hacer búsquedas profundas sin saturar

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

def ejecutar_consulta_ddgs(query, max_res=6):
    try:
        resultados = DDGS().text(query, region="ar-es", max_results=max_res)
        if not resultados:
            return ""
        snippets = []
        for item in resultados:
            snippets.append(f"- Título: {item.get('title')} | Desc: {item.get('body')} | Link: {item.get('href')}")
        return "\n".join(snippets)
    except Exception:
        return ""

def investigacion_exhaustiva_internet(lead):
    nombre = lead.get('partner_name') or lead.get('name') or ''
    ciudad = lead.get('city') or 'Cordoba Argentina'
    web = lead.get('website') or ''
    falta_email = not bool(lead.get('email_from'))
    falta_celular = not bool(limpiar_formato_celular(lead.get('phone')))

    reporte_busquedas = []

    # Ronda 1: Búsqueda general de contacto
    q1 = f'"{nombre}" {ciudad} contacto whatsapp telefono email'
    res1 = ejecutar_consulta_ddgs(q1, max_res=6)
    if res1:
        reporte_busquedas.append(f"=== BÚSQUEDA GENERAL ===\n{res1}")
    time.sleep(1.5)

    # Ronda 2: Búsqueda profunda en Redes Sociales (Instagram y Facebook)
    if falta_email or falta_celular:
        q2 = f'"{nombre}" {ciudad} (site:instagram.com OR site:facebook.com)'
        res2 = ejecutar_consulta_ddgs(q2, max_res=6)
        if res2:
            reporte_busquedas.append(f"=== REDES SOCIALES (INSTAGRAM / FACEBOOK) ===\n{res2}")
        time.sleep(1.5)

    # Ronda 3: Búsqueda específica de correo electrónico o sitio web oficial
    if falta_email:
        if web:
            dominio = web.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
            q3 = f'"{dominio}" OR "{nombre}" "@"'
        else:
            q3 = f'"{nombre}" {ciudad} ("@gmail.com" OR "@hotmail.com" OR "email" OR "correo")'
        res3 = ejecutar_consulta_ddgs(q3, max_res=6)
        if res3:
            reporte_busquedas.append(f"=== BÚSQUEDA ESPECÍFICA DE EMAIL ===\n{res3}")
        time.sleep(1.5)

    if not reporte_busquedas:
        return "No se encontraron resultados tras realizar múltiples búsquedas en web y redes sociales."
    
    return "\n\n".join(reporte_busquedas)

def analizar_lead_con_ia(lead, info_internet):
    prompt = f"""
    Eres un investigador de datos CRM experto en Córdoba, Argentina. Tu trabajo es encontrar el EMAIL y el CELULAR faltantes revisando a fondo los resultados de web, Instagram y Facebook.

    DATOS ACTUALES EN ODOO:
    - Nombre del Lead: {lead.get('name')}
    - Empresa / Contacto: {lead.get('partner_name') or lead.get('contact_name') or 'No especificado'}
    - Ciudad / País: {lead.get('city') or 'Córdoba'} {lead.get('country_id') if lead.get('country_id') else 'Argentina'}
    - Web: {lead.get('website') or 'No especificado'}
    - Email actual: {lead.get('email_from') or 'VACÍO'}
    - Teléfono actual: {lead.get('phone') or 'VACÍO'}

    RESULTADOS DE INVESTIGACIÓN EXHAUSTIVA:
    {info_internet}

    REGLAS ESTRICTAS DE EXTRACCIÓN Y VALIDACIÓN:
    1. TELÉFONOS (SOLO CELULARES):
       - IGNORA Y DESCARTA POR COMPLETO cualquier teléfono fijo (ej. números de Córdoba que empiezan con 0351 4..., 03573 4..., o líneas fijas sin 15/9). Nunca devuelvas un número fijo.
       - Revisa con lupa las descripciones de Instagram, Facebook y directorios para rescatar cualquier número de WhatsApp o celular (con 15, +54 9, o 351/3573 seguido de 7 dígitos).
       - Devuélvelo SIEMPRE en el formato exacto sin espacios ni guiones: +549 + código de área sin 0 + número sin 15 (Ejemplo: +5493573412724).
       - Si el teléfono actual en Odoo es un número fijo y no encontraste un celular en internet, devuelve "".
    2. EMAILS:
       - Revisa cada línea de los resultados (especialmente biografías de Instagram, páginas de Facebook y webs) para rescatar cualquier dirección de correo electrónico perteneciente al comercio o entidad.
       - Si ya tiene email, verifica que sea real y corrígelo si ves un error evidente. Si no aparece ninguno tras toda la investigación, devuelve "".
    3. CONDICIÓN OBLIGATORIA PARA APROBAR ('datos_validos_y_verificados' = TRUE):
       - Debe quedar con AMBOS DATOS COMPLETOS Y VERIFICADOS (Email Y Celular válido).
       - Si después de investigar sigue faltando el Email o sigue faltando el Celular, pon obligatoriamente FALSE para que permanezca en Prospectos.
    """

    response = client_ai.models.generate_content(
        model="gemini-3.5-flash-lite",
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
        [[['stage_id', '=', id_prospectos]]],
        {'fields': campos, 'limit': MAX_LEADS_POR_EJECUCION, 'order': 'write_date asc'}
    )

    print(f"Iniciando investigación exhaustiva en un lote de {len(leads)} leads...", flush=True)

    for lead in leads:
        print(f"\n--- Investigando a fondo Lead ID {lead['id']}: {lead['name']} ---", flush=True)
        
        info_internet = investigacion_exhaustiva_internet(lead)
        
        try:
            resultado = analizar_lead_con_ia(lead, info_internet)
        except Exception as e:
            print(f"Error con Gemini en este lead: {e}", flush=True)
            time.sleep(5)
            continue

        celular_formateado = limpiar_formato_celular(resultado.celular_corregido_o_encontrado)
        email_final = resultado.email_corregido_o_encontrado.strip()

        email_actual = (lead.get('email_from') or '').strip()
        celular_actual = limpiar_formato_celular(lead.get('phone'))

        tiene_email = bool(email_actual or email_final)
        tiene_celular = bool(celular_actual or celular_formateado)

        if not tiene_email or not tiene_celular:
            aprobado_final = False
            motivo_ajustado = f"{resultado.motivo} [Sigue en Prospectos: falta {'Email' if not tiene_email else 'Celular'} tras búsqueda profunda]."
        else:
            aprobado_final = resultado.datos_validos_y_verificados
            motivo_ajustado = resultado.motivo

        print(f"Resultado: Aprobado={aprobado_final} | Celular={celular_formateado or celular_actual} | Email={email_final or email_actual} | Motivo: {motivo_ajustado}", flush=True)

        valores_actualizar = {}

        if email_final and email_final != email_actual:
            valores_actualizar['email_from'] = email_final

        if celular_formateado and celular_formateado != lead.get('phone'):
            valores_actualizar['phone'] = celular_formateado

        if aprobado_final:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO
            print(f"-> Lead COMPLETO y verificado. Moviendo a etapa ID {ID_ETAPA_CHEQUEADO}.", flush=True)
        else:
            print("-> Aún falta algún dato. Se guarda lo encontrado y sigue en Prospectos.", flush=True)

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])
        
        mensaje_nota = f"Agente de Verificación: {motivo_ajustado}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [lead['id']], {'body': mensaje_nota})

        time.sleep(3)

if __name__ == "__main__":
    ejecutar_agente()
