# Once-ever introductions

Automatic introductions default off. They invite active members who have never
joined The Ledger to consider participation. They do not enroll anyone, award
XP, change membership or tool clearances, or create sponsorship credit.

## Eligibility and activation

Run `ledger init` to install the owned relationship lookup index. Set
`LEDGER_AUTO_INTRODUCTIONS_ENABLED=true` consistently for web and workers, then
restart them. Set `LEDGER_FACILITIES_SHOP_ID` to the source shop ID, or leave it
blank to resolve exactly one shop whose trimmed, case-insensitive name is
Facilities. Invalid or ambiguous configuration blocks activation. The resolved
ID is saved so renaming the shop cannot change its meaning during an activation.

The first periodic reconciliation (also scheduled at accounting-worker startup)
baselines source members' status and checkout history without messaging anyone.
It reads bounded pages and retries interrupted pages safely. The durable
`ledger_catalog` record `automatic-introductions` changes from `baseline` to
`active` and records `activated_at` on completion. Source reads use `MLAB_URI`;
all new state and indexes use existing `LEDGER_URI` collections.

After activation, either of these future changes can qualify:

- First-ever checkout outside Facilities, with a verified timestamp at or after
  activation and a non-revoked checkout. Historical revoked checkouts still
  count as prior history. Missing dates or unresolved tool/shop history cannot
  prove a new first checkout.
- Observed source status transition from `expired` to `activeMember`.
  **The approved attached plan requires no checkout for renewal.**

Both paths require `activeMember`, a unique Slack mapping, an active human Slack
account, no merged identity or `SLACKIDS_INELIGIBLE` exclusion, and no previous
Ledger participant record. `pending` is not active. Delivery and consent recheck
eligibility. No participant profile is created before explicit consent.

MQTT schedules authoritative source reads; packet contents never authorize a
send. Periodic scans include nonparticipants and recover missed notifications.
A complete expiry/renewal cycle missed by both events and polling cannot be
reconstructed. Concurrent observations retry with fresh source reads rather than
overwrite newer snapshots. Reads exceeding 1,000 checkouts per member fail rather
than guess. Disabling and reenabling performs a fresh baseline without replaying
activity from the disabled interval.

## Member experience

The initial DM asks “Hi! Do you enjoy role-playing games?” with Yes/No buttons.
Clear typed replies in that DM/thread also work. One unclear reply may receive
further explanation and buttons without extending the deadline. Unrelated
threads and questions retain ordinary routing; edited or old messages do not
advance this flow.

A Yes click opens the existing consent form with an explanation. Typed Yes sends
the explanation with an “Explore The Ledger and opt in” button, since Slack
requires an interaction trigger to open a modal. The explanation also offers No
thanks. Yes expresses interest; only the explicit checkbox and successful consent
submission enroll the member.

Each stage gets 14 days from confirmed question delivery and then from the first
available explanation, through the modal or DM. Closing the modal leaves it
pending; reopening never restarts the clock. No reminders are sent. No or timeout
closes the flow and deletes every bot-authored introduction message. Slack cannot
erase the member's replies or entire DM through bot message deletion. Stale
buttons/forms cannot restart the flow; `/ledger join` and peer invitations remain
available. Every join path stops pending introduction delivery in the consent
transaction. Successful joining removes old controls; an already declined or
expired introduction still finishes deletion.

## Durability and operations

`ledger_relationships` stores permanent `automatic_introduction` records keyed by
member ID and `automatic_introduction_identity` reservations preventing another
introduction after a source merge replaces the member ID under the same Slack
identity. Neither has a TTL. They are separate from participation, preferences,
and sponsorship. Decline, timeout, account relinking, and cleanup never reset the
lifetime marker or change peer-invitation preferences and attribution.

`intro_scan`/`intro_observe` use the accounting inbox. `intro_send` uses the delivery
outbox with fixed Python text and no inference. `intro_maintain` uses the independent
channel worker for deadlines, recovery, and cleanup. Periodic work converges on
one 15-minute polling bucket per member; separate deadline jobs and interaction
checks enforce expiry. Cleanup continues while paused or disabled and deletes
known receipts before reading sources. Disabling suppresses outstanding flows.

Each post has a stable `client_msg_id` and a durable `posting` reservation before
Slack is called. Explicit rejections may retry the same delivery. Crashes and
uncertain network results become `unknown` and are **never blindly reposted**.
Matching bot events or paginated DM history recover the original timestamp, even
after closure, so cleanup can delete the message. Unresolved uncertainty may mean
a missed introduction. Never delete lifetime records or reset `posting`/`unknown`
to force a send.

`/ledger-admin metrics` includes `automatic_introductions.states`,
`uncertain_deliveries`, and `cleanup_pending`. Inspect failed `intro_*` jobs and
sanitized `last_error` values for authorization/source errors. Cleanup retains its
receipts and schedules another bounded poll on failure; already-deleted messages
count as success. Replies do not enter conversation context or inference. The
existing durable Slack inbox remains the ingress record.

## Verification and rollout

Run both prompt validators and the full pytest suite. Mongo concurrency tests use
`LEDGER_TEST_MONGO_URI` and disposable databases; memory tests do not establish
replica-set behavior. Before broad enablement, pilot Slack human/blacklist checks,
buttons and typed replies, modal close/reopen, opt-in, both timeouts, deletion,
uncertain receipt recovery, and cleanup while inference is offline or maintenance
is enabled. Existing manifest capabilities suffice: `chat:write`, `im:write`,
`im:history`, user lookup, interactivity, and `message.im` events.

Policy version 53 adds the narrow nonparticipant introduction exception.
Google Doc operators must apply the complete updated policy and explicitly run
`/ledger-admin reload-prompts`. Nothing publishes that document automatically.
Previously reserved prompt deliveries keep their policy/text.
