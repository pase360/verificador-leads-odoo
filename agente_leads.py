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

def es_cordoba_capital(lead):
    # La ciudad de Odoo puede estar vacía aunque la dirección esté completa.
    # La pertenencia a Córdoba Capital se valida con evidencia durante la auditoría,
    # no se descarta automáticamente por un campo city vacío.
    ciudad = (lead.get('city') or '').strip().lower()
    ciudad = (ciudad.replace('á', 'a').replace('é', 'e')
                   .replace('í', 'i').replace('ó', 'o').replace('ú', 'u'))
    return ciudad in ('cordoba', 'cordoba capital', 'capital')


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
        snippets1.append(f"- {item.get('title')} | {item.get('body')} | {link}")
        if link.startswith("http") and "instagram.com" not in link and "facebook.com" not in link and len(urls_encontradas) < 3:
            urls_encontradas.append(link)
    if snippets1:
        reporte_total.append("=== BÚSQUEDA WEB GENERAL ===\n" + "\n".join(snippets1))
    time.sleep(1)

    q2 = f'"{nombre_corto}" {calle_busqueda} {ciudad} (site:facebook.com OR site:instagram.com)' if calle_busqueda else f'"{nombre_corto}" {ciudad} (site:facebook.com OR site:instagram.com)'
    print(f"-> Buscando en Facebook/Instagram: {q2}", flush=True)
    res2 = ejecutar_consulta_ddgs(q2, max_res=6)
    if res2:
        snippets2 = [f"- {i.get('title')} | {i.get('body')} | {i.get('href')}" for i in res2]
        reporte_total.append("=== RESULTADOS DE FACEBOOK E INSTAGRAM ===\n" + "\n".join(snippets2))
    time.sleep(1)

    q3 = f'"{nombre_corto}" {calle_busqueda} ("@gmail.com" OR "@hotmail.com" OR "@yahoo.com")'
    print(f"-> Rastreando correos electrónicos: {q3}", flush=True)
    res3 = ejecutar_consulta_ddgs(q3, max_res=5)
    if res3:
        snippets3 = [f"- {i.get('title')} | {i.get('body')} | {i.get('href')}" for i in res3]
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
            reporte_total.append(f"=== CONTENIDO WEB ({url}) ===\n{contenido}")
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
    campos = ['id', 'name', 'partner_name', 'contact_name', 'email_from', 'phone', 'website', 'street', 'city', 'country_id']
    # Evita repetir los mismos 50 pendientes todos los días.
    # Un lead auditado se puede volver a intentar después de 7 días.
    fecha_reintento = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    mensajes_recientes = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'mail.message', 'search_read',
        [[['model', '=', 'crm.lead'], ['body', 'ilike', 'Agente de Verificación:'], ['date', '>=', fecha_reintento]]],
        {'fields': ['res_id'], 'limit': 10000, 'order': 'id desc'}
    )
    ids_aun_revisados = list({m.get('res_id') for m in mensajes_recientes if m.get('res_id')})
    dominio = [['stage_id', '=', id_prospectos]]
    if ids_aun_revisados:
        dominio.append(['id', 'not in', ids_aun_revisados])
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
        [dominio],
        {'fields': campos, 'limit': MAX_LEADS_POR_EJECUCION, 'order': 'id asc'}
    )
    print(
        f"Auditoria de leads EXISTENTES en Prospectos por Controlar (etapa {id_prospectos}). "
        f"Encontrados: {len(leads)} | Maximo por ejecucion: {MAX_LEADS_POR_EJECUCION}. "
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

    for lead in leads:
        print(f"\n--- Auditando Lead ID {lead['id']}: {lead['name']} (Dir: {lead.get('street') or 'S/D'}) ---", flush=True)

        try:
            info_ddgs = investigacion_profunda_con_google_search(lead)
            resultado = analizar_lead_con_google_search_y_ia(lead, info_ddgs)
        except BusquedaSinEvidencia as e:
            fallas_busqueda += 1
            print(f"BUSQUEDA SIN EVIDENCIA. No se modifica el lead y queda pendiente: {e}", flush=True)
            continue
        except CuotaGeminiAgotada as e:
            print(f"CUOTA/LÍMITE GEMINI ALCANZADO. Se detiene toda la ejecución para proteger los pendientes: {e}", flush=True)
            break
        except ServicioGeminiTemporal as e:
            no_auditados += 1
            print(f"GEMINI SIGUE NO DISPONIBLE DESPUÉS DE LOS REINTENTOS. Se detiene sin modificar este lead: {e}", flush=True)
            break
        except Exception as e:
            print(f"Error de Gemini en lead {lead['id']}; queda pendiente y se continúa: {e}", flush=True)
            pendientes += 1
            continue

        email_final = (resultado.email_elegido or '').strip().lower()
        telefono_formateado = limpiar_formato_celular(resultado.telefono_elegido)
        email_actual = (lead.get('email_from') or '').strip().lower()
        telefono_actual = limpiar_formato_celular(lead.get('phone'))

        email_definitivo = email_final if email_final else email_actual
        telefono_definitivo = telefono_formateado if telefono_formateado else telefono_actual

        telefono_verificado = bool(resultado.telefono_verificado and telefono_formateado)
        aprobado_final = bool(
            resultado.coincide_ubicacion_y_rubro and
            resultado.datos_validos_y_verificados and
            telefono_verificado
        )

        if aprobado_final:
            motivo_ajustado = resultado.motivo
        else:
            faltantes = []
            if not telefono_verificado:
                faltantes.append("teléfono no confirmado")
            if not resultado.coincide_ubicacion_y_rubro:
                faltantes.append("ubicación/rubro no confirmado")
            motivo_ajustado = f"{resultado.motivo} [NO APROBADO: {', '.join(faltantes) or 'validación insuficiente'}]."

        print(
            f"Resultado: Aprobado={aprobado_final} | Email verificado={resultado.email_verificado} "
            f"| Teléfono verificado={resultado.telefono_verificado} "
            f"| Tel={telefono_definitivo} | Email={email_definitivo}",
            flush=True
        )
        print(f"Detalle: {motivo_ajustado}", flush=True)

        valores_actualizar = {}
        if resultado.coincide_ubicacion_y_rubro and email_final and email_final != email_actual and resultado.email_verificado:
            valores_actualizar['email_from'] = email_final
        if resultado.coincide_ubicacion_y_rubro and telefono_formateado and telefono_formateado != telefono_actual and resultado.telefono_verificado:
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
        f"| pendientes_auditados={pendientes} | fallas_busqueda={fallas_busqueda} "
        f"| no_auditados={len(leads)-procesados-fallas_busqueda}",
        flush=True
    )

if __name__ == "__main__":
    ejecutar_agente()
