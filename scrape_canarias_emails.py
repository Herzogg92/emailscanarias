import re
import csv
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://registrosfp.educacion.gob.es/registroestatalentidadesformacion/buscarPublico"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def extract_first_email(text: str) -> str:
    m = EMAIL_RE.search(text or "")
    return m.group(0) if m else ""


def extract_email_from_detail(detail_page) -> str:
    # Robusto: primero texto visible, luego HTML por si está oculto en el DOM
    try:
        visible = detail_page.inner_text("body")
        email = extract_first_email(visible)
        if email:
            return email
    except Exception:
        pass

    try:
        html = detail_page.content()
        return extract_first_email(html)
    except Exception:
        return ""


def main():
    data = []  # [codigo, nombre, email]
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # Timeouts altos para GitHub Actions
        page.set_default_timeout(120000)
        page.set_default_navigation_timeout(120000)

        # ❗ Evitamos networkidle (a veces nunca llega)
        page.goto(URL, wait_until="domcontentloaded", timeout=120000)

        # Esperar a que exista el filtro
        page.get_by_label("Comunidad Autónoma").wait_for(timeout=120000)

        # Aplicar filtro
        page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")

        # Espera a que se refresque la tabla
        page.wait_for_timeout(1500)

        # Tabla (fallback a la primera visible)
        table = page.locator("table").first
        table.wait_for(timeout=120000)

        def get_rows():
            return table.locator("tbody tr")

        def click_next_if_possible() -> bool:
            # DataTables suele usar "Siguiente"
            next_btn = page.locator("a:has-text('Siguiente')").first
            if next_btn.count() == 0:
                return False

            cls = (next_btn.get_attribute("class") or "").lower()
            aria = (next_btn.get_attribute("aria-disabled") or "").lower()

            if "disabled" in cls or aria == "true":
                return False

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

                # Botón en "Acciones" (última columna): suele ser un <a> con icono de lápiz
                action_btn = row.locator("td:last-child a, td:last-child button").first
                if action_btn.count() == 0:
                    data.append([codigo, nombre, ""])
                    continue

                # Puede abrir nueva pestaña o navegar en la misma.
                detail = None
                try:
                    with page.context.expect_page(timeout=5000) as new_page_info:
                        action_btn.click()
                    detail = new_page_info.value
                except PlaywrightTimeoutError:
                    # No abrió nueva página → navegación en la misma pestaña
                    action_btn.click()
                    detail = page

                # Esperar carga básica
                detail.wait_for_load_state("domcontentloaded", timeout=60000)
                detail.wait_for_selector("body", timeout=60000)

                email = extract_email_from_detail(detail)
                data.append([codigo, nombre, email])

                # Si era pestaña nueva, cerrar y seguir
                if detail is not page:
                    detail.close()
                else:
                    # Si navegó en la misma, volver atrás al listado
                    page.go_back(wait_until="domcontentloaded", timeout=60000)
                    page.get_by_label("Comunidad Autónoma").wait_for(timeout=60000)
                    page.wait_for_timeout(800)

                time.sleep(0.1)

            if not click_next_if_possible():
                break

        browser.close()

    output = "emails_centros_canarias.csv"
    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(data)

    print(f"✅ Terminado. Centros: {len(data)} | CSV: {output}")


if __name__ == "__main__":
    main()
