import os
import re
import time
import xmlrpc.client
import requests
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
MAX_LEADS_POR_EJECUCION = 50  # Lote de control

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
    es_positivo = str(numero).strip().startswith("+")
    solo_digitos = re.sub(r'\D', '', str(numero))
    
    if not solo_digitos:
        return ""
        
    if solo_digitos.startswith("549") and len(solo_digitos) == 13:
        return f"+{solo_digitos}"
    if solo_digitos.startswith("54") and len(solo_digitos) == 12:
        return f"+549{solo_digitos[2:]}"
    if solo_digitos.startswith("0") and len(solo_digitos) == 11:
        return f"+549{solo_digitos[1:]}"
    if len(solo_digitos) == 10:
        return f"+549{solo_digitos}"
        
    return f"+{solo_digitos}" if es_positivo else solo_digitos

def extraer_texto_de_url(url):
    """Navega y entra directamente a la página web para extraer el texto y enlaces ocultos"""
    if not url or not url.startswith("http"):
        return ""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            texto = response.text
            whatsapp_links = re.findall(r'(?:wa\.me|api\.whatsapp\.com/send\?phone=)([0-9]+)', texto)
            info_extra = ""
            if whatsapp_links:
                info_extra = f"\n[ENLACES DE BOTÓN DE WHATSAPP ENCONTRADOS EN LA WEB: {', '.join(whatsapp_links)}]\n"
            
            texto_limpio = re.sub(r'<[^>]+>', ' ', texto)
            texto_limpio = re.sub(r'\s+', ' ', texto_limpio)
            return f"{info_extra} Contenido de la web: {texto_limpio[:4000]}"
    except Exception as e:
        return f"[No se pudo acceder a la web: {str(e)}]"
    return ""

def ejecutar_consulta_ddgs(query, max_res=6):
    try:
        resultados = DDGS().text(query, region="ar-es", max_results=max_res)
        if not resultados:
            return []
        return resultados
    except Exception:
        return []

def investigacion_exhaustiva_y_scraping(lead):
    nombre = lead.get('partner_name') or lead.get('name') or ''
    ciudad = lead.get('city') or 'Cordoba Argentina'
    web_actual = lead.get('website') or ''
    
    reporte_total = []
    urls_encontradas = []
    
    if web_actual.startswith("http"):
        urls_encontradas.append(web_actual)

    q1 = f'"{nombre}" {ciudad} contacto whatsapp telefono email'
    resultados1 = ejecutar_consulta_ddgs(q1, max_res=5)
    
    snippets = []
    for item in resultados1:
        link = item.get('href', '')
        snippets.append(f"- Título: {item.get('title')} | Desc: {item.get('body')} | Link: {link}")
        if link.startswith("http") and "instagram" not in link and "facebook" not in link and len(urls_encontradas) < 3:
            urls_encontradas.append(link)
            
    reporte_total.append("=== RESULTADOS DE BÚSQUEDA EN INTERNET ===\n" + "\n".join(snippets))
    time.sleep(1)

    q2 = f'"{nombre}" {ciudad} (site:instagram.com OR site:facebook.com)'
    resultados2 = ejecutar_consulta_ddgs(q2, max_res=4)
    if resultados2:
        snippets_redes = [f"- {i.get('title')}: {i.get('body')}" for i in resultados2]
        reporte_total.append("=== REDES SOCIALES (INSTAGRAM / FACEBOOK) ===\n" + "\n".join(snippets_redes))
    time.sleep(1)

    for url in list(set(urls_encontradas)):
        print(f"-> Entrando e investigando la web interna: {url}", flush=True)
        contenido_interno = extraer_texto_de_url(url)
        if contenido_interno:
            reporte_total.append(f"=== CONTENIDO REAL DENTRO DE LA WEB ({url}) ===\n{contenido_interno}")
        time.sleep(1)

    return "\n\n".join(reporte_total)

def analizar_lead_con_ia(lead, info_internet):
    prompt = f"""
    Eres un investigador de datos CRM experto en Córdoba, Argentina. Tu trabajo es consolidar el EMAIL y el TELÉFONO/WHATSAPP del lead basándote en los datos de internet y el contenido extraído de sus páginas web.

    DATOS ACTUALES EN ODOO:
    - Nombre del Lead: {lead.get('name')}
    - Empresa / Contacto: {lead.get('partner_name') or lead.get('contact_name') or 'No especificado'}
    - Ciudad / País: {lead.get('city') or 'Córdoba'} {lead.get('country_id') if lead.get('country_id') else 'Argentina'}
    - Web: {lead.get('website') or 'No especificado'}
    - Email actual: {lead.get('email_from') or 'VACÍO'}
    - Teléfono actual: {lead.get('phone') or 'VACÍO'}

    RESULTADOS DE INVESTIGACIÓN Y SCRAPING INTERNO DE WEBS:
    {info_internet}

    REGLAS DE EXTRACCIÓN Y COINCIDENCIA:
    1. TELÉFONOS Y WHATSAPP:
       - Revisa minuciosamente la sección [ENLACES DE BOTÓN DE WHATSAPP ENCONTRADOS EN LA WEB]. Si hay números allí, extrae el número de contacto principal de la entidad.
       - Si el número actual o el encontrado en el texto de la web/redes figura como canal de atención, WhatsApp o contacto directo, ES TOTALMENTE VÁLIDO. No lo descartes por su prefijo.
       - Devuélvelo en formato numérico limpio (Ej de salida esperado: +5493518738200 o +5493573412724).
    2. EMAILS:
       - Busca direcciones de correo electrónico corporativas o de Gmail en el contenido interno de las webs analizadas y complétalo.
    3. CONDICIÓN DE APROBACIÓN ('datos_validos_y_verificados' = TRUE):
       - Debe ser TRUE si el lead cuenta con Correo Electrónico Y Teléfono/WhatsApp de contacto confirmados y válidos para recibir invitaciones.
       - Si tras buscar y raspar las webs falta el email o el teléfono de manera absoluta en internet, pon FALSE para que quede en revisión.
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

    print(f"Iniciando investigación profunda y scraping en {len(leads)} leads...", flush=True)

    for lead in leads:
        print(f"\n--- Investigando a fondo y navegando webs del Lead ID {lead['id']}: {lead['name']} ---", flush=True)
        
        info_internet = investigacion_exhaustiva_y_scraping(lead)
        
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
            motivo_ajustado = f"{resultado.motivo} [Sigue en Prospectos: falta {'Email' if not tiene_email else 'Teléfono'} tras análisis interno web]."
        else:
            aprobado_final = resultado.datos_validos_y_verificados
            motivo_ajustado = resultado.motivo

        print(f"Resultado: Aprobado={aprobado_final} | Celular={celular_formateado or celular_actual} | Email={email_final or email_actual} | Motivo: {motivo_ajustado}", flush=True)

        valores_actualizar = {}

        if email_final and email_final != email_actual:
            valores_actualizar['email_from'] = email_final

        telefono_para_guardar = celular_formateado or celular_actual
        if telefono_para_guardar and telefono_para_guardar != lead.get('phone'):
            valores_actualizar['phone'] = telefono_para_guardar

        if aprobado_final:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO
            print(f"-> Lead COMPLETO y verificado con Scraping. Moviendo a etapa ID {ID_ETAPA_CHEQUEADO}.", flush=True)
        else:
            print("-> Datos incompletos tras scraping de webs. Se mantiene en Prospectos.", flush=True)

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])

        mensaje_nota = f"Agente de Verificación: {motivo_ajustado}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [lead['id']], {'body': mensaje_nota})

        time.sleep(3)

if __name__ == "__main__":
    ejecutar_agente()
