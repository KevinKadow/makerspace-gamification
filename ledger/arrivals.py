"""Canonical check-in identity, one random draw, and durable member cooldowns."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import os
import random
import re

from .domain import Denied, Ledger
from .engagement import enabled
from .sources import object_id, sid
from .storage import enqueue, now


def checkin_time(doc):
    value = doc.get("timeOf") or doc.get("time")
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc)
    try:
        value = float(value)
        return datetime.fromtimestamp(value / 1000 if value > 100000000000 else value, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class Arrivals:
    def __init__(self, ledger, draw=None):
        self.l, self.draw = ledger, draw or random.SystemRandom().random

    def resolve(self, checkin_id):
        rows = self.l.sources.rows("checkins", {"_id": object_id(checkin_id)})
        if len(rows) != 1:
            return None
        record = rows[0]
        stamp = checkin_time(record)
        if not stamp or not now() - timedelta(minutes=10) <= stamp <= now() + timedelta(seconds=30):
            return None
        if not record.get("uid"):
            return None
        cards = self.l.sources.rows("cards", {"uid": record["uid"]})
        if len(cards) != 1 or not cards[0].get("member_id") or cards[0].get("validity") in ("lost", "stolen", "suspended", "revoked"):
            return None
        member = sid(cards[0]["member_id"])
        if not self.l.active(member) or not self.l.sources.good_standing(member):
            return None
        return member

    def reserve(self, checkin_id):
        if not enabled("WELCOMES"):
            return None
        def run(s):
            d = Ledger(s, self.l.sources)
            service = Arrivals(d, self.draw)
            key = "arrival:" + checkin_id
            prior = s.get("ledger_evidence", key)
            if prior:
                return prior
            member = service.resolve(checkin_id)
            if not member:
                return None
            p = d.participant(member)
            probability = float(os.environ.get("LEDGER_WELCOME_PROBABILITY", "0.2"))
            if not 0 <= probability <= 1:
                raise ValueError("Welcome probability must be between zero and one")
            selected = self.draw() < probability
            doc = {"_id": key, "kind": "arrival", "member_id": member, "checkin_id": checkin_id,
                   "selected": selected, "status": "skipped", "at": now(), "slot": p["rank"]}
            cooldown_key = "welcome-cooldown:" + member
            cooldown = s.get("ledger_evidence", cooldown_key)
            channel = s.get("ledger_channels", f"rank:{p['rank']}")
            membership = s.get("ledger_channels", f"membership:{member}:rank:{p['rank']}") or {}
            if (selected and p.get("preferences", {}).get("arrival_mentions", True) and channel and membership.get("present")
                    and not membership.get("voluntary_leave") and (not cooldown or now() - cooldown["at"] >= timedelta(days=10))):
                d.touch(member)
                s.put("ledger_evidence", {"_id": cooldown_key, "kind": "welcome_cooldown", "member_id": member, "at": now(), "arrival": key})
                doc.update(status="reserved", channel=channel["channel_id"], consent_generation=p.get("consent_generation", 0))
                enqueue(s, "ledger_outbox", key, "welcome", {"member_id": member, "arrival": key})
            s.put("ledger_evidence", doc)
            return doc
        return self.l.store.atomic(run)

    def favorite_shop(self, member):
        cleared = {sid(c["tool_id"]) for c in self.l.sources.rows("tool_checkouts", {"member_id": object_id(member), "revoked_at": None})}
        shops = {sid(s["_id"]): s for s in self.l.sources.rows("shops", {"disabled": {"$ne": True}})}
        counts = Counter(sid(t.get("shop_id")) for t in self.l.sources.rows("tools", {"disabled": {"$ne": True}})
                         if sid(t["_id"]) in cleared and sid(t.get("shop_id")) in shops)
        if not counts:
            return None
        best = min(counts, key=lambda i: (-counts[i], shops[i]["name"].casefold(), i))
        return shops[best]["name"]

    def live(self, arrival):
        member = arrival["member_id"]
        p = self.l.participant(member)
        channel = self.l.store.get("ledger_channels", f"rank:{arrival['slot']}")
        membership = self.l.store.get("ledger_channels", f"membership:{member}:rank:{arrival['slot']}") or {}
        return bool(enabled("WELCOMES") and self.resolve(arrival["checkin_id"]) == member and p and p["rank"] == arrival["slot"]
                    and p.get("preferences", {}).get("arrival_mentions", True) and p.get("consent_generation", 0) == arrival["consent_generation"]
                    and channel and channel["channel_id"] == arrival["channel"] and membership.get("present") and not membership.get("voluntary_leave"))

    def deliver(self, worker, job):
        arrival = self.l.store.get("ledger_evidence", job["payload"]["arrival"])
        if not arrival or arrival["status"] != "reserved":
            return  # Attempted/uncertain sends never release the reserved cooldown.
        if not self.live(arrival):
            raise Denied("This arrival is no longer deliverable.")
        uid = worker.valid_identity(arrival["member_id"])
        if not uid or not re.fullmatch(r"[UW][A-Z0-9]+", uid):
            raise Denied("Arrival identity is invalid.")
        facts = {"summary": "Welcome this arriving member to their current rank channel.", "rank": self.l.presentation(arrival["slot"])["name"]}
        try:
            shop = self.favorite_shop(arrival["member_id"])
            if shop:
                facts["shop"] = shop
        except Exception:
            pass  # Shop reads are optional and never prevent canonical consent checks.
        composed = worker.persist_composition(job, "status", "shared", facts)
        text = re.sub(r"<[^>]*>|@(?:here|channel|everyone)\b", "", composed["text"], flags=re.I).strip()[:1800]
        def begin(s):
            d = Ledger(s, self.l.sources)
            current = s.get("ledger_evidence", arrival["_id"])
            live_job = s.get("ledger_outbox", job["_id"])
            if not current or current["status"] != "reserved" or not live_job or live_job.get("lease") != job["lease"] or live_job["status"] != "working" or not Arrivals(d).live(current):
                raise Denied("Arrival consent or routing changed.")
            d.touch(arrival["member_id"])
            current.update(status="attempted", attempted_at=now())
            s.put("ledger_evidence", current)
        self.l.store.atomic(begin)
        receipt = worker.post_message(channel=arrival["channel"], text=text + f" <@{uid}>", unfurl_links=False, unfurl_media=False)
        def complete(s):
            saved = s.get("ledger_evidence", arrival["_id"])
            saved.update(status="delivered", delivered_at=now(), ts=receipt["ts"])
            s.put("ledger_evidence", saved)
        self.l.store.atomic(complete)
