import re
import csv
import json
import time
from pathlib import Path
from urllib.parse import urljoin

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


def is_json_like_datatable(obj) -> bool:
    # Aceptamos varios formatos
    if isinstance(obj, dict):
        if "data" in obj and isinstance(obj["data"], list):
            return True
        if "recordsTotal" in obj or "recordsFiltered" in obj:
            return True
        # otros nombres frecuentes
        for k in ["items", "results", "content"]:
            if k in obj and isinstance(obj[k], list):
                return True
    if isinstance(obj, list):
        return len(obj) > 0
    return False


def extract_rows(obj):
    # Normaliza a lista de filas
    if isinstance(obj, dict):
        if isinstance(obj.get("data"), list):
            return obj["data"]
        for k in ["items", "results", "content"]:
            if isinstance(obj.get(k), list):
                return obj[k]
    if isinstance(obj, list):
        return obj
    return []


def replace_param(postdata: str, key: str, value: str) -> str:
    if f"{key}=" not in postdata:
        return postdata + ("" if postdata == "" or postdata.endswith("&") else "&") + f"{key}={value}"
    return re.sub(rf"({re.escape(key)}=)[^&]*", rf"\1{value}", postdata)


def main():
    captured = []

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
            # Captura XHR/fetch y también POST
            if req.resource_type in ("xhr", "fetch") or req.method.lower() == "post":
                captured.append(
                    {
                        "url": req.url,
                        "method": req.method.upper(),
                        "resource_type": req.resource_type,
                        "headers": dict(req.headers),
                        "post_data": req.post_data or "",
                    }
                )

        page.on("request", on_request)

        page.goto(URL, wait_until="domcontentloaded", timeout=120000)

        # Filtrar Canarias
        try:
            page.get_by_label("Comunidad Autónoma").wait_for(timeout=120000)
        except PlaywrightTimeoutError:
            page.screenshot(path=str(DEBUG_DIR / "no_select.png"), full_page=True)
            dump(DEBUG_DIR / "no_select.html", page.content())
            raise RuntimeError("No aparece el select de Comunidad Autónoma en el runner.")

        page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")

        # Esperar a que JS dispare peticiones
        page.wait_for_timeout(2000)
        t0 = time.time()
        while (time.time() - t0) < 20:
            page.wait_for_timeout(500)

        page.screenshot(path=str(DEBUG_DIR / "after_filter.png"), full_page=True)
        dump(DEBUG_DIR / "after_filter.html", page.content())
        dump(DEBUG_DIR / "requests.json", json.dumps(captured, ensure_ascii=False, indent=2))

        if not captured:
            raise RuntimeError("No se capturaron requests. Mira debug/after_filter.*")

        # --- Elegir endpoint probando con page.request (MISMO CONTEXTO) ---
        def score(req):
            u = req["url"].lower()
            s = 0
            if "registrosfp.educacion.gob.es" in u:
                s += 3
            if "registroestatalentidadesformacion" in u:
                s += 2
            if any(k in u for k in ["buscar", "publico", "datatable", "list", "centro"]):
                s += 2
            if req["method"] == "POST":
                s += 1
            pd = req["post_data"] or ""
            if any(k in pd for k in ["draw=", "start=", "length="]):
                s += 3
            return s

        candidates = sorted(captured, key=score, reverse=True)

        chosen = None
        chosen_first_payload = None
        tested = []

        # Probar bastantes (a veces las “buenas” no están en top 25)
        for req in candidates[:80]:
            try:
                # Preparar fetch
                fetch_kwargs = {
                    "method": req["method"],
                    "headers": {
                        # copiamos headers relevantes, pero limpiamos algunos que dan problemas
                        "accept": req["headers"].get("accept", "application/json, text/plain, */*"),
                        "content-type": req["headers"].get("content-type", "application/x-www-form-urlencoded; charset=UTF-8"),
                        "x-requested-with": req["headers"].get("x-requested-with", "XMLHttpRequest"),
                        "referer": URL,
                        "origin": BASE,
                    },
                    "timeout": 60000,
                }

                if req["method"] == "POST":
                    fetch_kwargs["data"] = req["post_data"]

                resp = page.request.fetch(req["url"], **fetch_kwargs)
                tested.append({"url": req["url"], "status": resp.status})

                if resp.status != 200:
                    continue

                ct = (resp.headers.get("content-type") or "").lower()
                body = resp.text()

                # intentar JSON aunque el content-type no diga application/json
                try:
                    obj = json.loads(body)
                except Exception:
                    continue

                if is_json_like_datatable(obj):
                    chosen = req
                    chosen_first_payload = obj
                    break

            except Exception:
                continue

        dump(DEBUG_DIR / "tested_candidates.json", json.dumps(tested, ensure_ascii=False, indent=2))

        if not chosen:
            raise RuntimeError(
                "No pude identificar endpoint JSON incluso probando con page.request. "
                "Revisa debug/requests.json y debug/tested_candidates.json."
            )

        dump(DEBUG_DIR / "chosen_endpoint.txt", chosen["url"])

        # --- Descargar todas las filas (paginación si DataTables) ---
        all_rows = []
        all_rows.extend(extract_rows(chosen_first_payload))

        post_template = chosen.get("post_data", "") or ""
        is_dt = any(k in post_template for k in ["start=", "length=", "draw="])

        length = 50
        start = 0

        if is_dt:
            # Intentamos iterar hasta que no haya más filas
            while True:
                start += length
                postdata = post_template
                postdata = replace_param(postdata, "start", str(start))
                postdata = replace_param(postdata, "length", str(length))

                resp = page.request.fetch(
                    chosen["url"],
                    method="POST",
                    headers={
                        "accept": "application/json, text/plain, */*",
                        "content-type": chosen["headers"].get("content-type", "application/x-www-form-urlencoded; charset=UTF-8"),
                        "x-requested-with": "XMLHttpRequest",
                        "referer": URL,
                        "origin": BASE,
                    },
                    data=postdata,
                    timeout=60000,
                )
                if resp.status != 200:
                    break

                try:
                    obj = json.loads(resp.text())
                except Exception:
                    break

                rows = extract_rows(obj)
                if not rows:
                    break

                all_rows.extend(rows)
                if len(rows) < length:
                    break

                time.sleep(0.15)

        if not all_rows:
            raise RuntimeError("Endpoint encontrado pero no devolvió filas útiles.")

        # --- Parsear centros ---
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

                if not ficha_url:
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

        dump(DEBUG_DIR / "centros_detectados.txt", "\n".join([f"{c} | {n} | {u}" for c, n, u in centros[:400]]))

        # --- Visitar ficha y extraer email (también con page.request) ---
        out = []
        for codigo, nombre, ficha_url in centros:
            email = ""
            if ficha_url:
                try:
                    resp = page.request.get(
                        ficha_url,
                        headers={"referer": URL, "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                        timeout=60000,
                    )
                    if resp.status == 200:
                        email = extract_first_email(resp.text())
                except Exception:
                    email = ""
            out.append([codigo, nombre, email])
            time.sleep(0.12)

        browser.close()

    # Guardar CSV
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(out)

    print(f"✅ Terminado. Centros: {len(out)} | CSV: {OUT_CSV}")


if __name__ == "__main__":
    main()
