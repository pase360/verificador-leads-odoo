import os
import re
import time
import xmlrpc.client
import requests
from datetime import datetime, timedelta, timezone
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
MAX_LEADS_POR_EJECUCION = 50
INTERVALO_GEMINI_SEGUNDOS = 4.2
MODELO_GEMINI = "gemini-3.5-flash-lite"

client_ai = genai.Client(api_key=GEMINI_API_KEY)

class ResultadoValidacion(BaseModel):
    email_elegido: str
    telefono_elegido: str
    email_verificado: bool
    telefono_verificado: bool
    coincide_ubicacion_y_rubro: bool
    datos_validos_y_verificados: bool
    motivo: str

class BusquedaSinEvidencia(Exception):
    pass

class CuotaGeminiAgotada(Exception):
    pass

class ServicioGeminiTemporal(Exception):
    pass

def generar_con_gemini(**kwargs):
    ultimo_error = None
    for intento in range(3):
        time.sleep(INTERVALO_GEMINI_SEGUNDOS)
        try:
            return client_ai.models.generate_content(**kwargs)
        except Exception as exc:
            mensaje = str(exc).lower()
            if "429" in mensaje or "resource_exhausted" in mensaje or "quota" in mensaje or "rate limit" in mensaje:
                raise CuotaGeminiAgotada(str(exc)) from exc
            if any(x in mensaje for x in ("503", "unavailable", "service unavailable", "deadline exceeded", "timed out", "timeout")):
                ultimo_error = exc
                if intento < 2:
                    espera = 15 * (2 ** intento)
                    print(f"Gemini temporalmente no disponible. Reintento {intento + 1}/2 en {espera} segundos.", flush=True)
                    time.sleep(espera)
                    continue
                raise ServicioGeminiTemporal(str(exc)) from exc
            raise
    raise ServicioGeminiTemporal(str(ultimo_error or "Gemini no disponible"))

def conectar_odoo():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

def normalizar_texto(valor):
    valor = (valor or '').lower()
    valor = valor.replace('á', 'a').replace('é', 'e').replace('í', 'i').replace('ó', 'o').replace('ú', 'u')
    valor = re.sub(r'[^a-z0-9]+', ' ', valor)
    return re.sub(r'\s+', ' ', valor).strip()


def extraer_codigos_postales(lead):
    """Extrae CP tradicional (X5000) o CPA completo (X5019EHE) del lead."""
    textos = [str(lead.get('zip') or ''), str(lead.get('street') or '')]
    encontrados = []
    for texto in textos:
        encontrados.extend(re.findall(r'(?<![A-Z0-9])([A-Z]\d{4}[A-Z]{3}|[A-Z]\d{4})(?![A-Z0-9])', texto.upper()))
    return list(dict.fromkeys(encontrados))


def consultar_georef_direccion(lead, codigo_postal):
    """
    Consulta la API oficial Georef con la dirección completa.
    El código postal se conserva en el texto enviado para mejorar la identificación.
    No se fuerza el departamento Capital: se comprueba el resultado real para evitar
    convertir por error una dirección de otra localidad en Córdoba Capital.
    """
    direccion = str(lead.get('street') or '').strip()
    if not direccion:
        return 'UBICACION_INCIERTA', 'No hay dirección para georreferenciar'

    if codigo_postal and codigo_postal.lower() not in direccion.lower():
        direccion_consulta = f"{direccion}, {codigo_postal}"
    else:
        direccion_consulta = direccion

    try:
        respuesta = requests.get(
            "https://apis.datos.gob.ar/georef/api/v2.0/direcciones",
            params={
                "direccion": direccion_consulta,
                "provincia": "cordoba",
                "max": 5
            },
            headers={"User-Agent": "Pase360-VerificadorLeads/1.0"},
            timeout=12
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
    except Exception as exc:
        return 'UBICACION_INCIERTA', f"No se pudo consultar Georef: {exc}"

    resultados = datos.get('direcciones') or []
    if len(resultados) != 1:
        return 'UBICACION_INCIERTA', f"Georef devolvió {len(resultados)} coincidencias; no hay identificación única"

    item = resultados[0]
    provincia_item = normalizar_texto((item.get('provincia') or {}).get('nombre') or '')
    departamento_item = normalizar_texto((item.get('departamento') or {}).get('nombre') or '')
    localidad_item = normalizar_texto(
        (item.get('localidad_censal') or {}).get('nombre') or
        (item.get('localidad') or {}).get('nombre') or ''
    )

    if provincia_item and provincia_item != 'cordoba':
        return 'FUERA_DE_ALCANCE', f"Georef ubica la dirección en {provincia_item}"

    # En Córdoba, el departamento Capital identifica la jurisdicción de la ciudad.
    # Si la API no devuelve departamento, no se aprueba por el mero prefijo X5.
    if departamento_item == 'capital' and provincia_item == 'cordoba':
        if localidad_item and localidad_item not in ('cordoba', 'cordoba capital'):
            return 'UBICACION_INCIERTA', f"Georef devolvió localidad no coincidente: {localidad_item}"
        return 'CORDOBA_CAPITAL', (
            f"Georef confirmó Córdoba, departamento Capital"
            + (f", localidad {localidad_item}" if localidad_item else "")
        )

    if provincia_item == 'cordoba' and departamento_item:
        return 'FUERA_DE_ALCANCE', f"Georef ubica la dirección en departamento {departamento_item}"

    return 'UBICACION_INCIERTA', "Georef no confirmó el departamento Capital"


def clasificar_alcance_geografico(lead):
    """Devuelve CORDOBA_CAPITAL, FUERA_DE_ALCANCE o UBICACION_INCIERTA."""
    ciudad = normalizar_texto(lead.get('city') or '')
    pais = lead.get('country_id')
    pais_nombre = normalizar_texto(pais[1] if isinstance(pais, (list, tuple)) and len(pais) > 1 else '')
    provincia = lead.get('state_id')
    provincia_nombre = normalizar_texto(provincia[1] if isinstance(provincia, (list, tuple)) and len(provincia) > 1 else '')
    direccion = normalizar_texto(lead.get('street') or '')

    if pais_nombre and pais_nombre not in ('argentina',):
        return 'FUERA_DE_ALCANCE'

    if provincia_nombre and provincia_nombre not in ('cordoba', 'cordoba capital'):
        return 'FUERA_DE_ALCANCE'

    ciudades_capital = {
        'cordoba', 'cordoba capital', 'capital',
        'cordoba cordoba', 'cordoba capital cordoba',
        'cordoba argentina', 'cordoba cordoba argentina',
        'cordoba capital argentina', 'cordoba capital cordoba argentina'
    }
    if ciudad in ciudades_capital:
        return 'CORDOBA_CAPITAL'
    if ciudad:
        return 'FUERA_DE_ALCANCE'

    # Si la dirección identifica expresamente otra provincia o localidad, no consultar
    # el geocodificador para intentar reinterpretarla.
    patrones_fuera = (
        r'\bprovincia de buenos aires\b',
        r'\bprovincia de santa fe\b',
        r'\bprovincia de entre rios\b',
        r'\bprovincia de san luis\b',
        r'\bprovincia de la pampa\b',
        r'\bprovincia de tucuman\b',
        r'\bprovincia de salta\b',
        r'\bprovincia de mendoza\b',
        r'\bprovincia de neuquen\b',
        r'\bprovincia de rio negro\b',
        r'\bprovincia de chubut\b',
        r'\bprovincia de santa cruz\b',
        r'\bprovincia de misiones\b',
        r'\bprovincia de corrientes\b',
        r'\bprovincia de chaco\b',
        r'\bprovincia de formosa\b',
        r'\bprovincia de jujuy\b',
        r'\bprovincia de catamarca\b',
        r'\bprovincia de la rioja\b',
        r'\bprovincia de san juan\b',
        r'\bprovincia de santiago del estero\b',
        r'\bprovincia de tierra del fuego\b',
        r'\bbelen de escobar\b',
        r'\bvilla allende\b',
        r'\bvilla carlos paz\b',
        r'\bcarlos paz\b',
        r'\balta gracia\b',
        r'\bvilla maria\b',
        r'\brio cuarto\b',
        r'\bsan francisco cordoba\b',
        r'\bvilla del rosario\b',
        r'\bsaldan\b',
        r'\bla calera\b'
    )
    if any(re.search(patron, direccion) for patron in patrones_fuera):
        return 'FUERA_DE_ALCANCE'

    codigos = extraer_codigos_postales(lead)
    if len(codigos) > 1:
        print(f"Ubicación pendiente: hay códigos postales contradictorios {codigos}", flush=True)
        return 'UBICACION_INCIERTA'

    codigo_postal = codigos[0] if codigos else ''
    if codigo_postal:
        # La primera letra del CPA identifica la provincia. X corresponde a Córdoba;
        # el segundo carácter debe ser 5. X5 incluye muchas localidades, no solo Capital.
        if not codigo_postal.startswith('X5'):
            return 'FUERA_DE_ALCANCE'

    alcance, detalle = consultar_georef_direccion(lead, codigo_postal)
    print(
        f"Validación geográfica: CP={codigo_postal or 'no informado'} | "
        f"Resultado={alcance} | {detalle}",
        flush=True
    )
    return alcance

def evidencia_confirma_sucursal(lead, evidencia, resultado):
    """Solo acepta datos si nombre + calle/número + teléfono aparecen en la misma fuente."""
    nombre = normalizar_texto(lead.get('partner_name') or lead.get('name') or '')
    direccion = normalizar_texto(lead.get('street') or '')
    if not nombre or not direccion:
        return False, False, False

    # La dirección debe incluir calle y número; no alcanza con ciudad o barrio.
    partes_direccion = direccion.split()
    if len(partes_direccion) < 2 or not any(re.search(r'\d', p) for p in partes_direccion):
        return False, False, False

    nombre_ok = False
    telefono_ok = False
    email_ok = False
    telefono_ia = limpiar_formato_celular(resultado.telefono_elegido)
    email_ia = (resultado.email_elegido or '').strip().lower()

    for bloque in re.split(r'(?=\n=== FUENTE INDIVIDUAL ===)', evidencia):
        bloque_n = normalizar_texto(bloque)
        # El nombre completo debe figurar en la misma fuente que la dirección.
        if nombre not in bloque_n or direccion not in bloque_n:
            continue
        nombre_ok = True

        # El teléfono sugerido por Gemini debe aparecer literalmente, con formato
        # variable, en esa misma fuente. No basta con que aparezca en otro resultado.
        if telefono_ia:
            candidatos = re.findall(r'(?<!\d)(?:\+?54[\s().-]*9?[\s().-]*)?(?:0[\s().-]*)?\d{2,4}[\s().-]*(?:15[\s().-]*)?\d{2,4}[\s().-]*\d{4}(?!\d)', bloque)
            for candidato in candidatos:
                if limpiar_formato_celular(candidato) == telefono_ia:
                    telefono_ok = True
                    break

        if email_ia and email_ia in bloque.lower():
            email_ok = True

    return nombre_ok, telefono_ok, email_ok


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
    # Si el número ya viene internacional y contiene el 9 de móvil, conservarlo.
    if solo_digitos.startswith("549") and len(solo_digitos) == 13:
        return f"+{solo_digitos}"
    # No agregar el 9 móvil a teléfonos fijos: solo conservar el código internacional.
    if solo_digitos.startswith("54") and len(solo_digitos) == 12:
        return f"+{solo_digitos}"
    # Formato argentino con 0 + característica + 15 + número móvil.
    if solo_digitos.startswith("0") and len(solo_digitos) == 13 and solo_digitos[4:6] == "15":
        return f"+549{solo_digitos[1:4]}{solo_digitos[6:]}"
    if len(solo_digitos) == 12 and solo_digitos[3:5] == "15":
        return f"+549{solo_digitos[:3]}{solo_digitos[5:]}"
    # Formatos nacionales sin indicador de móvil: no inventar el prefijo 9.
    if solo_digitos.startswith("0") and len(solo_digitos) == 11:
        return f"+54{solo_digitos[1:]}"
    if len(solo_digitos) == 10:
        return f"+54{solo_digitos}"
    return f"+{solo_digitos}" if str(numero).strip().startswith("+") else solo_digitos

def extraer_texto_de_url(url):
    if not url or not url.startswith("http"):
        return ""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'es-AR,es;q=0.9'
        }
        response = requests.get(url, headers=headers, timeout=12)
        if response.status_code == 200:
            texto = response.text
            whatsapp_links = re.findall(r'(?:wa\.me/|whatsapp\.com/send\?phone=|whatsapp://send\?phone=)([0-9+\s-]+)', texto, re.IGNORECASE)
            whatsapp_limpios = [re.sub(r'\D', '', w) for w in whatsapp_links if len(re.sub(r'\D', '', w)) >= 8]
            emails_html = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', texto)
            emails_filtrados = list(set([e.lower() for e in emails_html if not e.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', 'sentry.io', 'wixpress.com', 'example.com'))]))
            
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
    ciudad = lead.get('city') or 'Córdoba Capital (verificar con la dirección; no asumir)'
    calle_busqueda = direccion.split(',')[0].strip() if direccion else ''
    
    reporte_total = []
    urls_encontradas = []

    q1 = f'{nombre_original} {calle_busqueda} {ciudad} contacto whatsapp email'
    print(f"-> Buscando en web: {q1}", flush=True)
    res1 = ejecutar_consulta_ddgs(q1, max_res=6)
    snippets1 = []
    for item in res1:
        link = item.get('href', '')
        snippets1.append(f"\n=== FUENTE INDIVIDUAL ===\nTítulo: {item.get('title')}\nTexto: {item.get('body')}\nURL: {link}")
        if link.startswith("http") and "instagram.com" not in link and "facebook.com" not in link and len(urls_encontradas) < 3:
            urls_encontradas.append(link)
    if snippets1:
        reporte_total.append("=== BÚSQUEDA WEB GENERAL ===\n" + "\n".join(snippets1))
    time.sleep(1)

    q2 = f'"{nombre_corto}" {calle_busqueda} {ciudad} (site:facebook.com OR site:instagram.com)' if calle_busqueda else f'"{nombre_corto}" {ciudad} (site:facebook.com OR site:instagram.com)'
    print(f"-> Buscando en Facebook/Instagram: {q2}", flush=True)
    res2 = ejecutar_consulta_ddgs(q2, max_res=6)
    if res2:
        snippets2 = [f"\n=== FUENTE INDIVIDUAL ===\nTítulo: {i.get('title')}\nTexto: {i.get('body')}\nURL: {i.get('href')}" for i in res2]
        reporte_total.append("=== RESULTADOS DE FACEBOOK E INSTAGRAM ===\n" + "\n".join(snippets2))
    time.sleep(1)

    q3 = f'"{nombre_corto}" {calle_busqueda} ("@gmail.com" OR "@hotmail.com" OR "@yahoo.com")'
    print(f"-> Rastreando correos electrónicos: {q3}", flush=True)
    res3 = ejecutar_consulta_ddgs(q3, max_res=5)
    if res3:
        snippets3 = [f"\n=== FUENTE INDIVIDUAL ===\nTítulo: {i.get('title')}\nTexto: {i.get('body')}\nURL: {i.get('href')}" for i in res3]
        reporte_total.append("=== RASTREO DE EMAILS ===\n" + "\n".join(snippets3))
    time.sleep(1)

    for url in list(set(urls_encontradas)):
        if "wa.me" in url or "api.whatsapp.com" in url:
            num_wa = re.sub(r'\D', '', url)
            reporte_total.append(f"=== ENLACE DIRECTO WHATSAPP ===\nNúmero: {num_wa}")
            continue
        print(f"-> Entrando a web oficial: {url}", flush=True)
        contenido = extraer_texto_de_url(url)
        if contenido:
            reporte_total.append(f"\n=== FUENTE INDIVIDUAL ===\nURL: {url}\n{contenido}")
        time.sleep(1)

    evidencia = "\n\n".join(reporte_total).strip()
    if not evidencia:
        raise BusquedaSinEvidencia("DuckDuckGo no entregó resultados ni páginas verificables.")
    return evidencia

def analizar_lead_con_google_search_y_ia(lead, info_duckduckgo):
    nombre_original = lead.get('partner_name') or lead.get('name') or ''
    nombre_corto = limpiar_nombre_busqueda(nombre_original)
    direccion = lead.get('street') or 'No especificada'
    ciudad = lead.get('city') or 'Córdoba'
    email_actual = lead.get('email_from') or 'VACÍO'
    telefono_actual = lead.get('phone') or 'VACÍO'

    prompt = f"""
    Eres un investigador experto en empresas y comercios de Argentina. Usa los resultados de DuckDuckGo y las páginas web obtenidas previamente para encontrar el EMAIL y el TELÉFONO/WHATSAPP reales de este comercio.

    DATOS EN ODOO:
    - Nombre en Odoo: {nombre_original} (Nombre corto en redes: {nombre_corto})
    - Dirección exacta cargada en Odoo: {direccion}
    - Código postal cargado en Odoo: {lead.get('zip') or 'No informado'}
    - Ciudad cargada en Odoo: {ciudad}
    - Email actual en Odoo: {email_actual}
    - Teléfono actual en Odoo: {telefono_actual}

    RESULTADOS PREVIOS DE RASTREO:
    {info_duckduckgo}

    INSTRUCCIONES OBLIGATORIAS DE BÚSQUEDA Y VALIDACIÓN:
    1. USA ÚNICAMENTE LA EVIDENCIA INCLUIDA:
       - Usa solo los resultados de DuckDuckGo y el contenido de páginas efectivamente descargadas.
       - No afirmes haber consultado fuentes que no aparecen en la evidencia.
       - No completes datos por intuición ni por similitud de nombres.
    2. IDENTIDAD, UBICACIÓN Y RUBRO:
       - Confirma que el nombre corresponde al comercio buscado y que la dirección/sucursal coincide con la dirección exacta de Odoo.
       - ALCANCE GEOGRÁFICO OBLIGATORIO: solo aprobar direcciones ubicadas dentro de Córdoba Capital, Córdoba, Argentina. La ciudad de Odoo puede estar vacía; en ese caso determina la localidad usando la dirección completa, código postal y evidencia de las fuentes. No confundas Córdoba Capital con localidades cercanas (por ejemplo Saldán, Villa Allende, La Calera, Carlos Paz, Villa María) ni con otras provincias. Si no puedes confirmar que está dentro del ejido de Córdoba Capital, coincide_ubicacion_y_rubro=FALSE.
       - Confirma que el rubro es compatible con el comercio. No uses datos de otra sucursal, otra ciudad, otro negocio homónimo ni de la marca en general si no se puede vincular al local.
       - Si no puedes confirmar comercio, dirección y rubro con evidencia concreta, coincide_ubicacion_y_rubro=FALSE y no apruebes.
    3. TELÉFONO OBLIGATORIO:
       - telefono_verificado=TRUE solo si el número aparece literalmente en una fuente pública confiable y se puede vincular a este comercio/sucursal.
       - Si el teléfono oficial encontrado difiere del de Odoo, devuelve el oficial normalizado con +549 cuando corresponda.
       - Prioriza WhatsApp o móvil si hay varios. Nunca valides un teléfono solo porque figura en Odoo.
    4. EMAIL OPCIONAL:
       - El email NO es requisito para aprobar.
       - Si encuentras un correo publicado literalmente y vinculado a este comercio/sucursal, devuelve email_elegido y email_verificado=TRUE.
       - Si no encuentras email confiable, devuelve email_elegido="" y email_verificado=FALSE. Esto NO debe impedir la aprobación.
       - Nunca inventes ni atribuyas emails de negocios homónimos, otras sucursales o empresas de otra ciudad.
    5. RESULTADO:
       - datos_validos_y_verificados=TRUE si y solo si la identidad del comercio, la dirección exacta/sucursal, el rubro y el teléfono están confirmados.
       - No exijas email para datos_validos_y_verificados.
       - Nunca marques como verificado un dato solo porque ya estaba cargado en Odoo.
    """

    # Una sola llamada por lead: toda la evidencia ya fue reunida por DuckDuckGo y la lectura de webs.
    response = generar_con_gemini(
        model=MODELO_GEMINI,
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

    # SOLO se leen leads ya existentes en la etapa Prospectos por Controlar.
    # No se crean contactos, leads ni oportunidades nuevas.
    id_prospectos = 5
    campos = ['id', 'name', 'partner_name', 'contact_name', 'email_from', 'phone', 'website', 'street', 'city', 'state_id', 'zip', 'country_id']
    # Trae todos los leads de etapa 5 y rota por los auditados más antiguos.
    # Así los pendientes recientes no bloquean indefinidamente a los demás: cada
    # ejecución toma hasta 50 y los que recibieron una nota nueva pasan al final.
    dominio = [['stage_id', '=', id_prospectos]]
    todos_los_leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
        [dominio],
        {'fields': campos, 'limit': 10000, 'order': 'id asc'}
    )
    mensajes_agente = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'mail.message', 'search_read',
        [[['model', '=', 'crm.lead'], ['body', 'ilike', 'Agente de Verificación:']]],
        {'fields': ['res_id', 'date'], 'limit': 20000, 'order': 'date desc'}
    )

    ultima_auditoria = {}
    for mensaje in mensajes_agente:
        res_id = mensaje.get('res_id')
        fecha = mensaje.get('date') or ''
        if res_id and (res_id not in ultima_auditoria or fecha > ultima_auditoria[res_id]):
            ultima_auditoria[res_id] = fecha

    # Sin auditoría previa primero; luego los auditados hace más tiempo.
    todos_los_leads.sort(
        key=lambda lead: (
            ultima_auditoria.get(lead['id'], ''),
            lead['id']
        )
    )
    leads = todos_los_leads[:MAX_LEADS_POR_EJECUCION]
    con_auditoria_previa = sum(1 for lead in todos_los_leads if lead['id'] in ultima_auditoria)
    print(
        f"Auditoria de leads EXISTENTES en Prospectos por Controlar (etapa {id_prospectos}). "
        f"Total en etapa: {len(todos_los_leads)} | Con auditoria previa: {con_auditoria_previa} "
        f"| Sin auditoria previa: {len(todos_los_leads) - con_auditoria_previa} "
        f"| Lote seleccionado: {len(leads)} | Maximo por ejecucion: {MAX_LEADS_POR_EJECUCION}. "
        f"Modelo: {MODELO_GEMINI}.",
        flush=True
    )
    if not leads:
        print("No hay leads pendientes para auditar.", flush=True)
        return

    aprobados = 0
    pendientes = 0
    procesados = 0
    fallas_busqueda = 0
    no_auditados = 0
    fuera_alcance = 0

    for lead in leads:
        print(f"\n--- Auditando Lead ID {lead['id']}: {lead['name']} (Dir: {lead.get('street') or 'S/D'}) ---", flush=True)

        try:
            alcance = clasificar_alcance_geografico(lead)
            if alcance == 'FUERA_DE_ALCANCE':
                nota = (
                    "Agente de Verificación: FUERA DE ALCANCE. La dirección cargada en Odoo "
                    f"no corresponde a Córdoba Capital (ciudad: {lead.get('city') or 'sin ciudad'}, "
                    f"provincia: {(lead.get('state_id') or ['', ''])[1] if isinstance(lead.get('state_id'), (list, tuple)) and len(lead.get('state_id')) > 1 else 'sin provincia'}, "
                    f"país: {(lead.get('country_id') or ['', ''])[1] if isinstance(lead.get('country_id'), (list, tuple)) and len(lead.get('country_id')) > 1 else 'sin país'}). "
                    "No se realizó búsqueda ni se modificaron datos."
                )
                models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead['id']]], {'body': nota})
                procesados += 1
                fuera_alcance += 1
                print(nota, flush=True)
                continue
            if alcance == 'UBICACION_INCIERTA':
                nota = (
                    "Agente de Verificación: PENDIENTE. No se pudo confirmar Córdoba Capital "
                    "con la dirección/código postal mediante Georef. No se realizó búsqueda "
                    "ni se modificaron datos para evitar aprobar otra localidad."
                )
                models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead['id']]], {'body': nota})
                procesados += 1
                pendientes += 1
                print(nota, flush=True)
                continue

            info_ddgs = investigacion_profunda_con_google_search(lead)
            resultado = analizar_lead_con_google_search_y_ia(lead, info_ddgs)
        except BusquedaSinEvidencia as e:
            fallas_busqueda += 1
            nota_error = f"Agente de Verificación: PENDIENTE. No se modificó el lead porque la búsqueda no devolvió evidencia suficiente: {e}"
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead['id']]], {'body': nota_error})
            print(f"BUSQUEDA SIN EVIDENCIA. No se modifica el lead y queda pendiente: {e}", flush=True)
            continue
        except CuotaGeminiAgotada as e:
            nota_error = f"Agente de Verificación: PENDIENTE. Ejecución detenida por límite de cuota de Gemini; no se modificaron los datos: {e}"
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead['id']]], {'body': nota_error})
            print(f"CUOTA/LÍMITE GEMINI ALCANZADO. Se detiene toda la ejecución para proteger los pendientes: {e}", flush=True)
            break
        except ServicioGeminiTemporal as e:
            no_auditados += 1
            nota_error = f"Agente de Verificación: PENDIENTE. Gemini no estuvo disponible; no se modificaron los datos: {e}"
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead['id']]], {'body': nota_error})
            print(f"GEMINI SIGUE NO DISPONIBLE DESPUÉS DE LOS REINTENTOS. Se detiene sin modificar este lead: {e}", flush=True)
            break
        except Exception as e:
            nota_error = f"Agente de Verificación: PENDIENTE. Error durante la auditoría; no se modificaron los datos: {e}"
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead['id']]], {'body': nota_error})
            print(f"Error de Gemini en lead {lead['id']}; queda pendiente y se continúa: {e}", flush=True)
            pendientes += 1
            continue

        email_final = (resultado.email_elegido or '').strip().lower()
        telefono_formateado = limpiar_formato_celular(resultado.telefono_elegido)
        email_actual = (lead.get('email_from') or '').strip().lower()
        telefono_actual = limpiar_formato_celular(lead.get('phone'))

        email_definitivo = email_final if email_final else email_actual
        telefono_definitivo = telefono_formateado if telefono_formateado else telefono_actual

        evidencia_nombre_direccion, evidencia_telefono, evidencia_email = evidencia_confirma_sucursal(
            lead, info_ddgs, resultado
        )
        telefono_verificado = bool(
            resultado.telefono_verificado and telefono_formateado and evidencia_telefono
        )
        identidad_sucursal_verificada = bool(evidencia_nombre_direccion and evidencia_telefono)
        aprobado_final = bool(
            alcance == 'CORDOBA_CAPITAL' and
            resultado.coincide_ubicacion_y_rubro and
            resultado.datos_validos_y_verificados and
            identidad_sucursal_verificada and
            telefono_verificado
        )

        if aprobado_final:
            motivo_ajustado = resultado.motivo
        else:
            faltantes = []
            if not telefono_verificado:
                faltantes.append("teléfono no confirmado")
            if not resultado.coincide_ubicacion_y_rubro or not evidencia_nombre_direccion:
                faltantes.append("nombre y dirección exacta no demostrados en una misma fuente")
            if not evidencia_telefono:
                faltantes.append("teléfono no vinculado públicamente a esa sucursal")
            motivo_ajustado = f"{resultado.motivo} [NO APROBADO: {', '.join(faltantes) or 'validación insuficiente'}]."

        print(
            f"Resultado: Aprobado={aprobado_final} | Email verificado={resultado.email_verificado} "
            f"| Teléfono verificado={resultado.telefono_verificado} "
            f"| Tel={telefono_definitivo} | Email={email_definitivo}",
            flush=True
        )
        print(f"Detalle: {motivo_ajustado}", flush=True)

        valores_actualizar = {}
        if evidencia_nombre_direccion and evidencia_email and email_final and email_final != email_actual and resultado.email_verificado:
            valores_actualizar['email_from'] = email_final
        if evidencia_nombre_direccion and evidencia_telefono and telefono_formateado and telefono_formateado != telefono_actual and resultado.telefono_verificado:
            valores_actualizar['phone'] = telefono_formateado
        if aprobado_final:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])

        mensaje_nota = f"Agente de Verificación: {motivo_ajustado}"
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead['id']]], {'body': mensaje_nota})

        procesados += 1
        if aprobado_final:
            aprobados += 1
        else:
            pendientes += 1

    print(
        f"\nRESUMEN: encontrados={len(leads)} | auditados={procesados} | aprobados={aprobados} "
        f"| pendientes_auditados={pendientes} | fuera_alcance={fuera_alcance} "
        f"| fallas_busqueda={fallas_busqueda} | no_auditados={len(leads)-procesados-fallas_busqueda}",
        flush=True
    )

if __name__ == "__main__":
    ejecutar_agente()
