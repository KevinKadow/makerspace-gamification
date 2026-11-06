# Separate Mongo connections and restricted roles

The service creates independent PyMongo clients for its two data sources:

| Setting | Purpose |
| --- | --- |
| `MLAB_URI` | Reads existing makerspace records through `Sources`; no application writes |
| `LEDGER_URI` | Reads/writes the explicit `ledger_*` collections through `MongoStore`, including transactions and index initialization |
| `MLAB_DATABASE` | Optional database-name override for the source connection |
| `LEDGER_DATABASE` | Optional database-name override for the Ledger connection |

Both URIs can name the same Atlas cluster and `makerauth` database with different database-user credentials. They can also point at different databases/clusters. The Ledger connection must support transactions. Source reads are independent of the Ledger transaction; reconciliation handles source changes, rather than claiming a cross-client atomic snapshot. `/ready` checks connectivity to both and transaction capability on the Ledger side.

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

| Resources | MongoDB actions | Reason |
| --- | --- | --- |
| `members`, `slack_users`, `shops`, `tools`, `tool_checkouts`, `volunteer_credits`, `volunteer_tasks`, `earned_memberships`, `groups` in the source database | `find` | Identity, permissions, catalog, evidence, and membership reads |
| `ledger_participants`, `ledger_relationships`, `ledger_rulesets`, `ledger_catalog`, `ledger_evidence`, `ledger_awards`, `ledger_quests`, `ledger_projects`, `ledger_channels`, `ledger_message_templates`, `ledger_inbox`, `ledger_outbox`, `ledger_context` in the Ledger database | `find`, `insert`, `update` | Reads, upserts, accounting, and leased jobs |
| `ledger_context` only | `remove` | Reflect deleted Slack messages in cached context |
| `ledger_participants`, `ledger_inbox`, `ledger_outbox`, `ledger_evidence`, `ledger_awards`, `ledger_context`, `ledger_relationships` only | `createIndex` | The indexes created by `ledger init` and `ledger bootstrap`, including context TTL |

Mongo `update` plus `insert` permits upserts; `insert` permits implicit creation of the named ordinary collections. The service does not call `dropCollection`, `dropDatabase`, `dropIndex`, `collMod`, role/user administration, or validation bypass. Its own `ping`/`hello`, transaction management, and TTL processing do not require adding a blanket `readWrite`, `dbAdmin`, or cluster-administration role. The listed index privilege supports the existing initialization commands; an operator who handles initialization separately can remove that action from steady-state users. [Privilege actions](https://www.mongodb.com/docs/manual/reference/privilege-actions/), [collection creation access](https://www.mongodb.com/docs/manual/reference/method/db.createcollection/).

There is no wildcard `ledger_*` resource in the examples: each collection is named. Future collections need an intentional role update. `checkout_approvers` and `volunteer_events` are currently MQTT trigger topics only, with no direct Mongo reads, so they receive no role privileges. The separate ChangeStream2MQTT service retains its own credentials and change-stream permissions; the Ledger user needs no `changeStream` or oplog access.

Mongo collection roles grant access to whole documents, not individual fields. The source adapter's projection still excludes sensitive fields from application use and AI context; it is not a field-level database authorization boundary.

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
