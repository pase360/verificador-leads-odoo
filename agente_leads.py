import os
import re
import time
import unicodedata
from types import SimpleNamespace
import xmlrpc.client
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin
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
MAX_LEADS_POR_EJECUCION = 60  # Lote por corrida (None = sin límite). Con 4 corridas diarias alcanza de sobra.
INTERVALO_GEMINI_SEGUNDOS = 4.2
MODELO_GEMINI = "gemini-3.5-flash-lite"

# --- Protecciones para que la corrida nunca llegue al límite de 6 horas de GitHub ---
TIEMPO_MAXIMO_SEGUNDOS = int(3.5 * 3600)  # Al llegar a este tiempo corta limpio y termina en verde
MAX_SIN_BUSCADOR_SEGUIDOS = 5             # Si 5 leads seguidos no reciben NINGÚN resultado, el buscador está bloqueado: se aborta
DIAS_REINTENTO_PENDIENTES = 0             # 0 = se revisan todos, los de auditoría más vieja primero (subir a 1 cuando la cola esté limpia)
DIAS_REINTENTO_SIN_EMAIL = 0              # 0 = se revisan todos, los más viejos primero (subir a 7 cuando la cola esté limpia)

# True  = conserva el comportamiento actual: un lead sin evidencia pero con WhatsApp +549351 ya cargado
#         pasa a Chequeado en amarillo, SIN que ese número haya sido verificado en ninguna página.
# False = esos leads quedan en Prospectos hasta que el número se confirme con evidencia.
PASAR_SOLO_WHATSAPP_SIN_VERIFICAR = True

INICIO_EJECUCION = time.time()

client_ai = genai.Client(api_key=GEMINI_API_KEY)

# Dominios de correo gratuitos: pueden ser de cualquiera, por eso se exige más evidencia.
DOMINIOS_CORREO_GENERICOS = (
    'gmail.com', 'hotmail.com', 'hotmail.com.ar', 'outlook.com', 'outlook.com.ar',
    'yahoo.com', 'yahoo.com.ar', 'live.com', 'icloud.com', 'msn.com'
)
# Directorios y plataformas: sus correos propios nunca son del comercio.
DOMINIOS_DIRECTORIO = (
    'paginasamarillas', 'guiaoleo', 'cylex', 'infoisinfo', 'cybo.', 'yelp.',
    'tripadvisor.', 'pedidosya.', 'rappi.', 'mercadolibre.', 'mapquest.',
    'zoominfo.', 'ubereats.', 'indeed.', 'glassdoor.', 'linkedin.',
    'facebook.com', 'instagram.com', 'wikipedia.org', 'google.com'
)
DOMINIOS_SOCIALES = ('facebook.com', 'instagram.com')
EMAILS_BASURA = (
    'sentry.io', 'wixpress.com', 'example.com', 'godaddy', 'squarespace',
    'wordpress.com', 'domain.com', 'tuemail', 'tucorreo', 'tumail',
    'usuario@', 'nombre@', 'ejemplo', 'email@email'
)
PALABRAS_CALLE_IGNORADAS = {'av', 'avenida', 'bv', 'boulevard', 'calle', 'pasaje', 'pje', 'ruta', 'diagonal'}
PALABRAS_NOMBRE_GENERICAS = {'cordoba', 'argentina', 'sucursal', 'centro', 'nueva', 'nuevo', 'general'}
PALABRAS_VACIAS_NOMBRE = {'en', 'de', 'del', 'la', 'el', 'los', 'las', 'y', 'e', 'un', 'una', 'con', 'para'}


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
    valor = unicodedata.normalize('NFKD', (valor or '').lower())
    valor = ''.join(c for c in valor if not unicodedata.combining(c))
    valor = re.sub(r'[^a-z0-9]+', ' ', valor)
    return re.sub(r'\s+', ' ', valor).strip()


def extraer_codigos_postales(lead):
    """Extrae CP tradicional (X5000) o CPA completo (X5019EHE) del lead."""
    textos = [str(lead.get('zip') or ''), str(lead.get('street') or '')]
    encontrados = []
    for texto in textos:
        encontrados.extend(re.findall(r'(?<![A-Z0-9])([A-Z]\d{4}[A-Z]{3}|[A-Z]\d{4})(?![A-Z0-9])', texto.upper()))
    return list(dict.fromkeys(encontrados))


def parte_con_altura(direccion):
    """
    De una dirección de Odoo ("Urca, José Roque Funes 1115 Local 2, X5000 Córdoba") devuelve
    (texto de calle y número sin local/piso, palabras de la calle, altura). None si no hay.
    """
    for parte in str(direccion or '').split(','):
        parte = re.sub(r'(?<![A-Z0-9])[A-Z]\d{4}(?:[A-Z]{3})?(?![A-Z0-9])', ' ', parte)
        parte = re.sub(r'\b(loc(?:al)?|piso|planta|pb|dpto|depto|oficina|of|galer[ií]a|torre|entrepiso)\b.*$', ' ', parte, flags=re.I)
        parte = re.sub(r'\b(Argentina|Córdoba|Cordoba|Capital)\s*$', ' ', parte, flags=re.I)
        parte = re.sub(r'\s+', ' ', parte).strip(' ,.-')
        calle, altura, con_letras = [], '', False
        for t in normalizar_texto(parte).split():
            if t.isdigit():
                if con_letras:
                    altura = t
                    break
                calle.append(t)
            else:
                con_letras = True
                calle.append(t)
        calle = [t for t in calle if t not in PALABRAS_CALLE_IGNORADAS]
        if altura and calle:
            return parte, calle, altura
    return None


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
    parte = parte_con_altura(direccion_original)
    if not parte:
        return 'UBICACION_INCIERTA', 'No se pudo separar calle y altura de la dirección de Odoo'
    direccion_consulta, _, altura_esperada = parte

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


def _nombre_es_util(texto):
    """False si el nombre quedó vacío de contenido real (solo 'en Córdoba', 'de la', etc.)."""
    tokens = [t for t in normalizar_texto(texto).split()
              if t not in PALABRAS_VACIAS_NOMBRE and t not in PALABRAS_NOMBRE_GENERICAS]
    return any(len(t) >= 3 for t in tokens)


def _quitar_rubro(nombre):
    nombre = (nombre or '').strip()
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
    limpio = limpio.strip(' ,.:;-|')
    palabras = limpio.split()
    huerfano = bool(palabras) and palabras[0].lower() in ('y', 'e', 'la', 'el', 'los', 'las', 'un', 'una', 'de', 'del')
    return limpio if (len(palabras) >= 2 and len(limpio) >= 5 and not huerfano) else nombre.strip(' ,.:;-|')


def limpiar_nombre_busqueda(nombre):
    # Quita la sucursal entre corchetes. Si hay un guion largo ("Marca – Rubro"), prueba primero
    # la parte anterior y, si esa quedó sin contenido real (ej. "en Córdoba"), prueba la posterior.
    nombre = re.sub(r'\[.*?\]', '', nombre or '').strip()
    partes = [p.strip() for p in re.split(r'\s[–—-]\s|\s\|\s|\s::\s', nombre, maxsplit=1)]
    if len(partes) == 2 and len(partes[0]) >= 4:
        candidatos = [partes[0], partes[1]]
    else:
        candidatos = [nombre]
    for candidato in candidatos:
        resultado = _quitar_rubro(candidato)
        if resultado and _nombre_es_util(resultado):
            return resultado
    return nombre.strip(' ,.:;-|')


def limpiar_formato_celular(numero):
    if not numero:
        return ""
    solo_digitos = re.sub(r'\D', '', str(numero))
    if not solo_digitos:
        return ""
    if solo_digitos.startswith("540") and len(solo_digitos) == 13:
        solo_digitos = "54" + solo_digitos[3:]

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


# ---------------------------------------------------------------------------
# Verificación de evidencia (reglas deterministas: nada se acepta si no aparece
# escrito en una página que pertenezca claramente al comercio)
# ---------------------------------------------------------------------------

def email_util(email):
    e = (email or '').lower().strip(".,;:()[]{}<>'\"")
    if not e or e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.js', '.css')):
        return ''
    if any(x in e for x in EMAILS_BASURA):
        return ''
    dominio = e.split('@')[-1]
    if any(d in dominio for d in DOMINIOS_DIRECTORIO):
        return ''
    return e


def emails_en_texto(texto):
    encontrados = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', texto)
    limpios = (email_util(e) for e in encontrados)
    return list(dict.fromkeys(e for e in limpios if e))


def separar_bloques(evidencia):
    return re.split(r'(?=\n=== FUENTE INDIVIDUAL ===)', evidencia)


def url_del_bloque(bloque):
    m = re.search(r'URL:\s*(\S+)', bloque)
    return m.group(1) if m else ''


def nombres_del_lead(lead):
    base = re.sub(r'\[.*?\]', '', lead.get('partner_name') or lead.get('name') or '')
    return normalizar_texto(base), normalizar_texto(limpiar_nombre_busqueda(base))


def nombre_en_bloque(lead, bloque_n):
    completo, corto = nombres_del_lead(lead)
    if completo and completo in bloque_n:
        return True
    return bool(corto) and (len(corto.split()) >= 2 or len(corto) >= 6) and corto in bloque_n


def calle_confirmada(lead, bloque_n):
    """Calle y altura de la sucursal presentes en el mismo texto."""
    parte = parte_con_altura(lead.get('street'))
    if not parte:
        return False
    _, calle, altura = parte
    palabras = set(bloque_n.split())
    return altura in palabras and all(t in palabras for t in calle)


def texto_coincide_con_nombre(lead, texto_compacto):
    _, corto = nombres_del_lead(lead)
    junto = corto.replace(' ', '')
    if len(junto) >= 5 and junto in texto_compacto:
        return True
    tokens = [t for t in corto.split() if len(t) >= 3 and t not in PALABRAS_NOMBRE_GENERICAS]
    return len(tokens) >= 2 and all(t in texto_compacto for t in tokens)


def url_coincide_con_nombre(lead, url):
    """True si la web (o el perfil de Facebook/Instagram) parece ser del propio comercio."""
    if not url:
        return False
    p = urlparse(url)
    host = p.netloc.lower().replace('www.', '')
    if any(d in host for d in DOMINIOS_SOCIALES):
        destino = re.sub(r'[^a-z0-9]', '', p.path.lower())
    else:
        if any(d in host for d in DOMINIOS_DIRECTORIO):
            return False
        destino = re.sub(r'[^a-z0-9]', '', host)
    return texto_coincide_con_nombre(lead, destino)


def email_coincide_con_nombre(lead, email):
    dominio = email.split('@')[-1]
    if dominio in DOMINIOS_CORREO_GENERICOS:
        return False
    return texto_coincide_con_nombre(lead, re.sub(r'[^a-z0-9]', '', dominio))


def tipo_de_fuente(lead, bloque, bloque_n, telefono_lead):
    """
    Devuelve el tipo de fuente si el bloque pertenece al comercio (y no a un homónimo):
      'oficial'  -> web propia (no red social) cuyo dominio coincide con el nombre y que cita Córdoba, la calle o el teléfono cargado
      'calle'    -> el nombre y la calle/altura aparecen juntos (en cualquier fuente)
      'telefono' -> el nombre y el teléfono que ya tiene Odoo aparecen juntos (solo sirve de identidad, no confirma datos nuevos)
    Si no, devuelve ''.
    """
    url = url_del_bloque(bloque)
    host = urlparse(url).netloc.lower().replace('www.', '') if url else ''
    es_red = any(d in host for d in DOMINIOS_SOCIALES)
    tiene_calle = calle_confirmada(lead, bloque_n)
    digitos = re.sub(r'\D', '', bloque)
    tiene_telefono = bool(telefono_lead and telefono_lead in digitos)
    if not es_red and url_coincide_con_nombre(lead, url) and (tiene_calle or tiene_telefono or 'cordoba' in bloque_n):
        return 'oficial'
    if not nombre_en_bloque(lead, bloque_n):
        return ''
    if tiene_calle:
        return 'calle'
    if tiene_telefono:
        return 'telefono'
    return ''


PATRON_TELEFONO = re.compile(
    r'(?<!\d)(?:\+?54[\s().-]*9?[\s().-]*)?(?:0[\s().-]*)?\d{2,4}[\s().-]*(?:15[\s().-]*)?\d{2,4}[\s().-]*\d{4}(?!\d)'
)
def es_telefono_cordoba(formateado):
    return bool(re.match(r'^(549|54)?351', re.sub(r'\D', '', formateado or '')))


PALABRAS_TELEFONO = ('tel', 'cel', 'whats', 'wsp', 'wpp', 'contact', 'llam', 'consult', 'ventas', 'pedidos')


def telefonos_en_texto(bloque):
    """Teléfonos publicados en la página, con su posición en el texto visible."""
    visible = bloque.split('Contenido visible:', 1)[-1]
    encontrados = []
    for m in PATRON_TELEFONO.finditer(visible):
        crudo = m.group(0)
        solo = re.sub(r'\D', '', crudo)
        if len(solo) < 8:
            continue
        antes = visible[max(0, m.start() - 40):m.start()].lower()
        con_palabra = any(k in antes for k in PALABRAS_TELEFONO)
        con_prefijo = crudo.strip().startswith('+54') or solo.startswith(('54', '0351', '351'))
        if con_palabra or con_prefijo:
            formateado = limpiar_formato_celular(crudo)
            if formateado and es_telefono_cordoba(formateado):
                encontrados.append((formateado, m.start()))
    for grupo in re.findall(r'WHATSAPP PUBLICADO:\s*([0-9,\s]+)', bloque):
        for n in grupo.split(','):
            solo = re.sub(r'\D', '', n)
            if len(solo) >= 10:
                fmt = '+' + solo if solo.startswith('54') else limpiar_formato_celular(solo)
                if es_telefono_cordoba(fmt):
                    encontrados.append((fmt, 10 ** 8))
    return encontrados


def evaluar_evidencia(lead, evidencia, resultado, alcance):
    """
    Revisa toda la evidencia y devuelve:
      email / telefono: (valor, url) o None -> dato confirmado, listo para guardar
      email_oficial / tel_oficial: True si salió de la web propia del comercio
      otros_emails: otros mails publicados por el comercio
      nombre_ok: bool
      candidatos: mails vistos junto al nombre pero sin confirmar (solo para la nota)
    Regla clave: si la web propia del comercio publica un dato, manda sobre redes y directorios.
    """
    prioridad = {'oficial': 0, 'calle': 1}
    # Se usan los últimos 7 dígitos (sin código de área): así coincide aunque la página
    # lo escriba como "351 702-2266", "0351 15 702 2266" o "+54 9 351 7022266".
    telefono_lead = re.sub(r'\D', '', str(lead.get('phone') or ''))[-7:]
    if len(telefono_lead) < 7:
        telefono_lead = ''
    telefono_ia = limpiar_formato_celular(resultado.telefono_elegido)
    digitos_ia = re.sub(r'\D', '', telefono_ia)[-8:] if telefono_ia else ''
    email_ia = email_util(resultado.email_elegido)
    exigir_cordoba = (alcance == 'UBICACION_INCIERTA')

    confirmados = {}   # email -> (tipo, rango, pos, orden, url)
    telefonos = {}     # ultimos 8 dígitos -> datos
    candidatos = {}    # email -> url
    nombre_ok = False
    orden = 0

    for bloque in separar_bloques(evidencia):
        bloque_n = normalizar_texto(bloque)
        url = url_del_bloque(bloque)
        con_nombre = nombre_en_bloque(lead, bloque_n)
        tipo = tipo_de_fuente(lead, bloque, bloque_n, telefono_lead)
        emails_bloque = emails_en_texto(bloque)

        if not tipo:
            if con_nombre:
                for e in emails_bloque:
                    candidatos.setdefault(e, url)
            continue
        if exigir_cordoba and 'cordoba' not in bloque_n and not calle_confirmada(lead, bloque_n):
            continue
        nombre_ok = True
        elegible = tipo in prioridad   # solo web propia o nombre+calle confirman datos nuevos
        visible = bloque.split('Contenido visible:', 1)[-1].lower()
        host_bloque = urlparse(url).netloc.lower().replace('www.', '') if url else ''

        for e in emails_bloque:
            if e in confirmados:
                continue
            dominio = e.split('@')[-1]
            if dominio in DOMINIOS_CORREO_GENERICOS:
                # Gmail/Hotmail: puede ser de cualquiera, el nombre debe estar en la misma fuente.
                aceptado = con_nombre
                rango = 10
            else:
                aceptado = (
                    email_coincide_con_nombre(lead, e)
                    or (host_bloque and dominio in host_bloque)
                    or tipo == 'calle'
                )
                rango = 0
            if aceptado and elegible:
                pos = visible.find(e)
                orden += 1
                confirmados[e] = (tipo, rango, pos if pos >= 0 else 10 ** 9, orden, url)
            elif con_nombre:
                candidatos.setdefault(e, url)

        if elegible:
            for tel, pos in telefonos_en_texto(bloque):
                clave = re.sub(r'\D', '', tel)[-8:]
                existente = telefonos.get(clave)
                if existente is None:
                    orden += 1
                    telefonos[clave] = {'tel': tel, 'tipo': tipo, 'pos': pos, 'orden': orden, 'url': url}
                elif tel.startswith('+549') and not existente['tel'].startswith('+549'):
                    existente['tel'] = tel

    # Email: si la web propia publica alguno, solo valen los de la web propia.
    email_final = None
    email_oficial = False
    otros_confirmados = []
    if confirmados:
        pool = list(confirmados.items())
        if any(v[0] == 'oficial' for _, v in pool):
            pool = [(e, v) for e, v in pool if v[0] == 'oficial']
            email_oficial = True
        ordenados = sorted(
            pool,
            key=lambda kv: (prioridad[kv[1][0]], kv[1][1], kv[1][2], 0 if kv[0] == email_ia else 1, kv[1][3])
        )
        email_final = (ordenados[0][0], ordenados[0][1][4])
        otros_confirmados = [e for e, _ in ordenados[1:4]]

    # Teléfono: misma regla; entre varios, el que aparece primero en la página.
    telefono_final = None
    tel_oficial = False
    if telefonos:
        pool = list(telefonos.values())
        if any(t['tipo'] == 'oficial' for t in pool):
            pool = [t for t in pool if t['tipo'] == 'oficial']
            tel_oficial = True
        pool.sort(key=lambda t: (
            prioridad[t['tipo']], t['pos'],
            0 if re.sub(r'\D', '', t['tel'])[-8:] == digitos_ia else 1, t['orden']
        ))
        telefono_final = (pool[0]['tel'], pool[0]['url'])

    sin_confirmar = [(e, u) for e, u in candidatos.items() if e not in confirmados][:3]
    return {
        'email': email_final,
        'email_oficial': email_oficial,
        'otros_emails': otros_confirmados,
        'telefono': telefono_final,
        'tel_oficial': tel_oficial,
        'nombre_ok': nombre_ok,
        'candidatos': sin_confirmar,
    }


# ---------------------------------------------------------------------------
# Búsqueda en internet
# ---------------------------------------------------------------------------

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
        html = response.text[:1500000]

        whatsapp_links = re.findall(r'(?:wa\.me/|whatsapp\.com/send\?phone=|whatsapp://send\?phone=)([0-9+\s-]+)', html, re.IGNORECASE)
        whatsapp_limpios = [re.sub(r'\D', '', w) for w in whatsapp_links if len(re.sub(r'\D', '', w)) >= 8]

        emails_html = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html)
        emails_filtrados = list(dict.fromkeys(e.lower() for e in emails_html if not e.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')) and not any(x in e.lower() for x in ('sentry.io', 'wixpress.com', 'example.com'))))

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


# Según la prueba hecha desde GitHub: DuckDuckGo directo está bloqueado, "auto" y Yandex responden,
# Google/Bing/Yahoo responden a medias. Se prueban en este orden hasta que uno devuelva resultados.
BACKENDS_BUSQUEDA = ["auto", "yandex", "google", "bing", "yahoo"]


def ejecutar_consulta_ddgs(query, max_res=7):
    for backend in BACKENDS_BUSQUEDA:
        try:
            resultados = DDGS().text(query, region="ar-es", max_results=max_res, backend=backend)
            if resultados:
                return resultados
        except Exception:
            pass
        time.sleep(1.5)
    return []


def investigacion_profunda_con_google_search(lead, reintento=True):
    """Busca primero emails: buscador, web oficial, páginas internas y redes públicas."""
    nombre_original = lead.get('partner_name') or lead.get('name') or ''
    nombre_corto = limpiar_nombre_busqueda(nombre_original)
    direccion = lead.get('street') or ''
    parte_dir = parte_con_altura(direccion)
    calle_busqueda = parte_dir[0] if parte_dir else ''
    calle_q = f' "{calle_busqueda}"' if calle_busqueda else ''
    telefono_odoo = re.sub(r'\D', '', str(lead.get('phone') or ''))

    base = f'"{nombre_corto}" "{calle_busqueda}" Córdoba Argentina' if calle_busqueda else f'"{nombre_corto}" Córdoba Argentina'

    reporte = []
    resultados_urls = []
    urls_vistas = set()

    dominios_no_utiles = (
        'wikipedia.org', 'pornhub.com', 'xvideos.com', 'youtube.com', 'tiktok.com',
        'linkedin.com', 'tripadvisor.', 'pedidosya.', 'rappi.', 'mercadolibre.',
        'ubereats.', 'indeed.', 'glassdoor.', 'mapquest.', 'yelp.', 'zoominfo.'
    )

    # Se quitó la búsqueda "correo gmail hotmail": repetía la de email y aumentaba el riesgo de bloqueo.
    consultas = [
        (f'"{nombre_corto}" Córdoba sitio web oficial contacto', 8, "BÚSQUEDA DE SITIO OFICIAL"),
        (f'{base} email correo electrónico contacto', 8, "BÚSQUEDA PRIORITARIA DE EMAIL"),
        (f'"{nombre_corto}"{calle_q} contacto teléfono email', 8, "BÚSQUEDA DE IDENTIDAD Y CONTACTO"),
        (f'"{nombre_corto}" Córdoba Facebook Instagram contacto email', 7, "BÚSQUEDA EN REDES SOCIALES"),
        (f'"{nombre_corto}"{calle_q} "@"', 7, "BÚSQUEDA EXACTA DE EMAIL"),
    ]
    # Teléfono: se busca por los últimos 7 dígitos (sin código de área), separados como los escriben
    # las páginas ("702 2266"), no pegados con el código de área como antes.
    ultimos7 = telefono_odoo[-7:]
    if len(ultimos7) == 7 and len(set(ultimos7)) > 2:
        consultas.append((f'"{nombre_corto}" "{ultimos7[:3]} {ultimos7[3:]}" Córdoba', 6, "BÚSQUEDA POR TELÉFONO CARGADO"))

    # En el reintento (después de un vacío total) solo se repiten las 3 primeras, no todas.
    if not reintento:
        consultas = consultas[:3]

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
        time.sleep(2)

    # Priorizar sitios propios; los resultados de redes quedan como evidencia de perfil.
    def puntuacion(item):
        link, titulo, cuerpo = item
        dominio = urlparse(link).netloc.lower()
        puntos = 0
        if url_coincide_con_nombre(lead, link):
            puntos += 8
        if any(x in dominio for x in ('facebook.com', 'instagram.com')):
            puntos += 2
        else:
            puntos += 5
        if any(x in (titulo + " " + cuerpo).lower() for x in ('contacto', 'email', 'correo', 'teléfono', 'telefono')):
            puntos += 3
        if calle_busqueda and normalizar_texto(calle_busqueda).split()[0] in normalizar_texto(titulo + " " + cuerpo):
            puntos += 3
        if any(d in dominio for d in dominios_no_utiles):
            puntos -= 20
        return puntos

    # Solo se abren páginas que nombran al comercio o su dirección (evita Outlook, Yahoo, etc.).
    def es_relevante(item):
        link, titulo, cuerpo = item
        texto = normalizar_texto(titulo + ' ' + cuerpo)
        return url_coincide_con_nombre(lead, link) or nombre_en_bloque(lead, texto) or calle_confirmada(lead, texto)
    resultados_urls = [x for x in resultados_urls if es_relevante(x)]
    resultados_urls.sort(key=puntuacion, reverse=True)
    urls_para_abrir = [x[0] for x in resultados_urls[:8]]

    # En los sitios que parecen del comercio, probar también las páginas de contacto habituales.
    extras_contacto = []
    for link in urls_para_abrir:
        p = urlparse(link)
        if not any(d in p.netloc.lower() for d in DOMINIOS_SOCIALES) and url_coincide_con_nombre(lead, link):
            raiz = f"{p.scheme}://{p.netloc}"
            for ruta in ('/contacto', '/contact', '/contactanos', '/nosotros'):
                extras_contacto.append(raiz + ruta)

    # Descargar páginas halladas y descubrir páginas internas de contacto.
    pendientes = list(dict.fromkeys(urls_para_abrir + extras_contacto))
    visitadas = set()
    while pendientes and len(visitadas) < 16:
        url = pendientes.pop(0)
        if url in visitadas:
            continue
        visitadas.add(url)
        print(f"-> Leyendo página para encontrar email/contacto: {url}", flush=True)
        contenido = extraer_texto_de_url(url)
        if contenido:
            reporte.append(f"\n=== FUENTE INDIVIDUAL ===\nURL: {url}\n{contenido}")
            for enlace in re.findall(r'https?://[^\s|\]]+', contenido):
                if any(k in enlace.lower() for k in ('contact', 'contacto', 'nosotros', 'about', 'empresa')):
                    if url_coincide_con_nombre(lead, url) and enlace not in visitadas and enlace not in pendientes and urlparse(enlace).netloc == urlparse(url).netloc:
                        pendientes.insert(0, enlace)
        time.sleep(0.7)

    evidencia = "\n\n".join(reporte).strip()
    if not evidencia:
        if reintento:
            # Casi siempre es un bloqueo momentáneo del buscador: se espera y se intenta una vez más (versión corta).
            print("Sin resultados; se espera 30 segundos y se reintenta la búsqueda.", flush=True)
            time.sleep(30)
            return investigacion_profunda_con_google_search(lead, reintento=False)
        raise BusquedaSinEvidencia("DuckDuckGo no entregó resultados ni páginas verificables.")
    return evidencia


def analizar_lead_con_google_search_y_ia(lead, info_duckduckgo):
    nombre_original = lead.get('partner_name') or lead.get('name') or ''
    nombre_corto = limpiar_nombre_busqueda(nombre_original)
    direccion = lead.get('street') or 'No especificada'
    ciudad = lead.get('city') or 'Córdoba'
    email_actual = lead.get('email_from') or 'VACÍO'
    telefono_actual = lead.get('phone') or 'VACÍO'
    tipo_lead = lead.get('_etiquetas') or 'No especificado'

    prompt = f"""
Eres un investigador exhaustivo de comercios e instituciones de Argentina. Tu prioridad número uno es encontrar y verificar el EMAIL público de este lead. No te conformes con el primer resultado ni abandones la búsqueda de email porque hayas encontrado un teléfono.

DATOS EN ODOO:
- Nombre: {nombre_original} (nombre corto: {nombre_corto})
- Tipo de lead (etiqueta): {tipo_lead}  (COMERCIO = negocio; GENERADOR = institución o empresa generadora; el rubro debe ser compatible con esa actividad)
- Dirección de la sucursal: {direccion}
- Ciudad: {ciudad}
- Email actual: {email_actual}
- Teléfono actual: {telefono_actual}

EVIDENCIA REUNIDA (resultados de búsqueda, HTML de webs y páginas internas):
{info_duckduckgo}

REGLAS:
1. EMAIL: revisa todos los resultados y especialmente las secciones [EMAILS PUBLICADOS EN EL HTML], páginas de Contacto, Nosotros, pie de página y perfiles públicos de Facebook/Instagram. Si aparece un email que pertenece inequívocamente a este lead, devuelve email_elegido exactamente como está escrito y email_verificado=TRUE. Los emails de Gmail, Hotmail, Outlook o Yahoo son válidos si están publicados en la web o perfil del propio lead, o junto a su nombre y dirección. No uses correos técnicos, de plataformas, de directorios, de terceros ni de negocios homónimos.
2. No marques email como no encontrado si la evidencia contiene un correo público claramente vinculado al lead. El email es prioritario y debe buscarse aunque ya exista teléfono.
3. TELÉFONO: puede verificarse como alternativa si no se encuentra email. Solo usa un teléfono publicado y vinculado a la misma sucursal.
4. IDENTIDAD: el nombre y la sucursal deben corresponder a este lead. No confundas sucursales, homónimos, marcas o localidades.
5. UBICACIÓN: solo Córdoba Capital, Córdoba, Argentina. No infieras la ubicación solo por el nombre de la ciudad.
6. RUBRO: debe ser compatible con el lead de Odoo.
7. Nunca inventes ni completes datos por intuición. Usa únicamente la evidencia incluida y copia los datos exactamente.
8. coincide_ubicacion_y_rubro debe ser TRUE solo si la identidad y el rubro están respaldados por evidencia concreta. datos_validos_y_verificados debe ser TRUE solo si hay un email o teléfono verificado.
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


# Colores de la tarjeta en Odoo (sin color = todavía sin revisar)
COLOR_PENDIENTE = 2       # naranja: revisado, falta confirmar
COLOR_FUERA_ALCANCE = 1   # rojo: fuera de Córdoba Capital
COLOR_APROBADO = 10       # verde: revisado y chequeado, con email
COLOR_SOLO_WHATSAPP = 3   # amarillo: chequeado pero sin email (solo WhatsApp)


def marcar_color(models, uid, lead_id, color):
    try:
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead_id], {'color': color}])
    except Exception as exc:
        print(f"No se pudo marcar el color del lead {lead_id}: {exc}", flush=True)


def registrar_nota(models, uid, lead_id, nota):
    models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead_id]], {'body': nota})


def ejecutar_agente():
    print("Conectando a Odoo...", flush=True)
    uid, models = conectar_odoo()

    # SOLO se leen leads ya existentes en la etapa Prospectos por Controlar
    # (COMERCIO y GENERADOR conviven en esa etapa). No se crean contactos, leads
    # ni oportunidades nuevas, y las etiquetas nunca se modifican.
    id_prospectos = 5
    campos = ['id', 'name', 'partner_name', 'contact_name', 'email_from', 'phone', 'website', 'street', 'city', 'state_id', 'zip', 'country_id', 'tag_ids', 'stage_id', 'color']

    # Trae los leads de etapa 5 y los amarillos de Chequeado; luego se filtran por antigüedad de auditoría.
    dominio = [
        '|', ['stage_id', '=', id_prospectos],
        '&', ['stage_id', '=', ID_ETAPA_CHEQUEADO], ['color', '=', COLOR_SOLO_WHATSAPP]
    ]
    try:
        todos_los_leads = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
            [dominio],
            {'fields': campos, 'limit': 10000, 'order': 'id asc'}
        )
    except Exception as exc:
        # Si esta versión de Odoo no tiene el campo de color, se revisa solo la etapa 5.
        print(f"No se pudo usar el color en la consulta ({exc}); se revisa solo la etapa {id_prospectos}.", flush=True)
        todos_los_leads = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
            [[['stage_id', '=', id_prospectos]]],
            {'fields': [c for c in campos if c != 'color'], 'limit': 10000, 'order': 'id asc'}
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

    ahora = datetime.now(timezone.utc)
    limite_pendientes = (ahora - timedelta(days=DIAS_REINTENTO_PENDIENTES)).strftime('%Y-%m-%d %H:%M:%S')
    limite_amarillos = (ahora - timedelta(days=DIAS_REINTENTO_SIN_EMAIL)).strftime('%Y-%m-%d %H:%M:%S')

    def _etapa_id(l):
        st = l.get('stage_id')
        return st[0] if isinstance(st, (list, tuple)) and st else st

    total_etapa_5 = sum(1 for l in todos_los_leads if _etapa_id(l) == id_prospectos)
    total_amarillos = len(todos_los_leads) - total_etapa_5

    # Un lead se revisa solo si nunca fue auditado o si pasó el tiempo de espera de su grupo.
    # Esto evita volver a procesar en cada corrida los mismos leads que ya se revisaron hoy.
    def _toca_revisar(l):
        ultima = ultima_auditoria.get(l['id'], '')
        limite = limite_pendientes if _etapa_id(l) == id_prospectos else limite_amarillos
        return ultima < limite

    todos_los_leads = [l for l in todos_los_leads if _toca_revisar(l)]

    # Sin auditoría previa primero; luego los auditados hace más tiempo.
    todos_los_leads.sort(
        key=lambda lead: (
            ultima_auditoria.get(lead['id'], ''),
            lead['id']
        )
    )

    leads = todos_los_leads if not MAX_LEADS_POR_EJECUCION else todos_los_leads[:MAX_LEADS_POR_EJECUCION]

    # Nombres de etiquetas (COMERCIO / GENERADOR): solo se leen, nunca se escriben.
    ids_tags = sorted({t for l in leads for t in (l.get('tag_ids') or [])})
    nombres_tags = {}
    if ids_tags:
        try:
            for tag in models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.tag', 'read', [ids_tags], {'fields': ['name']}):
                nombres_tags[tag['id']] = tag['name']
        except Exception as exc:
            print(f"No se pudieron leer las etiquetas: {exc}", flush=True)
    for lead in leads:
        lead['_etiquetas'] = ' / '.join(nombres_tags.get(t, '') for t in (lead.get('tag_ids') or []) if nombres_tags.get(t))

    sin_auditoria_previa = sum(1 for lead in todos_los_leads if lead['id'] not in ultima_auditoria)
    print(
        f"Etapa {id_prospectos}: {total_etapa_5} leads | Amarillos en Chequeado: {total_amarillos} | "
        f"Les toca revisión: {len(todos_los_leads)} (nunca auditados: {sin_auditoria_previa}) | "
        f"Lote de esta corrida: {len(leads)} | Tiempo máximo: {TIEMPO_MAXIMO_SEGUNDOS // 60} min | "
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
    fuera_alcance = 0
    sin_email = 0
    sin_buscador_seguidos = 0
    motivo_corte = ''

    for lead in leads:
        if time.time() - INICIO_EJECUCION > TIEMPO_MAXIMO_SEGUNDOS:
            motivo_corte = "TIEMPO MÁXIMO ALCANZADO. Se corta limpio; los leads restantes se toman en la próxima corrida."
            print(f"\n{motivo_corte}", flush=True)
            break

        print(f"\n--- Auditando Lead ID {lead['id']}: {lead['name']} [{lead.get('_etiquetas') or 'sin etiqueta'}] (Dir: {lead.get('street') or 'S/D'}) ---", flush=True)
        try:
            alcance = clasificar_alcance_geografico(lead)

            if alcance == 'FUERA_DE_ALCANCE':
                provincia = lead.get('state_id')
                pais = lead.get('country_id')
                nota = (
                    "Agente de Verificación: FUERA DE ALCANCE. La dirección cargada en Odoo "
                    f"no corresponde a Córdoba Capital (ciudad: {lead.get('city') or 'sin ciudad'}, "
                    f"provincia: {provincia[1] if isinstance(provincia, (list, tuple)) and len(provincia) > 1 else 'sin provincia'}, "
                    f"país: {pais[1] if isinstance(pais, (list, tuple)) and len(pais) > 1 else 'sin país'}). "
                    "No se realizó búsqueda ni se modificaron datos."
                )
                registrar_nota(models, uid, lead['id'], nota)
                marcar_color(models, uid, lead['id'], COLOR_FUERA_ALCANCE)
                procesados += 1
                fuera_alcance += 1
                print(nota, flush=True)
                continue

            # Si Georef no confirma la dirección exacta, igual se busca, pero en ese caso
            # cada fuente debe nombrar Córdoba para poder aprobar (ver evaluar_evidencia).
            info_ddgs = investigacion_profunda_con_google_search(lead)
            sin_buscador_seguidos = 0  # el buscador respondió: se reinicia el contador de bloqueo
            # Gemini solo se consulta si la búsqueda halló algún mail o teléfono que confirmar:
            # si no hay nada, no puede aportar y se ahorra cuota diaria.
            vacio = SimpleNamespace(email_elegido='', telefono_elegido='', coincide_ubicacion_y_rubro=False, motivo='')
            previa = evaluar_evidencia(lead, info_ddgs, vacio, alcance)
            if previa['email'] or previa['telefono']:
                resultado = analizar_lead_con_google_search_y_ia(lead, info_ddgs)
            else:
                resultado = vacio

        except BusquedaSinEvidencia as e:
            # El buscador no devolvió NADA para este lead: casi seguro está bloqueado.
            # No se escribe nota ni color (así el lead no se marca como auditado y no se
            # acumulan notas repetidas); simplemente se salta y se cuenta.
            fallas_busqueda += 1
            sin_buscador_seguidos += 1
            print(f"BUSQUEDA SIN RESULTADOS ({sin_buscador_seguidos}/{MAX_SIN_BUSCADOR_SEGUIDOS}). El lead no se modifica: {e}", flush=True)
            if sin_buscador_seguidos >= MAX_SIN_BUSCADOR_SEGUIDOS:
                motivo_corte = (
                    f"BUSCADOR BLOQUEADO: {MAX_SIN_BUSCADOR_SEGUIDOS} leads seguidos sin ningún resultado. "
                    "Se aborta la corrida para no perder horas; se reintenta en la próxima."
                )
                print(f"\n{motivo_corte}", flush=True)
                break
            continue
        except CuotaGeminiAgotada as e:
            motivo_corte = f"CUOTA/LÍMITE GEMINI ALCANZADO. Se detiene toda la ejecución para proteger los pendientes: {e}"
            print(motivo_corte, flush=True)
            break
        except ServicioGeminiTemporal as e:
            motivo_corte = f"GEMINI SIGUE NO DISPONIBLE DESPUÉS DE LOS REINTENTOS. Se detiene sin modificar este lead: {e}"
            print(motivo_corte, flush=True)
            break
        except Exception as e:
            nota_error = f"Agente de Verificación: PENDIENTE. Error durante la auditoría; no se modificaron los datos: {e}"
            registrar_nota(models, uid, lead['id'], nota_error)
            print(f"Error en lead {lead['id']}; queda pendiente y se continúa: {e}", flush=True)
            pendientes += 1
            continue

        evidencia = evaluar_evidencia(lead, info_ddgs, resultado, alcance)

        email_actual = (lead.get('email_from') or '').strip().lower()
        telefono_actual = limpiar_formato_celular(lead.get('phone'))

        email_hallado = evidencia['email']        # (email, url) o None
        telefono_hallado = evidencia['telefono']  # (telefono, url) o None

        # Un dato solo cuenta si coincide con el que ya está en Odoo, si Odoo lo tiene vacío
        # o si sale de la web propia del comercio. Si no, no se pisa nada y no se aprueba por él.
        email_confirmado = None
        telefono_confirmado = None
        sin_aplicar = []
        if email_hallado:
            if email_hallado[0] == email_actual or not email_actual or evidencia['email_oficial']:
                email_confirmado = email_hallado
            else:
                sin_aplicar.append(f"email {email_hallado[0]} (visto en {email_hallado[1] or 'búsqueda'})")
        mismo_numero = False
        if telefono_hallado:
            mismo_numero = re.sub(r'\D', '', telefono_hallado[0])[-8:] == re.sub(r'\D', '', telefono_actual or '')[-8:]
            if mismo_numero or not telefono_actual or evidencia['tel_oficial']:
                telefono_confirmado = telefono_hallado
            else:
                sin_aplicar.append(f"teléfono {telefono_hallado[0]} (visto en {telefono_hallado[1] or 'búsqueda'})")
        contacto_verificado = bool(email_confirmado or telefono_confirmado)

        aprobado_final = bool(
            alcance in ('CORDOBA_CAPITAL', 'UBICACION_INCIERTA') and
            resultado.coincide_ubicacion_y_rubro and
            evidencia['nombre_ok'] and
            contacto_verificado
        )

        # Sin email pero con WhatsApp de Córdoba ya cargado: pasa a Chequeado en amarillo.
        # El agente vuelve a buscar su email cada 7 días.
        # OJO: ese número NO fue verificado en ninguna página; solo se mira su formato.
        # Se controla con PASAR_SOLO_WHATSAPP_SIN_VERIFICAR (arriba).
        solo_whatsapp = bool(
            PASAR_SOLO_WHATSAPP_SIN_VERIFICAR and
            not aprobado_final and
            alcance in ('CORDOBA_CAPITAL', 'UBICACION_INCIERTA') and
            re.match(r'^\+549351\d{7}$', telefono_actual or '')
        )

        partes_nota = []
        extra_nota = []
        valores_actualizar = {}

        if email_confirmado and email_confirmado[0] != email_actual:
            valores_actualizar['email_from'] = email_confirmado[0]
            if email_actual:
                extra_nota.append(f"El email anterior era {email_actual}.")
        if telefono_confirmado and not mismo_numero:
            valores_actualizar['phone'] = telefono_confirmado[0]
            if telefono_actual:
                extra_nota.append(f"El teléfono anterior era {telefono_actual}.")
        # Solo se actualizan datos de contacto y la etapa; nunca la web ni las etiquetas.
        if aprobado_final or solo_whatsapp:
            valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO

        if aprobado_final:
            partes_nota.append("APROBADO.")
        elif solo_whatsapp:
            partes_nota.append(f"APROBADO SIN EMAIL. No se encontró un email público; pasa a Chequeado solo con el WhatsApp ya cargado ({telefono_actual}), que NO pudo verificarse en una página. Se volverá a buscar el email.")
        else:
            partes_nota.append("PENDIENTE.")
        if email_confirmado:
            partes_nota.append(f"Email confirmado: {email_confirmado[0]} (fuente: {email_confirmado[1] or 'resultado de búsqueda'}).")
            if evidencia['otros_emails']:
                partes_nota.append("Otros emails también publicados por el comercio: " + ", ".join(evidencia['otros_emails']) + ".")
        if telefono_confirmado:
            partes_nota.append(f"Teléfono confirmado: {telefono_confirmado[0]} (fuente: {telefono_confirmado[1] or 'resultado de búsqueda'}).")
        partes_nota.extend(extra_nota)
        if sin_aplicar:
            partes_nota.append("NO se cambió porque no salió de la web propia del comercio y Odoo ya tenía otro dato: " + "; ".join(sin_aplicar) + ".")
        if not aprobado_final and not solo_whatsapp:
            faltantes = []
            if not contacto_verificado:
                faltantes.append("no se confirmó email ni teléfono en una página que pertenezca claramente al comercio")
            if not evidencia['nombre_ok']:
                faltantes.append("el nombre no apareció junto a la calle, el teléfono cargado o una web propia")
            if contacto_verificado and not resultado.coincide_ubicacion_y_rubro:
                faltantes.append("no coincide ubicación o rubro")
            partes_nota.append("No pasa a Chequeado: " + ", ".join(faltantes or ["validación insuficiente"]) + ".")
            if evidencia['candidatos']:
                lista = "; ".join(f"{e} (visto en {u or 'resultado de búsqueda'})" for e, u in evidencia['candidatos'])
                partes_nota.append(f"Emails vistos sin confirmar, para revisar a mano: {lista}.")
        if alcance == 'UBICACION_INCIERTA':
            partes_nota.append("Georef no confirmó la dirección exacta; se exigió que cada fuente nombrara Córdoba o la calle y número.")

        print(
            f"Resultado: Aprobado={aprobado_final} | Email={email_confirmado} | Teléfono={telefono_confirmado}",
            flush=True
        )

        if valores_actualizar:
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])

        registrar_nota(models, uid, lead['id'], "Agente de Verificación: " + " ".join(partes_nota))
        marcar_color(models, uid, lead['id'], COLOR_APROBADO if aprobado_final else (COLOR_SOLO_WHATSAPP if solo_whatsapp else COLOR_PENDIENTE))

        procesados += 1
        if aprobado_final:
            aprobados += 1
        elif solo_whatsapp:
            sin_email += 1
        else:
            pendientes += 1

    minutos = int((time.time() - INICIO_EJECUCION) / 60)
    print(
        f"\nRESUMEN ({minutos} min): lote={len(leads)} | auditados={procesados} | aprobados_con_contacto_verificado={aprobados} "
        f"| aprobados_solo_whatsapp_sin_verificar={sin_email} | pendientes_auditados={pendientes} | fuera_alcance={fuera_alcance} "
        f"| sin_resultados_del_buscador={fallas_busqueda} | sin_tocar={len(leads) - procesados - fallas_busqueda}",
        flush=True
    )
    if motivo_corte:
        print(f"CORTE: {motivo_corte}", flush=True)


if __name__ == "__main__":
    ejecutar_agente()
