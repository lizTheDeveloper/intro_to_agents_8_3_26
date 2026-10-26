from util import ask_json
from tools import fetch_youtube_transcript
from url_safety import FetchError, check_reputation, pinned, resolve_public
from injection_screen import HIDDEN_CHARS, pattern_findings, screen, output_is_clean
from browser_fetch import render_page
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile

import requests

## Fetches content an agent shouldn't trust (links from chat, the open web), cleans it,
## classifies it for prompt injection, digests it, and stores only the digest.
## The calling agent never sees raw text, and no tool call is involved:
##   rows = process_and_store(items)

AGENTSWEB = "https://agentsweb.org"
DB_PATH = os.getenv("COMMUNITY_DB", os.getenv("LINKS_DB", "community.db"))  ## links, people, facts, shares, and outreach all live here
## this file used to be called links.db; move it rather than leave old personal data behind in a file nothing reads
if DB_PATH == "community.db" and os.path.exists("links.db") and not os.path.exists(DB_PATH):
    os.rename("links.db", DB_PATH)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 12000  ## what we keep after cleaning
MODEL_EXCERPT_CHARS = 6000  ## what the classifier and digester actually read
MIN_USEFUL_CHARS = 200  ## less than this from a page usually means JS that never rendered
PDF_PAGE_LIMIT = 12
USER_AGENT = "Mozilla/5.0 (compatible; link-digest-bot)"

## these need a login, so they would 403 anyway, and their URLs shouldn't go to a shared public cache
PRIVATE_DOC_HOSTS = ("docs.google.com", "drive.google.com", "notion.so", "notion.site", "sharepoint.com", "dropbox.com", "figma.com")
## meeting and call links are invitations, not content, and must not end up in a shared public cache
MEETING_HOSTS = ("meet.google.com", "zoom.us", "zoom.com", "zoomgov.com", "teams.microsoft.com", "teams.live.com", "whereby.com", "meet.jit.si")
MEETING_PATHS = ("/call/", "/meeting/", "/j/", "/meet/")
## short links hide where they go, so they never go to the shared cache; safe_get follows them hop by hop instead
SHORTENER_HOSTS = ("bit.ly", "t.co", "tinyurl.com", "g.co", "goo.gl", "ow.ly", "buff.ly", "is.gd", "s.id", "rb.gy", "cutt.ly", "shorturl.at", "lnkd.in")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
CATEGORIES = ("paper", "project", "document", "idea", "video", "tool", "article", "other")


def normalized_host(url):
    host = (urlparse(url).hostname or "").rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def host_in(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def is_meeting_url(url):
    return host_in(normalized_host(url), MEETING_HOSTS) or any(p in unquote(urlparse(url).path).lower() for p in MEETING_PATHS)


def is_unambiguous_url(url):
    """Python's parser and browsers (WHATWG) disagree about backslashes, control characters, and encoded hosts,
    so a crafted link can name one host to our checks and another to a fetcher. Those links are refused outright."""
    if re.search(r"[\\\s\x00-\x1f\x7f]", url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not re.search(r"[@%]", parsed.netloc)


class NeedsBrowser(FetchError):
    """The page loaded but has no text until its JavaScript runs."""


## ---------- fetching ----------

def safe_get(url):
    """GET with a size cap, validating every redirect hop. Returns (final_url, content_type, bytes)."""
    for _ in range(5):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or "@" in parsed.netloc or not parsed.hostname:
            raise FetchError("not a plain http(s) url")
        if not is_unambiguous_url(url) or is_meeting_url(url):
            raise FetchError("redirects to a meeting link or an unusual url, not fetched")
        address = resolve_public(parsed.hostname)
        ## connect to the address we just checked, so a second DNS answer can't redirect us somewhere internal
        with pinned(parsed.hostname, address):
            response = requests.get(url, stream=True, timeout=20, allow_redirects=False, headers={"User-Agent": USER_AGENT})
            body = b""
            if not response.is_redirect and response.status_code == 200:
                for chunk in response.iter_content(65536):
                    body += chunk
                    if len(body) > MAX_DOWNLOAD_BYTES:
                        raise FetchError("file too large")
        if response.is_redirect:
            url = urljoin(url, response.headers.get("location", ""))
            continue
        if response.status_code != 200:
            raise FetchError(f"http {response.status_code}")
        return url, response.headers.get("content-type", "").split(";")[0].strip().lower(), body
    raise FetchError("too many redirects")


def fetch_via_agentsweb(url):
    """Pre-digested markdown from the shared cache. Returns (markdown, trust_level)."""
    response = requests.get(f"{AGENTSWEB}/fetch", params={"url": url}, timeout=45)
    if response.status_code != 200:
        raise FetchError(f"agentsweb http {response.status_code}")
    data = response.json()
    markdown = data.get("markdown") or ""
    if markdown.startswith("%PDF"):
        raise FetchError("agentsweb returned raw pdf bytes")
    if len(markdown) < MIN_USEFUL_CHARS:
        raise FetchError("agentsweb returned too little text")
    return markdown, data.get("trust_level")


class TextExtractor(HTMLParser):
    """Bare-bones HTML to text for when agentsweb can't help. Drops script/style/hidden content."""
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)


def pdf_to_text(data):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        result = subprocess.run(["pdftotext", "-l", str(PDF_PAGE_LIMIT), "-q", f.name, "-"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or len(result.stdout.strip()) < MIN_USEFUL_CHARS:
        raise FetchError("pdf had no extractable text (scanned?)")
    return result.stdout


def fetch_direct(url):
    """Our own fetchers, picked by what the server says the content actually is."""
    final_url, content_type, body = safe_get(url)
    if content_type == "application/pdf" or body[:5] == b"%PDF-":
        return "document", pdf_to_text(body)
    if content_type.startswith("image/"):
        return "image", f"[image, {len(body)} bytes, not analyzed]"
    if content_type in ("text/html", "application/xhtml+xml"):
        extractor = TextExtractor()
        extractor.feed(body.decode("utf-8", errors="replace"))
        text = " ".join(extractor.parts)
        if len(text.strip()) < MIN_USEFUL_CHARS:
            raise NeedsBrowser("page has almost no text without JavaScript")
        return "article", f"{extractor.title.strip()}\n{text}"
    if content_type.startswith("text/") or content_type in ("application/json", "application/xml"):
        return "document", body.decode("utf-8", errors="replace")
    raise FetchError(f"unsupported content type {content_type or 'unknown'}")


def fetch_text(url):
    """Returns (kind, raw_text, trust_level). Raises FetchError."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()

    if parsed.scheme == "mxc":
        raise FetchError("matrix upload, needs an authenticated download")
    host = normalized_host(url)

    youtube_id = None
    if host in ("youtu.be", "www.youtu.be"):
        match = re.fullmatch(r"/([\w-]{11})/?", parsed.path)
    elif host.endswith("youtube.com"):
        match = re.search(r"[?&]v=([\w-]{11})(?:&|$)", parsed.query and "?" + parsed.query) or re.fullmatch(r"/(?:shorts|embed)/([\w-]{11})/?", parsed.path)
    else:
        match = None
    if match:
        youtube_id = match.group(1)
    if youtube_id:
        ## transcripts are written into a throwaway directory, so nothing is built from the id for cleanup
        with tempfile.TemporaryDirectory() as scratch:
            original_dir = os.getcwd()
            os.chdir(scratch)
            try:
                transcript = fetch_youtube_transcript(youtube_id)
            finally:
                os.chdir(original_dir)
        if not transcript:
            raise FetchError("no youtube transcript available")
        return "video", transcript, None

    ## for papers the abstract page is cleaner than the pdf and is what we need to decide if it's interesting
    arxiv = re.match(r"/(?:abs|pdf)/([\w.\-/]+?)(?:\.pdf)?$", parsed.path)
    if host.endswith("arxiv.org") and arxiv:
        markdown, trust = fetch_via_agentsweb(f"https://arxiv.org/abs/{arxiv.group(1)}")
        return "paper", markdown, trust

    if path.endswith(IMAGE_EXTENSIONS):
        return "image", "[image, not analyzed]", None
    if path.endswith(".pdf") or host_in(host, PRIVATE_DOC_HOSTS) or host_in(host, SHORTENER_HOSTS):
        kind, text = fetch_direct(url)
        return kind, text, None

    try:
        markdown, trust = fetch_via_agentsweb(url)
        return "article", markdown, trust
    except (FetchError, requests.RequestException, ValueError):
        pass
    try:
        kind, text = fetch_direct(url)
        return kind, text, None
    except NeedsBrowser:
        ## last resort and the riskiest fetcher, so the caller has already run the reputation check
        return "article", render_page(url), None


## ---------- cleaning ----------

def clean_text(text):
    text = HIDDEN_CHARS.sub("", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_TEXT_CHARS]


## ---------- digest ----------

def page_title(raw_text, url):
    """Title comes from the page itself, so no model has to write it."""
    match = re.search(r'^title:\s*"?(.+?)"?\s*$', raw_text[:1000], re.M)
    first_line = next((line.strip("# ").strip() for line in raw_text.splitlines() if line.strip() and not line.startswith("---")), "")
    title = match.group(1) if match else first_line or url
    return re.sub(r"[\[\]\r\n]+", " ", title)[:200]


def post_process(text, context):
    """The only step where a generative model reads page text, so it runs last and only on links already
    screened as safe. Whatever it writes is checked before it is stored. The chat message that shared the link
    is passed as data next to the page, never spliced into the instructions."""
    result = ask_json(
        "Someone shared a link in chat. The chat message and the page are both in the data below. "
        f'Return {{"category": one of {list(CATEGORIES)}, "summary": 2 sentences on what the page is, '
        '"why": 1 sentence on why it might be interesting}.',
        f"<chat_message>{context[:200]}</chat_message>\n<page>{text}</page>", "{")
    empty = {"summary": "", "why": ""}
    if not isinstance(result, dict):
        return empty
    fields = {"summary": str(result.get("summary", ""))[:600], "why": str(result.get("why", ""))[:300]}
    if result.get("category") in CATEGORIES:
        fields["category"] = result["category"]
    if not output_is_clean(fields["summary"], fields["why"]):
        return empty
    return fields


## ---------- storage ----------

def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS links (
        url TEXT PRIMARY KEY, kind TEXT, status TEXT, error TEXT, verdict TEXT, reasons TEXT,
        trust_level INTEGER, category TEXT, title TEXT, summary TEXT, why TEXT,
        shared_by TEXT, fetched_at TEXT)""")
    return db


def row_to_dict(row):
    data = dict(row)
    data["shared_by"] = json.loads(data["shared_by"] or "[]")
    data["reasons"] = json.loads(data["reasons"] or "[]")
    return data


## ---------- the whole pipeline ----------

def fetch_untrusted(url, context=""):
    """Check, fetch, clean, screen, digest one url. Returns a dict; raw text never leaves this function."""
    row = {"url": url, "kind": "other", "status": "ok", "error": "", "verdict": "", "reasons": [],
           "trust_level": None, "category": "other", "title": "", "summary": "", "why": ""}

    ## this gate runs before any outside call: blocklists, agentsweb, direct fetch, or the browser
    if not url.startswith("mxc://") and not is_unambiguous_url(url):
        row.update(status="failed", error="unusual url, not fetched")
        return row
    ## meeting links never leave this machine: not to blocklist services, not to any fetcher
    if is_meeting_url(url):
        row.update(status="failed", error="meeting or call link, not fetched")
        return row

    ## nothing is fetched, by any fetcher, until the url has passed the blocklists
    reputation = check_reputation(url)
    if reputation["verdict"] == "listed":
        row.update(status="blocked", verdict="unsafe", reasons=reputation["hits"])
        return row

    try:
        row["kind"], raw_text, row["trust_level"] = fetch_text(url)
    except (FetchError, requests.RequestException, subprocess.SubprocessError, ValueError) as error:
        row.update(status="failed", error=str(error)[:200])
        return row

    if row["kind"] == "image":
        row.update(status="unanalyzed", category="other", title=url.rsplit("/", 1)[-1], summary=raw_text)
        return row

    cleaned = clean_text(raw_text)
    excerpt = cleaned[:MODEL_EXCERPT_CHARS]  ## the safety model and the summarizer both read exactly this
    row["verdict"], row["reasons"], judged = screen(raw_text, excerpt)
    if not judged:
        row["status"] = "unjudged"  ## the safety model didn't answer; retried next run
    ## the chat message that shared the link is untrusted too, and it reaches the summarizer
    context_findings = pattern_findings(context)
    if context_findings:
        row["reasons"] += [f"chat message: {finding}" for finding in context_findings]
        row["verdict"] = max(row["verdict"], "suspicious", key=["safe", "suspicious", "unsafe"].index)
    if reputation["verdict"] == "unchecked":
        row["reasons"].append("no blocklist could be reached")
    row["title"] = page_title(cleaned, url)

    ## only a safe link gets a model-written category and summary, and that happens last
    if row["verdict"] == "safe":
        row.update(post_process(excerpt, context))
        if row["kind"] in ("paper", "video"):
            row["category"] = row["kind"]
    ## suspicious and unsafe links are kept as url + reasons only
    return row


def process_and_store(items):
    """items: [{url, context, shared_by: [..]}]. Digests anything new, saves to sqlite, returns rows."""
    db = connect()
    rows = []
    for item in items:
        url = item["url"]
        existing = db.execute("SELECT * FROM links WHERE url = ?", (url,)).fetchone()
        ## already digested: just record who else shared it. Failed and unjudged links get another try,
        ## but a link already judged unsafe is never re-screened.
        retry = existing and existing["status"] in ("failed", "unjudged") and existing["verdict"] != "unsafe"
        if existing and not retry:
            row = row_to_dict(existing)
            row["shared_by"] = sorted(set(row["shared_by"]) | set(item.get("shared_by", [])))
        else:
            row = fetch_untrusted(url, item.get("context", ""))
            row["shared_by"] = sorted(set(item.get("shared_by", [])) | set(row_to_dict(existing)["shared_by"] if existing else []))
            ## no verdict is carried over: failed links never had one, an unjudged link's "suspicious" is only a
            ## placeholder, and only our own code (not page or model text) can mark a link unjudged
            print(f"  {row['status']:10} {row['verdict'] or '-':10} {url[:90]}")
        db.execute(
            "INSERT OR REPLACE INTO links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url, row["kind"], row["status"], row["error"], row["verdict"], json.dumps(row["reasons"]),
             row["trust_level"], row["category"], row["title"], row["summary"], row["why"],
             json.dumps(row["shared_by"]), datetime.now(timezone.utc).isoformat()))
        db.commit()
        rows.append(row)
    return rows
