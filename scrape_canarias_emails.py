import re
import csv
import time
from playwright.sync_api import sync_playwright

URL = "https://registrosfp.educacion.gob.es/registroestatalentidadesformacion/buscarPublico"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

def extract_email_from_page(page) -> str:
    """
    Busca un email en el texto visible de la ficha.
    """
    text = page.inner_text("body")
    m = EMAIL_RE.search(text)
    return m.group(0) if m else ""

def main():
    data = []  # codigo, nombre, email

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")

        # ✅ Filtro Comunidad Autónoma
        page.get_by_label("Comunidad Autónoma").select_option(label="ISLAS CANARIAS")
        page.wait_for_timeout(1500)

        # Localizamos tabla
        table = page.locator("table").first

        def get_rows():
            return table.locator("tbody tr")

        def click_next_if_possible():
            # DataTables suele tener botón Siguiente
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

        seen = set()

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

                # ✅ clic en el icono lápiz (Acciones)
                # En tu captura se ve como un botón verde con un lápiz.
                action_btn = row.locator("td:last-child a, td:last-child button").first

                # Abrimos en nueva pestaña si es link
                with page.context.expect_page() as new_page_info:
                    action_btn.click()
                detail = new_page_info.value
                detail.wait_for_load_state("networkidle")

                email = extract_email_from_page(detail)
                data.append([codigo, nombre, email])

                detail.close()
                time.sleep(0.1)

            if not click_next_if_possible():
                break

        browser.close()

    # Guardar CSV
    output = "emails_centros_canarias.csv"
    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "nombre", "email"])
        w.writerows(data)

    print(f"✅ Terminado. Centros: {len(data)} | CSV: {output}")

if __name__ == "__main__":
    main()
