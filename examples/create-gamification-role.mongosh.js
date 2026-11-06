// SELF-MANAGED MONGODB ONLY: Atlas rejects db.createRole().
// For Atlas use mongodb-roles.js's Atlas JSON and docs/MONGODB_ACCESS.md.
// Run from the repository root as a role-provisioning administrator, not the bot.
load("examples/mongodb-roles.js");

const makerspaceDatabase = "makerauth";
const ledgerDatabase = "makerauth";
const roles = LedgerMongoRoles.buildRoles(makerspaceDatabase, ledgerDatabase);

// The admin database allows role resources in different databases if configured.
db = db.getSiblingDB("admin");

// For separate credentials, assign these roles to two different database users:
db.createRole(roles.source); // MLAB_URI user: source reads only.
db.createRole(roles.ledger); // LEDGER_URI user: named Ledger collections only.

// Optional alternative when one gamification user must hold both sets of rights:
// db.createRole(roles.gamification);
// A role is not a user; assign it when provisioning the corresponding DB user.
// No user/role administration permissions are granted to the application roles.
