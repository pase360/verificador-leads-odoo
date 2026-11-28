import os
import re
import time
import unicodedata
from types import SimpleNamespace
import xmlrpc.client
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin
from ddgs import DDGS
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import List

# Configuración desde los Secretos de GitHub
ODOO_URL = os.environ["ODOO_URL"]
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

ID_ETAPA_PROSPECTOS = 5  # Prospectos por Controlar: única etapa que se revisa
ID_ETAPA_CHEQUEADO = 7   # Chequeado: solo reciben leads VERDES (verificados)
ID_ETAPA_REVISAR = 11    # Revisar: revisión manual
MAX_LEADS_POR_EJECUCION = None  # None = todos los que haya en Prospectos; el límite real es el tiempo máximo de abajo.
SOLO_IDS = []  # Para probar: IDs de leads que estén en Prospectos, ej. [1512, 1130, 1518, 665]. Vacío = todos.
# True = un lead cargado desde Google Maps, con celular de Córdoba (351) y dirección confirmada en Córdoba Capital, pasa a
#        Chequeado en CELESTE aunque no haya una segunda fuente en internet (la mayoría de los comercios chicos no la tienen).
#        Sigue yendo a Revisar si cerró, si la ubicación no es clara o si el celular no es de Córdoba.
# False = solo pasa a Chequeado lo que se confirma en internet (verde); el resto va a Revisar.
ACEPTAR_DATO_DE_GOOGLE_MAPS = True
# Si el buscador no devuelve NADA para un mismo lead en tantas corridas, se decide con los datos de Odoo (para no dejarlo en Prospectos).
INTENTOS_SIN_RESULTADOS_ANTES_DE_DECIDIR = 2
INTERVALO_GEMINI_SEGUNDOS = 4.2  # Separación mínima entre llamadas a Gemini (ya no se suma al resto del trabajo)
MODELO_GEMINI = "gemini-3.5-flash-lite"

# --- Protecciones para que la corrida nunca llegue al límite de 6 horas de GitHub ---
TIEMPO_MAXIMO_SEGUNDOS = int(5.25 * 3600)  # Corta limpio a las 5 h 15 min; lo que falte se toma en la próxima corrida
MAX_SIN_BUSCADOR_SEGUIDOS = 5              # Si 5 leads seguidos no reciben NINGÚN resultado, el buscador está bloqueado: se aborta
HORAS_REINTENTO_PENDIENTES = 0             # Un lead que sigue en Prospectos (por error) se vuelve a revisar en la próxima corrida

# --- Velocidad ---
MAX_HILOS_PAGINAS = 8              # Páginas web que se descargan al mismo tiempo
MAX_PAGINAS_PRIMERA_TANDA = 18     # Páginas de la primera descarga paralela
MAX_PAGINAS_SEGUNDA_TANDA = 6      # Páginas internas de contacto descubiertas en la primera tanda
PAUSA_ENTRE_BUSQUEDAS = 0.7        # Antes 2 s
PAUSA_ENTRE_BACKENDS = 0.5         # Antes 1.5 s
ESPERA_REINTENTO_BUSCADOR = 20     # Antes 30 s

# True = un email/teléfono solo se confirma si sale de la web propia del comercio (o su perfil de Facebook/Instagram)
#        o si aparece en al menos 2 sitios distintos que nombran al comercio. Lo visto en un solo directorio NO alcanza.
# False = cualquier fuente que nombre al comercio alcanza (más leads verdes, menos seguridad).
EXIGIR_FUENTE_PROPIA_O_DOBLE = True

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
DOMINIOS_SOCIALES = ('facebook.com', 'instagram.com', 'linktr.ee')
EMAILS_BASURA = (
    'sentry.io', 'wixpress.com', 'example.com', 'godaddy', 'squarespace',
    'wordpress.com', 'domain.com', 'tuemail', 'tucorreo', 'tumail',
    'usuario@', 'nombre@', 'ejemplo', 'email@email'
)
# Webs y correos de otros países (ej. una librería de España con el mismo nombre) nunca son del comercio.
TLD_EXTRANJEROS = ('.es', '.mx', '.cl', '.uy', '.py', '.pe', '.co', '.br', '.it', '.au', '.fr', '.de',
                   '.uk', '.pt', '.ec', '.bo', '.ve', '.cr', '.pa', '.cu', '.do', '.gt', '.hn', '.ni', '.sv')


def dominio_extranjero(host):
    host = (host or '').lower().strip('/')
    return any(host.endswith(t) for t in TLD_EXTRANJEROS)


def con_prefijo_mas(telefono):
    solo = re.sub(r'\D', '', telefono or '')
    return ('+' + solo) if solo.startswith('54') else telefono


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


class SitiosOficiales(BaseModel):
    urls: List[str]


class BusquedaSinEvidencia(Exception):
    pass


class CuotaGeminiAgotada(Exception):
    pass


class ServicioGeminiTemporal(Exception):
    pass


_ULTIMA_LLAMADA_GEMINI = 0.0


def generar_con_gemini(**kwargs):
    """Llama a Gemini respetando una separación mínima entre llamadas (no una pausa fija por llamada)."""
    global _ULTIMA_LLAMADA_GEMINI
    ultimo_error = None
    for intento in range(3):
        faltante = INTERVALO_GEMINI_SEGUNDOS - (time.time() - _ULTIMA_LLAMADA_GEMINI)
        if faltante > 0:
            time.sleep(faltante)
        _ULTIMA_LLAMADA_GEMINI = time.time()
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


def localidad_tras_cp(direccion):
    """Localidad que acompaña al CP en su tramo de la dirección: 'X5885 Villa de Las Rosas' -> 'villa de las rosas'."""
    for parte in str(direccion or '').split(','):
        if not re.search(r'(?<![A-Z0-9])[A-Z]\d{4}(?:[A-Z]{3})?(?![A-Z0-9])', parte.upper()):
            continue
        resto = re.sub(r'(?<![A-Za-z0-9])[A-Za-z]\d{4}(?:[A-Za-z]{3})?(?![A-Za-z0-9])', ' ', parte)
        resto = re.sub(r'\b[A-Z]{2,3}\b', ' ', resto)   # sufijos de CPA sueltos ("X5000 BVD")
        return normalizar_texto(resto)
    return ''


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
        # La primera letra del CPA identifica la provincia: X es Córdoba, cualquier otra letra es otra provincia.
        if codigo_postal[0] != 'X':
            return 'FUERA_DE_ALCANCE'
        numero_cp = int(codigo_postal[1:5])
        if numero_cp // 1000 == 5:
            if 5000 <= numero_cp <= 5023 or numero_cp in (5147, 5149):
                # CP de Córdoba Capital informado en la dirección: alcanza para ubicar el lead sin consultar Georef.
                print(f"Validación geográfica: CP={codigo_postal} corresponde a Córdoba Capital.", flush=True)
                return 'CORDOBA_CAPITAL'
            # X5xxx fuera del rango de Capital: si la localidad escrita junto al CP no es Córdoba, está fuera de alcance.
            localidad = localidad_tras_cp(lead.get('street'))
            if localidad and not any(t in ('cordoba', 'capital') for t in localidad.split()):
                print(f"Validación geográfica: CP={codigo_postal} y localidad '{localidad}': fuera de Córdoba Capital.", flush=True)
                return 'FUERA_DE_ALCANCE'
            print(f"Ubicación pendiente: el CP {codigo_postal} no corresponde a Córdoba Capital; se exige evidencia explícita.", flush=True)
            return 'UBICACION_INCIERTA'
        print(f"CP {codigo_postal} con formato dudoso (X pero no X5xxx): se intenta confirmar la dirección con Georef.", flush=True)

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
    partes = [p.strip() for p in re.split(r'\s[–—-]\s|\s\|\s|\s::\s|\s/\s', nombre, maxsplit=1)]
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
    if dominio_extranjero(dominio):
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


RUBROS_NORMALIZADOS = {
    'dietetica', 'verduleria', 'ferreteria', 'floreria', 'farmacia', 'cotillon', 'panaderia', 'carniceria',
    'heladeria', 'pintureria', 'peluqueria', 'kiosco', 'autolavadero', 'tienda', 'almacen', 'lavadero',
    'jugueteria', 'optica', 'libreria', 'bazar', 'rotiseria', 'pasteleria', 'distribuidora', 'mayorista'
}


def texto_coincide_con_nombre(lead, texto_compacto):
    _, corto = nombres_del_lead(lead)
    junto = corto.replace(' ', '')
    if len(junto) >= 5 and junto in texto_compacto:
        return True
    tokens = [t for t in corto.split() if len(t) >= 3 and t not in PALABRAS_NOMBRE_GENERICAS]
    if len(tokens) >= 2 and all(t in texto_compacto for t in tokens):
        return True
    # Perfiles y webs suelen omitir el rubro ("trigatti" en vez de "panaderia trigatti"): se compara con
    # las palabras distintivas del nombre (sin rubro ni palabras vacías), que sumen al menos 5 letras.
    distintivos = [t for t in corto.split()
                   if len(t) >= 3 and t not in PALABRAS_NOMBRE_GENERICAS
                   and t not in RUBROS_NORMALIZADOS and t not in PALABRAS_VACIAS_NOMBRE]
    if distintivos and sum(len(t) for t in distintivos) >= 5 and all(t in texto_compacto for t in distintivos):
        return True
    # Nombres largos: las webs suelen acortarlos ("Brosas Frescas de Ternera" -> brosasfrescas.com.ar).
    # Con 3 o más palabras distintivas alcanza con las dos primeras juntas (mínimo 9 letras).
    if len(distintivos) >= 3:
        inicio = distintivos[0] + distintivos[1]
        if len(inicio) >= 9 and inicio in texto_compacto:
            return True
    return False


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
    if not es_red and not dominio_extranjero(host) and url_coincide_con_nombre(lead, url) and (tiene_calle or tiene_telefono or 'cordoba' in bloque_n):
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


def es_celular_argentino(formateado):
    """Detecta móvil argentino inequívoco (prefijo 549 o formato nacional con 15).
    No confunde fijos de Córdoba (351) con móviles. No confirma cuenta WhatsApp activa.
    """
    d = re.sub(r'\D', '', str(formateado or ''))
    if d.startswith('549') and len(d) == 13:
        return True
    if d.startswith('0') and len(d) == 13 and d[4:6] == '15':
        return True
    if len(d) == 11 and d[3:5] == '15':
        return True
    return False


def es_telefono_cordoba(formateado):
    """SOLO CELULARES: móvil argentino con característica 351 (Córdoba). Los fijos se descartan."""
    d = re.sub(r'\D', '', str(formateado or ''))
    return es_celular_argentino(d) and d.startswith('549351')


RE_WHATSAPP_ANTES = re.compile(r'(?:whats\s?app|wsp|wpp)\W{0,6}$', re.I)
RE_WHATSAPP_DESPUES = re.compile(r'^[\s*(\-–:]*(?:solo\s+|por\s+)?(?:whats\s?app|wsp|wpp)', re.I)


def _rotulado_whatsapp(antes, despues):
    """True si el número está pegado a la palabra WhatsApp (antes o inmediatamente después)."""
    return bool(RE_WHATSAPP_ANTES.search(antes[-20:]) or RE_WHATSAPP_DESPUES.match(despues[:25]))


PALABRAS_TELEFONO = ('tel', 'cel', 'whats', 'wsp', 'wpp', 'contact', 'llam', 'consult', 'ventas', 'pedidos')


def telefonos_en_texto(bloque):
    """Celulares de Córdoba publicados en la página, con su posición en el texto visible."""
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
            # Un número de Córdoba escrito sin 9 ni 15 pero rotulado "WhatsApp" es móvil (WhatsApp usa líneas móviles).
            if formateado and re.fullmatch(r'\+54351\d{7}', formateado) and _rotulado_whatsapp(antes, visible[m.end():m.end() + 25]):
                formateado = '+549' + formateado[3:]
            if formateado and es_telefono_cordoba(formateado):
                encontrados.append((formateado, m.start()))
    for grupo in re.findall(r'WHATSAPP PUBLICADO:\s*([0-9,\s]+)', bloque):
        for n in grupo.split(','):
            solo = re.sub(r'\D', '', n)
            if len(solo) >= 10:
                # Un enlace de WhatsApp solo funciona con líneas móviles: si viene como 54351xxxxxxx
                # (sin el 9), se lo normaliza al formato móvil 549351xxxxxxx.
                if solo.startswith('54351') and len(solo) == 12:
                    solo = '549' + solo[2:]
                fmt = '+' + solo if solo.startswith('54') else limpiar_formato_celular(solo)
                if es_telefono_cordoba(fmt):
                    encontrados.append((fmt, 10 ** 8))
    return encontrados


def es_fuente_propia(lead, url, dominios_propios):
    """True si la URL es la web propia del comercio (ya verificada) o su perfil de Facebook/Instagram."""
    if not url:
        return False
    host = urlparse(url).netloc.lower().replace('www.', '')
    if host in dominios_propios:
        return True
    if any(d in host for d in DOMINIOS_SOCIALES):
        return url_coincide_con_nombre(lead, url)
    return False


def evaluar_evidencia(lead, evidencia, resultado, alcance):
    """
    Revisa toda la evidencia y devuelve:
      email / telefono: (valor, url) o None -> dato confirmado, listo para guardar
      email_oficial / tel_oficial: True si salió de la web propia del comercio
      otros_emails: otros mails publicados por el comercio
      nombre_ok: bool
      tel_cargado: URL que respalda el teléfono ya cargado en Odoo (o None)
      candidatos / tel_debiles: datos vistos pero SIN respaldo suficiente (solo para la nota)

    La web propia se identifica a nivel de SITIO de dos maneras:
      a) el dominio coincide con el nombre del comercio, o
      b) Gemini la eligió entre los resultados de búsqueda (como haría una persona) Y alguna página
         del sitio confirma la calle y altura o el teléfono cargado en Odoo.
    Cuando un sitio es propio, TODAS sus páginas (contacto, nosotros, etc.) sirven.
    Los teléfonos solo se aceptan si son CELULARES de Córdoba.
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
    bloques = separar_bloques(evidencia)
    sitios_elegidos = set(re.findall(r'SITIO OFICIAL ELEGIDO:\s*(\S+)', evidencia))

    # Paso 1: sitios propios (a nivel de dominio completo).
    dominios_propios = set()
    for bloque in bloques:
        url = url_del_bloque(bloque)
        if not url:
            continue
        host = urlparse(url).netloc.lower().replace('www.', '')
        if any(d in host for d in DOMINIOS_SOCIALES) or dominio_extranjero(host):
            continue
        nombre_en_url = url_coincide_con_nombre(lead, url)
        if not nombre_en_url and host not in sitios_elegidos:
            continue
        bloque_n = normalizar_texto(bloque)
        digitos = re.sub(r'\D', '', bloque)
        confirma_dato = calle_confirmada(lead, bloque_n) or (telefono_lead and telefono_lead in digitos)
        if confirma_dato or (nombre_en_url and 'cordoba' in bloque_n):
            dominios_propios.add(host)

    confirmados = {}   # email -> (tipo, rango, pos, orden, url)
    email_hosts = {}   # email -> sitios donde aparece
    email_propia = set()
    telefonos = {}     # ultimos 8 dígitos -> datos
    candidatos = {}    # email -> url (sin respaldo suficiente)
    nombre_ok = False
    tel_hosts = set()  # sitios donde el teléfono ya cargado aparece junto al comercio
    tel_propia = False
    tel_url = None
    orden = 0

    for bloque in bloques:
        bloque_n = normalizar_texto(bloque)
        url = url_del_bloque(bloque)
        host_bloque = urlparse(url).netloc.lower().replace('www.', '') if url else ''
        con_nombre = nombre_en_bloque(lead, bloque_n)
        tipo = tipo_de_fuente(lead, bloque, bloque_n, telefono_lead)
        if not tipo and host_bloque in dominios_propios:
            tipo = 'oficial'   # otra página del mismo sitio ya verificado
        emails_bloque = emails_en_texto(bloque)

        if not tipo:
            if con_nombre:
                for e in emails_bloque:
                    candidatos.setdefault(e, url)
            continue
        if exigir_cordoba and host_bloque not in dominios_propios and 'cordoba' not in bloque_n and not calle_confirmada(lead, bloque_n):
            continue
        nombre_ok = True
        propia_bloque = es_fuente_propia(lead, url, dominios_propios)
        if telefono_lead and telefono_lead in re.sub(r'\D', '', bloque):
            tel_hosts.add(host_bloque or url)
            tel_url = tel_url or url or 'resultado de búsqueda'
            if propia_bloque:
                tel_propia = True
        elegible = tipo in prioridad   # solo web propia o nombre+calle confirman datos nuevos
        visible = bloque.split('Contenido visible:', 1)[-1].lower()

        for e in emails_bloque:
            email_hosts.setdefault(e, set()).add(host_bloque or url)
            if propia_bloque:
                email_propia.add(e)
            if e in confirmados:
                continue
            dominio = e.split('@')[-1]
            if dominio in DOMINIOS_CORREO_GENERICOS:
                # Gmail/Hotmail: puede ser de cualquiera, el nombre debe estar en la misma fuente.
                aceptado = con_nombre or host_bloque in dominios_propios
                rango = 10
            else:
                coincide = (
                    email_coincide_con_nombre(lead, e)
                    or bool(host_bloque and dominio in host_bloque)
                    or tipo == 'calle'
                )
                # Un correo publicado en la web propia del comercio es suyo aunque su dominio
                # no se parezca al nombre (ej. sitio "cobico.ar" con correo "cobico.com.ar").
                aceptado = coincide or host_bloque in dominios_propios
                rango = 0 if coincide else 3
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
                    existente = telefonos[clave] = {'tel': tel, 'tipo': tipo, 'pos': pos, 'orden': orden,
                                                    'url': url, 'hosts': set(), 'propia': False}
                elif tel.startswith('+549') and not existente['tel'].startswith('+549'):
                    existente['tel'] = tel
                existente['hosts'].add(host_bloque or url)
                if propia_bloque:
                    existente['propia'] = True

    def _firme(propia, hosts):
        return (not EXIGIR_FUENTE_PROPIA_O_DOBLE) or propia or len(hosts) >= 2

    # Email: solo los con respaldo suficiente. Si la web propia publica alguno, solo valen los de la web propia.
    email_final = None
    email_oficial = False
    otros_confirmados = []
    firmes = {}
    for e, v in confirmados.items():
        if _firme(e in email_propia, email_hosts.get(e, set())):
            firmes[e] = v
        else:
            candidatos.setdefault(e, v[4])
    if firmes:
        pool = list(firmes.items())
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
    tel_debiles = []
    todos_tel = list(telefonos.values())
    firmes_tel = [t for t in todos_tel if _firme(t['propia'], t['hosts'])]
    tel_debiles = [(t['tel'], t['url']) for t in todos_tel if t not in firmes_tel][:3]
    if firmes_tel:
        pool = firmes_tel
        if any(t['tipo'] == 'oficial' for t in pool):
            pool = [t for t in pool if t['tipo'] == 'oficial']
            tel_oficial = True
        pool.sort(key=lambda t: (
            prioridad[t['tipo']],
            0 if (telefono_lead and re.sub(r'\D', '', t['tel'])[-7:] == telefono_lead) else 1,
            t['pos'],
            0 if re.sub(r'\D', '', t['tel'])[-8:] == digitos_ia else 1, t['orden']
        ))
        telefono_final = (pool[0]['tel'], pool[0]['url'])

    # Teléfono ya cargado en Odoo: queda verificado solo con respaldo suficiente.
    tel_cargado = tel_url if (tel_url and _firme(tel_propia, tel_hosts)) else None
    tel_cargado_debil = tel_url if (tel_url and not tel_cargado) else None

    sin_confirmar = [(e, u) for e, u in candidatos.items() if e not in firmes][:3]
    return {
        'email': email_final,
        'email_oficial': email_oficial,
        'otros_emails': otros_confirmados,
        'telefono': telefono_final,
        'tel_oficial': tel_oficial,
        'nombre_ok': nombre_ok,
        'tel_cargado': tel_cargado,
        'tel_cargado_debil': tel_cargado_debil,
        'tel_debiles': tel_debiles,
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
        # (conexión, lectura): una web caída ya no frena la corrida 15 segundos.
        response = requests.get(url, headers=headers, timeout=(5, 10))
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


def leer_paginas_en_paralelo(urls):
    """Descarga varias páginas a la vez. Devuelve [(url, contenido)] en el mismo orden."""
    if not urls:
        return []
    print(f"-> Leyendo {len(urls)} páginas en paralelo para encontrar email/contacto", flush=True)
    with ThreadPoolExecutor(max_workers=MAX_HILOS_PAGINAS) as pool:
        contenidos = list(pool.map(extraer_texto_de_url, urls))
    return list(zip(urls, contenidos))


# Según la prueba hecha desde GitHub: DuckDuckGo directo está bloqueado, "auto" y Yandex responden,
# Google/Bing/Yahoo responden a medias. Se prueban en este orden hasta que uno devuelva resultados.
BACKENDS_BUSQUEDA = ["auto", "yandex", "google", "bing", "yahoo"]
_BACKEND_QUE_FUNCIONA = None  # Se recuerda el último que respondió para probarlo primero en la siguiente consulta


def ejecutar_consulta_ddgs(query, max_res=7):
    global _BACKEND_QUE_FUNCIONA
    orden = list(BACKENDS_BUSQUEDA)
    if _BACKEND_QUE_FUNCIONA in orden:
        orden.remove(_BACKEND_QUE_FUNCIONA)
        orden.insert(0, _BACKEND_QUE_FUNCIONA)
    for backend in orden:
        try:
            resultados = DDGS().text(query, region="ar-es", max_results=max_res, backend=backend)
            if resultados:
                _BACKEND_QUE_FUNCIONA = backend
                return resultados
        except Exception:
            pass
        time.sleep(PAUSA_ENTRE_BACKENDS)
    return []


def elegir_sitios_oficiales(lead, candidatos):
    """
    Hace lo que haría una persona: mira los resultados de la búsqueda del nombre y elige cuál es
    la web oficial del lead (aunque el dominio sea una sigla que no se parece al nombre).
    candidatos: lista de (url, titulo, texto). Devuelve una lista de URLs elegidas (máx. 3).
    """
    if not candidatos:
        return []
    nombre_original = lead.get('partner_name') or lead.get('name') or ''
    direccion = lead.get('street') or 'No especificada'
    listado = "\n".join(
        f"{i + 1}. URL: {u}\n   Título: {t}\n   Texto: {c}" for i, (u, t, c) in enumerate(candidatos[:15])
    )
    prompt = f"""
Buscaste en internet el nombre de este comercio o institución y tenés que decidir cuál es su SITIO WEB OFICIAL, como lo haría una persona mirando los resultados de Google.

LEAD:
- Nombre: {nombre_original}
- Dirección: {direccion}
- Ciudad: Córdoba Capital, Córdoba, Argentina

RESULTADOS:
{listado}

REGLAS:
- Elegí hasta 3 URLs de la lista que sean la web oficial del lead (o una página de esa web oficial).
- El dominio puede ser una sigla o un nombre distinto al del lead: lo importante es que el título o el texto muestren que es SU sitio.
- No elijas directorios, noticias, buscadores de empresas, ni sitios de otra ciudad o provincia con nombre parecido (por ejemplo una filial de otra provincia).
- Copiá las URLs exactamente como están en la lista. Si ninguna es la web oficial, devolvé una lista vacía.
"""
    try:
        response = generar_con_gemini(
            model=MODELO_GEMINI,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SitiosOficiales,
                temperature=0.0,
            ),
        )
        urls_validas = {u for u, _, _ in candidatos}
        elegidas = [u for u in (response.parsed.urls if response.parsed else []) if u in urls_validas]
        return elegidas[:3]
    except (CuotaGeminiAgotada, ServicioGeminiTemporal):
        raise
    except Exception as exc:
        print(f"No se pudo elegir el sitio oficial con Gemini: {exc}", flush=True)
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
        time.sleep(PAUSA_ENTRE_BUSQUEDAS)

    # Como lo haría una persona: de todos los resultados, elegir cuál es la web oficial del lead
    # (aunque su dominio sea una sigla, ej. "cobico.ar" para un colegio profesional).
    candidatos_oficiales = []
    hosts_vistos = set()
    for link, titulo, cuerpo in resultados_urls:
        host = urlparse(link).netloc.lower().replace('www.', '')
        if not host or host in hosts_vistos:
            continue
        if any(d in host for d in DOMINIOS_DIRECTORIO) or dominio_extranjero(host):
            continue
        hosts_vistos.add(host)
        candidatos_oficiales.append((link, titulo, cuerpo))

    # Velocidad: si algún resultado ya tiene un dominio que coincide con el nombre del lead, la web oficial
    # se detecta sola (ver evaluar_evidencia) y no hace falta gastar una llamada a Gemini.
    hay_dominio_coincidente = any(url_coincide_con_nombre(lead, u) for u, _, _ in candidatos_oficiales)
    elegidas = [] if hay_dominio_coincidente else elegir_sitios_oficiales(lead, candidatos_oficiales)
    # El sitio web que ya está cargado en Odoo se abre siempre. Sigue valiendo la regla de seguridad:
    # solo se vuelve "web propia" si alguna de sus páginas confirma la calle y altura o el teléfono cargado.
    sitio_odoo = (lead.get('website') or '').strip()
    if sitio_odoo and not sitio_odoo.lower().startswith('http'):
        sitio_odoo = 'http://' + sitio_odoo
    host_odoo = urlparse(sitio_odoo).netloc.lower().replace('www.', '') if sitio_odoo else ''
    if host_odoo and (any(d in host_odoo for d in DOMINIOS_DIRECTORIO) or any(d in host_odoo for d in DOMINIOS_SOCIALES) or dominio_extranjero(host_odoo)):
        sitio_odoo = ''
    if sitio_odoo and sitio_odoo not in elegidas:
        elegidas.insert(0, sitio_odoo)
        print(f"-> Sitio web cargado en Odoo: {sitio_odoo}", flush=True)
    hosts_elegidos = {urlparse(u).netloc.lower().replace('www.', '') for u in elegidas}
    if elegidas:
        print(f"-> Sitio oficial elegido: {', '.join(sorted(hosts_elegidos))}", flush=True)
        reporte.insert(0, "=== SITIOS OFICIALES ELEGIDOS ===\n" + "\n".join(f"SITIO OFICIAL ELEGIDO: {h}" for h in sorted(hosts_elegidos)))

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

    # Sitio oficial elegido: se abre la página elegida, la portada y las páginas de contacto habituales.
    urls_oficiales = []
    for link in elegidas:
        p = urlparse(link)
        raiz = f"{p.scheme}://{p.netloc}"
        urls_oficiales.append(link)
        if any(d in p.netloc.lower() for d in DOMINIOS_SOCIALES):
            continue
        for ruta in ('', '/contacto', '/contact', '/nosotros'):
            urls_oficiales.append(raiz + ruta)

    # En los sitios que parecen del comercio, probar también las páginas de contacto habituales.
    extras_contacto = []
    for link in urls_para_abrir:
        p = urlparse(link)
        if not any(d in p.netloc.lower() for d in DOMINIOS_SOCIALES) and url_coincide_con_nombre(lead, link):
            raiz = f"{p.scheme}://{p.netloc}"
            for ruta in ('/contacto', '/contact', '/nosotros'):
                extras_contacto.append(raiz + ruta)

    # Primera tanda: se descargan todas a la vez.
    primera_tanda = list(dict.fromkeys(urls_oficiales + urls_para_abrir + extras_contacto))[:MAX_PAGINAS_PRIMERA_TANDA]
    visitadas = set(primera_tanda)
    descubiertas = []
    for url, contenido in leer_paginas_en_paralelo(primera_tanda):
        if not contenido:
            continue
        reporte.append(f"\n=== FUENTE INDIVIDUAL ===\nURL: {url}\n{contenido}")
        host_url = urlparse(url).netloc.lower().replace('www.', '')
        es_sitio_del_lead = url_coincide_con_nombre(lead, url) or host_url in hosts_elegidos
        if not es_sitio_del_lead:
            continue
        for enlace in re.findall(r'https?://[^\s|\]]+', contenido):
            if any(k in enlace.lower() for k in ('contact', 'contacto', 'nosotros', 'about', 'empresa')):
                if enlace not in visitadas and enlace not in descubiertas and urlparse(enlace).netloc == urlparse(url).netloc:
                    descubiertas.append(enlace)

    # Segunda tanda: páginas internas de contacto descubiertas dentro de los sitios del lead, también en paralelo.
    segunda_tanda = descubiertas[:MAX_PAGINAS_SEGUNDA_TANDA]
    for url, contenido in leer_paginas_en_paralelo(segunda_tanda):
        if contenido:
            reporte.append(f"\n=== FUENTE INDIVIDUAL ===\nURL: {url}\n{contenido}")

    evidencia = "\n\n".join(reporte).strip()
    if not evidencia:
        if reintento:
            # Casi siempre es un bloqueo momentáneo del buscador: se espera y se intenta una vez más (versión corta).
            print(f"Sin resultados; se espera {ESPERA_REINTENTO_BUSCADOR} segundos y se reintenta la búsqueda.", flush=True)
            time.sleep(ESPERA_REINTENTO_BUSCADOR)
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
3. TELÉFONO: puede verificarse como alternativa si no se encuentra email. Solo se aceptan CELULARES (móviles) de Córdoba; ignora teléfonos fijos. Solo usa un teléfono publicado y vinculado a la misma sucursal.
4. IDENTIDAD: el nombre y la sucursal deben corresponder a este lead. No confundas sucursales, homónimos, marcas o localidades. El sitio web oficial puede tener un dominio que es una sigla y no se parece al nombre: si una página muestra el nombre (aunque sea con palabras de más o de menos, como "de la Provincia de Córdoba") y la misma dirección, es del lead.
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
COLOR_PENDIENTE = 2       # naranja: no se pudo verificar (pasa a Revisar)
COLOR_FUERA_ALCANCE = 1   # rojo: fuera de Córdoba Capital (pasa a Revisar)
COLOR_APROBADO = 10       # verde: verificado con evidencia (pasa a Chequeado)
COLOR_DATO_GOOGLE_MAPS = 4  # celeste: dato de Google Maps sin segunda fuente (pasa a Chequeado, confianza media)


def marcar_color(models, uid, lead_id, color):
    try:
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead_id], {'color': color}])
    except Exception as exc:
        print(f"No se pudo marcar el color del lead {lead_id}: {exc}", flush=True)


def registrar_nota(models, uid, lead_id, nota):
    models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post', [[lead_id]], {'body': nota})


def decidir_telefono(telefono_actual, evidencia):
    """
    Decide qué celular queda confirmado. Devuelve (telefono_confirmado, mismo_numero, sin_aplicar, otros_celulares).
      1) Si el celular que Odoo ya tiene aparece respaldado en una fuente firme (web propia o 2 sitios), SE CONSERVA,
         aunque la web publique otros celulares (esos se informan en la nota, pero no pisan el dato).
      2) Si no, un celular hallado reemplaza al de Odoo solo si Odoo no tiene celular, es el mismo número
         o sale de la web propia del comercio.
    """
    telefono_hallado = evidencia['telefono']
    ultimos8 = lambda t: re.sub(r'\D', '', t or '')[-8:]
    sin_aplicar, otros = [], []
    if telefono_actual and es_telefono_cordoba(telefono_actual) and evidencia['tel_cargado']:
        if telefono_hallado and ultimos8(telefono_hallado[0]) != ultimos8(telefono_actual):
            otros.append(telefono_hallado[0])
        return (telefono_actual, evidencia['tel_cargado']), True, sin_aplicar, otros
    if telefono_hallado:
        mismo_numero = ultimos8(telefono_hallado[0]) == ultimos8(telefono_actual)
        # Si Odoo tiene un FIJO cargado, un celular verificado lo reemplaza (el fijo no sirve para WhatsApp).
        odoo_tiene_celular = bool(telefono_actual) and es_celular_argentino(telefono_actual)
        if mismo_numero or not odoo_tiene_celular or evidencia['tel_oficial']:
            return telefono_hallado, mismo_numero, sin_aplicar, otros
        sin_aplicar.append(f"celular {telefono_hallado[0]} (visto en {telefono_hallado[1] or 'búsqueda'})")
    return None, False, sin_aplicar, otros


PATRON_COMERCIO_CERRADO = re.compile(
    r'no existe m[aá]s|otros due[ñn]os|cerrad[oa] (?:definitiva|permanente)mente|permanentemente cerrad|'
    r'cerr[oó] definitivamente|vendi[oó]|se mud[oó]|ya no (?:existe|funciona|atiende)|fuera de servicio', re.I)


def limpiar_html(texto):
    texto = re.sub(r'<[^>]+>', ' ', str(texto or ''))
    return re.sub(r'\s+', ' ', texto.replace('&nbsp;', ' ').replace('&amp;', '&')).strip()


def evaluar_dato_de_google_maps(lead, alcance):
    """
    Segundo nivel de aprobación (celeste). Devuelve (aprobado, motivo).
    Pasa si el lead se cargó desde Google Maps, la dirección está confirmada en Córdoba Capital, el teléfono cargado
    es un celular de Córdoba (351) y nada indica que el comercio cerró o cambió de dueño.
    No sirve para datos nuevos: solo valida lo que ya está cargado en Odoo.
    """
    descripcion = limpiar_html(lead.get('description'))
    if 'google maps' not in descripcion.lower():
        return False, 'el lead no figura como cargado desde Google Maps'
    if alcance != 'CORDOBA_CAPITAL':
        return False, 'la ubicación en Córdoba Capital no está confirmada'
    telefono = limpiar_formato_celular(lead.get('phone'))
    if not es_telefono_cordoba(telefono):
        return False, 'el teléfono cargado no es un celular de Córdoba (351)'
    texto = ' '.join([str(lead.get('partner_name') or ''), str(lead.get('name') or ''), str(lead.get('street') or ''), descripcion])
    if PATRON_COMERCIO_CERRADO.search(texto):
        return False, 'el nombre o la dirección indican que el comercio cerró o cambió de dueño'
    calidad = 'marcado como "probable"' if 'probable' in descripcion.lower() else 'confirmado por WhatsApp o por su formato de celular'
    return True, (
        f"El celular {telefono} ya estaba cargado en Odoo ({calidad}), tiene formato de celular de Córdoba y la dirección "
        "está en Córdoba Capital. No se halló nada que lo contradiga."
    )


def reclasificar_revisar(aplicar=False):
    """
    Revisa los leads que están en Revisar SIN buscar en internet: los que cumplen el segundo nivel (celeste) pasan a
    Chequeado. Con aplicar=False solo muestra qué haría (no cambia nada en Odoo).
    """
    print(f"Reclasificando los leads en Revisar | {'APLICANDO CAMBIOS' if aplicar else 'SIMULACIÓN: no se cambia nada'}", flush=True)
    uid, models = conectar_odoo()
    campos = ['id', 'name', 'partner_name', 'street', 'city', 'state_id', 'zip', 'country_id', 'phone', 'description']
    leads = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
                              [[['stage_id', '=', ID_ETAPA_REVISAR]]], {'fields': campos, 'limit': 10000, 'order': 'id asc'})
    print(f"Leads en Revisar: {len(leads)}", flush=True)
    pasan, quedan = [], []
    for lead in leads:
        alcance = clasificar_alcance_geografico(lead)
        ok, motivo = evaluar_dato_de_google_maps(lead, alcance) if ACEPTAR_DATO_DE_GOOGLE_MAPS else (False, 'desactivado')
        (pasan if ok else quedan).append((lead, alcance, motivo))

    print(f"\n=== PASAN A CHEQUEADO (celeste): {len(pasan)} ===", flush=True)
    for lead, _, _ in pasan:
        print(f"  {lead['id']}  {lead.get('partner_name') or lead.get('name')}", flush=True)
    resumen = Counter(f"{alc} | {mot}" for _, alc, mot in quedan)
    print(f"\n=== SE QUEDAN EN REVISAR: {len(quedan)} ===", flush=True)
    for clave, cant in resumen.most_common():
        print(f"{cant:4d}  {clave}", flush=True)
    print("\nDetalle de los que se quedan:", flush=True)
    for lead, alc, mot in quedan:
        print(f"  {lead['id']} | {lead.get('partner_name') or lead.get('name')} | {alc} | {mot} | {lead.get('street') or 'S/D'}", flush=True)

    if not aplicar:
        print("\nSIMULACIÓN: no se modificó nada. Para aplicar, ejecutá de nuevo eligiendo aplicar = si.", flush=True)
        return
    for lead, _, motivo in pasan:
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], {'stage_id': ID_ETAPA_CHEQUEADO}])
        registrar_nota(models, uid, lead['id'], "Agente de Verificación: APROBADO CON DATO DE GOOGLE MAPS (celeste): pasa a Chequeado con confianza media. " + motivo + " No se buscó una segunda fuente en esta reclasificación.")
        marcar_color(models, uid, lead['id'], COLOR_DATO_GOOGLE_MAPS)
    print(f"\nListo: {len(pasan)} leads pasaron a Chequeado (celeste).", flush=True)


def ejecutar_agente():
    if not ID_ETAPA_REVISAR:
        raise SystemExit("Falta configurar ID_ETAPA_REVISAR (el número de la etapa 'Revisar' en Odoo). No se hizo ningún cambio.")

    print("Conectando a Odoo...", flush=True)
    uid, models = conectar_odoo()

    # Regla de las etapas:
    #   - Solo se revisan los leads que están en Prospectos. Chequeado y Revisar no se tocan.
    #   - Verificado -> Chequeado (verde).
    #   - No verificado o fuera de Córdoba Capital -> Revisar (para revisión manual).
    # Las etiquetas nunca se modifican y no se crean leads nuevos.
    id_prospectos = ID_ETAPA_PROSPECTOS
    campos = ['id', 'name', 'partner_name', 'contact_name', 'email_from', 'phone', 'website', 'street', 'city', 'state_id', 'zip', 'country_id', 'tag_ids', 'stage_id', 'color', 'description']

    todos_los_leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
        [[['stage_id', '=', id_prospectos]]],
        {'fields': campos, 'limit': 10000, 'order': 'id asc'}
    )

    # Las notas de "no devolvió evidencia suficiente" son fallas del buscador, no auditorías reales:
    # no cuentan como revisión hecha.
    mensajes_agente = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'mail.message', 'search_read',
        [[['model', '=', 'crm.lead'], ['body', 'ilike', 'Agente de Verificación:'],
          ['body', 'not ilike', 'no devolvió evidencia suficiente']]],
        {'fields': ['res_id', 'date'], 'limit': 20000, 'order': 'date desc'}
    )
    ultima_auditoria = {}
    for mensaje in mensajes_agente:
        res_id = mensaje.get('res_id')
        fecha = mensaje.get('date') or ''
        if res_id and (res_id not in ultima_auditoria or fecha > ultima_auditoria[res_id]):
            ultima_auditoria[res_id] = fecha

    # Cuántas veces el buscador no devolvió nada para cada lead (esas notas no cuentan como auditoría).
    intentos_sin_evidencia = Counter(
        m['res_id'] for m in models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY, 'mail.message', 'search_read',
            [[['model', '=', 'crm.lead'], ['body', 'ilike', 'Agente de Verificación:'],
              ['body', 'ilike', 'no devolvió evidencia suficiente']]],
            {'fields': ['res_id'], 'limit': 20000}
        ) if m.get('res_id')
    )

    limite_pendientes = (datetime.now(timezone.utc) - timedelta(hours=HORAS_REINTENTO_PENDIENTES)).strftime('%Y-%m-%d %H:%M:%S')

    en_prospectos = len(todos_los_leads)

    # Un lead que sigue en Prospectos (por ejemplo, porque dio error) se vuelve a revisar
    # si nunca se auditó o pasaron las horas de espera.
    todos_los_leads = [l for l in todos_los_leads if ultima_auditoria.get(l['id'], '') < limite_pendientes]

    if SOLO_IDS:
        todos_los_leads = [l for l in todos_los_leads if l['id'] in SOLO_IDS]

    # Primero los nunca auditados; luego los auditados hace más tiempo.
    todos_los_leads.sort(key=lambda lead: (ultima_auditoria.get(lead['id'], ''), lead['id']))

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

    print(
        f"En Prospectos: {en_prospectos} | Les toca revisión ahora: {len(todos_los_leads)} | "
        f"Lote de esta corrida: {len(leads)} | "
        f"Tiempo máximo: {TIEMPO_MAXIMO_SEGUNDOS // 60} min | Modelo: {MODELO_GEMINI}.",
        flush=True
    )
    if not leads:
        print("No hay leads pendientes para auditar.", flush=True)
        return

    aprobados = 0          # pasaron a Chequeado en verde
    aprobados_gm = 0       # pasaron a Chequeado en celeste (dato de Google Maps sin segunda fuente)
    a_revisar = 0          # pasaron a Revisar (no verificados)
    pendientes = 0         # siguen en Prospectos por un error
    procesados = 0
    fallas_busqueda = 0
    fuera_alcance = 0
    sin_buscador_seguidos = 0
    motivo_corte = ''

    for lead in leads:
        if time.time() - INICIO_EJECUCION > TIEMPO_MAXIMO_SEGUNDOS:
            motivo_corte = "TIEMPO MÁXIMO ALCANZADO. Se corta limpio; los leads restantes se toman en la próxima corrida."
            print(f"\n{motivo_corte}", flush=True)
            break

        inicio_lead = time.time()
        print(f"\n--- Auditando Lead ID {lead['id']}: {lead['name']} [{lead.get('_etiquetas') or 'sin etiqueta'}] (Dir: {lead.get('street') or 'S/D'}) ---", flush=True)
        vacio = SimpleNamespace(email_elegido='', telefono_elegido='', coincide_ubicacion_y_rubro=False, motivo='')
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
                    "No se realizó búsqueda ni se modificaron datos. Pasa a Revisar para revisión manual."
                )
                models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], {'stage_id': ID_ETAPA_REVISAR}])
                registrar_nota(models, uid, lead['id'], nota)
                marcar_color(models, uid, lead['id'], COLOR_FUERA_ALCANCE)
                procesados += 1
                fuera_alcance += 1
                a_revisar += 1
                print(nota, flush=True)
                continue

            # Si Georef no confirma la dirección exacta, igual se busca, pero en ese caso
            # cada fuente debe nombrar Córdoba para poder aprobar (ver evaluar_evidencia).
            info_ddgs = investigacion_profunda_con_google_search(lead)
            sin_buscador_seguidos = 0  # el buscador respondió: se reinicia el contador de bloqueo
            # Gemini solo se consulta si la búsqueda halló algo que confirmar (email, celular nuevo
            # o el celular ya cargado visto junto al comercio): si no hay nada, no puede aportar.
            previa = evaluar_evidencia(lead, info_ddgs, vacio, alcance)
            if previa['email'] or previa['telefono'] or previa['tel_cargado']:
                resultado = analizar_lead_con_google_search_y_ia(lead, info_ddgs)
            else:
                resultado = vacio

        except BusquedaSinEvidencia as e:
            intentos_previos = intentos_sin_evidencia.get(lead['id'], 0)
            if intentos_previos >= INTENTOS_SIN_RESULTADOS_ANTES_DE_DECIDIR:
                # Ya se intentó en varias corridas y el buscador nunca devuelve nada para este lead: no es un bloqueo,
                # es un comercio sin presencia en internet. Se decide con los datos de Odoo para no dejarlo en Prospectos.
                print(f"Sin resultados del buscador por {intentos_previos + 1}ª vez: se decide con los datos de Odoo.", flush=True)
                info_ddgs = ''
                resultado = vacio
            else:
                # El buscador no devolvió NADA para este lead: puede ser un bloqueo. No se cambia etapa ni color;
                # se deja una nota (que no cuenta como auditoría) y se reintenta en la próxima corrida.
                fallas_busqueda += 1
                sin_buscador_seguidos += 1
                print(f"BUSQUEDA SIN RESULTADOS ({sin_buscador_seguidos}/{MAX_SIN_BUSCADOR_SEGUIDOS}). El lead no se modifica: {e}", flush=True)
                registrar_nota(models, uid, lead['id'], f"Agente de Verificación: la búsqueda no devolvió evidencia suficiente (intento {intentos_previos + 1}). El lead no se modificó y se reintentará en la próxima corrida.")
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
            print(f"Error en lead {lead['id']}; queda como estaba y se continúa: {e}", flush=True)
            pendientes += 1
            continue

        evidencia = evaluar_evidencia(lead, info_ddgs, resultado, alcance)

        email_actual = (lead.get('email_from') or '').strip().lower()
        telefono_actual = limpiar_formato_celular(lead.get('phone'))

        email_hallado = evidencia['email']        # (email, url) o None
        telefono_hallado = evidencia['telefono']  # (celular, url) o None

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
        telefono_confirmado, mismo_numero, sin_aplicar_tel, otros_celulares = decidir_telefono(telefono_actual, evidencia)
        sin_aplicar.extend(sin_aplicar_tel)
        contacto_verificado = bool(email_confirmado or telefono_confirmado)

        aprobado_final = bool(
            alcance in ('CORDOBA_CAPITAL', 'UBICACION_INCIERTA') and
            resultado.coincide_ubicacion_y_rubro and
            evidencia['nombre_ok'] and
            contacto_verificado
        )

        # Segundo nivel: dato cargado desde Google Maps sin una segunda fuente que lo confirme (celeste).
        via_google_maps, motivo_google_maps = False, ''
        if not aprobado_final and ACEPTAR_DATO_DE_GOOGLE_MAPS:
            via_google_maps, motivo_google_maps = evaluar_dato_de_google_maps(lead, alcance)
        pasa_a_chequeado = aprobado_final or via_google_maps

        partes_nota = []
        extra_nota = []
        valores_actualizar = {}

        if email_confirmado and email_confirmado[0] != email_actual:
            valores_actualizar['email_from'] = email_confirmado[0]
            if email_actual:
                extra_nota.append(f"El email anterior era {email_actual}.")
        if telefono_confirmado and not mismo_numero:
            valores_actualizar['phone'] = con_prefijo_mas(telefono_confirmado[0])
            if telefono_actual:
                extra_nota.append(f"El teléfono anterior era {telefono_actual}.")
        # Verificado -> Chequeado (verde); no verificado -> Revisar (revisión manual).
        valores_actualizar['stage_id'] = ID_ETAPA_CHEQUEADO if pasa_a_chequeado else ID_ETAPA_REVISAR

        if aprobado_final:
            partes_nota.append("APROBADO (verde): contacto verificado con evidencia. Pasa a Chequeado.")
        elif via_google_maps:
            partes_nota.append("APROBADO CON DATO DE GOOGLE MAPS (celeste): pasa a Chequeado con confianza media. " + motivo_google_maps)
        else:
            partes_nota.append("PENDIENTE: no se pudo verificar. Pasa a Revisar para revisión manual.")
        if email_confirmado:
            partes_nota.append(f"Email confirmado: {email_confirmado[0]} (fuente: {email_confirmado[1] or 'resultado de búsqueda'}).")
            if evidencia['otros_emails']:
                partes_nota.append("Otros emails también publicados por el comercio: " + ", ".join(evidencia['otros_emails']) + ".")
        if telefono_confirmado:
            partes_nota.append(f"Celular confirmado: {telefono_confirmado[0]} (fuente: {telefono_confirmado[1] or 'resultado de búsqueda'}).")
            if otros_celulares:
                partes_nota.append("Otros celulares publicados por el comercio (no se cambió el dato de Odoo): " + ", ".join(otros_celulares) + ".")
        partes_nota.extend(extra_nota)
        if sin_aplicar:
            partes_nota.append("NO se cambió porque no salió de la web propia del comercio y Odoo ya tenía otro dato: " + "; ".join(sin_aplicar) + ".")
        if not pasa_a_chequeado or via_google_maps:
            faltantes = []
            if not contacto_verificado:
                faltantes.append("no se confirmó email ni celular en una página que pertenezca claramente al comercio")
            if not evidencia['nombre_ok']:
                faltantes.append("el nombre no apareció junto a la calle, el teléfono cargado o una web propia")
            if contacto_verificado and not resultado.coincide_ubicacion_y_rubro:
                faltantes.append("no coincide ubicación o rubro")
            if not via_google_maps:
                partes_nota.append("No pasa a Chequeado: " + ", ".join(faltantes or ["validación insuficiente"]) + ".")
            else:
                partes_nota.append("No se halló una segunda fuente en internet para confirmar los datos.")
            if evidencia['candidatos']:
                lista = "; ".join(f"{e} (visto en {u or 'resultado de búsqueda'})" for e, u in evidencia['candidatos'])
                partes_nota.append(f"Emails vistos sin respaldo suficiente (una sola fuente no propia): {lista}.")
            if evidencia['tel_debiles'] or evidencia['tel_cargado_debil']:
                vistos = [f"{t} (visto en {u or 'resultado de búsqueda'})" for t, u in evidencia['tel_debiles']]
                if evidencia['tel_cargado_debil']:
                    vistos.append(f"el teléfono ya cargado (visto solo en {evidencia['tel_cargado_debil']})")
                partes_nota.append("Celulares vistos sin respaldo suficiente: " + "; ".join(vistos) + ".")
        if alcance == 'UBICACION_INCIERTA':
            partes_nota.append("Georef no confirmó la dirección exacta; se exigió que cada fuente nombrara Córdoba o la calle y número.")

        print(
            f"Resultado: Aprobado={aprobado_final} | Celeste(Google Maps)={via_google_maps} | Email={email_confirmado} | Celular={telefono_confirmado} "
            f"| Tiempo del lead: {int(time.time() - inicio_lead)} s",
            flush=True
        )

        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write', [[lead['id']], valores_actualizar])

        registrar_nota(models, uid, lead['id'], "Agente de Verificación: " + " ".join(partes_nota))
        marcar_color(models, uid, lead['id'], COLOR_APROBADO if aprobado_final else (COLOR_DATO_GOOGLE_MAPS if via_google_maps else COLOR_PENDIENTE))

        procesados += 1
        if aprobado_final:
            aprobados += 1
        elif via_google_maps:
            aprobados_gm += 1
        else:
            a_revisar += 1

    minutos = int((time.time() - INICIO_EJECUCION) / 60)
    print(
        f"\nRESUMEN ({minutos} min): lote={len(leads)} | auditados={procesados} | verdes_en_Chequeado={aprobados} | celestes_en_Chequeado={aprobados_gm} "
        f"| pasados_a_Revisar={a_revisar} (de ellos fuera_alcance={fuera_alcance}) | siguen_en_Prospectos_por_error={pendientes} "
        f"| sin_resultados_del_buscador={fallas_busqueda} | sin_tocar={len(leads) - procesados - fallas_busqueda - pendientes}",
        flush=True
    )
    if motivo_corte:
        print(f"CORTE: {motivo_corte}", flush=True)


if __name__ == "__main__":
    if os.environ.get("MODO") == "reclasificar":
        reclasificar_revisar(aplicar=os.environ.get("APLICAR") == "1")
    else:
        ejecutar_agente()
