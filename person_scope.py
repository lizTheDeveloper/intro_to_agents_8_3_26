## A snapshot of everything held about ONE person, and nothing else. The outreach and interview agents are
## handed one of these and never a database handle, so an agent (or a model's tool call) working on Alice has
## no way to ask for Bob's facts, shares, or messages. The orchestrating code loads the snapshot, lets the agent
## work on it, then applies the changes the agent asked for. Those changes are restricted to this person's rows.
##
## What this protects: facts noted about a person stay inside that person's own conversation until they are
## confirmed and published. What it assumes: things said in a room everyone shares are already visible to that room.

class PersonScope:
    def __init__(self, user_id, display_name, facts, links):
        self.user_id = user_id
        self.display_name = display_name
        self._facts = [dict(f) for f in facts]
        self._links = [tuple(link) for link in links]
        self._deleted_fact_ids = set()
        self._draft = None

    @classmethod
    def load(cls, db, user_id):
        user = db.execute("SELECT display_name FROM users WHERE user_id = ?", (user_id,)).fetchone()
        facts = db.execute("SELECT id, fact, category, quote, room, said_at, status FROM user_facts WHERE user_id = ? AND status != 'rejected'",
                           (user_id,)).fetchall()
        ## only links this person shared themselves, and only ones that passed the safety screen
        links = db.execute("""SELECT DISTINCT l.title, l.url FROM shares s JOIN links l ON l.url = s.url
                              WHERE s.user_id = ? AND l.verdict = 'safe' AND l.status = 'ok' ORDER BY s.shared_at DESC LIMIT 20""",
                           (user_id,)).fetchall()
        return cls(user_id, user["display_name"] if user else "", facts, [(l["title"] or l["url"], l["url"]) for l in links])

    @staticmethod
    def user_ids(db):
        """The orchestrator's list of who to process: ids only, no content."""
        from user_profiles import is_forgotten
        return [r["user_id"] for r in db.execute("SELECT user_id FROM users ORDER BY message_count DESC") if not is_forgotten(db, r["user_id"])]

    def facts(self):
        return [f for f in self._facts if f["id"] not in self._deleted_fact_ids]

    def links(self, limit=6):
        return self._links[:limit]

    def delete_fact(self, fact_id):
        if any(f["id"] == fact_id for f in self._facts):
            self._deleted_fact_ids.add(fact_id)

    def set_draft(self, message):
        self._draft = message

    def apply(self, db, now):
        """Write this person's pending changes. Every statement is pinned to this person's user_id."""
        for fact_id in self._deleted_fact_ids:
            db.execute("DELETE FROM user_facts WHERE id = ? AND user_id = ?", (fact_id, self.user_id))
        if self._draft is not None:
            db.execute("""INSERT INTO outreach (user_id, message, status, created_at) VALUES (?,?, 'draft', ?)
                          ON CONFLICT(user_id) DO UPDATE SET message = excluded.message, status = 'draft', created_at = excluded.created_at""",
                       (self.user_id, self._draft, now))
        db.commit()
