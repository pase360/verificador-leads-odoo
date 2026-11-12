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
    email_elegido: str
    telefono_elegido: str
    coincide_ubicacion_y_rubro: bool
    es_contacto_valido: bool
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
    ciudad = lead.get('city') or 'Cordoba Argentina'
    web_actual = lead.get('website') or ''
    
    calle_busqueda = direccion.split(',')[0].strip() if direccion else ''
    
    reporte_total = []
    urls_encontradas = []
    
    if web_actual.startswith("http") and "instagram.com" not in web_actual and "facebook.com" not in web_actual:
        urls_encontradas.append(web_actual)

    # Ronda 1: Búsqueda general
    q1 = f'{nombre} {calle_busqueda} {ciudad} contacto whatsapp telefono email' if calle_busqueda else f'{nombre} {ciudad} contacto whatsapp telefono email'
    resultados1 = ejecutar_consulta_ddgs(q1, max_res=6)
    
    snippets = []
    for item in resultados1:
        link = item.get('href', '')
        snippets.append(f"- Título: {item.get('title')} | Desc: {item.get('body')} | Link: {link}")
        if link.startswith("http") and "instagram.com" not in link and "facebook.com" not in link and len(urls_encontradas) < 3:
            urls_encontradas.append(link)
            
    if snippets:
        reporte_total.append("=== RESULTADOS DE BÚSQUEDA EN INTERNET ===\n" + "\n".join(snippets))
    time.sleep(1)

    # Ronda 2: Redes Sociales (Instagram y Facebook)
    q2 = f'{nombre} {ciudad} (site:instagram.com OR site:facebook.com)'
    resultados2 = ejecutar_consulta_ddgs(q2, max_res=6)
    if resultados2:
        snippets_redes = [f"- {i.get('title')}: {i.get('body')} (Link: {i.get('href')})" for i in resultados2]
        reporte_total.append("=== REDES SOCIALES (INSTAGRAM / FACEBOOK) ===\n" + "\n".join(snippets_redes))
    time.sleep(1)

    # Ronda 3: Búsqueda específica de email (@gmail.com, @hotmail.com, correo)
    q3 = f'{nombre} {ciudad} ("@gmail.com" OR "@hotmail.com" OR "email" OR "correo")'
    resultados3 = ejecutar_consulta_ddgs(q3, max_res=5)
    if resultados3:
        snippets_mail = [f"- {i.get('title')}: {i.get('body')} (Link: {i.get('href')})" for i in resultados3]
        reporte_total.append("=== BÚSQUEDA ESPECÍFICA DE CORREO ELECTRÓNICO ===\n" + "\n".join(snippets_mail))
    time.sleep(1)

    # Ronda 4: Entrar a las webs encontradas
    for url in list(set(urls_encontradas)):
        if "wa.me" in url or "api.whatsapp.com" in url:
            num_wa = re.sub(r'\D', '', url)
            reporte_total.append(f"=== ENLACE DIRECTO DE WHATSAPP HALLADO EN BUSCADOR ===\nNúmero WhatsApp: {num_wa}")
            continue
        print(f"-> Entrando a investigar la web: {url}", flush=True)
        contenido_interno = extraer_texto_de_url(url)
        if contenido_interno:
            reporte_total.append(f"=== CONTENIDO REAL DENTRO DE LA WEB ({url}) ===\n{contenido_interno}")
        time.sleep(1)

    return "\n\n".join(reporte_total)

def analizar_lead_con_ia(lead, info_internet):
    prompt = f"""
    Eres un auditor de datos CRM en Argentina. Tu trabajo es auditar, corregir y completar el EMAIL y el TELÉFONO/WHATSAPP de este lead en Odoo para enviarle una propuesta comercial de alianzas y beneficios (red de descuentos y promociones).

    DATOS ACTUALES EN ODOO (PUEDEN TENER ERRORES Y DEBEN SER CORREGIDOS SI LA WEB/RED SOCIAL DICE OTRA COSA):
    - Nombre del Lead: {lead.get('name')}
    - Empresa / Contacto: {lead.get('partner_name') or lead.get('contact_name') or 'No especificado'}
    - Dirección en Odoo: {lead.get('street') or 'No especificada'}
    - Ciudad / Provincia: {lead.get('city') or 'Córdoba'}
    - Email actual en Odoo: {lead.get('email_from') or 'VACÍO'}
    - Teléfono actual en Odoo: {lead.get('phone') or 'VACÍO'}

    RESULTADOS DE INVESTIGACIÓN EN WEB Y REDES SOCIALES:
    {info_internet}

    REGLAS ESTRICTAS DE AUDITORÍA, CORRECCIÓN Y SELECCIÓN:
    1. CERO INVENCIÓN DE DATOS:
       - Nunca inventes ni supongas un email o teléfono que no esté escrito literalmente en los resultados.
    2. CONTROL DE UBICACIÓN Y RUBRO:
       - Si el resultado encontrado está en la misma ciudad/localidad (ej. Córdoba, Villa Carlos Paz) y corresponde al mismo negocio y rubro, pon 'coincide_ubicacion_y_rubro' = TRUE.
       - Pon FALSE únicamente si es otro rubro o está en otra provincia distinta.
    3. REGLA DE TELÉFONO Y SELECCIÓN SI HAY VARIOS (LA WEB/RED SOCIAL MANDA SOBRE ODOO):
       - Compara el teléfono publicado en la red social (Facebook/Instagram) o página web oficial del negocio con el teléfono actual de Odoo.
       - SI NO COINCIDEN (por ejemplo, Odoo tiene +5493518502550 pero el Facebook oficial del local publica 0351 659-1654), EL DE LA RED SOCIAL O WEB OFICIAL ES EL CORRECTO Y DEBES DEVOLVER ESE EN 'telefono_elegido' PARA SOBRESCRIBIR Y CORREGIR ODOO.
       - Si la web o red social publica MÁS DE UN teléfono: prioriza SIEMPRE el que diga "WhatsApp", tenga el ícono o botón de WhatsApp ([BOTÓN DE WHATSAPP DETECTADO]), o sea un celular móvil por encima de una línea fija. Si solo tiene un número oficial publicado en su red social/web (como contacto principal), úsalo normalizado con +549 adelante.
       - Si en internet no aparece ningún teléfono publicado para ese comercio pero en Odoo ya hay uno cargado, devuelve el de Odoo normalizado.
    4. REGLA DE EMAIL Y SELECCIÓN SI HAY VARIOS (IDEAL PARA PROPUESTAS COMERCIALES):
       - Si la web o red social publica MÁS DE UN correo electrónico (ej. compras@, rrhh@, proveedores@, ventas@, info@, contacto@, administracion@), elige SIEMPRE el más adecuado para recibir una propuesta comercial, alianza o adhesión a una red de beneficios (prioriza en este orden: comercial@, marketing@, alianzas@, contacto@, info@, administracion@, gerencia@, o el correo general del negocio como gmail/hotmail). Descarta correos exclusivos de soporte técnico, facturas o currículums (rrhh@).
       - Si el email de Odoo no coincide con el de la web/red social oficial, reemplázalo por el oficial.
    5. CONDICIÓN DE APROBACIÓN ('datos_validos_y_verificados' = TRUE):
       - Pon TRUE únicamente si 'coincide_ubicacion_y_rubro' = TRUE y el lead queda con AMBOS DATOS (Email válido Y Teléfono de contacto/WhatsApp) confirmados y corregidos.
       - Si tras investigar sigue faltando el Email o sigue faltando el Teléfono, pon FALSE para que se guarde el avance pero permanezca en Prospectos.
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

    print(f"Iniciando auditoría y corrección inteligente en {len(leads)} leads...", flush=True)

    for lead in leads:
        print(f"\n--- Auditando Lead ID {lead['id']}: {lead['name']} (Dir: {lead.get('street') or 'S/D'}) ---", flush=True)
        
        info_internet = investigacion_exhaustiva_y_scraping(lead)
        
        try:
            resultado = analizar_lead_con_ia(lead, info_internet)
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

        tiene_email = bool(email_definitivo)
        tiene_telefono = bool(telefono_definitivo) and resultado.es_contacto_valido

        if not resultado.coincide_ubicacion_y_rubro or not tiene_email or not tiene_telefono:
            aprobado_final = False
            motivo_ajustado = f"{resultado.motivo} [Mantenido en Prospectos: falta {'Email' if not tiene_email else 'Teléfono'} o confirmación]."
        else:
            aprobado_final = resultado.datos_validos_y_verificados
            motivo_ajustado = resultado.motivo

        print(f"Resultado: Aprobado={aprobado_final} | CoincideDir={resultado.coincide_ubicacion_y_rubro} | Tel={telefono_definitivo} | Email={email_definitivo}", flush=True)

        valores_actualizar = {}

        # Sobrescribir email en Odoo si el de la web/red social es distinto o completa uno vacío
        if resultado.coincide_ubicacion_y_rubro and email_final and email_final != email_actual:
            valores_actualizar['email_from'] = email_final
            print(f"-> Corrigiendo/Completando Email en Odoo: {email_actual or 'VACÍO'} => {email_final}", flush=True)

        # Sobrescribir teléfono en Odoo si el de la web/red social no coincide con el guardado
        if resultado.coincide_ubicacion_y_rubro and telefono_formateado and telefono_formateado != lead.get('phone'):
            valores_actualizar['phone'] = telefono_formateado
            print(f"-> Corrigiendo/Completando Teléfono en Odoo: {lead.get('phone') or 'VACÍO'} => {telefono_formateado}", flush=True)

        if aprobado_final:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO
            print(f"-> Lead COMPLETO y corregido. Moviendo a etapa ID {ID_ETAPA_CHEQUEADO}.", flush=True)
        else:
            print("-> Avance guardado (si halló datos). Sigue en Prospectos hasta completar ambos.", flush=True)

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])

        mensaje_nota = f"Agente de Verificación: {motivo_ajustado}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [lead['id']], {'body': mensaje_nota})

        time.sleep(3)

if __name__ == "__main__":
    ejecutar_agente()
