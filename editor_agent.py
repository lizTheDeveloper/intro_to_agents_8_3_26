from user_profiles import connect_profiles, forget_user, SENSITIVE_PATTERN
from person_scope import PersonScope
from injection_screen import md_text
from util import ask_json
from datetime import datetime, timezone
from urllib.parse import quote
import argparse
import hashlib
import json
import os
import re

import requests

## The editor agent: turns what second_agent noted about each member into a permission request,
## and sends it only after a human has approved that exact message.
##   draft    -> writes a message per person into the outreach table and outreach_drafts.md for you to read
##   approve  -> marks drafts as OK to send (one person, or --all)
##   send     -> DMs the approved drafts, asking for confirmation first
##   forget   -> deletes everything we hold about a person
## Sending uses MATRIX_EDITOR_TOKEN, a different token from the read-only MATRIX_TOKEN second_agent uses.
## The message text is built from a template, so no model writes words that go out to a person.

HOMESERVER = os.getenv("MATRIX_HOMESERVER", "https://matrix.themultiverse.school").rstrip("/")
MAGAZINE_NAME = os.getenv("MAGAZINE_NAME", "our community magazine")
MAX_LINKS_PER_PERSON = 6

SENSITIVE_CHECK_INSTRUCTIONS = (
    "For each numbered fact about a person, decide whether it reveals private or special-category information: "
    "health, disability, neurodivergence, sexuality, gender identity, religion, politics, finances, immigration status, "
    "family, legal trouble, or anything similarly personal. Return ONLY a JSON list of objects {\"id\": number, \"sensitive\": true or false}.")


def connect_outreach():
    db = connect_profiles()
    db.execute("""CREATE TABLE IF NOT EXISTS outreach (
        user_id TEXT PRIMARY KEY, message TEXT, status TEXT, created_at TEXT, approved_at TEXT,
        sent_at TEXT, room_id TEXT, event_id TEXT)""")
    return db


## ---------- drafting ----------

def find_sensitive(facts):
    """Returns the ids of facts to withhold, or None if the check couldn't be completed (then we withhold everything).
    A fact passes only if the model explicitly says it is not sensitive."""
    flagged = {f["id"] for f in facts if SENSITIVE_PATTERN.search(f["fact"] + " " + f["quote"])}
    rest = [f for f in facts if f["id"] not in flagged]
    if rest:
        result = ask_json(SENSITIVE_CHECK_INSTRUCTIONS, json.dumps([{"id": f["id"], "fact": f["fact"]} for f in rest]), "[")
        if not isinstance(result, list):
            return None
        answers = {r.get("id"): r.get("sensitive") for r in result if isinstance(r, dict)}
        flagged |= {f["id"] for f in rest if answers.get(f["id"]) is not False}
    return flagged


def one_line(text, limit):
    return re.sub(r"\s+", " ", str(text)).strip()[:limit]


def compose_message(name, facts, links):
    lines = [f"Hi {name}! This is an automated assistant helping put together {MAGAZINE_NAME}. "
             "We'd love to feature you, and nothing gets published unless you say yes.", ""]
    if facts:
        lines.append("Here's what we'd say about you, based on things you've shared in the community:")
        lines += [f"- {one_line(f['fact'], 200)}" for f in facts]
        lines.append("")
    if links:
        lines.append("And these are things you shared that we'd like to mention:")
        lines += [f"- {one_line(title, 80)}: {url}" for title, url in links]
        lines.append("")
    lines += [
        "Could you reply with:",
        "1. Are you happy to be featured? (yes or no)",
        "2. Anything above that's wrong, or that you'd rather we leave out.",
        "3. Anything else you'd like us to include about yourself. That can include anything personal, like your identity "
        "or background, if you want it in. We haven't included anything like that, and we won't unless you tell us to.",
        "",
        "If you'd rather not be featured at all, just say so and we'll delete what we've noted about you.",
    ]
    return "\n".join(lines)


def draft_for_person(scope):
    """Works on one person's snapshot only. Returns None if drafted, otherwise the reason it was skipped."""
    facts = scope.facts()
    withheld = find_sensitive(facts) if facts else set()
    if withheld is None:
        return "couldn't check facts for sensitive content"
    ## sensitive facts are deleted outright, not just left out of the message
    for fact_id in withheld:
        scope.delete_fact(fact_id)
    facts = scope.facts()
    links = scope.links(MAX_LINKS_PER_PERSON)
    if not facts and not links:
        return "nothing to feature yet"
    name = re.sub(r"[^\w .'-]", "", scope.display_name or "")[:40].strip() or "there"
    scope.set_draft(compose_message(name, facts, links))
    return None


def draft_messages(db):
    """Each person is drafted in isolation: the agent code only ever sees that person's PersonScope."""
    drafted, skipped = [], []
    for user_id in PersonScope.user_ids(db):
        existing = db.execute("SELECT status FROM outreach WHERE user_id = ?", (user_id,)).fetchone()
        if existing and existing["status"] in ("approved", "sent"):
            continue  ## never rewrite a message that has been approved or delivered
        scope = PersonScope.load(db, user_id)
        reason = draft_for_person(scope)
        scope.apply(db, datetime.now(timezone.utc).isoformat())
        (skipped if reason else drafted).append((user_id, reason) if reason else user_id)
    return drafted, skipped


def write_drafts_file(db, path):
    lines = ["# Outreach drafts", "Read each message, then run `python editor_agent.py approve <user_id>` (or `--all`). Nothing is sent until you run `send`.", ""]
    for row in db.execute("SELECT * FROM outreach ORDER BY status, user_id"):
        lines += [f"## {md_text(row['user_id'], 80)} [{row['status']}]", "", "```", row["message"].replace("```", "'''"), "```", ""]
    with open(path, "w") as f:
        f.write("\n".join(lines))


## ---------- sending ----------

def matrix(method, path, token, **kwargs):
    response = requests.request(method, f"{HOMESERVER}/_matrix/client/v3{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def dm_room_for(user_id, token):
    """Reuse an existing direct chat with this person, or start one."""
    me = matrix("GET", "/account/whoami", token)["user_id"]
    try:
        direct = matrix("GET", f"/user/{quote(me)}/account_data/m.direct", token)
    except requests.HTTPError:
        direct = {}
    if direct.get(user_id):
        return direct[user_id][0]
    room_id = matrix("POST", "/createRoom", token, json={"is_direct": True, "invite": [user_id], "preset": "trusted_private_chat"})["room_id"]
    direct[user_id] = [room_id]
    matrix("PUT", f"/user/{quote(me)}/account_data/m.direct", token, json=direct)
    return room_id


def send_approved(db, token):
    sent = 0
    for row in db.execute("SELECT * FROM outreach WHERE status = 'approved'").fetchall():
        try:
            room_id = dm_room_for(row["user_id"], token)
            ## a fixed transaction id per message means a retry can never deliver it twice
            txn = hashlib.sha256((row["user_id"] + row["created_at"]).encode()).hexdigest()[:24]
            event = matrix("PUT", f"/rooms/{quote(room_id)}/send/m.room.message/{txn}", token, json={"msgtype": "m.text", "body": row["message"]})
        except requests.RequestException as error:
            print(f"  failed for {row['user_id']}: {error}")
            continue
        db.execute("UPDATE outreach SET status = 'sent', sent_at = ?, room_id = ?, event_id = ? WHERE user_id = ?",
                   (datetime.now(timezone.utc).isoformat(), room_id, event["event_id"], row["user_id"]))
        db.commit()
        sent += 1
        print(f"  sent to {row['user_id']}")
    return sent


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("draft").add_argument("--out", default="outreach_drafts.md")
    approve = commands.add_parser("approve")
    approve.add_argument("user_ids", nargs="*")
    approve.add_argument("--all", action="store_true")
    send = commands.add_parser("send")
    send.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    commands.add_parser("forget").add_argument("user_id")
    args = parser.parse_args()

    db = connect_outreach()
    if args.command == "draft":
        drafted, skipped = draft_messages(db)
        write_drafts_file(db, args.out)
        print(f"Drafted {len(drafted)} messages into {args.out}. Skipped: {skipped or 'none'}")
    elif args.command == "approve":
        if not args.all and not args.user_ids:
            raise SystemExit("Name who to approve, or pass --all")
        marks = "" if args.all else " AND user_id IN (%s)" % ",".join("?" * len(args.user_ids))
        cursor = db.execute(f"UPDATE outreach SET status = 'approved', approved_at = ? WHERE status = 'draft'{marks}",
                            [datetime.now(timezone.utc).isoformat()] + ([] if args.all else args.user_ids))
        db.commit()
        print(f"Approved {cursor.rowcount} drafts")
    elif args.command == "send":
        token = os.getenv("MATRIX_EDITOR_TOKEN")
        if not token:
            raise SystemExit("Set MATRIX_EDITOR_TOKEN (a separate token from the read-only MATRIX_TOKEN)")
        waiting = db.execute("SELECT user_id FROM outreach WHERE status = 'approved'").fetchall()
        print(f"{len(waiting)} approved messages: {', '.join(r['user_id'] for r in waiting) or 'none'}")
        if waiting and (args.yes or input("Send them now? [y/N] ").strip().lower() == "y"):
            print(f"Sent {send_approved(db, token)} messages")
    elif args.command == "forget":
        forget_user(db, args.user_id)
        print(f"Deleted everything held about {args.user_id}")
