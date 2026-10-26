from untrusted_fetch import process_and_store
import user_profiles
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import argparse
import json
import os
import re
import requests

## READ-ONLY: this agent only ever makes GET requests to Matrix. It has no code path
## that sends a message, joins a room, or marks anything as read.

HOMESERVER = os.getenv("MATRIX_HOMESERVER", "https://matrix.themultiverse.school").rstrip("/")
HEADERS = {"Authorization": f"Bearer {os.getenv('MATRIX_TOKEN')}"}

URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+")
## links that are Matrix plumbing, not things people made
IGNORED_PREFIXES = ("https://matrix.to/", "https://matrix.org/_matrix/")


def matrix_get(path, params=None):
    response = requests.get(f"{HOMESERVER}/_matrix/client/v3{path}", headers=HEADERS, params=params)
    response.raise_for_status()
    return response.json()


def get_display_name(user_id):
    try:
        return matrix_get(f"/profile/{quote(user_id)}/displayname").get("displayname") or user_id
    except requests.HTTPError:
        return user_id


def is_direct_message(room_id):
    """Rooms with two or fewer members are treated as private conversations and never mined for profile facts."""
    members = matrix_get(f"/rooms/{quote(room_id)}/joined_members")["joined"]
    return len(members) <= 2


def get_room_name(room_id):
    try:
        return matrix_get(f"/rooms/{quote(room_id)}/state/m.room.name")["name"]
    except requests.HTTPError:
        return room_id  ## unnamed rooms (DMs, etc.)


def fetch_room_messages(room_id, since_ms):
    """Page backwards through a room until we pass the cutoff."""
    events = []
    page_token = None
    while True:
        params = {"dir": "b", "limit": 100}
        if page_token:
            params["from"] = page_token
        page = matrix_get(f"/rooms/{quote(room_id)}/messages", params)
        for event in page.get("chunk", []):
            if event["origin_server_ts"] < since_ms:
                return events
            if event["type"] == "m.room.message":
                events.append(event)
        page_token = page.get("end")
        if not page_token or not page.get("chunk"):
            return events


def extract_items(room_name, events):
    items = []
    for event in events:
        content = event["content"]
        body = content.get("body", "")
        ## edits repeat the original message, so only keep originals
        if "m.relates_to" in content and content["m.relates_to"].get("rel_type") == "m.replace":
            continue
        links = set(URL_PATTERN.findall(body + " " + content.get("formatted_body", "")))
        for link in links:
            link = link.rstrip(".,;")
            if link.startswith(IGNORED_PREFIXES):
                continue
            items.append({
                "event_id": event["event_id"],
                "url": link,
                "room": room_name,
                "sender": event["sender"],
                "time": datetime.fromtimestamp(event["origin_server_ts"] / 1000, timezone.utc).strftime("%Y-%m-%d"),
                "context": body[:300],
            })
        ## files people uploaded directly (pdfs, docs, images)
        if content.get("msgtype") in ("m.file", "m.image", "m.video") and content.get("url"):
            items.append({
                "event_id": event["event_id"],
                "url": content["url"],  ## mxc:// url, needs the homeserver to download
                "room": room_name,
                "sender": event["sender"],
                "time": datetime.fromtimestamp(event["origin_server_ts"] / 1000, timezone.utc).strftime("%Y-%m-%d"),
                "context": f"[file upload] {body}"[:300],
            })
    return items


def dedupe(items):
    """Same link shared in several places collapses into one entry listing everyone who shared it."""
    by_url = {}
    for item in items:
        if item["url"] in by_url:
            by_url[item["url"]]["shared_by"].append(f"{item['sender']} in {item['room']} ({item['time']})")
        else:
            by_url[item["url"]] = {**item, "shared_by": [f"{item['sender']} in {item['room']} ({item['time']})"]}
    return list(by_url.values())


def write_digest(rows, days, path):
    lines = [f"# Matrix digest: last {days} days", f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}, {len(rows)} links", ""]
    ok = [r for r in rows if r["verdict"] == "safe" or r["status"] == "unanalyzed"]
    flagged = [r for r in rows if r["verdict"] in ("suspicious", "unsafe")]
    failed = [r for r in rows if r["status"] == "failed"]
    for category in sorted({r["category"] for r in ok}):
        lines.append(f"## {category}")
        for r in (r for r in ok if r["category"] == category):
            lines.append(f"- [{r['title'] or r['url']}]({r['url']})")
            for detail in (r["summary"], r["why"]):
                if detail:
                    lines.append(f"  - {detail}")
            lines.append(f"  - shared by: {'; '.join(r['shared_by'])}")
        lines.append("")
    if flagged:
        lines += ["## flagged by safety check (not summarized, open with care)"]
        lines += [f"- {r['url']} [{r['verdict']}]: {'; '.join(r['reasons'][:3])}" for r in flagged] + [""]
    if failed:
        lines += ["## couldn't fetch"]
        lines += [f"- {r['url']}: {r['error']} (shared by {'; '.join(r['shared_by'][:2])})" for r in failed] + [""]
    with open(path, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="how far back to look")
    parser.add_argument("--out", default="matrix_digest.md")
    parser.add_argument("--profiles-out", default="user_profiles.md")
    args = parser.parse_args()

    if not os.getenv("MATRIX_TOKEN"):
        raise SystemExit("Set MATRIX_TOKEN (and MATRIX_HOMESERVER if not matrix.org) in your .env")

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
    all_items = []
    db = user_profiles.connect_profiles()
    display_names = {}
    user_messages = {}
    for room_id in matrix_get("/joined_rooms")["joined_rooms"]:
        room_name = get_room_name(room_id)
        events = fetch_room_messages(room_id, since_ms)
        room_items = extract_items(room_name, events)
        for sender in {e["sender"] for e in events} - display_names.keys():
            display_names[sender] = get_display_name(sender)
        room_messages = user_profiles.record_activity(db, room_name, events, is_direct_message(room_id), display_names)
        for user, messages in room_messages.items():
            user_messages.setdefault(user, []).extend(messages)
        print(f"{room_name}: {len(events)} messages, {len(room_items)} links")
        all_items.extend(room_items)

    user_profiles.record_shares(db, all_items)
    items = dedupe(all_items)
    rows = process_and_store(items)
    write_digest(rows, args.days, args.out)
    print(f"Wrote {len(rows)} links to {args.out}")

    new_facts = sum(user_profiles.extract_facts(db, user, messages) for user, messages in user_messages.items())
    user_profiles.write_profiles(db, args.profiles_out)
    print(f"Noted {new_facts} new facts across {len(user_messages)} people, wrote {args.profiles_out}")
