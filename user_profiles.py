from util import ask_json
from untrusted_fetch import connect
from injection_screen import output_is_clean
from datetime import datetime, timezone
import json
import re

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
    """)
    return db


def strip_reply_fallback(body):
    """Matrix replies quote the earlier message in '> ' lines. Those words aren't this person's."""
    return "\n".join(line for line in body.splitlines() if not line.startswith("> ")).strip()


def record_activity(db, room_name, events, is_direct, display_names):
    """Update per-user counts and shares; returns {user_id: [messages]} for fact extraction (never from DMs)."""
    messages = {}
    for event in events:
        content = event["content"]
        if content.get("msgtype") not in ("m.text", "m.notice", "m.emote"):
            continue
        ## edits repeat the original, and bridges/bots post as m.notice
        if content.get("m.relates_to", {}).get("rel_type") == "m.replace" or content.get("msgtype") == "m.notice":
            continue
        user = event["sender"]
        when = datetime.fromtimestamp(event["origin_server_ts"] / 1000, timezone.utc).isoformat()
        row = db.execute("SELECT rooms FROM users WHERE user_id = ?", (user,)).fetchone()
        rooms = set(json.loads(row["rooms"])) if row else set()
        if not is_direct:
            rooms.add(room_name)
        db.execute("""INSERT INTO users (user_id, display_name, first_seen, last_seen, message_count, rooms) VALUES (?,?,?,?,1,?)
                      ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name,
                        first_seen = MIN(first_seen, excluded.first_seen), last_seen = MAX(last_seen, excluded.last_seen),
                        message_count = message_count + 1, rooms = excluded.rooms""",
                   (user, display_names.get(user, user), when, when, json.dumps(sorted(rooms))))
        if not is_direct:
            body = strip_reply_fallback(content.get("body", ""))
            if body:
                messages.setdefault(user, []).append({"event_id": event["event_id"], "room": room_name, "time": when, "body": body})
    db.commit()
    return messages


def record_shares(db, items):
    """items are second_agent link items, one per (message, url) before de-duplication."""
    for item in items:
        db.execute("INSERT OR IGNORE INTO shares VALUES (?,?,?,?,?,?)",
                   (item["sender"], item["url"], item["room"], item["event_id"], item["time"], item["context"][:300]))
    db.commit()


def normalize(text):
    return re.sub(r"\s+", " ", text).casefold().strip()


def extract_facts(db, user, messages):
    """Ask a model for self-stated facts, then keep only the ones that pass deterministic checks."""
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
                if not fact or source is None or CONTACT_PATTERN.search(fact + " " + quote) or not output_is_clean(fact):
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


def write_profiles(db, path):
    lines = ["# Community profiles (draft)",
             "Facts are what people said in chat, not verified. Nothing here is ready to publish until the person confirms it.", ""]
    for user in db.execute("SELECT * FROM users ORDER BY message_count DESC"):
        name = user["display_name"] or user["user_id"]
        lines += [f"## {name} ({user['user_id']})",
                  f"- {user['message_count']} messages, {user['first_seen'][:10]} to {user['last_seen'][:10]}, rooms: {', '.join(json.loads(user['rooms'])) or 'none'}"]
        facts = db.execute("SELECT * FROM user_facts WHERE user_id = ? AND status != 'rejected' ORDER BY status DESC, category", (user["user_id"],)).fetchall()
        if facts:
            lines.append("- facts:")
            for f in facts:
                lines.append(f"  - [{f['status']}] ({f['category']}) {f['fact']}")
                lines.append(f"    - said in {f['room']} on {f['said_at'][:10]}: \"{f['quote']}\" (event {f['event_id']})")
        shares = db.execute("""SELECT s.url, s.shared_at, s.room, l.title, l.category, l.verdict FROM shares s LEFT JOIN links l ON l.url = s.url
                               WHERE s.user_id = ? ORDER BY s.shared_at DESC""", (user["user_id"],)).fetchall()
        if shares:
            lines.append("- shared:")
            for s in shares:
                flag = " [flagged by safety check]" if s["verdict"] in ("suspicious", "unsafe") else ""
                lines.append(f"  - {(s['title'] or s['url']).replace(chr(10), ' ')} ({s['category'] or 'unknown'}){flag} {s['url']} ({s['room']}, {s['shared_at']})")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
