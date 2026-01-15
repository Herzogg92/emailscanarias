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

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

# Email normal
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Email ofuscado típico: "nombre (at) dominio (dot) com" / "arroba" / "[at]" etc.
OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*(?:\(|\[)?\s*(?:at|arroba)\s*(?:\)|\])?\s*([a-zA-Z0-9.\-]+)\s*(?:\(|\[)?\s*(?:dot|punto)\s*(?:\)|\])?\s*([a-zA-Z]{2,})",
    re.IGNORECASE
)


def dump_text(name: str, content: str):
    (DEBUG_DIR / name).write_text(content, encoding="utf-8")


def dump_json(name: str, obj):
    (DEBUG_DIR / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_emails_from_text(text: str) -> list[str]:
    if not text:
        return []
    found = set(m.group(0) for m in EMAIL_RE.finditer(text))

    # ofuscados
    for m in OBFUSCATED_RE.finditer(text):
        found.add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")

    return sorted(found)


def extract_emails_from_page(page) -> list[str]:
    # 1) mailto:
    emails = set()
    try:
        links = page.locator("a[href^='mailto:']")
        for i in range(min(links.count(), 20)):
            href = links.nth(i).get_attribute("href") or ""
            href = href.replace("mailto:", "").split("?")[0].strip()
            if EMAIL_RE.fullmatch(href):
                emails.add(href)
    except Exception:
        pass

    # 2) texto visible
    try:
        visible = page.inner_text("body")
        for e in extract_emails_from_text(visible):
            emails.add(e)
    except Exception:
        pass

    # 3) HTML completo (por si está oculto)
    try:
        html = page.content()
        for e in extract_emails_from_text(html):
            emails.add(e)
    except Exception:
        pass

    return sorted(emails)


def is_json_with_rows(obj) -> bool:
    if isinstance(obj, dict):
        if "data" in obj and isinstance(obj["data"], list):
            return True
        for k in ["items", "results", "content"]:
            if k in obj and isinstance(obj[k], list):
                return True
    if isinstance(obj, list):
        return True
    return False


def extract_rows(obj):
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


def try_click_search(page):
    candidates = [
        "button:has-text('Buscar')",
        "button:has-text('Filtrar')",
        "input[type='submit']",
        "button:has-text('Aplicar')",
    ]
    for sel in candidates:
        btn = page.locator(sel).first
        try:
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def main():
    captured_requests = []

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
            if req.resource_type in ("xhr", "fetch") or req.method.lower() == "post":
                captured_requests.append(
                    {
                        "url": req.url,
                        "method": req.method.upper(),
                        "resource_type": req.resource_type,
                        "headers": dict(req.headers),
                        "post_data": req.post_data or "",
                    }
                )

        page.on("request", on_request)

        # --- abrir buscador y filtrar canarias ---
        page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        page.get_by_label("Comunidad Autónoma").wait_for(timeout=120000)
        page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")
        page.wait_for_timeout(800)
        try_click_search(page)

        # esperar requests
        page.wait_for_timeout(1500)
        t0 = time.time()
        while (time.time() - t0) < 15:
            page.wait_for_timeout(500)

        page.screenshot(path=str(DEBUG_DIR / "after_filter.png"), full_page=True)
        dump_text("after_filter.html", page.content())
        dump_json("requests.json", captured_requests)

        if not captured_requests:
            raise RuntimeError("No se capturaron requests del listado (ver debug/after_filter.*).")

        # --- detectar endpoint JSON con page.request ---
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

        candidates = sorted(captured_requests, key=score, reverse=True)

        tested = []
        chosen = None
        chosen_payload = None

        for req in candidates[:150]:
            try:
                fetch_kwargs = {
                    "method": req["method"],
                    "headers": {
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
                body = resp.text()
                tested.append({"url": req["url"], "status": resp.status})

                if resp.status != 200:
                    continue

                try:
                    obj = json.loads(body)
                except Exception:
                    continue

                if is_json_with_rows(obj):
                    chosen = req
                    chosen_payload = obj
                    break
            except Exception:
                continue

        dump_json("tested_candidates.json", tested)

        if not chosen:
            raise RuntimeError("No pude detectar endpoint JSON del listado. Revisa debug/requests.json.")

        dump_text("chosen_endpoint.txt", chosen["url"])

        # --- paginar listado para sacar TODOS los centros ---
        all_rows = extract_rows(chosen_payload)
        post_template = chosen.get("post_data", "") or ""
        is_dt = any(k in post_template for k in ["start=", "length=", "draw="])

        length = 50
        start = 0

        if is_dt:
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

        # --- parse centros: codigo + nombre y URL ficha (si no hay, se construye) ---
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

                # buscar href en acciones/código
                href = re.search(r'href="([^"]+)"', raw0)
                if href:
                    ficha_url = urljoin(BASE, href.group(1))
                if not ficha_url:
                    href2 = re.search(r'href="([^"]+)"', str(row[-1]))
                    if href2:
                        ficha_url = urljoin(BASE, href2.group(1))

            codigo = re.sub(r"\s+", " ", codigo).strip()
            nombre = re.sub(r"\s+", " ", nombre).strip()

            if codigo and nombre:
                if not ficha_url and codigo.isdigit():
                    # URL estándar, pero OJO: puede que la ficha pública sea otra.
                    ficha_url = f"{BASE}/registroestatalentidadesformacion/centro/{codigo}"
                centros.append((codigo, nombre, ficha_url))

        dump_text("centros_count.txt", str(len(centros)))
        dump_text("centros_detectados.txt", "\n".join([f"{c} | {n} | {u}" for c, n, u in centros[:300]]))

        # --- abrir fichas EN NAVEGADOR y extraer email ---
        # Importante: así se ejecuta el JS de la ficha si carga el email dinámicamente.
        out = []
        failed_samples = 0

        for idx, (codigo, nombre, ficha_url) in enumerate(centros, start=1):
            email = ""

            # Abrimos ficha en una pestaña aparte para no romper el listado
            detail = context.new_page()
            try:
                detail.goto(ficha_url, wait_until="domcontentloaded", timeout=120000)
                # esperar un poco a JS
                detail.wait_for_timeout(1200)

                emails = extract_emails_from_page(detail)
                if emails:
                    email = emails[0]  # si hay varios, nos quedamos con el primero

                # si no hay email, guardamos muestra de 3 fichas para ver estructura
                if not email and failed_samples < 3:
                    detail.screenshot(path=str(DEBUG_DIR / f"no_email_{failed_samples+1}_{codigo}.png"), full_page=True)
                    dump_text(f"no_email_{failed_samples+1}_{codigo}.html", detail.content())
                    failed_samples += 1

            except Exception:
                # si falla la ficha, dejamos vacío
                pass
            finally:
                detail.close()

            out.append([codigo, nombre, email])
            # rate limit suave
            time.sleep(0.12)

            if idx % 50 == 0:
                print(f"Procesados {idx}/{len(centros)}...")

        browser.close()

    # CSV “Excel friendly”: ; + BOM
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(out)

    print(f"✅ Terminado. Centros: {len(out)} | CSV: {OUT_CSV}")


if __name__ == "__main__":
    main()
