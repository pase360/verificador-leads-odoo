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
MAX_LEADS_POR_EJECUCION = 100  # Lote de control

client_ai = genai.Client(api_key=GEMINI_API_KEY)

class ResultadoValidacion(BaseModel):
    email_elegido: str
    telefono_elegido: str
    coincide_ubicacion_y_rubro: bool
    datos_validos_y_verificados: bool
    motivo: str

def conectar_odoo():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

def limpiar_nombre_busqueda(nombre):
    palabras_rubro = [
        r'\bdiet[eé]tica\b', r'\bverduler[ií]a\b', r'\bferreter[ií]a\b',
        r'\bflorer[ií]a\b', r'\bfarmacia\b', r'\bcotill[oó]n\b',
        r'\bpanader[ií]a\b', r'\bcarnicer[ií]a\b', r'\bhelader[ií]a\b',
        r'\bpinturer[ií]a\b', r'\bpeluquer[ií]a\b', r'\bkiosco\b',
        r'\bautolavadero\b', r'\btienda\b', r'\balmac[eé]n\b'
    ]
    limpio = nombre
    for patron in palabras_rubro:
        limpio = re.sub(patron, '', limpio, flags=re.IGNORECASE)
    limpio = re.sub(r'\s+', ' ', limpio).strip()
    return limpio if len(limpio) >= 3 else nombre

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
    if not url or not url.startswith("http"):
        return ""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
            'Accept-Language': 'es-AR,es;q=0.9'
        }
        response = requests.get(url, headers=headers, timeout=12)
        if response.status_code == 200:
            texto = response.text
            whatsapp_links = re.findall(r'(?:wa\.me/|whatsapp\.com/send\?phone=|whatsapp://send\?phone=)([0-9+\s-]+)', texto, re.IGNORECASE)
            whatsapp_limpios = [re.sub(r'\D', '', w) for w in whatsapp_links if len(re.sub(r'\D', '', w)) >= 8]
            emails_html = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', texto)
            emails_filtrados = list(set([e.lower() for e in emails_html if not e.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', 'sentry.io', 'wixpress.com'))]))
            
            info_extra = ""
            if whatsapp_limpios:
                info_extra += f"\n[BOTÓN DE WHATSAPP EN WEB: {', '.join(set(whatsapp_limpios))}]"
            if emails_filtrados:
                info_extra += f"\n[CORREOS DETECTADOS EN EL CÓDIGO DE LA WEB: {', '.join(emails_filtrados[:5])}]"
            
            texto_limpio = re.sub(r'<script[^>]*>.*?</script>', ' ', texto, flags=re.DOTALL | re.IGNORECASE)
            texto_limpio = re.sub(r'<style[^>]*>.*?</style>', ' ', texto_limpio, flags=re.DOTALL | re.IGNORECASE)
            texto_limpio = re.sub(r'<[^>]+>', ' ', texto_limpio)
            texto_limpio = re.sub(r'\s+', ' ', texto_limpio)
            return f"{info_extra}\nContenido visible: {texto_limpio[:4000]}"
    except Exception:
        return ""
    return ""

def ejecutar_consulta_ddgs(query, max_res=7):
    try:
        resultados = DDGS().text(query, region="ar-es", max_results=max_res)
        return resultados if resultados else []
    except Exception:
        return []

def investigacion_profunda_con_google_search(lead):
    nombre_original = lead.get('partner_name') or lead.get('name') or ''
    nombre_corto = limpiar_nombre_busqueda(nombre_original)
    direccion = lead.get('street') or ''
    ciudad = lead.get('city') or 'Cordoba Argentina'
    calle_busqueda = direccion.split(',')[0].strip() if direccion else ''
    
    reporte_total = []
    urls_encontradas = []

    # 1. Búsqueda general con nombre original y calle
    q1 = f'{nombre_original} {calle_busqueda} {ciudad} contacto whatsapp email'
    print(f"-> Buscando en web: {q1}", flush=True)
    res1 = ejecutar_consulta_ddgs(q1, max_res=6)
    snippets1 = []
    for item in res1:
        link = item.get('href', '')
        snippets1.append(f"- {item.get('title')} | {item.get('body')} | {link}")
        if link.startswith("http") and "instagram.com" not in link and "facebook.com" not in link and len(urls_encontradas) < 3:
            urls_encontradas.append(link)
    if snippets1:
        reporte_total.append("=== BÚSQUEDA WEB GENERAL ===\n" + "\n".join(snippets1))
    time.sleep(1)

    # 2. Búsqueda específica en Facebook e Instagram con nombre corto + calle
    q2 = f'"{nombre_corto}" {calle_busqueda} {ciudad} (site:facebook.com OR site:instagram.com)' if calle_busqueda else f'"{nombre_corto}" {ciudad} (site:facebook.com OR site:instagram.com)'
    print(f"-> Buscando en Facebook/Instagram: {q2}", flush=True)
    res2 = ejecutar_consulta_ddgs(q2, max_res=6)
    if res2:
        snippets2 = [f"- {i.get('title')} | {i.get('body')} | {i.get('href')}" for i in res2]
        reporte_total.append("=== RESULTADOS DE FACEBOOK E INSTAGRAM ===\n" + "\n".join(snippets2))
        for i in res2:
            link_fb = i.get('href', '')
            if "facebook.com" in link_fb:
                contenido_fb = extraer_texto_de_url(link_fb)
                if contenido_fb and ("@" in contenido_fb or "659" in contenido_fb or "Información" in contenido_fb):
                    reporte_total.append(f"=== CONTENIDO EXTRAÍDO DE FACEBOOK ({link_fb}) ===\n{contenido_fb}")
    time.sleep(1)

    # 3. Búsqueda directa de correos (@gmail.com / @hotmail.com) con nombre corto y calle
    q3 = f'"{nombre_corto}" {calle_busqueda} ("@gmail.com" OR "@hotmail.com" OR "@yahoo.com")'
    print(f"-> Rastreando correos electrónicos: {q3}", flush=True)
    res3 = ejecutar_consulta_ddgs(q3, max_res=5)
    if res3:
        snippets3 = [f"- {i.get('title')} | {i.get('body')} | {i.get('href')}" for i in res3]
        reporte_total.append("=== RASTREO DE EMAILS ===\n" + "\n".join(snippets3))
    time.sleep(1)

    # 4. Scraping de webs oficiales encontradas
    for url in list(set(urls_encontradas)):
        if "wa.me" in url or "api.whatsapp.com" in url:
            num_wa = re.sub(r'\D', '', url)
            reporte_total.append(f"=== ENLACE DIRECTO WHATSAPP ===\nNúmero: {num_wa}")
            continue
        print(f"-> Entrando a web oficial: {url}", flush=True)
        contenido = extraer_texto_de_url(url)
        if contenido:
            reporte_total.append(f"=== CONTENIDO WEB ({url}) ===\n{contenido}")
        time.sleep(1)

    return "\n\n".join(reporte_total)

def analizar_lead_con_google_search_y_ia(lead, info_duckduckgo):
    nombre_original = lead.get('partner_name') or lead.get('name') or ''
    nombre_corto = limpiar_nombre_busqueda(nombre_original)
    direccion = lead.get('street') or 'No especificada'
    ciudad = lead.get('city') or 'Córdoba'
    email_actual = lead.get('email_from') or 'VACÍO'
    telefono_actual = lead.get('phone') or 'VACÍO'

    prompt = f"""
    Eres un investigador experto en empresas y comercios de Argentina. Usa tu herramienta de búsqueda en Google (Google Search) Y los resultados proporcionados abajo para encontrar el EMAIL y el TELÉFONO/WHATSAPP reales de este comercio.

    DATOS EN ODOO:
    - Nombre en Odoo: {nombre_original} (Nombre corto en redes: {nombre_corto})
    - Dirección exacta: {direccion}, {ciudad}
    - Email actual en Odoo: {email_actual}
    - Teléfono actual en Odoo: {telefono_actual}

    RESULTADOS PREVIOS DE RASTREO:
    {info_duckduckgo}

    INSTRUCCIONES OBLIGATORIAS DE BÚSQUEDA Y VALIDACIÓN:
    1. USA GOOGLE SEARCH AHORA MISMO:
       - Busca en Google: "{nombre_corto}" "{direccion.split(',')[0]}" {ciudad} facebook OR instagram OR email OR telefono
       - En Facebook (por ejemplo páginas como Rincón Bonito en Av Ricardo Rojas 9113), revisa la sección "Información de contacto" donde figura su teléfono (ej. 0351 659-1654) y su correo (ej. rinconbonitodiet@gmail.com).
    2. CORRECCIÓN DE TELÉFONO Y SELECCIÓN SI HAY VARIOS:
       - Si el teléfono publicado en la página oficial de Facebook, Instagram o web del comercio NO coincide con el teléfono de Odoo, EL DE LA WEB/RED SOCIAL ES EL CORRECTO y debes devolverlo en 'telefono_elegido' (normalizado con +549 adelante, ej. +5493516591654) para sobrescribir Odoo.
       - Si hay más de un teléfono, prioriza siempre el que indique WhatsApp o sea móvil.
    3. SELECCIÓN DE EMAIL Y CERO INVENCIÓN:
       - Extrae únicamente correos que existan literalmente en Google, Facebook, Instagram o la web del comercio. Nunca inventes correos.
       - Si hay varios correos, elige el más adecuado para propuestas comerciales o alianzas (comercial@, marketing@, contacto@, info@, administracion@ o el correo principal del comercio).
    4. REGLA DE APROBACIÓN ('datos_validos_y_verificados' = TRUE):
       - Pon TRUE si el comercio coincide en ubicación/rubro y logras tener AL MENOS UN CANAL DE CONTACTO CONFIRMADO (Email verificado O Teléfono/WhatsApp verificado en internet), completando ambos siempre que existan en Google/Facebook/Web.
       - Pon FALSE únicamente si no pudiste confirmar ni el teléfono ni el email en internet, o si el negocio pertenece a otra provincia/rubro distinto.
    """

    # Usamos Gemini con Google Search Grounding activado (gratis en Gemini API)
    response = client_ai.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[{"google_search": {}}],
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

    campos = ['id', 'name', 'partner_name', 'contact_name', 'email_from', 'phone', 'website', 'street', 'city', 'country_id']
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
        [[['stage_id', '=', id_prospectos]]],
        {'fields': campos, 'limit': MAX_LEADS_POR_EJECUCION, 'order': 'write_date asc'}
    )

    print(f"Iniciando investigación con Google Search + Facebook en {len(leads)} leads...", flush=True)

    for lead in leads:
        print(f"\n--- Auditando Lead ID {lead['id']}: {lead['name']} (Dir: {lead.get('street') or 'S/D'}) ---", flush=True)
        
        info_ddgs = investigacion_profunda_con_google_search(lead)
        
        try:
            resultado = analizar_lead_con_google_search_y_ia(lead, info_ddgs)
        except Exception as e:
            print(f"Error con Gemini en este lead: {e}", flush=True)
            time.sleep(5)
            continue

        telefono_formateado = limpiar_formato_celular(resultado.telefono_elegido)
        email_final = resultado.email_elegido.strip().lower()

        email_actual = (lead.get('email_from') or '').strip().lower()
        telefono_actual = limpiar_formato_celular(lead.get('phone'))

        email_definitivo = email_final if email_final else email_actual
        telefono_definitivo = telefono_formateado if telefono_formateado else telefono_actual

        tiene_al_menos_un_dato_verificado = bool(email_final or telefono_formateado)

        if not resultado.coincide_ubicacion_y_rubro or not tiene_al_menos_un_dato_verificado:
            aprobado_final = False
            motivo_ajustado = f"{resultado.motivo} [Mantenido en Prospectos: sin confirmación suficiente en internet]."
        else:
            aprobado_final = resultado.datos_validos_y_verificados
            motivo_ajustado = resultado.motivo

        print(f"Resultado: Aprobado={aprobado_final} | CoincideDir={resultado.coincide_ubicacion_y_rubro} | Tel={telefono_definitivo} | Email={email_definitivo}", flush=True)
        print(f"Detalle: {motivo_ajustado}", flush=True)

        valores_actualizar = {}

        if resultado.coincide_ubicacion_y_rubro and email_final and email_final != email_actual:
            valores_actualizar['email_from'] = email_final
            print(f"-> Actualizando Email en Odoo: {email_actual or 'VACÍO'} => {email_final}", flush=True)

        if resultado.coincide_ubicacion_y_rubro and telefono_formateado and telefono_formateado != lead.get('phone'):
            valores_actualizar['phone'] = telefono_formateado
            print(f"-> Actualizando Teléfono en Odoo: {lead.get('phone') or 'VACÍO'} => {telefono_formateado}", flush=True)

        if aprobado_final:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO
            print(f"-> Lead VERIFICADO. Moviendo a etapa ID {ID_ETAPA_CHEQUEADO}.", flush=True)
        else:
            print("-> Permanece en Prospectos por Controlar.", flush=True)

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])

        mensaje_nota = f"Agente de Verificación: {motivo_ajustado}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [lead['id']], {'body': mensaje_nota})

        time.sleep(4)

if __name__ == "__main__":
    ejecutar_agente()
