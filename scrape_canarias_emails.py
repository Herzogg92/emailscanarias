import asyncio
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://registrosfp.educacion.gob.es/registroestatalentidadesformacion/buscarPublico"
BASE = "https://registrosfp.educacion.gob.es"
OUT_CSV = "emails_centros_canarias.csv"

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*(?:\(|\[)?\s*(?:at|arroba)\s*(?:\)|\])?\s*([a-zA-Z0-9.\-]+)\s*(?:\(|\[)?\s*(?:dot|punto)\s*(?:\)|\])?\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)

# Ajusta si quieres más/menos “agresivo”
CONCURRENCY = 6          # paralelismo de fichas
DETAIL_TIMEOUT_MS = 25000
LIST_TIMEOUT_MS = 120000


def dump_text(name: str, content: str):
    (DEBUG_DIR / name).write_text(content, encoding="utf-8")


def dump_json(name: str, obj):
    (DEBUG_DIR / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_emails(text: str) -> set[str]:
    found = set()
    if not text:
        return found
    for m in EMAIL_RE.finditer(text):
        found.add(m.group(0))
    for m in OBFUSCATED_RE.finditer(text):
        found.add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    return found


def replace_param(postdata: str, key: str, value: str) -> str:
    if f"{key}=" not in postdata:
        sep = "" if postdata == "" or postdata.endswith("&") else "&"
        return postdata + sep + f"{key}={value}"
    return re.sub(rf"({re.escape(key)}=)[^&]*", rf"\1{value}", postdata)


def is_dt_payload(obj) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("data"), list)


def rows_from_payload(obj):
    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        return obj["data"]
    if isinstance(obj, dict):
        for k in ("items", "results", "content"):
            if isinstance(obj.get(k), list):
                return obj[k]
    if isinstance(obj, list):
        return obj
    return []


async def try_click_search(page):
    candidates = [
        "button:has-text('Buscar')",
        "button:has-text('Filtrar')",
        "button:has-text('Aplicar')",
        "input[type='submit']",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=2000)
                await page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    return False


async def detect_list_endpoint_and_template(page):
    """
    Detecta endpoint + post_data + headers de la request que devuelve JSON con 'data'.
    """
    captured = []

    async def on_request(req):
        if req.resource_type in ("xhr", "fetch") or req.method.lower() == "post":
            captured.append(
                {
                    "url": req.url,
                    "method": req.method.upper(),
                    "resource_type": req.resource_type,
                    "headers": dict(req.headers),
                    "post_data": (await req.post_data()) or "",
                }
            )

    page.on("request", on_request)

    await page.goto(URL, wait_until="domcontentloaded", timeout=LIST_TIMEOUT_MS)

    # Filtrar Canarias
    await page.get_by_label("Comunidad Autónoma").wait_for(timeout=LIST_TIMEOUT_MS)
    await page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")
    await page.wait_for_timeout(600)
    clicked = await try_click_search(page)
    dump_text("clicked_search.txt", f"clicked={clicked}")

    # Esperar a que lance requests
    await page.wait_for_timeout(1500)
    t0 = time.time()
    while time.time() - t0 < 15:
        await page.wait_for_timeout(500)

    await page.screenshot(path=str(DEBUG_DIR / "after_filter.png"), full_page=True)
    dump_text("after_filter.html", await page.content())
    dump_json("requests.json", captured)

    # Scoring
    def score(req):
        u = req["url"].lower()
        s = 0
        if "registrosfp.educacion.gob.es" in u:
            s += 3
        if "registroestatalentidadesformacion" in u:
            s += 2
        if any(k in u for k in ("buscar", "publico", "datatable", "list", "centro")):
            s += 2
        if req["method"] == "POST":
            s += 1
        pd = req["post_data"] or ""
        if any(k in pd for k in ("draw=", "start=", "length=")):
            s += 3
        return s

    candidates = sorted(captured, key=score, reverse=True)
    tested = []

    for req in candidates[:200]:
        try:
            headers = {
                "accept": req["headers"].get("accept", "application/json, text/plain, */*"),
                "content-type": req["headers"].get("content-type", "application/x-www-form-urlencoded; charset=UTF-8"),
                "x-requested-with": req["headers"].get("x-requested-with", "XMLHttpRequest"),
                "referer": URL,
                "origin": BASE,
            }

            if req["method"] == "POST":
                resp = await page.request.fetch(req["url"], method="POST", headers=headers, data=req["post_data"], timeout=60000)
            else:
                resp = await page.request.fetch(req["url"], method=req["method"], headers=headers, timeout=60000)

            tested.append({"url": req["url"], "status": resp.status})

            if resp.status != 200:
                continue

            body = await resp.text()
            try:
                obj = json.loads(body)
            except Exception:
                continue

            if is_dt_payload(obj) or len(rows_from_payload(obj)) > 0:
                dump_json("tested_candidates.json", tested)
                dump_text("chosen_endpoint.txt", req["url"])
                dump_text("chosen_body_snippet.txt", body[:2000])
                return req, obj

        except Exception:
            continue

    dump_json("tested_candidates.json", tested)
    raise RuntimeError("No pude detectar el endpoint JSON del listado. Revisa debug/requests.json y debug/tested_candidates.json.")


async def fetch_all_centers(page, req_template, first_payload):
    """
    Pagina hasta el final usando recordsFiltered/recordsTotal.
    Si el servidor ignora length, se adapta al tamaño real que devuelva.
    """
    url = req_template["url"]
    method = req_template["method"]
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": req_template["headers"].get("content-type", "application/x-www-form-urlencoded; charset=UTF-8"),
        "x-requested-with": "XMLHttpRequest",
        "referer": URL,
        "origin": BASE,
    }
    post_template = req_template.get("post_data", "") or ""

    all_rows = rows_from_payload(first_payload)

    # Determinar total si es DataTables
    total = None
    if isinstance(first_payload, dict):
        total = first_payload.get("recordsFiltered") or first_payload.get("recordsTotal")

    # Longitud real que devuelve el server (muchas veces 10)
    page_size_real = max(1, len(rows_from_payload(first_payload)))

    # Intento de pedir mucho (si el server lo ignora, no pasa nada)
    requested_length = 500

    # draw si existe
    draw = None
    m = re.search(r"(?:^|&)draw=([^&]+)", post_template)
    if m:
        try:
            draw = int(m.group(1))
        except Exception:
            draw = None

    start = 0

    # Si no hay plantilla DataTables, devolvemos lo que tengamos
    is_dt = any(k in post_template for k in ("start=", "length=", "draw=")) and method == "POST"
    if not is_dt:
        return all_rows

    # Loop hasta completar total o hasta que no haya más filas nuevas
    seen_fingerprints = set()
    def fingerprint(rows):
        # huella ligera para evitar bucles
        return hash("|".join(str(r[0]) if isinstance(r, list) and r else str(r) for r in rows[:10]))

    # Guardamos huella de la primera
    seen_fingerprints.add(fingerprint(all_rows))

    while True:
        start += page_size_real  # nos movemos por lo que realmente devuelve

        postdata = post_template
        postdata = replace_param(postdata, "start", str(start))
        postdata = replace_param(postdata, "length", str(requested_length))

        if draw is not None:
            draw += 1
            postdata = replace_param(postdata, "draw", str(draw))

        resp = await page.request.fetch(url, method="POST", headers=headers, data=postdata, timeout=60000)
        if resp.status != 200:
            break

        body = await resp.text()
        try:
            obj = json.loads(body)
        except Exception:
            break

        rows = rows_from_payload(obj)
        if not rows:
            break

        fp = fingerprint(rows)
        if fp in seen_fingerprints:
            # Evita loop infinito si el backend ignora start y siempre devuelve lo mismo
            break
        seen_fingerprints.add(fp)

        all_rows.extend(rows)

        # Actualizar total si viene
        if total is None and isinstance(obj, dict):
            total = obj.get("recordsFiltered") or obj.get("recordsTotal")

        # Ajustar tamaño real (si el server cambia)
        page_size_real = max(1, len(rows))

        # Condición de fin por total
        if total is not None and len(all_rows) >= int(total):
            break

        await asyncio.sleep(0.12)

    dump_text("list_total_rows.txt", str(len(all_rows)))
    dump_text("records_total.txt", str(total) if total is not None else "unknown")
    return all_rows


def parse_centers(rows):
    """
    Convierte filas del listado en (codigo, nombre, ficha_url).
    """
    centers = []
    for row in rows:
        codigo = ""
        nombre = ""
        ficha_url = ""

        if isinstance(row, list) and len(row) >= 2:
            raw0 = str(row[0])
            raw1 = str(row[1])
            codigo = re.sub(r"<[^>]+>", " ", raw0).strip()
            nombre = re.sub(r"<[^>]+>", " ", raw1).strip()

            # href en código o en acciones
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
                ficha_url = f"{BASE}/registroestatalentidadesformacion/centro/{codigo}"
            centers.append((codigo, nombre, ficha_url))

    return centers


async def extract_email_from_detail(context, codigo, ficha_url, sample_dump=False):
    """
    Abre la ficha como página real (ejecuta JS) y escanea:
    - mailto:
    - texto visible
    - HTML
    - TODAS las respuestas XHR/fetch (muchas veces el correo viene por JSON)
    """
    emails = set()
    page = await context.new_page()

    async def on_response(resp):
        try:
            rt = resp.request.resource_type
            if rt not in ("xhr", "fetch"):
                return
            if resp.status != 200:
                return
            # Limitar tamaño para eficiencia
            text = await resp.text()
            if len(text) > 2_000_000:
                return
            emails.update(extract_emails(text))
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(ficha_url, wait_until="domcontentloaded", timeout=DETAIL_TIMEOUT_MS)
        # Espera corta para que cargue XHR internos
        await page.wait_for_timeout(1200)

        # mailto
        try:
            links = page.locator("a[href^='mailto:']")
            cnt = await links.count()
            for i in range(min(cnt, 30)):
                href = (await links.nth(i).get_attribute("href")) or ""
                href = href.replace("mailto:", "").split("?")[0].strip()
                if EMAIL_RE.fullmatch(href):
                    emails.add(href)
        except Exception:
            pass

        # texto visible + HTML
        try:
            emails.update(extract_emails(await page.inner_text("body")))
        except Exception:
            pass
        try:
            emails.update(extract_emails(await page.content()))
        except Exception:
            pass

        email = sorted(emails)[0] if emails else ""

        # Dump de muestra si no saca email (para verificar estructura)
        if sample_dump and not email:
            await page.screenshot(path=str(DEBUG_DIR / f"no_email_{codigo}.png"), full_page=True)
            dump_text(f"no_email_{codigo}.html", await page.content())

        return email

    except Exception:
        return ""
    finally:
        await page.close()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            locale="es-ES",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()

        # 1) Endpoint + primera página
        req_template, first_payload = await detect_list_endpoint_and_template(page)

        # 2) Traer TODOS los centros (no solo 10)
        rows = await fetch_all_centers(page, req_template, first_payload)
        centers = parse_centers(rows)

        dump_text("centers_count.txt", str(len(centers)))
        dump_text("centers_preview.txt", "\n".join([f"{c} | {n} | {u}" for c, n, u in centers[:200]]))

        if len(centers) <= 10:
            # Señal clara de que el backend está ignorando start o devolviendo siempre lo mismo
            dump_text(
                "warning.txt",
                "Solo se detectaron <=10 centros. Revisa debug/requests.json, chosen_endpoint.txt y chosen_body_snippet.txt."
            )

        # 3) Emails en paralelo
        sem = asyncio.Semaphore(CONCURRENCY)

        async def worker(i, codigo, nombre, ficha_url):
            async with sem:
                # guarda 3 dumps de muestra si no hay email
                sample = i in (0, 1, 2)
                email = await extract_email_from_detail(context, codigo, ficha_url, sample_dump=sample)
                return (codigo, nombre, email)

        tasks = [worker(i, c, n, u) for i, (c, n, u) in enumerate(centers)]
        results = []
        for chunk_start in range(0, len(tasks), 100):
            chunk = tasks[chunk_start:chunk_start + 100]
            results.extend(await asyncio.gather(*chunk))
            # pequeña pausa para no saturar
            await asyncio.sleep(0.2)

        await browser.close()

    # 4) CSV Excel-friendly: ; + UTF-8 BOM
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(results)

    print(f"✅ Terminado. Centros: {len(results)} | CSV: {OUT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())
