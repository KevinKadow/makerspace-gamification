# Separate Mongo connections and restricted roles

The service creates independent PyMongo clients for its two data sources:

| Setting | Purpose |
| --- | --- |
| `MLAB_URI` | Reads existing makerspace records through `Sources`; optional narrowly scoped ticket-event note writes can be provisioned separately |
| `LEDGER_URI` | Reads/writes the explicit `ledger_*` collections through `MongoStore`, including transactions and index initialization |
| `MLAB_DATABASE` | Optional database-name override for the source connection |
| `LEDGER_DATABASE` | Optional database-name override for the Ledger connection |

Both URIs can name the same Atlas cluster and `makerauth` database with different database-user credentials. They can also point at different databases/clusters. The Ledger connection must support transactions. Source reads are independent of the Ledger transaction; reconciliation handles source changes, rather than claiming a cross-client atomic snapshot. Optional ticket-note writes transact only within the source database and are not atomic with Ledger XP or Slack delivery. `/ready` checks connectivity to both and transaction capability on the Ledger side.

Action summaries use existing owned `ledger_evidence` and `ledger_outbox`; no collection or role privilege is added. `ledger init` adds an evidence index on `kind`, `action_id`, and `authorization` for completed-action lookup. Existing role examples continue to cover these reads, writes, and index initialization.

```dotenv
MLAB_URI=mongodb+srv://gamification_source_reader:ENCODED_PASSWORD@YOUR_CLUSTER.mongodb.net/makerauth?authSource=admin
LEDGER_URI=mongodb+srv://gamification_ledger_writer:ENCODED_PASSWORD@YOUR_CLUSTER.mongodb.net/makerauth?authSource=admin
```

Replace the placeholders and URL-encode credentials. Database selection is: the connection-specific `*_DATABASE`, then legacy `MONGO_DATABASE`, then that URI's database path, then `makerauth`. Each missing URI can fall back to legacy `MONGO_URI` for compatibility. `MLAB_URI` never implicitly supplies `LEDGER_URI`, nor the reverse; without the explicit counterpart or legacy fallback, startup fails. Remove the legacy variables after migrating to separate credentials.

## Atlas: use Custom Database Roles

**Atlas does not support `db.createRole()` through mongosh.** Its supported provisioning interfaces are Custom Database Roles in the UI, the Atlas CLI, and the Atlas Administration API. The requested JavaScript definitions are in [mongodb-roles.js](../examples/mongodb-roles.js); they emit the actual Atlas action/resource schema. The self-managed `db.createRole()` equivalent is in [create-gamification-role.mongosh.js](../examples/create-gamification-role.mongosh.js). [MongoDB compatibility reference](https://www.mongodb.com/docs/manual/reference/method/db.createrole/), [Atlas custom roles](https://www.mongodb.com/docs/atlas/security-add-mongodb-roles/).

Generate role payloads without making network calls or using credentials:

```powershell
node examples/mongodb-roles.js atlas source makerauth makerauth > source-role.json
node examples/mongodb-roles.js atlas ledger makerauth makerauth > ledger-role.json
# Optional combined role for a single gamification database user:
node examples/mongodb-roles.js atlas gamification makerauth makerauth > gamification-role.json
```

The final two arguments are the source and Ledger database names. Keep them aligned with the application configuration. Each output is one Atlas role payload shaped as follows (abbreviated here; the generator includes every required resource):

```javascript
{
  roleName: "gamification",
  actions: [
    { action: "FIND", resources: [
      { cluster: false, db: "makerauth", collection: "members" },
      { cluster: false, db: "makerauth", collection: "ledger_participants" }
      // ...all other explicitly enumerated resources from the generator
    ] },
    { action: "UPDATE", resources: [
      { cluster: false, db: "makerauth", collection: "ledger_participants" }
      // ...other Ledger-owned collections; no legacy collections
    ] }
    // INSERT, REMOVE, and CREATE_INDEX use their own exact resource lists.
  ],
  inheritedRoles: []
}
```

Use the complete generated files with the [Create One Custom Role API](https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-creategroupcustomdbrolerole), `POST /api/atlas/v2/groups/{projectId}/customDBRoles/roles`, or configure identical actions/resources in the Atlas UI. Provision using an authorized Atlas administrator. Assign `gamification_source_reader` to the user in `MLAB_URI` and `gamification_ledger_writer` to the user in `LEDGER_URI`. For a single-user setup, assign only the combined `gamification` role. Do not also assign broad built-in roles, because privileges are additive. Roles do not create users; create or edit database users separately in Atlas and scope them to the intended clusters. Separate Atlas projects require provisioning roles/users in their respective projects.

## Exact access used by this implementation

The [one-shot quest generator](QUEST_GENERATION.md) uses existing owned quest definitions/revisions, evidence audits, temporary context, outbox notices and relationship acceptances/shared projects. Its relationship project and generation-lease indexes use the existing `createIndex` resources. The Atlas and self-managed role examples and their tests were reviewed; no new collection, wildcard privilege or source write is needed.

Quest-inspiration redaction also reads a complete bounded `members` directory through `MLAB_URI`, projecting only `firstname`/`lastname` and the adapter's ID field. Include inactive, revoked, merged and unlinked records because names need redaction regardless of eligibility. Directory records never enter saved context or inference; incomplete or failed reads omit chat inspiration. Existing `members` find privileges and projection allowlists cover this read without additional grants or source writes.

| Resources | MongoDB actions | Reason |
| --- | --- | --- |
| `members`, `slack_users`, `shops`, `tools`, `tool_checkouts`, `volunteer_credits`, `volunteer_tasks`, `volunteer_events`, `earned_memberships`, `groups`, `checkins`, `cards`, `fix_tickets`, `fix_ticket_events` in the source database | `find` | Identity, review scope, catalog, evidence, membership, read-only queries, arrival resolution, and ticket verification quests |
| Optional `fix_tickets` source collection | `find`, `update` | Advance only the ticket `revision` and `updated_at` atomically with a Ledger-authored note event |
| Optional `fix_ticket_events` source collection | `find`, `insert` | Idempotently create a completed `note` event for a quest response |
| `ledger_participants`, `ledger_relationships`, `ledger_rulesets`, `ledger_catalog`, `ledger_evidence`, `ledger_awards`, `ledger_quests`, `ledger_projects`, `ledger_channels`, `ledger_message_templates`, `ledger_inbox`, `ledger_outbox`, `ledger_context`, `ledger_files` in the Ledger database | `find`, `insert`, `update` | Reads, upserts, accounting, leased jobs, and cached Slack file references for rank icons and per-member skill-tree images |
| `ledger_context` only | `remove` | Reflect deleted Slack messages in cached context |
| `ledger_participants`, `ledger_inbox`, `ledger_outbox`, `ledger_evidence`, `ledger_awards`, `ledger_context`, `ledger_relationships`, `ledger_quests`, `ledger_catalog` only | `createIndex` | Owned indexes created by initialization/preparation, including context/catalog TTL, review grants, acceptances, quest heads and observations |

Mongo `update` plus `insert` permits upserts; `insert` permits implicit creation of the named ordinary collections. The service does not call `dropCollection`, `dropDatabase`, `dropIndex`, `collMod`, role/user administration, or validation bypass. Its own `ping`/`hello`, transaction management, and TTL processing do not require adding a blanket `readWrite`, `dbAdmin`, or cluster-administration role. The listed index privilege supports the existing initialization commands; an operator who handles initialization separately can remove that action from steady-state users. [Privilege actions](https://www.mongodb.com/docs/manual/reference/privilege-actions/), [collection creation access](https://www.mongodb.com/docs/manual/reference/method/db.createcollection/).

`ledger_files` is new and needs `find`, `insert`, and `update` on the Ledger database before deploying this version. It has no application-created indexes beyond MongoDB's built-in `_id` index. Regenerate and apply the updated least-privilege role; existing database-user assignments do not gain this collection automatically.

There is no wildcard `ledger_*` resource in the examples: each collection is named. Future collections need an intentional role update. The optional `gamification_ticket_note_writer` role is separate from the standard source reader; it grants only `fix_tickets` find/update and `fix_ticket_events` find/insert. Assign it to the MLAB_URI user only when note persistence is enabled. The normal feature remains usable if that role is absent. A combined `gamification` role includes these optional grants and should be used only when that behavior is intended. `checkout_approvers` remains a trigger-only topic. `volunteer_events` has bounded query reads; `checkins` and `cards` have internal arrival-only reads and are not exposed to conversation tools. The separate ChangeStream2MQTT service retains its own credentials and change-stream permissions; the Ledger user needs no `changeStream` or oplog access.

Mongo collection roles grant access to whole documents, not individual fields. The source adapter's projection still excludes sensitive fields from application use and AI context; it is not a field-level database authorization boundary.

## Interactive-read preparation

The [optimized read path](QUERY_OPTIMIZATION.md) uses fixed read-only aggregation joins inside each connection's database. Source `$lookup`/`$graphLookup` inputs remain allowlisted existing collections and use their existing `find` grants; no source index, view, write or stream permission is added. Owned quest joins and aggregate counts use existing owned `find` grants. Role-inventory tests include aggregation join inputs and the count/exists/batch helpers, rather than relying only on direct `find` calls.

The only new action/resource is `CREATE_INDEX` on `ledger_catalog`. It supports generation/source-ID and generation/shop compound indexes, challenge/quest display indexes and the `expires_at` TTL index. Quest heads order by `title_key`, `revision`, `_id`; acceptances order by `title_key`, `quest_revision`, `_id`. Other new context/quest/acceptance compound indexes use already granted owned collections. Review/provision the updated role before `ledger prepare-reads --verify`; the application never provisions Atlas roles itself. It backfills owned display/order metadata, including challenge/open-quest title keys, and expiring cache generations without deleting business records or changing consent/accounting. Server TTL requires no extra application `remove` action; explicit application deletion remains limited to `ledger_context`.

No new collection, wildcard privilege or direct change-stream grant is required. The existing bridge's events and five-minute reconciliation schedule display refreshes; source safety/identity/consent checks remain current. The sample environment keeps optimized reads off until preparation and shadow comparison complete. Reverting the read flag does not require removing indexes or metadata.

## Self-managed `db.createRole()` equivalent

From mongosh connected as a provisioning administrator, run the supplied `.mongosh.js` example from the repository root. It uses this syntax with the full enumerated privileges:

```javascript
load("examples/mongodb-roles.js");
const definitions = LedgerMongoRoles.buildRoles("makerauth", "makerauth");
db = db.getSiblingDB("admin");
db.createRole(definitions.gamification);
// Alternatively create definitions.source and definitions.ledger for two users.
```

Creating a role does not assign it to an existing user and does not revoke previously granted roles. Review user assignments independently. No provisioning example has been run against a live database by this change.

Default observation adds no collection or source permission. Nonparticipant preferences and notice receipts use existing `ledger_relationships` find/insert/update privileges; first join retains an inert migration marker in that collection instead of deleting it. Participation, XP and game authorization still require their own game records. The exact Mongo role examples already cover these operations; no extra remove privilege is required.

Review notifications use existing `ledger_outbox` and activity collection find/insert/update permissions (`ledger_evidence`, `ledger_quests`, `ledger_relationships`); delivery leases and Slack timestamp receipts are fields on those activities. No new collection or remove/index privilege is introduced. Review/outbox writes share Mongo transactions, with Slack calls outside them.

Selective review reconciliation adds parent `review_notice_channel`/`review_notice_dirty` indexes, legacy `review_channel_id` indexes, a quest status index, and an outbox kind/status/resolution index in those same owned collections. The Atlas and self-managed role examples already permit their `createIndex` operations; no extra grant or collection is required. `MLAB_URI` remains read-only. Legacy nested contribution addresses are selected using Mongo's [objectToArray](https://www.mongodb.com/docs/manual/reference/operator/aggregation/objectToArray/) and [anyElementTrue](https://www.mongodb.com/docs/manual/reference/operator/aggregation/anyElementTrue/) expressions before deciding whether an activity needs a transaction.

Admin command eligibility and invitation recipients are read from the existing MLAB_URI members/slack_users projections only. Administrative consent invitations use ledger_outbox kind admin_invitation. Private review access uses ledger_channels kind review_membership plus ledger_outbox review_channel_invite/remove jobs. Broken-tool verification quests add no collection; their read path uses `fix_tickets`/`fix_ticket_events`, while optional note writes use the separate scoped role above. Review membership rows are separate from game-channel registrations and never authorize ambient inference or earned rank-channel access.
