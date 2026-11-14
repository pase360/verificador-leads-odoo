import os
import re
import time
import unicodedata
import xmlrpc.client
from urllib.parse import urlparse, urljoin, quote_plus
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ODOO_URL = os.environ['ODOO_URL'].rstrip('/')
ODOO_DB = os.environ['ODOO_DB']
ODOO_USER = os.environ['ODOO_USER']
ODOO_API_KEY = os.environ['ODOO_API_KEY']

ID_ETAPA_PROSPECTOS = 5
ID_ETAPA_CHEQUEADO = 7
MAX_LEADS_POR_EJECUCION = 400
MAX_RESULTADOS_GOOGLE = 7
MAX_PAGINAS_POR_DOMINIO = 7
TIMEOUT_NAVEGACION_MS = 18000
PAUSA_ENTRE_LEADS = 1.5


class ErrorBusquedaGoogle(Exception):
    pass


def normalizar(texto):
    texto = unicodedata.normalize('NFKD', str(texto or ''))
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', texto.lower()).strip()


def conectar_odoo():
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common', allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError('No se pudo autenticar con Odoo.')
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)
    return uid, models


def limpiar_telefono(valor):
    digitos = re.sub(r'\D', '', str(valor or ''))
    if digitos.startswith('549') and len(digitos) == 13:
        return digitos
    if digitos.startswith('54') and len(digitos) == 12:
        return '549' + digitos[2:]
    if digitos.startswith('0'):
        digitos = digitos[1:]
    if len(digitos) in (11, 12) and digitos[2:4] == '15':
        digitos = digitos[:2] + digitos[4:]
    elif len(digitos) in (11, 12) and digitos[3:5] == '15':
        digitos = digitos[:3] + digitos[5:]
    if len(digitos) == 10:
        return '549' + digitos
    return digitos


def extraer_telefonos(texto):
    patrones = re.findall(
        r'(?:\+?54\s*9?\s*)?(?:\(?0?\d{2,4}\)?[\s.-]*)?(?:15[\s.-]*)?\d{6,8}',
        texto or ''
    )
    encontrados = []
    for patron in patrones:
        numero = limpiar_telefono(patron)
        if 10 <= len(numero) <= 13 and numero not in encontrados:
            encontrados.append(numero)
    return encontrados


def extraer_emails(texto):
    encontrados = re.findall(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', texto or '', re.I)
    salida = []
    for email in encontrados:
        email = email.lower().strip('.,;:()[]{}<>')
        if not any(x in email for x in ('example.com', 'wixpress.com', 'sentry.io', 'cloudflare.com')):
            if email not in salida:
                salida.append(email)
    return salida


def texto_pagina(page):
    try:
        texto = page.locator('body').inner_text(timeout=5000)
        try:
            hrefs = page.locator('a[href]').evaluate_all(
                "els => els.map(a => a.href).slice(0, 180)"
            )
            texto += '\nENLACES PUBLICADOS:\n' + '\n'.join(hrefs)
        except Exception:
            pass
        return texto[:24000]
    except Exception:
        return ''


def buscar_en_google(page, nombre, direccion, consulta_extra=''):
    consulta = f'"{nombre}" "{direccion}" Córdoba Argentina {consulta_extra}'.strip()
    url = 'https://www.google.com/search?q=' + quote_plus(consulta) + '&hl=es&gl=ar'
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=TIMEOUT_NAVEGACION_MS)
        page.wait_for_timeout(1800)
    except PlaywrightTimeoutError:
        pass
    actual = page.url.lower()
    contenido = texto_pagina(page)
    contenido_norm = normalizar(contenido)
    if 'sorry' in actual or 'unusual traffic' in contenido_norm or 'captcha' in contenido_norm:
        raise ErrorBusquedaGoogle('Google solicito una verificacion antirobot.')
    links = []
    try:
        anchors = page.locator('a:has(h3)')
        for i in range(min(anchors.count(), MAX_RESULTADOS_GOOGLE)):
            anchor = anchors.nth(i)
            href = anchor.get_attribute('href') or ''
            titulo = anchor.inner_text(timeout=1500) or ''
            if href.startswith('http') and 'google.com' not in urlparse(href).netloc:
                if href not in [item[0] for item in links]:
                    links.append((href, titulo))
    except Exception:
        pass
    if not links and not contenido:
        raise ErrorBusquedaGoogle('Google no devolvio resultados legibles.')
    return consulta, contenido, links


def recolectar_sitio(contexto, url):
    paginas = []
    redes = []
    try:
        page = contexto.new_page()
        page.set_default_navigation_timeout(TIMEOUT_NAVEGACION_MS)
        page.goto(url, wait_until='domcontentloaded', timeout=TIMEOUT_NAVEGACION_MS)
        page.wait_for_timeout(700)
        paginas.append((page.url, texto_pagina(page)))
        dominio = urlparse(page.url).netloc.lower().replace('www.', '')
        enlaces = page.locator('a[href]')
        internas = []
        for i in range(min(enlaces.count(), 250)):
            href = enlaces.nth(i).get_attribute('href') or ''
            if not href:
                continue
            absoluto = urljoin(page.url, href)
            host = urlparse(absoluto).netloc.lower().replace('www.', '')
            if any(red in host for red in ('facebook.com', 'instagram.com', 'linkedin.com', 'tiktok.com')):
                if absoluto not in redes:
                    redes.append(absoluto)
            elif host == dominio and any(
                palabra in normalizar(absoluto)
                for palabra in ('contact', 'contacto', 'ventas', 'sucursal', 'nosotros', 'about', 'empresa', 'whatsapp')
            ):
                if absoluto not in internas and absoluto != page.url:
                    internas.append(absoluto)
        for suburl in internas[:MAX_PAGINAS_POR_DOMINIO - 1]:
            try:
                page.goto(suburl, wait_until='domcontentloaded', timeout=TIMEOUT_NAVEGACION_MS)
                page.wait_for_timeout(350)
                paginas.append((page.url, texto_pagina(page)))
            except Exception:
                continue
        page.close()
    except Exception:
        pass
    return paginas, redes


def investigar_lead(contexto, lead):
    nombre = (lead.get('partner_name') or lead.get('name') or '').strip()
    direccion = (lead.get('street') or '').strip()
    if not nombre or not direccion:
        return {
            'aprobado': False,
            'motivo': 'Falta nombre o direccion en Odoo.',
            'email': '',
            'telefono': '',
            'email_verificado': False,
            'telefono_verificado': False,
        }

    google = contexto.new_page()
    google.set_default_navigation_timeout(TIMEOUT_NAVEGACION_MS)
    consulta, google_texto, resultados = buscar_en_google(google, nombre, direccion)
    urls = [url for url, _ in resultados]
    evidencias_google = [f'GOOGLE {consulta}\n{google_texto[:12000]}']

    consulta_social = f'"{nombre}" "{direccion}" (site:facebook.com OR site:instagram.com) contacto email telefono'
    try:
        _, texto_social, resultados_sociales = buscar_en_google(
            google, nombre, direccion,
            '(site:facebook.com OR site:instagram.com) contacto email telefono'
        )
        evidencias_google.append(f'GOOGLE REDES {consulta_social}\n{texto_social[:8000]}')
        urls.extend(url for url, _ in resultados_sociales if url not in urls)
    except ErrorBusquedaGoogle:
        pass
    google.close()

    paginas = []
    redes = []
    dominios_visitados = set()
    for url in urls:
        host = urlparse(url).netloc.lower().replace('www.', '')
        if not host or host in dominios_visitados:
            continue
        dominios_visitados.add(host)
        paginas_sitio, redes_sitio = recolectar_sitio(contexto, url)
        paginas.extend(paginas_sitio)
        redes.extend(redes_sitio)
        if len(paginas) >= 18:
            break

    # Solo se visitan perfiles sociales que aparecen enlazados desde los resultados/sitios.
    for red in list(dict.fromkeys(redes))[:4]:
        try:
            page = contexto.new_page()
            page.goto(red, wait_until='domcontentloaded', timeout=10000)
            paginas.append((page.url, texto_pagina(page)))
            page.close()
        except Exception:
            continue

    nombre_norm = normalizar(nombre)
    direccion_norm = normalizar(direccion)
    palabras_nombre = [x for x in re.findall(r'[a-z0-9]+', nombre_norm) if len(x) > 2]
    numeros_direccion = re.findall(r'\d+', direccion_norm)
    palabras_calle = [
        x for x in re.findall(r'[a-z]+', direccion_norm)
        if len(x) > 3 and x not in ('avenida', 'calle', 'cordoba', 'argentina')
    ]

    # Nombre, calle, numero y Cordoba deben aparecer juntos en una misma fuente.
    fuentes = [('Google', google_texto)] + paginas
    fuente_identidad_direccion = False
    for _, texto in fuentes:
        t = normalizar(texto)
        nombre_ok = bool(palabras_nombre) and sum(x in t for x in palabras_nombre) >= max(1, min(2, len(palabras_nombre)))
        numero_ok = bool(numeros_direccion) and any(n in t for n in numeros_direccion)
        calle_ok = not palabras_calle or any(x in t for x in palabras_calle[:2])
        cordoba_ok = 'cordoba' in t
        if nombre_ok and numero_ok and calle_ok and cordoba_ok:
            fuente_identidad_direccion = True
            break

    # Solo se toman telefonos y correos de dominios cuyo contenido identifica al comercio.
    dominios_del_comercio = set()
    for url, texto in paginas:
        t = normalizar(texto)
        coincidencias_nombre = sum(x in t for x in palabras_nombre)
        if palabras_nombre and coincidencias_nombre >= max(1, min(2, len(palabras_nombre))):
            host = urlparse(url).netloc.lower().replace('www.', '')
            if host:
                dominios_del_comercio.add(host)

    telefonos_paginas = []
    telefonos_whatsapp = []
    emails = []
    for url, texto in paginas:
        host = urlparse(url).netloc.lower().replace('www.', '')
        if host not in dominios_del_comercio:
            continue
        telefonos_paginas.extend(extraer_telefonos(texto))
        emails.extend(extraer_emails(texto))
        for coincidencia in re.findall(
            r'(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp\.com/send\?phone=)([+\d%20-]+)',
            texto, re.I
        ):
            numero = limpiar_telefono(coincidencia)
            if 10 <= len(numero) <= 13:
                telefonos_whatsapp.append(numero)

    telefonos_whatsapp = list(dict.fromkeys(telefonos_whatsapp))
    telefonos_paginas = list(dict.fromkeys(telefonos_paginas))
    emails = list(dict.fromkeys(emails))
    telefono_odoo = limpiar_telefono(lead.get('phone'))

    if telefonos_whatsapp:
        telefono_elegido = telefonos_whatsapp[0]
        telefono_verificado = True
    elif telefono_odoo and telefono_odoo in telefonos_paginas:
        telefono_elegido = telefono_odoo
        telefono_verificado = True
    else:
        telefono_elegido = ''
        telefono_verificado = False

    email_elegido = emails[0] if emails else ''
    ciudad_odo = normalizar(lead.get('city'))
    localidades_excluidas = (
        'villa allende', 'saldan', 'la calera', 'villa carlos paz',
        'carlos paz', 'alta gracia', 'villa maria', 'san francisco',
        'rio cuarto', 'rio tercero', 'jesus maria', 'cosquin'
    )
    ciudad_odo_ok = (
        (not ciudad_odo or 'cordoba' in ciudad_odo or 'capital' in ciudad_odo)
        and not any(localidad in ciudad_odo for localidad in localidades_excluidas)
    )
    aprobado = fuente_identidad_direccion and ciudad_odo_ok and telefono_verificado
    faltantes = []
    if not fuente_identidad_direccion:
        faltantes.append('no se confirmaron juntos nombre, direccion y Cordoba en una misma fuente')
    if not ciudad_odo_ok:
        faltantes.append('la ciudad cargada en Odoo esta fuera de Cordoba Capital')
    if not telefono_verificado:
        faltantes.append('no se verifico el telefono de Odoo ni un WhatsApp publicado')
    motivo = (
        'Nombre, direccion y telefono verificados en fuentes publicas.'
        if aprobado else '; '.join(faltantes)
    )
    return {
        'aprobado': aprobado,
        'motivo': motivo,
        'email': email_elegido,
        'telefono': telefono_elegido,
        'email_verificado': bool(email_elegido),
        'telefono_verificado': telefono_verificado,
        'nombre_encontrado': fuente_identidad_direccion,
        'direccion_encontrada': fuente_identidad_direccion,
        'telefonos_whatsapp': telefonos_whatsapp,
        'telefonos_paginas': telefonos_paginas,
        'urls': urls,
    }


def ejecutar_agente():
    print('Conectando a Odoo...', flush=True)
    uid, models = conectar_odoo()
    campos = [
        'id', 'name', 'partner_name', 'contact_name', 'email_from',
        'phone', 'website', 'street', 'city', 'country_id'
    ]
    leads = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'search_read',
        [[['stage_id', '=', ID_ETAPA_PROSPECTOS]]],
        {'fields': campos, 'limit': MAX_LEADS_POR_EJECUCION, 'order': 'id asc'}
    )
    total = len(leads)
    print(
        f'Leads encontrados en Prospectos por Controlar: {total}. '
        f'Limite por ejecucion: {MAX_LEADS_POR_EJECUCION}.',
        flush=True
    )
    if not leads:
        return

    procesados = aprobados = pendientes = errores_google = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        contexto = browser.new_context(
            locale='es-AR',
            timezone_id='America/Argentina/Cordoba',
            viewport={'width': 1365, 'height': 900}
        )
        consecutivos_google = 0
        for lead in leads:
            print(
                f"\n--- Lead {lead['id']}: {lead.get('partner_name') or lead.get('name')} "
                f"| {lead.get('street') or 'SIN DIRECCION'} ---",
                flush=True
            )
            try:
                resultado = investigar_lead(contexto, lead)
                consecutivos_google = 0
            except ErrorBusquedaGoogle as exc:
                errores_google += 1
                consecutivos_google += 1
                pendientes += 1
                print(f'BUSQUEDA NO DISPONIBLE: {exc}. Se conserva pendiente.', flush=True)
                if consecutivos_google >= 3:
                    print(
                        'Google bloqueo o no entrego resultados en 3 leads consecutivos. '
                        'Se detiene para evitar procesar mal.',
                        flush=True
                    )
                    break
                continue
            except Exception as exc:
                pendientes += 1
                print(f"ERROR EN LEAD {lead['id']}: {exc}. Se conserva pendiente.", flush=True)
                continue

            print(
                f"Verificacion: nombre/direccion={resultado.get('nombre_encontrado')} "
                f"telefono={resultado.get('telefono_verificado')} "
                f"WhatsApp={resultado.get('telefonos_whatsapp')} "
                f"email={resultado.get('email') or 'no encontrado'}",
                flush=True
            )
            print(f"Detalle: {resultado['motivo']}", flush=True)

            valores = {}
            email_actual = (lead.get('email_from') or '').strip().lower()
            telefono_actual = limpiar_telefono(lead.get('phone'))
            if resultado.get('email_verificado') and resultado['email'] and resultado['email'] != email_actual:
                valores['email_from'] = resultado['email']
            if resultado.get('telefono_verificado') and resultado['telefono'] and resultado['telefono'] != telefono_actual:
                valores['phone'] = '+' + resultado['telefono']
            if resultado['aprobado']:
                valores['stage_id'] = ID_ETAPA_CHEQUEADO

            if valores:
                models.execute_kw(
                    ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'write',
                    [[lead['id']], valores]
                )
            nota = 'Agente revisor (navegacion web): ' + resultado['motivo']
            models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY, 'crm.lead', 'message_post',
                [[lead['id']]], {'body': nota}
            )
            procesados += 1
            if resultado['aprobado']:
                aprobados += 1
            else:
                pendientes += 1
            time.sleep(PAUSA_ENTRE_LEADS)
        browser.close()

    no_alcanzados = total - procesados - pendientes
    print(
        f'\nRESUMEN: encontrados={total} | procesados={procesados} | '
        f'aprobados={aprobados} | pendientes={pendientes} | '
        f'fallas_google={errores_google} | no_alcanzados={no_alcanzados}',
        flush=True
    )


if __name__ == '__main__':
    ejecutar_agente()
