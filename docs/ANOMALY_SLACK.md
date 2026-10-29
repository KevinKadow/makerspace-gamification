# Anomaly analytics in Slack

`SLACK_CHANNEL_ANOMALY` names the existing anomaly channel, default `ledger-audit`;
an exact Slack channel ID is also accepted. Invite The Ledger bot manually.
The service does not create, join, or enroll people into this channel. It must be
active, accessible to the bot, and not externally shared.

## Access

Active human members of the configured channel may ask aggregate data and trend
questions there or in their own one-to-one DM with The Ledger. They need no source
member link or Ledger game opt-in. A current uniquely linked source `admin` or
`board_member`, including one with `expired` membership, may request private staff
detail and administer monitoring in their own DM; suspended, revoked, merged, or unknown source memberships do not grant
staff authority. Resource-manager titles and game ranks grant no anomaly staff
rights. Channel replies are always aggregate-only, including staff requests.

Game opt-out and `SLACKIDS_INELIGIBLE` do not determine analytics permission.
Slack human status, current channel membership, source identity and staff role
are rechecked before data access and delivery. Unknown or malformed authorization
responses deny access; queueing a request is not a permanent access grant. A
different person's DM, group DM, or unrelated channel is never a detail surface.

## Commands and routing

Set `ANOMALY_ENABLED=true` on the Ledger ingress and worker deployment to enable
analytics requests. When unset or false, messages retain normal Ledger routing
and `/ledger-anomaly` returns an ephemeral disabled response without opening a DM
or enqueueing a job.

Install the updated [slack-manifest.json](../slack-manifest.json) and re-authorize
the app for `im:read`, which verifies each caller's own DM. All commands below use
the single `/ledger-anomaly` registration and Ledger's existing signed ingress:

- `/ledger-anomaly ask <question>` asks about the supported data and trend reports.
- `/ledger-anomaly status` reports monitoring status; an empty command uses this.
- Staff use `/ledger-anomaly detectors list|review|propose|approve|disable|resolve` and
  `/ledger-anomaly schema review|approve`; the anomaly consumer validates each
  operation and its target revision. `detectors review` lists open finding IDs;
  `detectors resolve <finding-id>` records a human resolution and permits future
  normal observations to enter the baseline. It never changes source records or
  approves a detector. AI cannot resolve findings or grant review authority.

A slash command used outside the anomaly channel opens the caller's DM and
applies the same authorization. Natural-language analytics questions are routed
before the general public-count handler and game participation gate. This covers
anomalies, baselines, schema, membership cancellation/expiry, retention, revocation
research, and check-in/billing trends. Ordinary count questions and unrelated
chatter retain their existing behavior. A recognized analytics request lacking
authorization never falls through to a less restrictive general handler.

The existing signed, workspace-checked Slack ingress persists an idempotent
`anomaly_query` in `ledger_outbox`, with requester, destination, question and thread
timestamp. General Ledger workers exclude this kind; the anomaly service consumes
it. Acknowledgment means persisted, not completed. No source records, memberships,
clearances, game consent, or channel enrollment are modified by a question.

## Prompt policy

Matrix version 52 documents the `anomaly_channel_reader` permission, security access, and independent
admin/board authority. `anomaly_explain` and `anomaly_propose` are separate rotating
JSON prompt sets. Only sanitized application-generated evidence enters inference;
member detail is rendered outside the model after authorization. Small cohorts
are suppressed in application code. AI-generated detector candidates remain inert
until a human approves a validated revision; schema/data/access changes are never
model-driven. Risk associations are not proof of cause or misconduct.

Operators using a Google Doc policy override must copy the complete version-52
matrix and explicitly run `/ledger-admin reload-prompts`. The reload still uses
the existing game-administration authorization; anomaly permission does not grant
general prompt publication rights. No external document is edited automatically.
Missing required roles retain the last valid or bundled policy, and reload does
not replace already reserved prompts or composed deliveries.


Security monitoring adds `/ledger-anomaly security summary` for aggregate readers.
The following forms require a verified staff DM:

- `/ledger-anomaly security incidents` lists incidents.
- `/ledger-anomaly security incident <ID>` shows an incident (`detail` is an alias).
- `/ledger-anomaly security evidence <ID>` shows permitted normalized evidence.
- `/ledger-anomaly security resolve <ID>` records a staff resolution.
- `/ledger-anomaly security detectors list` lists built-in and learned revisions.
- `/ledger-anomaly security detectors review <revision>` shows a specific revision.
- `/ledger-anomaly security detectors propose` requests a bounded proposal in shadow.
- `/ledger-anomaly security detectors approve <revision>` approves the exact revision, subject to its readiness gates.
- `/ledger-anomaly security detectors disable <revision>` disables the exact revision.

`security detector` is an alias for `security detectors`. Automatic security alerts
use only `ANOMALY_SECURITY_DM_USER_IDS`, never the anomaly channel. Every attempt
rechecks current staff identity and source eligibility, including expired staff.
See [security analytics](ANOMALY_SECURITY.md).
