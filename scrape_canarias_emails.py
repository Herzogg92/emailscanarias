import re
import csv
import json
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://registrosfp.educacion.gob.es/registroestatalentidadesformacion/buscarPublico"
BASE = "https://registrosfp.educacion.gob.es"
OUT_CSV = "emails_centros_canarias.csv"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)


def dump(path: Path, content: str):
    path.write_text(content, encoding="utf-8")


def extract_first_email(text: str) -> str:
    m = EMAIL_RE.search(text or "")
    return m.group(0) if m else ""


def main():
    # 1) Abrimos la página SOLO para capturar:
    #    - URL del endpoint XHR del listado
    #    - headers/cookies necesarios
    endpoint_url = None
    captured_request = None
    captured_headers = None
    captured_postdata = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            locale="es-ES",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(120000)
        page.set_default_navigation_timeout(120000)

        # Listener para detectar la llamada XHR que trae los datos del listado
        def on_request(req):
            nonlocal endpoint_url, captured_request, captured_headers, captured_postdata
            if req.method.lower() in ("post", "get"):
                u = req.url
                # heurística: DataTables suele pedir JSON con draw/start/length
                # y muchas veces la ruta contiene buscarPublico o datatable o entidades
                if "registroestatalentidadesformacion" in u and ("buscar" in u or "datatable" in u or "publico" in u):
                    post = req.post_data or ""
                    if ("draw=" in post) or ("start=" in post) or ("length=" in post):
                        endpoint_url = u
                        captured_request = req
                        captured_headers = req.headers
                        captured_postdata = post

        page.on("request", on_request)

        page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        # Aplicamos el filtro para forzar la llamada XHR
        page.get_by_label("Comunidad Autónoma").wait_for(timeout=120000)
        page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")

        # Esperamos a que se dispare la XHR
        t0 = time.time()
        while endpoint_url is None and (time.time() - t0) < 30:
            page.wait_for_timeout(250)

        # Guardamos debug por si no encontramos endpoint
        dump(DEBUG_DIR / "page_url.txt", page.url)
        dump(DEBUG_DIR / "endpoint_detected.txt", str(endpoint_url))

        # cookies de la sesión para requests
        cookies = context.cookies()
        browser.close()

    if not endpoint_url:
        raise RuntimeError(
            "No pude detectar el endpoint XHR del listado. Revisa debug/endpoint_detected.txt "
            "y considera ejecutar local para ver si cambia."
        )

    # 2) Usamos requests para paginar por API (mucho más estable que el DOM)
    sess = requests.Session()

    # cargar cookies de Playwright en requests
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

    # headers base
    headers = {
        "User-Agent": captured_headers.get("user-agent", "Mozilla/5.0"),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": captured_headers.get("content-type", "application/x-www-form-urlencoded; charset=UTF-8"),
        "Origin": BASE,
        "Referer": URL,
        "X-Requested-With": "XMLHttpRequest",
    }

    # DataTables: vamos a iterar start=0, length=50 (o lo que acepte)
    # Partimos del postdata capturado y solo cambiamos start/length.
    def replace_param(postdata: str, key: str, value: str) -> str:
        # reemplaza key=... en x-www-form-urlencoded
        if f"{key}=" not in postdata:
            # si no existe, lo añadimos
            return postdata + f"&{key}={value}"
        return re.sub(rf"({re.escape(key)}=)[^&]*", rf"\1{value}", postdata)

    length = 50
    start = 0
    all_rows = []

    while True:
        postdata = captured_postdata or ""
        postdata = replace_param(postdata, "start", str(start))
        postdata = replace_param(postdata, "length", str(length))

        r = sess.post(endpoint_url, headers=headers, data=postdata, timeout=60)
        r.raise_for_status()

        try:
            payload = r.json()
        except json.JSONDecodeError:
            dump(DEBUG_DIR / f"bad_json_start_{start}.txt", r.text[:5000])
            raise RuntimeError("El endpoint no devolvió JSON. Mira debug/bad_json_*")

        # DataTables típicamente devuelve: {"data":[...], "recordsTotal":..., "recordsFiltered":...}
        rows = payload.get("data", [])
        if not rows:
            break

        all_rows.extend(rows)

        # si ya no hay más
        if len(rows) < length:
            break

        start += length
        time.sleep(0.2)

    # 3) Parseo de filas: intentamos extraer código/nombre/url ficha desde "data"
    # Como no conocemos exacto el formato, lo hacemos tolerante:
    # - Si cada fila es lista: [codigo, nombre, ... , acciones_html]
    # - Si es dict: buscamos keys parecidas
    centros = []
    for row in all_rows:
        codigo = ""
        nombre = ""
        ficha_url = ""

        if isinstance(row, list) and len(row) >= 2:
            codigo = re.sub(r"\s+", " ", str(row[0])).strip()
            nombre = re.sub(r"\s+", " ", str(row[1])).strip()

            # a veces el código viene con HTML <a href="...">
            href = re.search(r'href="([^"]+)"', str(row[0]))
            if href:
                ficha_url = urljoin(BASE, href.group(1))

            # o el lápiz está en la última columna con href
            if not ficha_url and len(row) >= 3:
                href2 = re.search(r'href="([^"]+)"', str(row[-1]))
                if href2:
                    ficha_url = urljoin(BASE, href2.group(1))

        elif isinstance(row, dict):
            # posibles claves
            for k in ["codigo", "codigoCentro", "codCentro", "code"]:
                if k in row:
                    codigo = str(row[k]).strip()
                    break
            for k in ["nombre", "nombreCentro", "denominacion", "name"]:
                if k in row:
                    nombre = str(row[k]).strip()
                    break
            # posibles URL
            for k in ["url", "detalle", "detailUrl", "fichaUrl"]:
                if k in row:
                    ficha_url = urljoin(BASE, str(row[k]))
                    break

        if codigo and nombre:
            if not ficha_url and codigo.isdigit():
                ficha_url = f"{BASE}/registroestatalentidadesformacion/centro/{codigo}"
            centros.append((codigo, nombre, ficha_url))

    dump(DEBUG_DIR / "centros_detectados.txt", "\n".join([f"{c} | {n} | {u}" for c, n, u in centros[:200]]))

    # 4) Visitar cada ficha (requests) y extraer email por regex
    out = []
    for codigo, nombre, ficha_url in centros:
        email = ""
        if ficha_url:
            try:
                rr = sess.get(ficha_url, headers={"User-Agent": headers["User-Agent"], "Referer": URL}, timeout=60)
                rr.raise_for_status()
                email = extract_first_email(rr.text)
            except Exception:
                email = ""
        out.append([codigo, nombre, email])

        time.sleep(0.15)

    # 5) Guardar CSV
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(out)

    print(f"✅ Terminado. Centros: {len(out)} | CSV: {OUT_CSV}")


if __name__ == "__main__":
    main()
