/*
 * Least-privilege role examples for The Ledger. This file makes NO network calls.
 * Node: node examples/mongodb-roles.js atlas gamification makerauth makerauth
 *       node examples/mongodb-roles.js mongosh source makerauth makerauth
 * mongosh: load("examples/mongodb-roles.js") exposes LedgerMongoRoles.
 *
 * Atlas does NOT support db.createRole(). Send the Atlas payload to its
 * Administration API, or configure equivalent privileges through its CLI/UI.
 * See docs/MONGODB_ACCESS.md for provisioning and credential assignment.
 */
var LedgerMongoRoles = (() => {
  // Collections actually queried by the current source adapter/call sites.
  // checkout_approvers is a reconciliation trigger only. cards/checkins are
  // internal arrival reads, never collections exposed to conversation tools.
  const sourceCollections = [
    "members", "slack_users", "shops", "tools", "tool_checkouts",
    "volunteer_credits", "volunteer_tasks", "volunteer_events", "earned_memberships", "groups", "checkins", "cards",
    "fix_tickets", "fix_ticket_events",
  ];
  const ledgerCollections = [
    "ledger_participants", "ledger_relationships", "ledger_rulesets",
    "ledger_catalog", "ledger_evidence", "ledger_awards", "ledger_quests",
    "ledger_projects", "ledger_channels", "ledger_message_templates",
    "ledger_inbox", "ledger_outbox", "ledger_context", "ledger_files",
  ];
  // Exactly the collections where MongoStore.indexes() calls create_index().
  const indexedCollections = new Set([
    "ledger_participants", "ledger_inbox", "ledger_outbox", "ledger_evidence",
    "ledger_awards", "ledger_context", "ledger_relationships", "ledger_quests", "ledger_catalog",
  ]);

  function buildRoles(sourceDatabase = "makerauth", ledgerDatabase = "makerauth") {
    for (const name of [sourceDatabase, ledgerDatabase]) {
      if (typeof name !== "string" || !name.trim() || name.includes("*")) {
        throw new Error("Supply explicit nonempty database names, never wildcards.");
      }
    }
    const reads = sourceCollections.map(collection => ({
      resource: { db: sourceDatabase, collection }, actions: ["find"],
    }));
    const writes = ledgerCollections.map(collection => {
      // replace_one(upsert=True) requires both insert and update; reads and
      // find_one_and_update need find. Deletion is used only for cached context.
      const actions = ["find", "insert", "update"];
      if (collection === "ledger_context") actions.push("remove");
      if (indexedCollections.has(collection)) actions.push("createIndex");
      return { resource: { db: ledgerDatabase, collection }, actions };
    });
    // Optional best-effort Rails event note persistence. It also needs update
    // on fix_tickets so the ticket and event revision can commit together.
    const ticketNoteWriter = [
      { resource: { db: sourceDatabase, collection: "fix_tickets" }, actions: ["find", "update"] },
      { resource: { db: sourceDatabase, collection: "fix_ticket_events" }, actions: ["find", "insert"] },
    ];
    return {
      source: { role: "gamification_source_reader", privileges: reads, roles: [] },
      ledger: { role: "gamification_ledger_writer", privileges: writes, roles: [] },
      ticket_note_writer: { role: "gamification_ticket_note_writer", privileges: ticketNoteWriter, roles: [] },
      // Optional single-user role includes the explicitly scoped note grants.
      gamification: { role: "gamification", privileges: [...reads, ...writes, ...ticketNoteWriter], roles: [] },
    };
  }

  function toAtlas(role) {
    const actionNames = { find: "FIND", insert: "INSERT", update: "UPDATE", remove: "REMOVE", createIndex: "CREATE_INDEX" };
    const actions = new Map();
    for (const privilege of role.privileges) {
      for (const name of privilege.actions) {
        if (!actionNames[name]) throw new Error(`Unsupported action: ${name}`);
        if (!actions.has(name)) actions.set(name, []);
        actions.get(name).push({ cluster: false, db: privilege.resource.db, collection: privilege.resource.collection });
      }
    }
    return {
      roleName: role.role,
      actions: [...actions].map(([name, resources]) => ({ action: actionNames[name], resources })),
      inheritedRoles: [],
    };
  }

  return { buildRoles, toAtlas };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = LedgerMongoRoles;
  if (typeof require !== "undefined" && require.main === module) {
    const [format = "atlas", kind = "gamification", sourceDb = "makerauth", ledgerDb = "makerauth"] = process.argv.slice(2);
    const roles = LedgerMongoRoles.buildRoles(sourceDb, ledgerDb);
    if (!["atlas", "mongosh"].includes(format) || !Object.hasOwn(roles, kind)) {
      throw new Error("Usage: node examples/mongodb-roles.js atlas|mongosh source|ledger|gamification [sourceDb] [ledgerDb]");
    }
    console.log(JSON.stringify(format === "atlas" ? LedgerMongoRoles.toAtlas(roles[kind]) : roles[kind], null, 2));
  }
}
