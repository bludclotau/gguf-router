from playwright.sync_api import sync_playwright


def fetch_page(url):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=60000)
                title = page.title()
                text = page.inner_text("body")
                content = page.content()
                return {
                    "url": url,
                    "title": title,
                    "text": (text or "")[:8000],
                    "content": (content or "")[:8000],
                }
            finally:
                browser.close()
    except Exception as exc:
        return {"error": str(exc), "url": url}
