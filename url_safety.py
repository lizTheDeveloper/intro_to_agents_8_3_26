from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlparse
import ipaddress
import os
import socket
import threading
import time

import requests
import urllib3

## Checks a url before anything fetches it: is it a public address, and is it on a known phishing/malware/spam list?

OPENPHISH_FEED = "https://openphish.com/feed.txt"
FEED_CACHE = os.getenv("OPENPHISH_CACHE", ".openphish_feed.txt")
FEED_MAX_AGE_SECONDS = 6 * 3600
SURBL_ZONE = "multi.surbl.org"
DNS_TIMEOUT_SECONDS = 4


class FetchError(Exception):
    pass


def resolve_public(host):
    """Resolve once and return an address that is safe to connect to. Raises if any answer is non-public.
    Callers must connect to the returned address, not look the name up again, or DNS rebinding can swap it."""
    try:
        addresses = [info[4][0] for info in socket.getaddrinfo(host, None)]
    except socket.gaierror:
        raise FetchError(f"can't resolve {host}")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise FetchError(f"{host} resolves to a non-public address")
    return sorted(addresses, key=lambda a: ":" in a)[0]  ## prefer IPv4


def assert_public_host(host):
    resolve_public(host)


## urllib3 asks DNS for every connection. For hosts we have already checked, hand it the checked address instead,
## while the hostname still drives TLS certificate verification and the Host header.
_pins = threading.local()
_original_create_connection = urllib3.util.connection.create_connection


def _pin_key(host):
    """Normalize the way urllib3 sees the host: lowercase, IDNA-encoded, no brackets or trailing dot."""
    host = host.strip("[]").rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _pinned_create_connection(address, *args, **kwargs):
    host, port = address
    pins = getattr(_pins, "map", None)
    if pins:
        ## inside a pinned request, connecting anywhere we didn't check is refused rather than looked up again
        if _pin_key(host) not in pins:
            raise OSError(f"refusing unpinned connection to {host}")
        host = pins[_pin_key(host)]
    return _original_create_connection((host, port), *args, **kwargs)


urllib3.util.connection.create_connection = _pinned_create_connection


@contextmanager
def pinned(host, address):
    _pins.map = {_pin_key(host): address}
    try:
        yield
    finally:
        _pins.map = None


## ---------- OpenPhish: free community feed of live phishing urls, no key ----------

_feed = {"urls": None, "hosts": None}


def load_openphish():
    fresh = os.path.exists(FEED_CACHE) and time.time() - os.path.getmtime(FEED_CACHE) < FEED_MAX_AGE_SECONDS
    if not fresh:
        try:
            response = requests.get(OPENPHISH_FEED, timeout=20)
            response.raise_for_status()
            with open(FEED_CACHE, "w") as f:
                f.write(response.text)
        except requests.RequestException:
            pass  ## fall back to a stale cache if we have one
    if not os.path.exists(FEED_CACHE):
        return None
    with open(FEED_CACHE) as f:
        urls = {line.strip().rstrip("/") for line in f if line.strip()}
    return urls, {urlparse(u).hostname for u in urls}


def check_openphish(url):
    if _feed["urls"] is None:
        loaded = load_openphish()
        if loaded is None:
            return None
        _feed["urls"], _feed["hosts"] = loaded
    host = urlparse(url).hostname
    ## an exact url match is a hit; a host match is too, since phishing hosts rarely serve anything else
    if url.rstrip("/") in _feed["urls"] or host in _feed["hosts"]:
        return "openphish: listed as phishing"
    return ""


## ---------- SURBL: free DNS blocklist of spam/phishing/malware domains, no key ----------

def check_surbl(host):
    labels = host.split(".")
    if len(labels) < 2 or host.replace(".", "").isdigit():
        return ""
    ## no public-suffix list here, so try the last two and last three labels
    candidates = {".".join(labels[-2:]), ".".join(labels[-3:])}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {name: pool.submit(socket.gethostbyname, f"{name}.{SURBL_ZONE}") for name in candidates}
        for name, future in futures.items():
            try:
                answer = future.result(timeout=DNS_TIMEOUT_SECONDS)
            except socket.gaierror:
                continue  ## NXDOMAIN means not listed
            except TimeoutError:
                return None
            ## 127.0.0.2 and up are list-membership bitmasks. Anything else is a refusal or rate limit, not a listing.
            last_octet = int(answer.split(".")[-1])
            if answer.startswith("127.0.0.") and last_octet >= 2:
                return f"surbl: {name} is listed ({answer})"
    return ""


## ---------- keyed services, used only if you set the key ----------

def check_safe_browsing(url):
    key = os.getenv("GOOGLE_SAFE_BROWSING_KEY")
    if not key:
        return None
    body = {
        "client": {"clientId": "link-digest", "clientVersion": "1.0"},
        "threatInfo": {
            "threatTypes": ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": url}],
        },
    }
    response = requests.post("https://safebrowsing.googleapis.com/v4/threatMatches:find", params={"key": key}, json=body, timeout=15)
    response.raise_for_status()
    matches = response.json().get("matches", [])
    return f"safe browsing: {matches[0]['threatType']}" if matches else ""


def check_urlhaus(url):
    key = os.getenv("URLHAUS_AUTH_KEY")  ## free from abuse.ch, but they require one now
    if not key:
        return None
    response = requests.post("https://urlhaus-api.abuse.ch/v1/url/", data={"url": url}, headers={"Auth-Key": key}, timeout=15)
    response.raise_for_status()
    data = response.json()
    return f"urlhaus: {data.get('threat', 'malware')} url" if data.get("query_status") == "ok" else ""


def check_reputation(url):
    """verdict is listed | clean | unchecked. 'clean' means at least one source answered and none listed it."""
    host = (urlparse(url).hostname or "").lower()
    hits, answered = [], []
    for name, check in (("openphish", lambda: check_openphish(url)), ("surbl", lambda: check_surbl(host)),
                        ("safe_browsing", lambda: check_safe_browsing(url)), ("urlhaus", lambda: check_urlhaus(url))):
        try:
            result = check()
        except (requests.RequestException, ValueError):
            result = None  ## source down: unknown, which is different from clean
        if result is None:
            continue
        answered.append(name)
        if result:
            hits.append(result)
    return {"verdict": "listed" if hits else ("clean" if answered else "unchecked"), "hits": hits, "sources": answered}
