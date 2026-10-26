from untrusted_fetch import process_and_store
import user_profiles
from injection_screen import md_text, md_url
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


def matrix_get(path, params=None, headers=None, api="/_matrix/client/v3"):
    response = requests.get(f"{HOMESERVER}{api}{path}", headers=headers or HEADERS, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def admin_get(path, params=None):
    """Synapse admin API, with a token that is only ever passed in for one command (never stored in .env)."""
    return matrix_get(path, params, headers={"Authorization": f"Bearer {os.environ['MATRIX_ADMIN_TOKEN']}"}, api="/_synapse/admin/v1")


def get_display_name(user_id):
    try:
        return matrix_get(f"/profile/{quote(user_id)}/displayname").get("displayname") or user_id
    except requests.HTTPError:
        return user_id


def direct_room_ids():
    """Rooms the account itself has marked as direct messages."""
    me = matrix_get("/account/whoami")["user_id"]
    try:
        data = matrix_get(f"/user/{quote(me)}/account_data/m.direct")
    except requests.HTTPError:
        return set()
    return {room for rooms in data.values() for room in rooms}


def is_private_room(room_id, direct_rooms):
    """Anything that isn't clearly community-visible counts as private. Private rooms still feed your own link
    digest, but are never used for member profiles. If we can't tell, it's private."""
    if room_id in direct_rooms:
        return True
    try:
        if len(matrix_get(f"/rooms/{quote(room_id)}/joined_members")["joined"]) <= 2:
            return True
        join_rule = matrix_get(f"/rooms/{quote(room_id)}/state/m.room.join_rules").get("join_rule")
    except requests.HTTPError:
        return True
    return join_rule not in ("public", "restricted")  ## invite, knock, knock_restricted, private


def get_room_name(room_id):
    try:
        return matrix_get(f"/rooms/{quote(room_id)}/state/m.room.name")["name"]
    except requests.HTTPError:
        return room_id  ## unnamed rooms (DMs, etc.)


def fetch_room_messages(room_id, since_ms, as_admin=False):
    """Page backwards through a room until we pass the cutoff. as_admin reads through the Synapse admin API,
    which needs no membership and ignores the room's history visibility."""
    events = []
    page_token = None
    while True:
        params = {"dir": "b", "limit": 100}
        if page_token:
            params["from"] = page_token
        if as_admin:
            page = admin_get(f"/rooms/{quote(room_id)}/messages", params)
        else:
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
            lines.append(f"- [{md_text(r['title'] or r['url'], 120)}](<{md_url(r['url'])}>)")
            for detail in (r["summary"], r["why"]):
                if detail:
                    lines.append(f"  - {md_text(detail, 600)}")
            lines.append(f"  - shared by: {md_text('; '.join(r['shared_by']), 600)}")
        lines.append("")
    if flagged:
        lines += ["## flagged by safety check (not summarized, open with care)"]
        lines += [f"- <{md_url(r['url'])}> [{r['verdict']}]: {md_text('; '.join(r['reasons'][:3]), 300)}" for r in flagged] + [""]
    if failed:
        lines += ["## couldn't fetch"]
        lines += [f"- <{md_url(r['url'])}>: {md_text(r['error'], 200)} (shared by {md_text('; '.join(r['shared_by'][:2]), 200)})" for r in failed] + [""]
    with open(path, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="how far back to look")
    parser.add_argument("--rooms", nargs="*", help="only read rooms whose name contains one of these (case-insensitive)")
    parser.add_argument("--no-profiles", action="store_true", help="gather links only; don't track people or extract facts")
    parser.add_argument("--admin-room", help="read this one room id's history through the admin API (needs MATRIX_ADMIN_TOKEN "
                        "for this command only). Always links only: no people, no facts.")
    parser.add_argument("--markdown", action="store_true", help="also write matrix_digest.md and user_profiles.md (the database is the main output)")
    parser.add_argument("--out", default="matrix_digest.md")
    parser.add_argument("--profiles-out", default="user_profiles.md")
    args = parser.parse_args()

    if not os.getenv("MATRIX_TOKEN"):
        raise SystemExit("Set MATRIX_TOKEN (and MATRIX_HOMESERVER if not matrix.org) in your .env")

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)

    if args.admin_room:
        if not os.getenv("MATRIX_ADMIN_TOKEN"):
            raise SystemExit("--admin-room needs MATRIX_ADMIN_TOKEN set for this command")
        room_name = admin_get(f"/rooms/{quote(args.admin_room)}").get("name") or args.admin_room
        events = fetch_room_messages(args.admin_room, since_ms, as_admin=True)
        ## links only: this read skips the room's history setting, so nothing about people is kept
        items = extract_items(room_name, events)
        for item in items:
            item["private"] = True
            item["sender"] = "a member"  ## who posted it isn't kept
        print(f"{room_name}: {len(events)} messages, {len(items)} links (admin read, links only)", flush=True)
        rows = process_and_store(dedupe(items))
        print(f"Saved {len(rows)} links to the database")
        if args.markdown:
            write_digest(rows, args.days, args.out)
            print(f"Wrote {args.out}")
        raise SystemExit(0)

    all_items = []
    db = user_profiles.connect_profiles()
    display_names = {}
    user_messages = {}
    direct_rooms = direct_room_ids()
    for room_id in matrix_get("/joined_rooms")["joined_rooms"]:
        room_name = get_room_name(room_id)
        if args.rooms and not any(wanted.lower() in room_name.lower() for wanted in args.rooms):
            continue
        events = fetch_room_messages(room_id, since_ms)
        room_items = extract_items(room_name, events)
        for sender in {e["sender"] for e in events} - display_names.keys():
            display_names[sender] = get_display_name(sender)
        private = is_private_room(room_id, direct_rooms)
        for item in room_items:
            item["private"] = private
        if not private and not args.no_profiles:
            room_messages = user_profiles.record_activity(db, room_name, events, display_names)
            for user, messages in room_messages.items():
                user_messages.setdefault(user, []).extend(messages)
        print(f"{room_name}: {len(events)} messages, {len(room_items)} links{' (private: links only, no profiles)' if private else ''}")
        all_items.extend(room_items)

    if not args.no_profiles:
        user_profiles.record_shares(db, all_items)
    items = dedupe(all_items)
    rows = process_and_store(items)
    print(f"Saved {len(rows)} links to the database")
    if args.markdown:
        write_digest(rows, args.days, args.out)
        print(f"Wrote {args.out}")

    if args.no_profiles:
        raise SystemExit(0)
    new_facts = sum(user_profiles.extract_facts(db, user, messages) for user, messages in user_messages.items())
    print(f"Noted {new_facts} new facts across {len(user_messages)} people")
    if args.markdown:
        user_profiles.write_profiles(db, args.profiles_out)
        print(f"Wrote {args.profiles_out}")
    print("Query it with: sqlite3 community.db 'SELECT * FROM profile_summary'")
