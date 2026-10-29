# Security anomaly monitoring

Security monitoring runs inside `ledger-anomaly`, using its PostgreSQL volume,
read-only Mongo streams, signed Ledger Slack ingress and existing inference API.
It adds no model, source writes, service, or host-local dependency. The independent
Compose stack works with remote Mongo, Ledger queue, Slack and inference endpoints.

## Enable and route alerts

Set these on the analytics deployment; keep `ANOMALY_ENABLED=true` on Ledger's
Slack ingress deployment as described in [Slack access](ANOMALY_SLACK.md).

```dotenv
ANOMALY_SECURITY_ENABLED=true
ANOMALY_SECURITY_ALERT_MODE=active
ANOMALY_SECURITY_DM_USER_IDS=["U0123456789","U9876543210"]
ANOMALY_SECURITY_RULES_PATH=/app/ledger/anomaly/security_rules.json
ANOMALY_SECURITY_EVALUATION_SECONDS=15
```

Monitoring defaults off. `shadow` records findings without sending alerts.
Explicit reviewed rules become active on opt-in; learned rules remain shadow.
Rule customization requires a versioned JSON file with the packaged catalog's
closed shape. Mount an override read-only and set its container path. Rule content
hashes identify revisions, so changes do not reuse prior approvals.

Only configured current admin/board Slack users receive DMs. Each attempt checks
active human identity, unique source linkage, unmerged membership, and source role.
Expired staff remain eligible; suspended, revoked and unknown statuses do not.
No recipients or no authorized recipients produces `security_delivery` health
status `configuration_problem`. Findings continue; there is no channel fallback.

Critical incidents take priority within ten messages per recipient per UTC hour.
Repeated rate signals have a 24-hour detector/entity/recipient cooldown. Rate
overflow is summarized; critical overflow and failed deliveries remain pending.
Missing Slack or Ledger queue configuration also leaves deliveries pending for
retry after configuration is restored and the service restarts. Verified loss of
recipient authorization cancels that recipient's delivery.
Each verified privilege chain has its own incident and deterministic delivery ID.
Shadow and historical backfill incidents are never converted into alert floods.

## Evidence and limits

Source corrections and deletions revalidate resolved incidents as well as open
and shadow incidents. Unsupported findings become retracted and leave aggregate
reports on the next report refresh. Findings that still qualify keep their staff
resolution and refreshed evidence; revalidation never reopens their alerts.

The separate projection keeps category markers, dated events, member/actor IDs,
reviewed role transitions, evidence references and confidence. It never stores
password hashes, reset tokens, changed profile values, email addresses/content,
raw webhook payloads, or IP addresses. Email comparisons use a local persistent
secret and domain-separated HMAC fingerprints for domain, alias family, local
part and trigrams. Back up the PostgreSQL volume, including that key.

Member update descriptions supply operation counts before sensitive values are
discarded. One edit affecting several profile fields counts once. Both reset
fields in one update count once; token clearing is not issuance. Snapshots and
replacements do not manufacture change operations. `audit_logs` supplies dated
registrations, historical profile/password activity, actors, roles and MFA
evidence. Matching stream/audit operations count once; audit-only history is
explicitly incomplete. Registration requires `member_registered` evidence;
ObjectId timestamps and mutable membership-start fields do not establish signup.
Other account creation is reported separately.

`mailtrap` provider events are deduplicated by provider event ID. Distinct messages
use `mailtrap_message_id` and provider message IDs, with `mailtrap_messages` linkage
for member attribution when available. Bounce denominators count distinct known
delivery/bounce message outcomes, not webhook deliveries or all sent mail. Missing
message identifiers are excluded and denominator uncertainty remains explicit.

Reset lifetime follows Rails' 24-hour configuration. Password change plus token
clearing supports **observed completion**; other password changes support
**possible completion**. Other labels are pending, no observed completion and
incomplete coverage. Later issuances supersede earlier tokens. Onboarding resets
are marked. Mongo cannot observe requests that caused no database change; no
missing event is called a failed reset.

MFA disabling/reset requires an attributed audit and privileged role evidence
dated at or before the event. Missing historical role evidence cannot prove a
privileged MFA incident. Unattributed changes remain in normalized evidence and
aggregate privilege trends without claiming a verified chain.

## Packaged detectors

| Signal | Default review threshold |
| --- | --- |
| Profile edits | 5/member/15 minutes, or 20 across at least 5 members |
| Password changes | 3/member/hour, or 10 across at least 5 members |
| Reset issuance | 5/member/15 minutes, or 20 across at least 5 members |
| Similar registrations/account creations | 3 plus-family accounts/hour, or 4 similar accounts/hour |
| Confirmed self-service registrations | 10/15 minutes |
| Bounces | 5 distinct messages/hour, at least 20% of at least 20 known outcomes |
| Privilege propagation | Attributed promotion to board/admin, then that subject grants another account privileges within inclusive 48 hours |
| Privileged MFA disable/reset | Every verified event |
| Bulk privilege grants | One attributed actor grants additional privileges to 3 distinct accounts/hour |

Role order is member, resource manager, board, admin. First-step promotions
include board to admin; reductions and no-op edits are excluded. Second-step
grants include additional resource-manager scopes. Similarity uses normalized
local-part equality across domains, excluding generic mailboxes, or trigram
Jaccard similarity at least 0.8 for local parts of at least eight characters.
Every cluster member must match its representative. Accounts are never merged.
Candidate and comparison budgets report bounded coverage in health when exceeded.

## Persistence, coverage and learned rules

Indexed PostgreSQL events and dirty-hour rollups commit with capture processing.
Replay receipts and checkpoints survive restart. Reconciliation does not add
unchanged snapshots; corrections/deletions replace source contributions, dirty
affected hours and retract unsupported incidents. Incidents retain sanitized
evidence snapshots. Pending alerts recheck their evidence before delivery.

Coverage intervals begin at stream opening and extend only after caught-up
batches. Reconnect downtime is conservatively incomplete even when replay
recovers events. Zero counts enter learning only for complete monitored hours.
Historical backfill is incremental and bounded; current exact signals are
evaluated first. Reports refresh every five minutes; membership analytics keeps
its independent hourly schedule.

Learned rules use local weekday/hour, twelve weeks, at least eight complete
comparable observations, median/MAD and empirical upper quantiles with absolute
floors. Repeated DST hours are one calendar observation. Unresolved anomalous
hours are excluded from automatic learning; frozen comparison baselines expose
gradual drift. Chronological holdouts, seven days of recorded shadow observation,
50 evaluated windows, a passing alert budget and exact-revision staff approval
are required. Corrections supersede learned generations and require evaluation
and approval again; old models and evidence remain reviewable.

Normalized security history follows `ANOMALY_HISTORY_MONTHS` (60 by default).
Processed captures follow `ANOMALY_EVENT_RETENTION_DAYS` (90). Unresolved incident
evidence and pending deliveries are preserved. When resolved or retracted incidents
expire, retention also removes their completed delivery receipts and message
snapshots in the same transaction, including orphans left by older retention code.
A pending summary retains all referenced incidents, evidence and receipts until
its delivery finishes. Completed summaries containing an expired incident are
removed as a whole; receipts for other, retained incidents still prevent duplicate
delivery. No detector changes accounts,
membership, passwords, access, billing, roles or disciplinary decisions.

## Commands and dashboards

```text
/ledger-anomaly security summary
/ledger-anomaly security incidents
/ledger-anomaly security incident <incident ID>
/ledger-anomaly security resolve <incident ID>
/ledger-anomaly security detectors list
/ledger-anomaly security detectors review <revision>
/ledger-anomaly security detectors propose
/ledger-anomaly security detectors approve <revision>
/ledger-anomaly security detectors disable <revision>
```

Anomaly-channel readers may request suppressed aggregate security summaries in
the channel or their own DM. Incident details and detector/incident administration
require a verified staff DM, independent of game participation. Authorization is
rechecked before execution and delivery. AI receives only suppressed aggregates
and the closed feature catalog; it cannot choose queries, change schema, grant
permissions or activate detectors.

Rerun the existing Metabase bootstrap to seed eight security dashboards: account
changes, resets, reset completions, registrations, email similarity, bounces,
privilege changes and coverage. They query the existing read-only
`reporting.cohort_metrics` view. Security partitions with any cell below the
minimum cohort size (at least five), including absent categories with zero members,
are suppressed with their totals before
reports, charts, views or inference. Private security tables receive no reporting
grants. Saved-card chart delivery uses the existing authorized renderer and text
fallback.

Prompt Matrix **52** updates admin, board and anomaly-reader policy. Google Doc
overrides need the complete matching content and an **explicit reload**. Nothing
publishes an external document. `anomaly_propose` and `anomaly_explain` version 2
add paired security variations; retry reservations retain their saved text.

## Rollout and verification

Review the freshly extracted Rails contracts and `find`/`changeStream` access for
`members`, `audit_logs`, `mailtrap` and `mailtrap_messages`. The current schema
inventory already includes them; security adds no Mongo collection or permission.
Use the dedicated read-only source principal, never optional ticket-write grants.
Backfill with shadow delivery, inspect coverage and evidence, catch up streams,
configure staff recipients, then select active mode. Review learned readiness
before approving any exact revision.

Run both prompt validators, full pytest, and disposable PostgreSQL tests with
`ANOMALY_TEST_DATABASE_URL` naming a `ledger_anomaly_test_*` database. Validate
Metabase bootstrap/rendering, Slack recipients, Mongo replay and backup/restore
against deployment endpoints before production activation. Restore the full
PostgreSQL volume and verify incident, baseline, checkpoint and delivery continuity.
