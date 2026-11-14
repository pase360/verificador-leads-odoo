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
    Normaliza únicamente calle y altura, como exige Georef.
    Filtra desde la API por provincia Córdoba y departamento Capital.
    Solo confirma si la respuesta identifica sin ambigüedad la misma altura.
    """
    direccion_original = str(lead.get('street') or '').strip()
    if not direccion_original:
        return 'UBICACION_INCIERTA', 'No hay dirección para georreferenciar'

    # Georef requiere calle/altura, no localidad, provincia ni código postal.
    # En Odoo suelen estar separados por comas: "Av. Colón 52, X5000 Córdoba, Argentina".
    direccion_consulta = direccion_original.split(',')[0].strip()
    direccion_consulta = re.sub(
        r'(?<![A-Z0-9])[A-Z]\d{4}[A-Z]{3}(?![A-Z0-9])|(?<![A-Z0-9])[A-Z]\d{4}(?![A-Z0-9])',
        ' ', direccion_consulta, flags=re.IGNORECASE
    )
    direccion_consulta = re.sub(
        r'\b(Argentina|Córdoba|Cordoba|Capital)\b',
        ' ', direccion_consulta, flags=re.IGNORECASE
    )
    direccion_consulta = re.sub(r'\s+', ' ', direccion_consulta).strip(' ,.-')
    alturas = re.findall(r'\d+', direccion_consulta)
    if not direccion_consulta or not alturas:
        return 'UBICACION_INCIERTA', 'No se pudo separar calle y altura de la dirección de Odoo'
    altura_esperada = alturas[-1]

    try:
        respuesta = requests.get(
            "https://apis.datos.gob.ar/georef/api/v2.0/direcciones",
            params={
                "direccion": direccion_consulta,
                "provincia": "cordoba",
                "departamento": "capital",
                "max": 10,
                "campos": "estandar"
            },
            headers={"User-Agent": "Pase360-VerificadorLeads/1.0"},
            timeout=15
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
    except Exception as exc:
        return 'UBICACION_INCIERTA', f"No se pudo consultar Georef: {exc}"

    resultados = datos.get('direcciones') or []
    if not resultados:
        return 'UBICACION_INCIERTA', 'Georef no encontró la dirección dentro del departamento Capital'

    coincidencias = []
    for item in resultados:
        provincia_item = normalizar_texto((item.get('provincia') or {}).get('nombre') or '')
        departamento_item = normalizar_texto((item.get('departamento') or {}).get('nombre') or '')
        localidad_item = normalizar_texto((item.get('localidad_censal') or {}).get('nombre') or '')
        altura_item = (item.get('altura') or {}).get('valor')
        calle_item = (item.get('calle') or {}).get('id') or (item.get('calle') or {}).get('nombre') or ''

        if provincia_item != 'cordoba' or departamento_item != 'capital':
            continue
        if localidad_item and localidad_item != 'cordoba':
            continue
        if altura_item is None or str(altura_item) != altura_esperada:
            continue
        coincidencias.append((str(calle_item), str(altura_item), localidad_item))

    unicas = set(coincidencias)
    if len(unicas) == 1:
        _, altura_confirmada, localidad = next(iter(unicas))
        return 'CORDOBA_CAPITAL', (
            f"Georef confirmó Córdoba, departamento Capital, altura {altura_confirmada}"
            + (f", localidad censal {localidad}" if localidad else "")
        )

    if len(unicas) > 1:
        return 'UBICACION_INCIERTA', 'Georef devolvió más de una calle posible para esa altura'
    return 'UBICACION_INCIERTA', (
        f"Georef no pudo confirmar la altura {altura_esperada} en Córdoba Capital"
    )


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
    if ciudad and ciudad not in ciudades_capital:
        return 'FUERA_DE_ALCANCE'
    # Si city está vacío o indica Córdoba Capital, Georef debe confirmar calle y altura.

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
    """Valida nombre + calle/altura + al menos un contacto en una misma fuente."""
    nombre = normalizar_texto(lead.get('partner_name') or lead.get('name') or '')
    direccion = str(lead.get('street') or '').split(',')[0].strip()
    direccion_n = normalizar_texto(direccion)
    if not nombre or not direccion_n:
        return False, False, False

    # Exigir calle y altura, pero no CP/localidad/país: muchas fuentes publican solo calle y número.
    tokens = direccion_n.split()
    numeros = re.findall(r'\d+', direccion_n)
    if not numeros or len(tokens) < 2:
        return False, False, False
    altura = numeros[-1]
    calle_tokens = [t for t in tokens if not t.isdigit()]
    calle_clave = " ".join(calle_tokens)
    # Normalizar abreviaturas frecuentes sin aceptar coincidencias por altura aislada.
    calle_clave = re.sub(r'\b(av|avenida)\b', 'avenida', calle_clave)
    calle_clave = re.sub(r'\b(bv|boulevard)\b', 'boulevard', calle_clave)

    nombre_ok = telefono_ok = email_ok = False
    telefono_ia = limpiar_formato_celular(resultado.telefono_elegido)
    email_ia = (resultado.email_elegido or '').strip().lower()

    for bloque in re.split(r'(?=\n=== FUENTE INDIVIDUAL ===)', evidencia):
        bloque_n = normalizar_texto(bloque)
        if nombre not in bloque_n:
            # Permitir nombre corto solo si tiene al menos dos palabras significativas.
            nombre_corto = normalizar_texto(limpiar_nombre_busqueda(lead.get('partner_name') or lead.get('name') or ''))
            if len(nombre_corto.split()) < 2 or nombre_corto not in bloque_n:
                continue

        # La misma fuente debe contener la calle y altura de la sucursal.
        if altura not in re.findall(r'\d+', bloque_n):
            continue
        calle_norm_bloque = re.sub(r'\b(av|avenida)\b', 'avenida', bloque_n)
        calle_norm_bloque = re.sub(r'\b(bv|boulevard)\b', 'boulevard', calle_norm_bloque)
        if not calle_clave or not all(t in calle_norm_bloque.split() for t in calle_clave.split()):
            continue
        nombre_ok = True

        # Correo: tomar el elegido por Gemini solo si aparece literalmente en esta fuente.
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', bloque)
        emails = [e.lower().strip('.,;:()[]{}<>') for e in emails]
        emails = [e for e in emails if not any(x in e for x in ('example.com','sentry.io','wixpress.com'))]
        if email_ia and email_ia in emails:
            email_ok = True

        # Teléfono: comparar el elegido por Gemini con números publicados en esa misma fuente.
        if telefono_ia:
            candidatos = re.findall(r'(?<!\d)(?:\+?54[\s().-]*9?[\s().-]*)?(?:0[\s().-]*)?\d{2,4}[\s().-]*(?:15[\s().-]*)?\d{2,4}[\s().-]*\d{4}(?!\d)', bloque)
            for candidato in candidatos:
                if limpiar_formato_celular(candidato) == telefono_ia:
                    telefono_ok = True
                    break

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
    """Descarga una página y extrae contenido, emails, WhatsApp y enlaces internos de contacto."""
    if not url or not url.startswith("http"):
        return ""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
            'Accept-Language': 'es-AR,es;q=0.9'
        }
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return ""
        from urllib.parse import urljoin
        html = response.text[:1500000]
        whatsapp_links = re.findall(r'(?:wa\.me/|whatsapp\.com/send\?phone=|whatsapp://send\?phone=)([0-9+\s-]+)', html, re.IGNORECASE)
        whatsapp_limpios = [re.sub(r'\D', '', w) for w in whatsapp_links if len(re.sub(r'\D', '', w)) >= 8]
        emails_html = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html)
        emails_filtrados = sorted(set(e.lower() for e in emails_html if not e.lower().endswith(('.png','.jpg','.jpeg','.gif','.svg','.webp')) and not any(x in e.lower() for x in ('sentry.io','wixpress.com','example.com'))))
        enlaces_contacto = []
        for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
            texto_link = re.sub(r'<[^>]+>', ' ', label)
            if re.search(r'contact|contacto|nosotros|empresa|about|quienes|quiénes|sucursal|local', href + " " + texto_link, re.I):
                destino = urljoin(url, href.split('#')[0])
                if destino.startswith("http") and destino not in enlaces_contacto:
                    enlaces_contacto.append(destino)
        texto_limpio = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        texto_limpio = re.sub(r'<style[^>]*>.*?</style>', ' ', texto_limpio, flags=re.DOTALL | re.IGNORECASE)
        texto_limpio = re.sub(r'<[^>]+>', ' ', texto_limpio)
        texto_limpio = re.sub(r'&nbsp;|&#160;', ' ', texto_limpio, flags=re.I)
        texto_limpio = re.sub(r'&amp;', '&', texto_limpio, flags=re.I)
        texto_limpio = re.sub(r'\s+', ' ', texto_limpio)
        extras = []
        if whatsapp_limpios:
            extras.append("[WHATSAPP PUBLICADO: " + ", ".join(sorted(set(whatsapp_limpios))) + "]")
        if emails_filtrados:
            extras.append("[EMAILS PUBLICADOS EN EL HTML: " + ", ".join(emails_filtrados[:15]) + "]")
        if enlaces_contacto:
            extras.append("[PÁGINAS INTERNAS DE CONTACTO: " + " | ".join(enlaces_contacto[:12]) + "]")
        return "\n".join(extras) + "\nContenido visible: " + texto_limpio[:10000]
    except Exception as exc:
        print(f"No se pudo leer {url}: {exc}", flush=True)
        return ""
def ejecutar_consulta_ddgs(query, max_res=7):
    try:
        resultados = DDGS().text(query, region="ar-es", max_results=max_res)
        return resultados if resultados else []
    except Exception:
        return []

def investigacion_profunda_con_google_search(lead):
    """Busca primero emails: buscador, web oficial, páginas internas y redes públicas."""
    from urllib.parse import urlparse
    nombre_original = lead.get('partner_name') or lead.get('name') or ''
    nombre_corto = limpiar_nombre_busqueda(nombre_original)
    direccion = lead.get('street') or ''
    calle_busqueda = direccion.split(',')[0].strip() if direccion else ''
    ciudad = lead.get('city') or 'Córdoba Capital, Argentina'
    base = f'"{nombre_corto}" "{calle_busqueda}" Córdoba Argentina' if calle_busqueda else f'"{nombre_corto}" Córdoba Argentina'

    reporte = []
    resultados_urls = []
    urls_vistas = set()
    dominios_no_utiles = (
        'wikipedia.org','pornhub.com','xvideos.com','youtube.com','tiktok.com',
        'linkedin.com','tripadvisor.','pedidosya.','rappi.','mercadolibre.',
        'ubereats.','indeed.','glassdoor.','mapquest.','yelp.','zoominfo.'
    )

    consultas = [
        (f'{base} email correo electrónico contacto', 8, "BÚSQUEDA PRIORITARIA DE EMAIL"),
        (f'{base} mail @gmail.com OR @hotmail.com OR @outlook.com OR @yahoo.com', 8, "BÚSQUEDA DE CORREOS PÚBLICOS"),
        (f'"{nombre_corto}" "{calle_busqueda}" contacto teléfono email', 8, "BÚSQUEDA DE IDENTIDAD Y CONTACTO"),
        (f'"{nombre_corto}" Córdoba Facebook Instagram contacto email', 7, "BÚSQUEDA EN REDES SOCIALES"),
        (f'"{nombre_corto}" "{calle_busqueda}" "@"', 7, "BÚSQUEDA EXACTA DE EMAIL"),
    ]

    for query, limite, etiqueta in consultas:
        print(f"-> {etiqueta}: {query}", flush=True)
        resultados = ejecutar_consulta_ddgs(query, max_res=limite)
        bloques = []
        for item in resultados:
            titulo = item.get('title') or ''
            cuerpo = item.get('body') or ''
            link = item.get('href') or ''
            bloques.append(f"\n=== FUENTE INDIVIDUAL ===\nTítulo: {titulo}\nTexto: {cuerpo}\nURL: {link}")
            if link.startswith("http") and not any(d in urlparse(link).netloc.lower() for d in dominios_no_utiles):
                if link not in urls_vistas:
                    urls_vistas.add(link)
                    resultados_urls.append((link, titulo, cuerpo))
        if bloques:
            reporte.append(f"=== {etiqueta} ===\n" + "\n".join(bloques))
        time.sleep(1)

    # Priorizar sitios propios; los resultados de redes quedan como evidencia de perfil.
    def puntuacion(item):
        link, titulo, cuerpo = item
        dominio = urlparse(link).netloc.lower()
        puntos = 0
        if any(x in dominio for x in ('facebook.com','instagram.com')): puntos += 2
        else: puntos += 5
        if any(x in (titulo + " " + cuerpo).lower() for x in ('contacto','email','correo','teléfono','telefono')): puntos += 3
        if calle_busqueda and normalizar_texto(calle_busqueda).split()[0] in normalizar_texto(titulo + " " + cuerpo): puntos += 3
        if any(d in dominio for d in dominios_no_utiles): puntos -= 20
        return puntos

    resultados_urls.sort(key=puntuacion, reverse=True)
    urls_para_abrir = [x[0] for x in resultados_urls[:8]]

    # Descargar páginas halladas y descubrir páginas internas de contacto.
    pendientes = list(urls_para_abrir)
    visitadas = set()
    while pendientes and len(visitadas) < 12:
        url = pendientes.pop(0)
        if url in visitadas:
            continue
        visitadas.add(url)
        print(f"-> Leyendo página para encontrar email/contacto: {url}", flush=True)
        contenido = extraer_texto_de_url(url)
        if contenido:
            reporte.append(f"\n=== FUENTE INDIVIDUAL ===\nURL: {url}\n{contenido}")
            for enlace in re.findall(r'https?://[^\s|]+', contenido):
                if any(k in enlace.lower() for k in ('contact','contacto','nosotros','about','empresa')):
                    if enlace not in visitadas and enlace not in pendientes and urlparse(enlace).netloc == urlparse(url).netloc:
                        pendientes.insert(0, enlace)
        time.sleep(0.7)

    evidencia = "\n\n".join(reporte).strip()
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
    Eres un investigador exhaustivo de comercios de Argentina. Tu prioridad número uno es encontrar y verificar el EMAIL público del comercio. No te conformes con el primer resultado ni abandones la búsqueda de email porque hayas encontrado un teléfono.

    DATOS EN ODOO:
    - Nombre: {nombre_original} (nombre corto: {nombre_corto})
    - Dirección de la sucursal: {direccion}
    - Ciudad: {ciudad}
    - Email actual: {email_actual}
    - Teléfono actual: {telefono_actual}

    EVIDENCIA REUNIDA (resultados de búsqueda, HTML de webs y páginas internas):
    {info_duckduckgo}

    REGLAS:
    1. EMAIL: revisa todos los resultados y especialmente las secciones [EMAILS PUBLICADOS EN EL HTML], páginas de Contacto, Nosotros, pie de página y perfiles públicos de Facebook/Instagram. Si aparece un email que pertenece inequívocamente al comercio o a la sucursal, devuelve email_elegido exactamente y email_verificado=TRUE. No uses correos técnicos, de plataformas, de terceros ni de negocios homónimos.
    2. No marques email como no encontrado si la evidencia contiene un correo público claramente vinculado al comercio. El email es prioritario y debe buscarse aunque ya exista teléfono.
    3. TELÉFONO: puede verificarse como alternativa si no se encuentra email. Solo usa un teléfono publicado y vinculado a la misma sucursal.
    4. IDENTIDAD: el nombre y la calle/altura deben corresponder a este comercio y esta sucursal. No confundas sucursales, homónimos, marcas o localidades.
    5. UBICACIÓN: solo Córdoba Capital, Córdoba, Argentina. Georef ya realiza la validación geográfica; no infieras la ubicación por el nombre de la ciudad.
    6. RUBRO: debe ser compatible con el comercio de Odoo.
    7. Nunca inventes ni completes datos por intuición. Usa únicamente la evidencia incluida.
    8. coincide_ubicacion_y_rubro y datos_validos_y_verificados deben ser TRUE solo si identidad, sucursal/dirección y rubro están respaldados por evidencia concreta.
    9. email_verificado y telefono_verificado son independientes. Devuelve ambos si hay evidencia. No es obligatorio encontrar los dos.
    10. Si no hay email fiable, devuelve email_elegido="" y email_verificado=FALSE. Si no hay teléfono fiable, devuelve telefono_elegido="" y telefono_verificado=FALSE.
    11. El motivo debe explicar qué email fue encontrado y en qué tipo de fuente, o qué impidió encontrarlo.
    """

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
        email_verificado = bool(
            resultado.email_verificado and email_final and evidencia_email
        )
        contacto_verificado = bool(telefono_verificado or email_verificado)
        identidad_sucursal_verificada = bool(evidencia_nombre_direccion and contacto_verificado)
        aprobado_final = bool(
            alcance == 'CORDOBA_CAPITAL' and
            resultado.coincide_ubicacion_y_rubro and
            resultado.datos_validos_y_verificados and
            identidad_sucursal_verificada and
            contacto_verificado
        )

        if aprobado_final:
            motivo_ajustado = resultado.motivo
        else:
            faltantes = []
            if not contacto_verificado:
                faltantes.append("no se confirmó email ni teléfono público en la misma fuente que identifica la sucursal")
            if not resultado.coincide_ubicacion_y_rubro or not evidencia_nombre_direccion:
                faltantes.append("nombre y calle/altura no demostrados en una misma fuente")
            motivo_ajustado = f"{resultado.motivo} [NO APROBADO: {', '.join(faltantes) or 'validación insuficiente'}]."

        print(
            f"Resultado: Aprobado={aprobado_final} | Email IA={resultado.email_verificado} "
            f"| Email evidencia={evidencia_email} | Teléfono IA={resultado.telefono_verificado} "
            f"| Teléfono evidencia={evidencia_telefono} | Contacto confirmado={contacto_verificado} "
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
