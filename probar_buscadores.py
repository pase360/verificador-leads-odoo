"""
Prueba diagnostica: que buscadores gratuitos responden desde los servidores de GitHub.
No toca Odoo ni Gemini. Solo hace unas pocas busquedas y cuenta resultados.
"""
import time
from ddgs import DDGS

BACKENDS = ["auto", "duckduckgo", "bing", "yahoo", "brave", "mojeek", "yandex", "google"]
CONSULTAS = [
    '"Patio Olmos Shopping" Córdoba contacto teléfono',
    '"Córdoba Shopping" Córdoba contacto email',
    '"Panadería Trigatti" Córdoba contacto',
]

resumen = {}
for backend in BACKENDS:
    print(f"\n=== Buscador: {backend} ===", flush=True)
    con_resultados = 0
    errores = []
    for q in CONSULTAS:
        inicio = time.time()
        try:
            res = DDGS().text(q, region="ar-es", max_results=8, backend=backend)
            n = len(res or [])
            print(f"  {n} resultados en {time.time() - inicio:.1f}s -> {q}", flush=True)
            if n > 0:
                con_resultados += 1
        except Exception as exc:
            msg = str(exc)[:120].replace("\n", " ")
            errores.append(msg)
            print(f"  ERROR en {time.time() - inicio:.1f}s -> {q} | {msg}", flush=True)
        time.sleep(4)
    resumen[backend] = (con_resultados, errores[:1])

print("\n\n########## RESUMEN ##########", flush=True)
for backend, (ok, errs) in resumen.items():
    estado = "FUNCIONA" if ok == len(CONSULTAS) else ("PARCIAL" if ok else "BLOQUEADO/FALLA")
    extra = f" | {errs[0]}" if errs and not ok else ""
    print(f"{backend:12s} {ok}/{len(CONSULTAS)} consultas con resultados -> {estado}{extra}", flush=True)
