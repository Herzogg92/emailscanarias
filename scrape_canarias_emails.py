import re
import csv
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL_BUSCADOR = "https://registrosfp.educacion.gob.es/registroestatalentidadesformacion/buscarPublico"
URL_FICHA = "https://registrosfp.educacion.gob.es/registroestatalentidadesformacion/centro/{}"
OUT_CSV = "emails_centros_canarias.csv"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*(?:\(|\[)?\s*(?:at|arroba)\s*(?:\)|\])?\s*([a-zA-Z0-9.\-]+)\s*(?:\(|\[)?\s*(?:dot|punto)\s*(?:\)|\])?\s*([a-zA-Z]{2,})",
    re.IGNORECASE
)

def extract_email(text: str) -> str:
    if not text:
        return ""
    m = EMAIL_RE.search(text)
    if m:
        return m.group(0)
    m2 = OBFUSCATED_RE.search(text)
    if m2:
        return f"{m2.group(1)}@{m2.group(2)}.{m2.group(3)}"
    return ""

def try_click_search(page):
    for sel in [
        "button:has-text('Buscar')",
        "button:has-text('Filtrar')",
        "button:has-text('Aplicar')",
        "input[type='submit']",
    ]:
        btn = page.locator(sel).first
        try:
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
    return False

def accept_cookies_if_any(page):
    for sel in [
        "button:has-text('Aceptar')",
        "button:has-text('Aceptar todo')",
        "button:has-text('Aceptar todas')",
        "button:has-text('Aceptar cookies')",
        "a:has-text('Aceptar')",
    ]:
        b = page.locator(sel).first
        try:
            if b.count() > 0 and b.is_visible():
                b.click(timeout=2000)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue

def wait_table_rows(page, timeout=60000):
    # Espera a que exista alguna fila
    page.wait_for_selector("table tbody tr", timeout=timeout)

def get_current_page_codes(page):
    # Devuelve lista [(codigo, nombre)]
    rows = page.locator("table tbody tr")
    out = []
    for i in range(rows.count()):
        r = rows.nth(i)
        tds = r.locator("td")
        if tds.count() < 2:
            continue
        codigo = tds.nth(0).inner_text().strip()
        nombre = tds.nth(1).inner_text().strip()
        if codigo.isdigit():
            out.append((codigo, nombre))
    return out

def click_next(page):
    # DataTables suele usar "Siguiente"
    next_btn = page.locator("a:has-text('Siguiente')").first
    if next_btn.count() == 0:
        return False
    cls = (next_btn.get_attribute("class") or "").lower()
    aria = (next_btn.get_attribute("aria-disabled") or "").lower()
    if "disabled" in cls or aria == "true":
        return False

    # Detectar cambio: guardamos primer código actual para esperar que cambie
    first_code = ""
    try:
        first_code = page.locator("table tbody tr td").first.inner_text().strip()
    except Exception:
        pass

    next_btn.click()
    # Esperar a que cambie el contenido de la tabla
    t0 = time.time()
    while time.time() - t0 < 20:
        page.wait_for_timeout(400)
        try:
            new_first = page.locator("table tbody tr td").first.inner_text().strip()
            if new_first and new_first != first_code:
                return True
        except Exception:
            continue
    # si no detectamos cambio, aún así intentamos continuar
    return True

def extract_email_by_clicking_green_icon(context, codigo):
    """
    Abre ficha /centro/<codigo> y hace click en el icono verde (lápiz) para ver datos.
    Extrae email del contenido final.
    """
    url = URL_FICHA.format(codigo)
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(800)
        accept_cookies_if_any(page)

        # 1) buscar el icono verde: normalmente es un <a> con clase btn-success o un icono con color verde.
        # Probamos varios selectores robustos:
        green_candidates = [
            "a.btn-success",                 # bootstrap típico
            "button.btn-success",
            "a:has(.fa-pencil), a:has(.fa-pen)",  # fontawesome lápiz
            "a:has(i[class*='pencil']), a:has(i[class*='edit'])",
            "a[title*='Editar' i], a[aria-label*='Editar' i]",
            "a:has(svg)",  # fallback, luego filtramos por visibilidad
        ]

        clicked = False
        for sel in green_candidates:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    with page.expect_navigation(wait_until="domcontentloaded", timeout=8000):
                        loc.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    clicked = True
                    break
            except PlaywrightTimeoutError:
                # Puede abrir sin navegación; seguimos
                try:
                    loc.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    clicked = True
                    break
                except Exception:
                    pass
            except Exception:
                continue

        # 2) Extraer email (visible + html)
        text = ""
        try:
            text = page.inner_text("body")
        except Exception:
            pass
        email = extract_email(text)
        if email:
            return email

        try:
            html = page.content()
            email = extract_email(html)
            if email:
                return email
        except Exception:
            pass

        # 3) Si no se encontró nada, devolvemos vacío (pero el código seguirá)
        return ""
    finally:
        page.close()

def main():
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
        page.set_default_timeout(60000)
        page.set_default_navigation_timeout(60000)

        # 1) Abrir buscador
        page.goto(URL_BUSCADOR, wait_until="domcontentloaded", timeout=120000)
        accept_cookies_if_any(page)

        # 2) Filtrar Canarias
        page.get_by_label("Comunidad Autónoma").wait_for(timeout=120000)
        page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")
        page.wait_for_timeout(800)
        try_click_search(page)

        # 3) Recorrer TODAS las páginas del listado sacando códigos
        wait_table_rows(page, timeout=120000)

        codes = {}
        safety = 0
        while True:
            for codigo, nombre in get_current_page_codes(page):
                codes[codigo] = nombre

            safety += 1
            if safety > 500:  # por si algo se rompe
                break

            if not click_next(page):
                break

        codes_list = sorted(codes.items(), key=lambda x: x[0])
        print(f"✅ Códigos detectados: {len(codes_list)}")

        # 4) Para cada centro: abrir ficha y click icono verde para sacar email
        rows_out = []
        for idx, (codigo, nombre) in enumerate(codes_list, start=1):
            email = extract_email_by_clicking_green_icon(context, codigo)
            rows_out.append([codigo, nombre, email])

            if idx % 50 == 0:
                print(f"Procesados {idx}/{len(codes_list)}...")

            time.sleep(0.10)

        browser.close()

    # 5) CSV Excel ES
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(rows_out)

    print(f"✅ Terminado. CSV: {OUT_CSV} | Filas: {len(rows_out)}")

if __name__ == "__main__":
    main()
