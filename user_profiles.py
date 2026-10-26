from util import ask_json
from untrusted_fetch import connect
from injection_screen import md_text, md_url, output_is_clean
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

## Tracks who is in the community, what they shared, and facts they have said about themselves,
## as raw material for a magazine/YouTube profile. Everything here is a CLAIM from a chat message:
## facts start as 'unverified' and carry the exact quote and source, so a later interview agent
## can confirm each one with the person (status -> 'confirmed' or 'rejected') before anything is published.

FACT_CATEGORIES = ("work", "project", "skill", "interest", "idea", "background", "other")
MAX_CHUNK_CHARS = 3500
MAX_CHUNKS_PER_USER = 3  ## per run, newest first
MIN_CHARS_TO_EXTRACT = 80
MAX_FACTS_PER_CHUNK = 8
## facts that would expose contact details are dropped even if the model produced them
CONTACT_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{8,}\d|\b\d{1,5}\s+\w+\s+(street|st|avenue|ave|road|rd|lane|ln|drive|dr)\b", re.I)

## Words that signal special-category or private topics. This is a backstop for the model's own instructions:
## a fact that matches is never stored, and the editor agent asks people about such things in general terms instead.
SENSITIVE_PATTERN = re.compile(
    r"\b(neurodivergen\w*|autis\w*|adhd|dyslex\w*|queer|gay|lesbian|bisexual|trans|transgender|nonbinary|non-binary|lgbt\w*|sexual\w*|"
    r"pregnan\w*|disab\w*|depress\w*|anxiety|bipolar|therap\w*|diagnos\w*|medicat\w*|illness|cancer|chronic|mental health|"
    r"religio\w*|christian|muslim|jewish|atheist|republican|democrat|immigra\w*|visa|undocumented|salary|debt|bankrupt\w*|"
    r"divorc\w*|abus\w*|assault|addict\w*|sober|arrest\w*|convict\w*|lawsuit|ethnic\w*|racial\w*)\b", re.I)

EXTRACTION_INSTRUCTIONS = f"""These are chat messages written by ONE community member. List facts they state about themselves that could go in a profile of them in a community magazine: what they work on, what they have built or made, skills, interests, ideas they are developing, public background such as their role or studies.
Rules:
- Only things the person says about themselves. Never facts about other people, and never guesses or inferences.
- Skip health, relationships and family, politics, religion, sexuality, money, precise location, contact details, and anything about children.
- Each fact needs a "quote": a short span copied EXACTLY from their messages that supports it.
Return ONLY a JSON list (at most {MAX_FACTS_PER_CHUNK}, possibly empty) of objects with keys: fact (one short sentence), category (one of {list(FACT_CATEGORIES)}), quote."""


def connect_profiles():
    db = connect()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, display_name TEXT, first_seen TEXT, last_seen TEXT,
            message_count INTEGER DEFAULT 0, rooms TEXT DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS user_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, fact TEXT, category TEXT, quote TEXT,
            room TEXT, event_id TEXT, said_at TEXT, status TEXT DEFAULT 'unverified', extracted_at TEXT,
            UNIQUE (user_id, fact));
        CREATE TABLE IF NOT EXISTS shares (
            user_id TEXT, url TEXT, room TEXT, event_id TEXT, shared_at TEXT, context TEXT,
            UNIQUE (user_id, url, event_id));
        CREATE TABLE IF NOT EXISTS seen_events (event_id TEXT PRIMARY KEY);
        -- people who asked to be forgotten, stored as a hash so we keep no readable trace of them
        CREATE TABLE IF NOT EXISTS forgotten (user_hash TEXT PRIMARY KEY);

        -- Read-only conveniences for you, the operator. They span every person, so agents get a PersonScope instead.
        CREATE VIEW IF NOT EXISTS user_links AS
            SELECT s.user_id, u.display_name, s.url, l.title, l.category, l.verdict, l.status, l.summary, l.why, s.room, s.shared_at
            FROM shares s LEFT JOIN users u ON u.user_id = s.user_id LEFT JOIN links l ON l.url = s.url;
        CREATE VIEW IF NOT EXISTS user_facts_view AS
            SELECT f.id, f.user_id, u.display_name, f.fact, f.category, f.status, f.quote, f.room, f.said_at
            FROM user_facts f LEFT JOIN users u ON u.user_id = f.user_id;
        CREATE VIEW IF NOT EXISTS profile_summary AS
            SELECT u.user_id, u.display_name, u.message_count, u.first_seen, u.last_seen, u.rooms,
                   (SELECT COUNT(*) FROM user_facts f WHERE f.user_id = u.user_id) AS facts,
                   (SELECT COUNT(*) FROM user_facts f WHERE f.user_id = u.user_id AND f.status = 'confirmed') AS confirmed_facts,
                   (SELECT COUNT(*) FROM shares s WHERE s.user_id = u.user_id) AS links_shared,
                   (SELECT status FROM outreach o WHERE o.user_id = u.user_id) AS outreach_status
            FROM users u;
        CREATE VIEW IF NOT EXISTS link_sharers AS
            SELECT l.url, l.title, l.category, l.verdict, l.status, COUNT(DISTINCT s.user_id) AS sharers, MAX(s.shared_at) AS last_shared
            FROM links l LEFT JOIN shares s ON s.url = l.url GROUP BY l.url;
    """)
    return db


def user_hash(user_id):
    return hashlib.sha256(user_id.encode()).hexdigest()


def is_forgotten(db, user_id):
    return db.execute("SELECT 1 FROM forgotten WHERE user_hash = ?", (user_hash(user_id),)).fetchone() is not None


def strip_reply_fallback(body):
    """Matrix replies quote the earlier message in '> ' lines. Those words aren't this person's."""
    return "\n".join(line for line in body.splitlines() if not line.startswith("> ")).strip()


def record_activity(db, room_name, events, display_names):
    """Update per-user counts; returns {user_id: [messages]}. Callers must not pass private rooms or DMs."""
    messages = {}
    for event in events:
        content = event["content"]
        if content.get("msgtype") not in ("m.text", "m.notice", "m.emote"):
            continue
        ## edits repeat the original, and bridges/bots post as m.notice
        if content.get("m.relates_to", {}).get("rel_type") == "m.replace" or content.get("msgtype") == "m.notice":
            continue
        user = event["sender"]
        if is_forgotten(db, user):
            continue
        when = datetime.fromtimestamp(event["origin_server_ts"] / 1000, timezone.utc).isoformat()
        row = db.execute("SELECT rooms FROM users WHERE user_id = ?", (user,)).fetchone()
        rooms = set(json.loads(row["rooms"])) if row else set()
        rooms.add(room_name)
        db.execute("""INSERT INTO users (user_id, display_name, first_seen, last_seen, message_count, rooms) VALUES (?,?,?,?,1,?)
                      ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name,
                        first_seen = MIN(first_seen, excluded.first_seen), last_seen = MAX(last_seen, excluded.last_seen),
                        message_count = message_count + 1, rooms = excluded.rooms""",
                   (user, display_names.get(user, user), when, when, json.dumps(sorted(rooms))))
        body = strip_reply_fallback(content.get("body", ""))
        if body:
            messages.setdefault(user, []).append({"event_id": event["event_id"], "room": room_name, "time": when, "body": body})
    db.commit()
    return messages


def record_shares(db, items):
    """items are second_agent link items, one per (message, url) before de-duplication. Private rooms are never recorded."""
    for item in items:
        if item.get("private") or is_forgotten(db, item["sender"]):
            continue
        db.execute("INSERT OR IGNORE INTO shares VALUES (?,?,?,?,?,?)",
                   (item["sender"], item["url"], item["room"], item["event_id"], item["time"], item["context"][:300]))
    db.commit()


def normalize(text):
    return re.sub(r"\s+", " ", text).casefold().strip()


def extract_facts(db, user, messages):
    """Ask a model for self-stated facts, then keep only the ones that pass deterministic checks."""
    if is_forgotten(db, user):
        return 0
    fresh = [m for m in messages if not db.execute("SELECT 1 FROM seen_events WHERE event_id = ?", (m["event_id"],)).fetchone()]
    if sum(len(m["body"]) for m in fresh) < MIN_CHARS_TO_EXTRACT:
        return 0
    chunks, current, size = [], [], 0
    for message in sorted(fresh, key=lambda m: m["time"], reverse=True):
        if size + len(message["body"]) > MAX_CHUNK_CHARS and current:
            chunks.append(current)
            current, size = [], 0
        current.append(message)
        size += len(message["body"])
    chunks.append(current)

    stored = 0
    for chunk in chunks[:MAX_CHUNKS_PER_USER]:
        text = "\n---\n".join(m["body"] for m in chunk)
        result = ask_json(EXTRACTION_INSTRUCTIONS, text, "[", max_chars=MAX_CHUNK_CHARS + 500)
        if isinstance(result, list):
            haystack = normalize(text)
            for item in result[:MAX_FACTS_PER_CHUNK]:
                if not isinstance(item, dict):
                    continue
                fact, quote = str(item.get("fact", "")).strip()[:300], str(item.get("quote", "")).strip()[:300]
                category = item.get("category") if item.get("category") in FACT_CATEGORIES else "other"
                ## the quote must really be in what they wrote, which catches hallucinated and injected "facts"
                source = next((m for m in chunk if normalize(quote) and normalize(quote) in normalize(m["body"])), None)
                if not fact or source is None or CONTACT_PATTERN.search(fact + " " + quote) or SENSITIVE_PATTERN.search(fact + " " + quote) or not output_is_clean(fact, quote):
                    continue
                cursor = db.execute("INSERT OR IGNORE INTO user_facts (user_id, fact, category, quote, room, event_id, said_at, extracted_at) VALUES (?,?,?,?,?,?,?,?)",
                                    (user, fact, category, quote, source["room"], source["event_id"], source["time"], datetime.now(timezone.utc).isoformat()))
                stored += cursor.rowcount
        ## mark processed only after the model answered, so a failed call is retried next run
        if result is not None:
            db.executemany("INSERT OR IGNORE INTO seen_events VALUES (?)", [(m["event_id"],) for m in chunk])
    db.commit()
    return stored


def set_fact_status(db, fact_id, status):
    """For the interview agent: 'confirmed' or 'rejected' once the person has answered."""
    if status not in ("unverified", "confirmed", "rejected"):
        raise ValueError(status)
    db.execute("UPDATE user_facts SET status = ? WHERE id = ?", (status, fact_id))
    db.commit()


def forget_user(db, user_id):
    """Delete everything we hold about one person, and stop collecting about them in future runs."""
    for table in ("user_facts", "shares", "users"):
        db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    try:
        db.execute("DELETE FROM outreach WHERE user_id = ?", (user_id,))
    except sqlite3.OperationalError:
        pass  ## outreach table only exists once the editor agent has run
    ## links keep a "shared by" list for the digest; take them out of it
    for row in db.execute("SELECT url, shared_by FROM links").fetchall():
        sharers = json.loads(row["shared_by"] or "[]")
        kept = [s for s in sharers if not s.startswith(f"{user_id} in ")]
        if kept != sharers:
            db.execute("UPDATE links SET shared_by = ? WHERE url = ?", (json.dumps(kept), row["url"]))
    db.execute("INSERT OR IGNORE INTO forgotten VALUES (?)", (user_hash(user_id),))
    db.commit()


def write_profiles(db, path):
    lines = ["# Community profiles (draft)",
             "Facts are what people said in chat, not verified. Nothing here is ready to publish until the person confirms it.", ""]
    for user in db.execute("SELECT * FROM users ORDER BY message_count DESC"):
        name = md_text(user["display_name"] or user["user_id"], 80)
        rooms = ", ".join(md_text(r, 80) for r in json.loads(user["rooms"])) or "none"
        lines += [f"## {name} ({md_text(user['user_id'], 80)})",
                  f"- {user['message_count']} messages, {user['first_seen'][:10]} to {user['last_seen'][:10]}, rooms: {rooms}"]
        facts = db.execute("SELECT * FROM user_facts WHERE user_id = ? AND status != 'rejected' ORDER BY status DESC, category", (user["user_id"],)).fetchall()
        if facts:
            lines.append("- facts:")
            for f in facts:
                lines.append(f"  - [{f['status']}] ({f['category']}) {md_text(f['fact'])}")
                lines.append(f"    - said in {md_text(f['room'], 80)} on {f['said_at'][:10]}: \"{md_text(f['quote'])}\" (event {md_text(f['event_id'], 80)})")
        shares = db.execute("""SELECT s.url, s.shared_at, s.room, l.title, l.category, l.verdict FROM shares s LEFT JOIN links l ON l.url = s.url
                               WHERE s.user_id = ? ORDER BY s.shared_at DESC""", (user["user_id"],)).fetchall()
        if shares:
            lines.append("- shared:")
            for s in shares:
                flag = " [flagged by safety check]" if s["verdict"] in ("suspicious", "unsafe") else ""
                lines.append(f"  - {md_text(s['title'] or s['url'], 120)} ({md_text(s['category'] or 'unknown', 30)}){flag} <{md_url(s['url'])}> ({md_text(s['room'], 80)}, {s['shared_at'][:10]})")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
