import re
import csv
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://registrosfp.educacion.gob.es/registroestatalentidadesformacion/buscarPublico"
OUT_CSV = "emails_centros_canarias.csv"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)


def extract_first_email(text: str) -> str:
    m = EMAIL_RE.search(text or "")
    return m.group(0) if m else ""


def safe_debug_dump(page, prefix: str):
    """
    Guarda screenshot + html para inspeccionar qué vio el runner.
    """
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{prefix}.png"), full_page=True)
    except Exception:
        pass

    try:
        html = page.content()
        (DEBUG_DIR / f"{prefix}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass

    try:
        (DEBUG_DIR / f"{prefix}.txt").write_text(page.inner_text("body"), encoding="utf-8")
    except Exception:
        pass


def try_accept_cookies(page):
    """
    Intenta cerrar/aceptar banners típicos en España/UE.
    No falla si no existe.
    """
    candidates = [
        "button:has-text('Aceptar')",
        "button:has-text('Aceptar todo')",
        "button:has-text('Aceptar todas')",
        "button:has-text('Aceptar cookies')",
        "button:has-text('Aceptar y continuar')",
        "a:has-text('Aceptar')",
    ]
    for sel in candidates:
        btn = page.locator(sel).first
        try:
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(800)
                break
        except Exception:
            continue


def extract_email_from_detail(detail_page) -> str:
    # Visible text first
    try:
        visible = detail_page.inner_text("body")
        email = extract_first_email(visible)
        if email:
            return email
    except Exception:
        pass

    # HTML fallback
    try:
        html = detail_page.content()
        return extract_first_email(html)
    except Exception:
        return ""


def main():
    data = []
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            locale="es-ES",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )

        # reduce huella "webdriver"
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page = context.new_page()
        page.set_default_timeout(120000)
        page.set_default_navigation_timeout(120000)

        # 1) Entrar sin networkidle
        page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(1000)
        try_accept_cookies(page)

        # 2) Esperar filtro
        try:
            page.get_by_label("Comunidad Autónoma").wait_for(timeout=120000)
        except PlaywrightTimeoutError:
            safe_debug_dump(page, "no_select_comunidad")
            raise RuntimeError("No aparece el select de 'Comunidad Autónoma' en el runner (mira debug/*).")

        # 3) Aplicar filtro
        page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")
        page.wait_for_timeout(1500)
        try_accept_cookies(page)

        # 4) En vez de esperar "table visible", esperamos a que existan filas en tbody.
        #    Esto evita el problema de "table invisible" por CSS/layout.
        #    Si el site cambia, capturamos debug.
        try:
            page.wait_for_selector("table tbody tr", timeout=120000)
        except PlaywrightTimeoutError:
            safe_debug_dump(page, "no_table_rows_after_filter")
            raise RuntimeError("No aparecen filas en la tabla tras filtrar (mira debug/*).")

        # Tomamos la primera tabla que tenga tbody
        table = page.locator("table:has(tbody)").first

        def get_rows():
            return table.locator("tbody tr")

        def click_next_if_possible() -> bool:
            # DataTables suele tener botón Siguiente
            next_btn = page.locator("a:has-text('Siguiente')").first
            if next_btn.count() == 0:
                return False

            try:
                cls = (next_btn.get_attribute("class") or "").lower()
                aria = (next_btn.get_attribute("aria-disabled") or "").lower()
                if "disabled" in cls or aria == "true":
                    return False
            except Exception:
                pass

            next_btn.click()
            page.wait_for_timeout(1200)
            return True

        while True:
            rows = get_rows()
            count = rows.count()

            for i in range(count):
                row = rows.nth(i)
                cells = row.locator("td")
                if cells.count() < 2:
                    continue

                codigo = cells.nth(0).inner_text().strip()
                nombre = cells.nth(1).inner_text().strip()

                if not codigo or codigo in seen:
                    continue
                seen.add(codigo)

                # Última columna: acciones (lápiz)
                action_btn = row.locator("td:last-child a, td:last-child button").first
                if action_btn.count() == 0:
                    data.append([codigo, nombre, ""])
                    continue

                detail = None
                try:
                    with page.context.expect_page(timeout=5000) as new_page_info:
                        action_btn.click()
                    detail = new_page_info.value
                except PlaywrightTimeoutError:
                    # misma pestaña
                    action_btn.click()
                    detail = page

                detail.wait_for_load_state("domcontentloaded", timeout=60000)
                detail.wait_for_timeout(800)
                try_accept_cookies(detail)

                # asegurar body
                detail.wait_for_selector("body", timeout=60000)

                email = extract_email_from_detail(detail)
                data.append([codigo, nombre, email])

                if detail is not page:
                    detail.close()
                else:
                    page.go_back(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(800)
                    try_accept_cookies(page)
                    # re-asegurar que seguimos en el listado
                    page.wait_for_selector("table tbody tr", timeout=60000)

                time.sleep(0.1)

            if not click_next_if_possible():
                break

        browser.close()

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(data)

    print(f"✅ Terminado. Centros: {len(data)} | CSV: {OUT_CSV}")


if __name__ == "__main__":
    main()
