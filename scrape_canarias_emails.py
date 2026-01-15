import asyncio
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright

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

# Más concurrencia = más rápido (si el sitio no bloquea). 6 suele ir bien en Actions.
CONCURRENCY = 6
DETAIL_TIMEOUT_MS = 35000
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
                await loc.click(timeout=3000)
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


async def detect_list_endpoint_and_template(page):
    """
    Detecta endpoint + post_data + headers que devuelva JSON con filas.
    """
    captured = []

    async def on_request(req):
        # ✅ FIX: post_data es PROPIEDAD, no función async
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

    await page.goto(URL, wait_until="domcontentloaded", timeout=LIST_TIMEOUT_MS)

    await page.get_by_label("Comunidad Autónoma").wait_for(timeout=LIST_TIMEOUT_MS)
    await page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")
    await page.wait_for_timeout(600)
    clicked = await try_click_search(page)
    dump_text("clicked_search.txt", f"clicked={clicked}")

    # esperar a que lance requests
    await page.wait_for_timeout(1500)
    t0 = time.time()
    while time.time() - t0 < 18:
        await page.wait_for_timeout(500)

    await page.screenshot(path=str(DEBUG_DIR / "after_filter.png"), full_page=True)
    dump_text("after_filter.html", await page.content())
    dump_json("requests.json", captured)

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

    # probamos muchos candidatos
    for req in candidates[:250]:
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
                dump_text("chosen_body_snippet.txt", body[:2500])
                return req, obj

        except Exception:
            continue

    dump_json("tested_candidates.json", tested)
    raise RuntimeError("No pude detectar el endpoint JSON del listado. Revisa debug/requests.json y debug/tested_candidates.json.")


async def fetch_all_centers(page, req_template, first_payload):
    """
    Pagina hasta el final usando recordsFiltered/recordsTotal.
    Si el backend ignora length y siempre devuelve 10, adaptamos el avance.
    """
    url = req_template["url"]
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": req_template["headers"].get("content-type", "application/x-www-form-urlencoded; charset=UTF-8"),
        "x-requested-with": "XMLHttpRequest",
        "referer": URL,
        "origin": BASE,
    }
    post_template = req_template.get("post_data", "") or ""

    all_rows = rows_from_payload(first_payload)

    total = None
    if isinstance(first_payload, dict):
        total = first_payload.get("recordsFiltered") or first_payload.get("recordsTotal")

    # tamaño real devuelto (siempre 10 en tu caso actual)
    page_size_real = max(1, len(rows_from_payload(first_payload)))

    # pedimos mucho, aunque nos devuelva 10
    requested_length = 500

    # draw (si existe)
    draw = None
    m = re.search(r"(?:^|&)draw=([^&]+)", post_template)
    if m:
        try:
            draw = int(m.group(1))
        except Exception:
            draw = None

    is_dt = all(k in post_template for k in ("start=", "length=")) and req_template["method"] == "POST"
    if not is_dt:
        return all_rows

    start = 0
    seen_hashes = set()

    def fingerprint(rows):
        return hash("|".join(str(r[0]) if isinstance(r, list) and r else str(r) for r in rows[:10]))

    seen_hashes.add(fingerprint(all_rows))

    while True:
        start += page_size_real  # avanzamos por lo que realmente devuelve

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
        if fp in seen_hashes:
            # backend ignora start y repite página -> param start no es el correcto
            dump_text("pagination_loop_detected.txt", f"Loop detectado en start={start}. Revisa chosen_body_snippet.txt")
            break
        seen_hashes.add(fp)

        all_rows.extend(rows)

        if total is None and isinstance(obj, dict):
            total = obj.get("recordsFiltered") or obj.get("recordsTotal")

        page_size_real = max(1, len(rows))

        if total is not None and len(all_rows) >= int(total):
            break

        await asyncio.sleep(0.12)

    dump_text("list_total_rows.txt", str(len(all_rows)))
    dump_text("records_total.txt", str(total) if total is not None else "unknown")
    return all_rows


def parse_centers(rows):
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
    emails = set()
    page = await context.new_page()

    async def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            if resp.status != 200:
                return
            txt = await resp.text()
            if len(txt) > 2_000_000:
                return
            emails.update(extract_emails(txt))
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(ficha_url, wait_until="domcontentloaded", timeout=DETAIL_TIMEOUT_MS)
        await page.wait_for_timeout(1400)

        # mailto
        try:
            links = page.locator("a[href^='mailto:']")
            cnt = await links.count()
            for i in range(min(cnt, 50)):
                href = (await links.nth(i).get_attribute("href")) or ""
                href = href.replace("mailto:", "").split("?")[0].strip()
                if EMAIL_RE.fullmatch(href):
                    emails.add(href)
        except Exception:
            pass

        # texto + html
        try:
            emails.update(extract_emails(await page.inner_text("body")))
        except Exception:
            pass
        try:
            emails.update(extract_emails(await page.content()))
        except Exception:
            pass

        email = sorted(emails)[0] if emails else ""

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

        req_template, first_payload = await detect_list_endpoint_and_template(page)

        rows = await fetch_all_centers(page, req_template, first_payload)
        centers = parse_centers(rows)

        dump_text("centers_count.txt", str(len(centers)))
        dump_text("centers_preview.txt", "\n".join([f"{c} | {n} | {u}" for c, n, u in centers[:200]]))

        sem = asyncio.Semaphore(CONCURRENCY)

        async def worker(i, c, n, u):
            async with sem:
                sample = i in (0, 1, 2)
                email = await extract_email_from_detail(context, c, u, sample_dump=sample)
                return (c, n, email)

        tasks = [worker(i, c, n, u) for i, (c, n, u) in enumerate(centers)]
        results = []

        # chunks para no petar memoria
        for start in range(0, len(tasks), 120):
            chunk = tasks[start:start + 120]
            results.extend(await asyncio.gather(*chunk))
            await asyncio.sleep(0.2)

        await browser.close()

    # CSV excel ES: ; + BOM
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(results)

    print(f"✅ Terminado. Centros: {len(results)} | CSV: {OUT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())
