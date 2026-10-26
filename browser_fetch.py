from url_safety import FetchError, assert_public_host, check_reputation
from urllib.parse import urlparse

## Renders a page in a real headless browser for links that only work with JavaScript.
## The browser is a dumb renderer: no model decides what to click, and the only thing that
## leaves this file is the page's visible text, which goes through the same cleaning and
## screening as every other fetch. Callers must check reputation before calling render_page.

NAVIGATION_TIMEOUT_MS = 30000
SETTLE_TIMEOUT_MS = 8000
MAX_REQUESTS = 150  ## a page that wants more than this is probably a tracker farm or a loop
MAX_TEXT_CHARS = 50000
BLOCKED_RESOURCES = {"image", "media", "font"}  ## text is all we want, and this makes pages load faster


def render_page(url):
    """Returns the visible text of the rendered page. Raises FetchError."""
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError:
        raise FetchError("playwright isn't installed, so JavaScript pages can't be rendered")

    host_ok = {}  ## per-host cache so a page with 100 requests doesn't do 100 DNS lookups
    request_count = 0

    def guard(route):
        """Every request the page makes, including redirects and subresources, passes through here."""
        nonlocal request_count
        request = route.request
        request_count += 1
        parsed = urlparse(request.url)
        try:
            if request_count > MAX_REQUESTS or request.resource_type in BLOCKED_RESOURCES:
                raise FetchError("skipped")
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise FetchError("not http(s)")
            if parsed.hostname not in host_ok:
                assert_public_host(parsed.hostname)
                host_ok[parsed.hostname] = True
            ## a redirect or iframe can land somewhere the original url check never saw
            if request.is_navigation_request() and check_reputation(request.url)["verdict"] == "listed":
                raise FetchError("navigation to a listed url")
        except FetchError:
            route.abort()
            return
        route.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                accept_downloads=False,
                service_workers="block",
                permissions=[],
                java_script_enabled=True,
                viewport={"width": 1280, "height": 900},
            )
            context.route("**/*", guard)
            ## route() doesn't see WebSockets, which could otherwise reach internal hosts
            context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.new_page()
            ## popups and new tabs are not followed (registered after the main page so it isn't closed too)
            context.on("page", lambda popup: popup.close())
            ## alert/confirm/prompt dialogs would hang the page, so dismiss them all
            page.on("dialog", lambda dialog: dialog.dismiss())
            try:
                page.goto(url, timeout=NAVIGATION_TIMEOUT_MS, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                except PlaywrightError:
                    pass  ## chatty pages never go idle; whatever has rendered by now is what we get
                ## innerText is what a person would see, so hidden and off-screen elements are left out
                ## belt and braces: the page we are about to read must itself be on a public http(s) host
                final = urlparse(page.url)
                if final.scheme not in ("http", "https") or not final.hostname:
                    raise FetchError("page ended up somewhere that isn't http(s)")
                assert_public_host(final.hostname)
                title = page.title()
                text = page.evaluate("document.body ? document.body.innerText : ''")
            except PlaywrightError as error:
                raise FetchError(f"browser couldn't load the page: {str(error).splitlines()[0][:150]}")
        finally:
            browser.close()

    if len(text.strip()) < 100:
        raise FetchError("page rendered with almost no visible text")
    return f"{title}\n{text}"[:MAX_TEXT_CHARS]
