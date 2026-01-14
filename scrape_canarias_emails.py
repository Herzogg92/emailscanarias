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


def looks_like_datatables_json(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    # DataTables típico
    return ("data" in obj and isinstance(obj["data"], list)) or ("recordsTotal" in obj) or ("recordsFiltered" in obj)


def main():
    captured = []  # lista de dicts con request info
    cookies = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            locale="es-ES",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()
        page.set_default_timeout(120000)
        page.set_default_navigation_timeout(120000)

        def on_request(req):
            # capturamos XHR/fetch + también POST (aunque venga como document)
            rtype = req.resource_type
            if rtype in ("xhr", "fetch") or req.method.lower() == "post":
                captured.append(
                    {
                        "url": req.url,
                        "method": req.method,
                        "resource_type": rtype,
                        "headers": dict(req.headers),
                        "post_data": req.post_data or "",
                    }
                )

        page.on("request", on_request)

        page.goto(URL, wait_until="domcontentloaded", timeout=120000)

        # Esperar el filtro y aplicarlo
        try:
            page.get_by_label("Comunidad Autónoma").wait_for(timeout=120000)
        except PlaywrightTimeoutError:
            page.screenshot(path=str(DEBUG_DIR / "no_select.png"), full_page=True)
            dump(DEBUG_DIR / "no_select.html", page.content())
            raise RuntimeError("No aparece el select de Comunidad Autónoma en el runner.")

        page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")

        # Esperar un rato a que JS dispare peticiones
        page.wait_for_timeout(2000)
        t0 = time.time()
        while (time.time() - t0) < 25:
            page.wait_for_timeout(500)

        # Guardar evidencias
        page.screenshot(path=str(DEBUG_DIR / "after_filter.png"), full_page=True)
        dump(DEBUG_DIR / "after_filter.html", page.content())
        dump(DEBUG_DIR / "requests.json", json.dumps(captured, ensure_ascii=False, indent=2))

        cookies = context.cookies()
        browser.close()

    # Si no hubo requests, no hay nada que hacer: el runner no está ejecutando el JS esperado
    if not captured:
        raise RuntimeError("No se capturó ninguna request XHR/fetch/POST tras filtrar. Revisa debug/after_filter.*")

    # 2) Probar candidatos: buscamos requests que parezcan devolver JSON tipo DataTables
    sess = requests.Session()
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

    best = None  # guardará (req_dict, parsed_json)
    tested = []

    # prioriza urls del mismo host y que contengan palabras típicas
    def score(req):
        u = req["url"]
        s = 0
        if "registrosfp.educacion.gob.es" in u:
            s += 3
        if "registroestatalentidadesformacion" in u:
            s += 2
        if any(k in u.lower() for k in ["buscar", "publico", "datatable", "list", "centro"]):
            s += 2
        if req["method"].upper() == "POST":
            s += 1
        if ("draw=" in (req["post_data"] or "")) or ("start=" in (req["post_data"] or "")) or ("length=" in (req["post_data"] or "")):
            s += 3
        return s

    candidates = sorted(captured, key=score, reverse=True)

    for req in candidates[:25]:  # probamos top 25
        try:
            headers = {
                "User-Agent": req["headers"].get("user-agent", "Mozilla/5.0"),
                "Accept": "application/json, text/plain, */*",
                "Referer": URL,
                "Origin": BASE,
                "X-Requested-With": "XMLHttpRequest",
            }

            if req["method"].upper() == "POST":
                ct = req["headers"].get("content-type", "application/x-www-form-urlencoded; charset=UTF-8")
                headers["Content-Type"] = ct
                resp = sess.post(req["url"], headers=headers, data=req["post_data"], timeout=60)
            else:
                resp = sess.get(req["url"], headers=headers, timeout=60)

            tested.append({"url": req["url"], "status": resp.status_code})
            if resp.status_code != 200:
                continue

            # ¿JSON?
            try:
                obj = resp.json()
            except Exception:
                continue

            if looks_like_datatables_json(obj):
                best = (req, obj)
                break
        except Exception:
            continue

    dump(DEBUG_DIR / "tested_candidates.json", json.dumps(tested, ensure_ascii=False, indent=2))

    if not best:
        raise RuntimeError(
            "No pude identificar el endpoint JSON. Revisa debug/requests.json y debug/tested_candidates.json "
            "(ahí verás qué URLs se llamaron)."
        )

    endpoint_req, first_payload = best
    endpoint_url = endpoint_req["url"]
    postdata_template = endpoint_req["post_data"] or ""
    dump(DEBUG_DIR / "chosen_endpoint.txt", endpoint_url)

    # 3) Paginación DataTables (si aplica)
    def replace_param(postdata: str, key: str, value: str) -> str:
        if f"{key}=" not in postdata:
            return postdata + ("" if postdata.endswith("&") or postdata == "" else "&") + f"{key}={value}"
        return re.sub(rf"({re.escape(key)}=)[^&]*", rf"\1{value}", postdata)

    all_rows = []
    length = 50
    start = 0

    # Si no es DataTables clásico, igual first_payload trae todos. Lo intentamos de forma segura:
    def get_rows(payload):
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return []

    rows0 = get_rows(first_payload)
    all_rows.extend(rows0)

    # Si hay pinta DataTables, iteramos
    # Condición: existe postdata con start/length o draw
    is_dt = ("start=" in postdata_template) or ("length=" in postdata_template) or ("draw=" in postdata_template)

    if is_dt:
        # ya añadimos la primera página; seguimos
        while True:
            start += length
            postdata = postdata_template
            postdata = replace_param(postdata, "start", str(start))
            postdata = replace_param(postdata, "length", str(length))

            headers = {
                "User-Agent": endpoint_req["headers"].get("user-agent", "Mozilla/5.0"),
                "Accept": "application/json, text/plain, */*",
                "Referer": URL,
                "Origin": BASE,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": endpoint_req["headers"].get("content-type", "application/x-www-form-urlencoded; charset=UTF-8"),
            }

            resp = sess.post(endpoint_url, headers=headers, data=postdata, timeout=60)
            if resp.status_code != 200:
                break

            try:
                payload = resp.json()
            except Exception:
                break

            rows = get_rows(payload)
            if not rows:
                break

            all_rows.extend(rows)

            if len(rows) < length:
                break

            time.sleep(0.2)

    if not all_rows:
        raise RuntimeError("El endpoint detectado devolvió JSON pero sin filas en 'data'.")

    # 4) Parseo tolerante (list o dict) y construir ficha_url
    centros = []
    for row in all_rows:
        codigo = ""
        nombre = ""
        ficha_url = ""

        if isinstance(row, list) and len(row) >= 2:
            raw0 = str(row[0])
            raw1 = str(row[1])
            codigo = re.sub(r"<[^>]+>", " ", raw0).strip()
            nombre = re.sub(r"<[^>]+>", " ", raw1).strip()

            href = re.search(r'href="([^"]+)"', raw0)
            if href:
                ficha_url = urljoin(BASE, href.group(1))

            if not ficha_url and len(row) >= 3:
                href2 = re.search(r'href="([^"]+)"', str(row[-1]))
                if href2:
                    ficha_url = urljoin(BASE, href2.group(1))

        elif isinstance(row, dict):
            for k in ["codigo", "codigoCentro", "codCentro", "code"]:
                if k in row:
                    codigo = str(row[k]).strip()
                    break
            for k in ["nombre", "nombreCentro", "denominacion", "name"]:
                if k in row:
                    nombre = str(row[k]).strip()
                    break
            for k in ["url", "detalle", "detailUrl", "fichaUrl"]:
                if k in row:
                    ficha_url = urljoin(BASE, str(row[k]))
                    break

        codigo = re.sub(r"\s+", " ", codigo).strip()
        nombre = re.sub(r"\s+", " ", nombre).strip()

        if codigo and nombre:
            if not ficha_url and codigo.isdigit():
                ficha_url = f"{BASE}/registroestatalentidadesformacion/centro/{codigo}"
            centros.append((codigo, nombre, ficha_url))

    dump(DEBUG_DIR / "centros_detectados.txt", "\n".join([f"{c} | {n} | {u}" for c, n, u in centros[:300]]))

    # 5) Visitar fichas y extraer email
    out = []
    for codigo, nombre, ficha_url in centros:
        email = ""
        if ficha_url:
            try:
                rr = sess.get(ficha_url, headers={"User-Agent": "Mozilla/5.0", "Referer": URL}, timeout=60)
                if rr.status_code == 200:
                    email = extract_first_email(rr.text)
            except Exception:
                email = ""
        out.append([codigo, nombre, email])
        time.sleep(0.15)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(out)

    print(f"✅ Terminado. Centros: {len(out)} | CSV: {OUT_CSV}")


if __name__ == "__main__":
    main()
