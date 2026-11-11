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
    solo_digitos = re.sub(r'\D', '', str(numero))
    
    if not solo_digitos:
        return ""
        
    # Si ya tiene 549 + 10 dígitos (13 dígitos total, ej: 5493573412724)
    if solo_digitos.startswith("549") and len(solo_digitos) == 13:
        return f"+{solo_digitos}"
    # Si tiene 54 + 10 dígitos (le falta el 9, ej: 543573412724 -> +5493573412724)
    if solo_digitos.startswith("54") and len(solo_digitos) == 12:
        return f"+549{solo_digitos[2:]}"
    # Si empieza con 0 + 10 dígitos (ej: 03573412724 -> +5493573412724)
    if solo_digitos.startswith("0") and len(solo_digitos) == 11:
        return f"+549{solo_digitos[1:]}"
    # Si tiene 10 dígitos directos sin 0 ni 549 (ej: 3573412724 o 3518738200 -> +549...)
    if len(solo_digitos) == 10:
        return f"+549{solo_digitos}"
    # Si tiene 12 dígitos empezando con 0 y 15 intercalado (ej: 0351156123456 -> +5493516123456)
    if solo_digitos.startswith("0") and len(solo_digitos) == 13 and solo_digitos[4:6] == "15":
        return f"+549{solo_digitos[1:4]}{solo_digitos[6:]}"
    # Si tiene código de área + 15 + número (ej: 351156123456 -> +5493516123456)
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
            # Detectar enlaces de botones de WhatsApp en cualquier variante
            whatsapp_links = re.findall(r'(?:wa\.me/|whatsapp\.com/send\?phone=|whatsapp://send\?phone=)([0-9+\s-]+)', texto, re.IGNORECASE)
            whatsapp_limpios = [re.sub(r'\D', '', w) for w in whatsapp_links if len(re.sub(r'\D', '', w)) >= 8]
            
            info_extra = ""
            if whatsapp_limpios:
                info_extra = f"\n[BOTÓN DE WHATSAPP DETECTADO EN EL CÓDIGO DE LA WEB CON EL NÚMERO: {', '.join(set(whatsapp_limpios))}]\n"
            
            # Limpiar HTML
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
    ciudad = lead.get('city') or 'Cordoba Argentina'
    web_actual = lead.get('website') or ''
    
    reporte_total = []
    urls_encontradas = []
    
    if web_actual.startswith("http"):
        urls_encontradas.append(web_actual)

    # Ronda 1: Búsqueda general
    q1 = f'"{nombre}" {ciudad} contacto whatsapp telefono email'
    resultados1 = ejecutar_consulta_ddgs(q1, max_res=5)
    
    snippets = []
    for item in resultados1:
        link = item.get('href', '')
        snippets.append(f"- Título: {item.get('title')} | Desc: {item.get('body')} | Link: {link}")
        if link.startswith("http") and "instagram" not in link and "facebook" not in link and len(urls_encontradas) < 3:
            urls_encontradas.append(link)
            
    if snippets:
        reporte_total.append("=== RESULTADOS DE BÚSQUEDA EN INTERNET ===\n" + "\n".join(snippets))
    time.sleep(1)

    # Ronda 2: Redes Sociales (Instagram y Facebook)
    q2 = f'"{nombre}" {ciudad} (site:instagram.com OR site:facebook.com)'
    resultados2 = ejecutar_consulta_ddgs(q2, max_res=5)
    if resultados2:
        snippets_redes = [f"- {i.get('title')}: {i.get('body')} (Link: {i.get('href')})" for i in resultados2]
        reporte_total.append("=== REDES SOCIALES (INSTAGRAM / FACEBOOK) ===\n" + "\n".join(snippets_redes))
    time.sleep(1)

    # Ronda 3: Entrar a las webs encontradas y extraer botones de WhatsApp y textos internos
    for url in list(set(urls_encontradas)):
        print(f"-> Entrando a investigar la web: {url}", flush=True)
        contenido_interno = extraer_texto_de_url(url)
        if contenido_interno:
            reporte_total.append(f"=== CONTENIDO REAL DENTRO DE LA WEB ({url}) ===\n{contenido_interno}")
        time.sleep(1)

    return "\n\n".join(reporte_total)

def analizar_lead_con_ia(lead, info_internet):
    prompt = f"""
    Eres un investigador de datos CRM experto en Argentina. Tu objetivo es validar y completar el EMAIL y el TELÉFONO/WHATSAPP del lead.

    DATOS ACTUALES EN ODOO:
    - Nombre del Lead: {lead.get('name')}
    - Empresa / Contacto: {lead.get('partner_name') or lead.get('contact_name') or 'No especificado'}
    - Ciudad / País: {lead.get('city') or 'Córdoba'} {lead.get('country_id') if lead.get('country_id') else 'Argentina'}
    - Web: {lead.get('website') or 'No especificado'}
    - Email actual: {lead.get('email_from') or 'VACÍO'}
    - Teléfono actual: {lead.get('phone') or 'VACÍO'}

    RESULTADOS DE INVESTIGACIÓN Y SCRAPING INTERNO DE WEBS:
    {info_internet}

    REGLAS DE VALIDACIÓN FLEXIBLES PARA ARGENTINA (MUY IMPORTANTE):
    1. SOBRE TELÉFONOS Y WHATSAPP (NO RECHAZAR POR FORMATO):
       - En Argentina las empresas publican sus números de forma desprolija (a veces con +549, a veces +54, a veces 0351, o directamente 3518738200 / 3573412724).
       - NUNCA rechaces un teléfono por faltarle el "9" o por su característica (como 351 8... o 351 5...).
       - Si el número aparece en la web, en un botón de WhatsApp ([BOTÓN DE WHATSAPP DETECTADO]), en redes sociales (Instagram/Facebook) o en la descripción marcado como WhatsApp/contacto, ES 100% VÁLIDO.
       - Si el teléfono que ya está en Odoo coincide con el que aparece en su web o redes sociales (aunque en la web esté escrito sin el +549), DARLO POR VÁLIDO y devolverlo normalizado con +549 adelante (Ej: +5493518738200).
    2. SOBRE EMAILS:
       - Si falta el email, búscalo en las webs analizadas y en redes sociales. Si ya está en Odoo y es correcto, confírmalo.
    3. CONDICIÓN DE APROBACIÓN ('datos_validos_y_verificados' = TRUE):
       - Pon TRUE si el lead queda con AMBOS datos (Email Y Teléfono/WhatsApp) confirmados en internet.
       - Pon FALSE únicamente si tras buscar en web y redes sigue faltando el email o el teléfono, o si el dato cargado en Odoo es claramente erróneo y no encontraste el real.
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
        print(f"\n--- Investigando Lead ID {lead['id']}: {lead['name']} ---", flush=True)
        
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
            motivo_ajustado = f"{resultado.motivo} [Sigue en Prospectos: falta {'Email' if not tiene_email else 'Teléfono'}]."
        else:
            aprobado_final = resultado.datos_validos_y_verificados
            motivo_ajustado = resultado.motivo

        print(f"Resultado: Aprobado={aprobado_final} | Tel={celular_formateado or celular_actual} | Email={email_final or email_actual} | Motivo: {motivo_ajustado}", flush=True)

        valores_actualizar = {}

        if email_final and email_final != email_actual:
            valores_actualizar['email_from'] = email_final

        telefono_para_guardar = celular_formateado or celular_actual
        if telefono_para_guardar and telefono_para_guardar != lead.get('phone'):
            valores_actualizar['phone'] = telefono_para_guardar

        if aprobado_final:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO
            print(f"-> Lead COMPLETO y verificado. Moviendo a etapa ID {ID_ETAPA_CHEQUEADO}.", flush=True)
        else:
            print("-> Datos incompletos o no verificados. Se mantiene en Prospectos.", flush=True)

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])

        mensaje_nota = f"Agente de Verificación: {motivo_ajustado}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [lead['id']], {'body': mensaje_nota})

        time.sleep(3)

if __name__ == "__main__":
    ejecutar_agente()
