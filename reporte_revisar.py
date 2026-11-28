"""
Reporte de los leads que están en la etapa Revisar.
SOLO LEE de Odoo: no cambia etapas, datos, colores ni notas.

Para cada lead junta sus datos y la última nota del Agente de Verificación, la clasifica por motivo
y muestra: 1) un resumen con cantidades y 2) un CSV completo entre las marcas INICIO/FIN.
"""
import csv
import html
import io
import os
import re
import xmlrpc.client
from collections import Counter

ODOO_URL = os.environ["ODOO_URL"]
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USER"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

ID_ETAPA_REVISAR = 11
LARGO_MAXIMO_NOTA = 700

# Motivos (se muestran todos los que apliquen) y motivo principal (el primero que coincida, en este orden).
SENALES = [
    ('FUERA_DE_ALCANCE', r'FUERA DE ALCANCE'),
    ('ERROR_DE_AUDITORIA', r'Error durante la auditor'),
    ('EMAIL_EN_UNA_SOLA_FUENTE', r'Emails vistos sin respaldo'),
    ('CELULAR_EN_UNA_SOLA_FUENTE', r'Celulares vistos sin respaldo'),
    ('DATO_DISTINTO_AL_DE_ODOO', r'NO se cambió porque'),
    ('NOMBRE_NO_CONFIRMADO', r'el nombre no apareció'),
    ('UBICACION_O_RUBRO', r'no coincide ubicación o rubro'),
    ('NO_ENCONTRO_EMAIL_NI_CELULAR', r'no se confirmó email ni celular'),
    ('GEOREF_NO_CONFIRMO', r'Georef no confirmó'),
]
PRINCIPAL = [
    ('FUERA_DE_ALCANCE', ['FUERA_DE_ALCANCE']),
    ('ERROR_DE_AUDITORIA', ['ERROR_DE_AUDITORIA']),
    ('DATO_VISTO_EN_UNA_SOLA_FUENTE', ['EMAIL_EN_UNA_SOLA_FUENTE', 'CELULAR_EN_UNA_SOLA_FUENTE']),
    ('DATO_DISTINTO_AL_DE_ODOO', ['DATO_DISTINTO_AL_DE_ODOO']),
    ('NOMBRE_NO_CONFIRMADO', ['NOMBRE_NO_CONFIRMADO']),
    ('UBICACION_O_RUBRO', ['UBICACION_O_RUBRO']),
    ('NO_ENCONTRO_NADA', ['NO_ENCONTRO_EMAIL_NI_CELULAR']),
]


def limpiar_html(texto):
    texto = re.sub(r'<[^>]+>', ' ', texto or '')
    return re.sub(r'\s+', ' ', html.unescape(texto)).strip()


def clasificar(nota):
    if not nota:
        return 'SIN_NOTA_DEL_AGENTE', []
    senales = [nombre for nombre, patron in SENALES if re.search(patron, nota, re.I)]
    for principal, requeridas in PRINCIPAL:
        if any(r in senales for r in requeridas):
            return principal, senales
    return 'OTRO', senales


def main():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    def ejecutar(modelo, metodo, args, kwargs=None):
        return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, modelo, metodo, args, kwargs or {})

    leads = ejecutar('crm.lead', 'search_read', [[['stage_id', '=', ID_ETAPA_REVISAR]]], {
        'fields': ['id', 'name', 'partner_name', 'street', 'city', 'zip', 'phone', 'email_from',
                   'website', 'tag_ids', 'description', 'color'],
        'limit': 10000, 'order': 'id asc'
    })
    print(f"Leads en Revisar: {len(leads)}", flush=True)
    if not leads:
        return

    # Última nota del agente por lead (se consulta de a tandas para no pasarse de tamaño).
    ultima_nota = {}
    ids = [l['id'] for l in leads]
    for i in range(0, len(ids), 150):
        tanda = ids[i:i + 150]
        mensajes = ejecutar('mail.message', 'search_read', [[
            ['model', '=', 'crm.lead'], ['res_id', 'in', tanda],
            ['body', 'ilike', 'Agente de Verificación:']
        ]], {'fields': ['res_id', 'date', 'body'], 'order': 'date desc', 'limit': 5000})
        for m in mensajes:
            ultima_nota.setdefault(m['res_id'], (m.get('date') or '', limpiar_html(m.get('body'))))

    # Nombres de etiquetas
    ids_tags = sorted({t for l in leads for t in (l.get('tag_ids') or [])})
    nombres_tags = {}
    if ids_tags:
        for tag in ejecutar('crm.tag', 'read', [ids_tags], {'fields': ['name']}):
            nombres_tags[tag['id']] = tag['name']

    filas = []
    por_motivo = Counter()
    por_senal = Counter()
    for l in leads:
        fecha, nota = ultima_nota.get(l['id'], ('', ''))
        principal, senales = clasificar(nota)
        por_motivo[principal] += 1
        for s in senales:
            por_senal[s] += 1
        filas.append([
            l['id'],
            l.get('partner_name') or l.get('name') or '',
            ' / '.join(nombres_tags.get(t, '') for t in (l.get('tag_ids') or []) if nombres_tags.get(t)),
            l.get('street') or '',
            l.get('phone') or '',
            l.get('email_from') or '',
            l.get('website') or '',
            limpiar_html(l.get('description'))[:150],
            principal,
            ' + '.join(senales),
            fecha[:16],
            nota[:LARGO_MAXIMO_NOTA],
        ])

    print("\n=== RESUMEN POR MOTIVO PRINCIPAL ===", flush=True)
    for motivo, cantidad in por_motivo.most_common():
        print(f"{cantidad:4d}  {motivo}", flush=True)
    print("\n=== SEÑALES (un lead puede tener varias) ===", flush=True)
    for senal, cantidad in por_senal.most_common():
        print(f"{cantidad:4d}  {senal}", flush=True)
    con_telefono = sum(1 for f in filas if f[4])
    con_email = sum(1 for f in filas if f[5])
    con_web = sum(1 for f in filas if f[6])
    print(f"\nDe {len(filas)} leads: con teléfono cargado={con_telefono} | con email cargado={con_email} | con sitio web cargado={con_web}", flush=True)

    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    escritor.writerow(['id', 'nombre', 'etiqueta', 'direccion', 'telefono', 'email', 'web', 'descripcion',
                       'motivo_principal', 'senales', 'fecha_nota', 'nota_del_agente'])
    escritor.writerows(filas)
    with open('reporte_revisar.csv', 'w', encoding='utf-8-sig', newline='') as f:
        f.write(salida.getvalue())

    print("\n=== INICIO REPORTE CSV ===", flush=True)
    print(salida.getvalue(), flush=True)
    print("=== FIN REPORTE CSV ===", flush=True)


if __name__ == "__main__":
    main()
