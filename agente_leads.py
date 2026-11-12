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
    coincide_ubicacion_y_rubro: bool
    es_celular_o_whatsapp_real: bool
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
    if solo_digitos.startswith("0") and len(solo_digitos) == 13 and solo_digitos[4:6] == "15":
        return f"+549{solo_digitos[1:4]}{solo_digitos[6:]}"
    if len(solo_digitos) == 12 and solo_digitos[3:5] == "15":
        return f"+549{solo_digitos[:3]}{solo_digitos[5:]}"
        
    return f"+{solo_digitos}" if str(numero).strip().startswith("+") else solo_digitos

def extraer_texto_de_url(url):
    """Navega directamente a la página web para extraer textos y enlaces de botones de WhatsApp"""
    if not url or not url.startswith("http"):
        return ""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=12)
        if response.status_code == 200:
            texto = response.text
            whatsapp_links = re.findall(r'(?:wa\.me/|whatsapp\.com/send\?phone=|whatsapp://send\?phone=)([0-9+\s-]+)', texto, re.IGNORECASE)
            whatsapp_limpios = [re.sub(r'\D', '', w) for w in whatsapp_links if len(re.sub(r'\D', '', w)) >= 8]
            
            info_extra = ""
            if whatsapp_limpios:
                info_extra = f"\n[BOTÓN DE WHATSAPP DETECTADO EN EL CÓDIGO DE LA WEB CON EL NÚMERO: {', '.join(set(whatsapp_limpios))}]\n"
            
            texto_limpio = re.sub(r'<script[^>]*>.*?</script>', ' ', texto, flags=re.DOTALL | re.IGNORECASE)
            texto_limpio = re.sub(r'<style[^>]*>.*?</style>', ' ', texto_limpio, flags=re.DOTALL | re.IGNORECASE)
            texto_limpio = re.sub(r'<[^>]+>', ' ', texto_limpio)
            texto_limpio = re.sub(r'\s+', ' ', texto_limpio)
            return f"{info_extra} Contenido de la web: {texto_limpio[:4500]}"
    except Exception as e:
        return f"[No se pudo acceder a la web: {str(e)}]"
    return ""

def ejecutar_consulta_ddgs(query, max_res=6):
    try:
        resultados = DDGS().text(query, region="ar-es", max_results=max_res)
        return resultados if resultados else []
    except Exception:
        return []

def investigacion_exhaustiva_y_scraping(lead):
    nombre = lead.get('partner_name') or lead.get('name') or ''
    direccion = lead.get('street') or ''
    ciudad = lead.get('city') or 'Cordoba Capital Argentina'
    web_actual = lead.get('website') or ''
    
    calle_busqueda = direccion.split(',')[0].strip() if direccion else ''
    
    reporte_total = []
    urls_encontradas = []
    
    if web_actual.startswith("http"):
        urls_encontradas.append(web_actual)

    # Ronda 1: Búsqueda local exacta con dirección y ciudad
    q1 = f'"{nombre}" "{calle_busqueda}" {ciudad} whatsapp celular contacto email' if calle_busqueda else f'"{nombre}" {ciudad} whatsapp celular contacto email'
    resultados1 = ejecutar_consulta_ddgs(q1, max_res=5)
    
    snippets = []
    for item in resultados1:
        link = item.get('href', '')
        snippets.append(f"- Título: {item.get('title')} | Desc: {item.get('body')} | Link: {link}")
        if link.startswith("http") and "instagram" not in link and "facebook" not in link and len(urls_encontradas) < 3:
            urls_encontradas.append(link)
            
    if snippets:
        reporte_total.append("=== RESULTADOS DE BÚSQUEDA LOCAL EXACTA ===\n" + "\n".join(snippets))
    time.sleep(1)

    # Ronda 2: Redes Sociales (Instagram y Facebook) para encontrar el WhatsApp móvil publicado en biografías
    q2 = f'"{nombre}" {ciudad} (site:instagram.com OR site:facebook.com) whatsapp OR celular OR 351'
    resultados2 = ejecutar_consulta_ddgs(q2, max_res=5)
    if resultados2:
        snippets_redes = [f"- {i.get('title')}: {i.get('body')} (Link: {i.get('href')})" for i in resultados2]
        reporte_total.append("=== REDES SOCIALES (INSTAGRAM / FACEBOOK) ===\n" + "\n".join(snippets_redes))
    time.sleep(1)

    # Ronda 3: Búsqueda específica de WhatsApp o celular del comercio en directorios
    q3 = f'"{nombre}" {calle_busqueda} {ciudad} "whatsapp" OR "cel" OR "móvil"'
    resultados3 = ejecutar_consulta_ddgs(q3, max_res=4)
    if resultados3:
        snippets_wa = [f"- {i.get('title')}: {i.get('body')}" for i in resultados3]
        reporte_total.append("=== BÚSQUEDA ESPECÍFICA DE WHATSAPP / CELULAR ===\n" + "\n".join(snippets_wa))
    time.sleep(1)

    # Ronda 4: Entrar a las webs encontradas y extraer botones de WhatsApp y textos internos
    for url in list(set(urls_encontradas)):
        print(f"-> Entrando a investigar la web: {url}", flush=True)
        contenido_interno = extraer_texto_de_url(url)
        if contenido_interno:
            reporte_total.append(f"=== CONTENIDO REAL DENTRO DE LA WEB ({url}) ===\n{contenido_interno}")
        time.sleep(1)

    return "\n\n".join(reporte_total)

def analizar_lead_con_ia(lead, info_internet):
    prompt = f"""
    Eres un auditor de datos CRM ultra estricto en Córdoba Capital, Argentina. Tu trabajo es validar y completar el EMAIL y el WHATSAPP/CELULAR del lead, SIN INVENTAR NINGÚN DATO y SIN CONFUNDIR negocios homónimos.

    DATOS ACTUALES EN ODOO:
    - Nombre del Lead: {lead.get('name')}
    - Empresa / Contacto: {lead.get('partner_name') or lead.get('contact_name') or 'No especificado'}
    - Dirección exacta en Odoo: {lead.get('street') or 'No especificada'}
    - Ciudad / Provincia: {lead.get('city') or 'Córdoba Capital'}
    - Email actual: {lead.get('email_from') or 'VACÍO'}
    - Teléfono actual: {lead.get('phone') or 'VACÍO'}

    RESULTADOS DE INVESTIGACIÓN Y SCRAPING INTERNO DE WEBS:
    {info_internet}

    REGLAS ESTRICTAS DE VALIDACIÓN (PROHIBIDO INVENTAR O ADIVINAR):
    1. CERO INVENCIÓN DE DATOS:
       - Nunca inventes, supongas ni construyas un email o teléfono que no esté escrito explícitamente en los resultados de internet de ese comercio exacto.
       - Si un dato no aparece literal y comprobado en internet para esa dirección/comercio, deja el campo vacío ("").
    2. CONTROL ANTI-HOMÓNIMOS Y DIRECCIÓN:
       - Verifica que la web, Instagram o directorio pertenezca exactamente al negocio ubicado en la dirección de Odoo ({lead.get('street') or 'Córdoba'}).
       - Si pertenece a otro negocio con el mismo nombre en otra calle o provincia, pon 'coincide_ubicacion_y_rubro' = FALSE y descarta todos sus datos.
    3. DISTINCIÓN ENTRE TELÉFONO FIJO Y WHATSAPP / CELULAR:
       - En Córdoba, los números fijos tradicionales empiezan con 42..., 43..., 45..., 47..., 48..., 727... (por ejemplo 351 7272922 o 351 4823645 son LÍNEAS FIJAS).
       - NO apruebes una línea fija tradicional como si fuera WhatsApp, A MENOS QUE en la web o redes aparezca explícitamente dentro de un botón de WhatsApp ([BOTÓN DE WHATSAPP DETECTADO]) o indicado textualmente como "WhatsApp".
       - Si en Odoo hay cargado un teléfono fijo (ej. +5493517272922) y en internet o redes (como Instagram/Facebook) encuentras el verdadero celular móvil del negocio (ej. 351 2099608), REEMPLAZA el fijo por ese celular móvil (+5493512099608) y pon 'es_celular_o_whatsapp_real' = TRUE.
       - Si solo existe un teléfono fijo tradicional y no encontraste su número de WhatsApp/celular, pon 'es_celular_o_whatsapp_real' = FALSE y devuelve "" en celular_corregido_o_encontrado.
    4. CONDICIÓN DE APROBACIÓN ('datos_validos_y_verificados' = TRUE):
       - Pon TRUE únicamente si 'coincide_ubicacion_y_rubro' = TRUE, 'es_celular_o_whatsapp_real' = TRUE, y el lead tiene tanto EMAIL como WHATSAPP/CELULAR verificados de forma literal en internet.
    """

    response = client_ai.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ResultadoValidacion,
            temperature=0.0,
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

    campos = ['id', 'name', 'partner_name', 'contact_name', 'email_from', 'phone', 'website', 'street', 'city', 'country_id', 'description']
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
        [[['stage_id', '=', id_prospectos]]],
        {'fields': campos, 'limit': MAX_LEADS_POR_EJECUCION, 'order': 'write_date asc'}
    )

    print(f"Iniciando investigación con tolerancia cero a datos inventados o fijos en {len(leads)} leads...", flush=True)

    for lead in leads:
        print(f"\n--- Investigando Lead ID {lead['id']}: {lead['name']} (Dir: {lead.get('street') or 'S/D'}) ---", flush=True)
        
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
        tiene_email = bool(email_actual or email_final)
        tiene_whatsapp_valido = resultado.es_celular_o_whatsapp_real and bool(celular_formateado)

        if not resultado.coincide_ubicacion_y_rubro or not tiene_email or not tiene_whatsapp_valido:
            aprobado_final = False
            motivo_ajustado = f"{resultado.motivo} [Mantenido en Prospectos: falta WhatsApp móvil real, email o confirmación de dirección]."
        else:
            aprobado_final = resultado.datos_validos_y_verificados
            motivo_ajustado = resultado.motivo

        print(f"Resultado: Aprobado={aprobado_final} | CoincideDir={resultado.coincide_ubicacion_y_rubro} | EsWhatsApp={resultado.es_celular_o_whatsapp_real} | Tel={celular_formateado} | Email={email_final or email_actual}", flush=True)

        valores_actualizar = {}

        if resultado.coincide_ubicacion_y_rubro and email_final and email_final != email_actual:
            valores_actualizar['email_from'] = email_final

        # Si encontró un WhatsApp móvil real, reemplaza incluso si antes había un teléfono fijo cargado
        if resultado.coincide_ubicacion_y_rubro and resultado.es_celular_o_whatsapp_real and celular_formateado and celular_formateado != lead.get('phone'):
            valores_actualizar['phone'] = celular_formateado

        if aprobado_final:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO
            print(f"-> Lead COMPLETO y verificado con WhatsApp real. Moviendo a etapa ID {ID_ETAPA_CHEQUEADO}.", flush=True)
        else:
            print("-> No se aprobó (solo se halló fijo, falta dato o no coincide dirección). Sigue en Prospectos.", flush=True)

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])

        mensaje_nota = f"Agente de Verificación: {motivo_ajustado}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [lead['id']], {'body': mensaje_nota})

        time.sleep(3)

if __name__ == "__main__":
    ejecutar_agente()
