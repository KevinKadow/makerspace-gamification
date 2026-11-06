# The Ledger

An opt-in Slack world system for makerspace members. The journey is **learn → make → receive feedback → demonstrate competence → teach and contribute**. XP makes progress visible; it cannot replace clearance, breadth, mentoring, or service requirements.

The implementation is a Python Slack Bolt application with durable Mongo inbox/outbox workers, a vLLM Chat Completions composer, and MQTT reconciliation. Rails and React collections remain read-only. No game rank grants tool access or administrative authority.

## Run locally

The Ledger can also author an individual or cooperative quest for human review with `python -m ledger.generate_quest`. Use `--rank N` to select an enabled rank, or omit it for weighted demand-based selection; `--dry-run --seed 42` previews without writes. See [quest generation, review and rollout](docs/QUEST_GENERATION.md) for required channel-use notices, configuration, resumable requests and shared project completion.

Requires Python 3.12+, an existing Mongo replica set, an MQTT broker, a Slack app, and a reachable vLLM endpoint. The supplied Compose deployment serves `nvidia/Qwen3.8-27B-NVFP4` using `vllm-gb10` on a Linux ARM64 NVIDIA DGX Spark/GB10 host. The Python bot can run separately on other hardware.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e '.[test]'
# Supply variables listed in .env.example through your process environment.
ledger init
ledger dry-run
ledger bootstrap
# Run in separate terminals:
ledger serve --port 3000
ledger worker
```

`.env.example` is documentation; the Python CLI does not automatically load `.env`. Docker Compose does. Never commit a populated `.env`. The repository's pre-existing staged deletions, including `.gitignore`, have deliberately been preserved.

Set `MLAB_URI` for the makerspace reader and `LEDGER_URI` for the Ledger writer. They use independent Mongo clients and may use different credentials even on the same database. Optional `MLAB_DATABASE`/`LEDGER_DATABASE` override each URI's database. Legacy `MONGO_URI`/`MONGO_DATABASE` remain supported as fallbacks. See [MongoDB access and JavaScript role examples](docs/MONGODB_ACCESS.md) for exact collection permissions, Atlas role payloads, and the self-managed `db.createRole()` equivalent.

For deployment, populate `.env` with database/Slack/MQTT credentials, a shared vLLM API key, and a Cloudflare Tunnel token. Compose includes `ghcr.io/timothystewart6/vllm-gb10` and `cloudflare/cloudflared`; the tunnel's published hostname forwards to `http://ledger-web:3000`. The AI endpoint stays inside Docker. Follow the [GB10 and tunnel deployment guide](docs/DEPLOYMENT.md) for startup, model warmup, and verification, then the [pilot steps](docs/OPERATIONS.md).

After updating the deployment checkout, run `bash scripts/rebuild.sh` on the Linux deployment host to rebuild images and recreate all services while preserving named volumes and external data. See [rebuild behavior and verification](docs/DEPLOYMENT.md#rebuild-after-a-repository-update).

The importable [Slack manifest JSON](slack-manifest.json) includes all seven commands, seven event subscriptions, App Home, modal interactions, and required bot scopes. Replace every `LEDGER_HOST` with the tunnel's public hostname. See the [Slack setup and permission mapping](docs/SLACK.md); check that `/kudos` is available before installation.

The bot's leatherbound grimoire icon is included at [512 × 512](icons/ledger-bot-512.png) for Slack upload and [36 × 36](icons/ledger-bot-36.png) for mobile rendering. See [icon assets and installation](icons/README.md) for the source illustration and generation prompt.

The Ledger narration uses [23 message-type JSON files](ledger/prompts), with three paired variations per type and two additional System variations in five completion types. The bot avoids the last two voices before selecting randomly, using shared history for channel posts and separate history per DM recipient. It substitutes verified member/event details, and delivery retries reuse the composed text. See the [rank-up example](ledger/prompts/rank_up.json) and [prompt authoring guide](docs/PROMPTS.md) for variables, audience settings, and admin preview/publication.

The shared [Prompt Matrix Template](ledger/prompts/prompt_matrix.xml.md) codifies roles, game rules, personality, and channel/DM conduct in hybrid XML/Markdown. Set `LEDGER_PROMPT_MATRIX_DOC_URL` to optionally load it from a Google Doc on worker startup; `/ledger-admin reload-prompts` requests a fresh copy. Failed refreshes retain valid policy, and existing deliveries retain their snapshots. See [matrix design, authentication, reload, and validation](docs/PROMPT_MATRIX.md).

## Member experience

| Command | Purpose |
| --- | --- |
| `/ledger join` | Read consent and explicitly opt in; chatting with the bot also offers onboarding |
| `/ledger leave` | Confirm opt-out; a DM saying `opt out` also works |
| `/ledger` | Current progress and command guidance |
| `/ledger sponsor @member` | Invite someone to explore and opt in; never enrolls them automatically |
| `/ledger invite @member chat` | Invite an eligible participant to Ledge Chat |
| `/ledger invite @member rank:2` | Restore an earned rank channel, subject to inviter membership |
| `/ledger feedback <message>` | Record feedback for the staff pilot review |
| `/ledger-skills [shop name or ID]` | Private skill-tree image and complete accessible text file, generated from actual prerequisites |

Skill-tree PNGs are cached per member in Slack and reused when the generated text checksum is unchanged; missing Slack files or changed skill text produce a fresh image upload. Rank icons use the same cache. See [Slack setup and file caching](docs/SLACK.md).
| `/ledger-quests` | Browse First Build, self-directed challenges, and cooperative quests |
| `/ledger-quests submit <catalog-id>` | Submit evidence for independent verification |
| `/ledger-quests join <quest-id> <discipline>` | Join a cooperative volunteer quest |
| `/ledger-quests contribute <quest-id> <description>` | Submit your contribution for verification |
| `/ledger-mentor log` | Record substantive help or a workshop, including learners and optional shop |
| `/ledger-mentor offer @member` | Initiate-or-higher participant offers Success Buddy support |
| `/ledger-mentor accept <relationship-id>` | Accept a buddy offer; the invitation also has a button |
| `/ledger-mentor end <relationship-id>` | Either participant can end the pairing |
| `/kudos [@member]` | Choose the recipient first, then write thanks and optionally share in Ledge Chat |
| `/ledger-project new` | Start a project showcase and feedback thread |
| `/ledger-project update <project-id>` | Share progress, revisions, and collaborator credit in its thread |

The App Home's Character Sheet shows the member's rank, cached rank artwork and skill tree, XP, next-rank requirements, a project gallery, and channel/consent controls. On the first Home-tab open, it publishes a short `Processing...` placeholder while a durable delivery job builds and publishes the view. Later opens retain the published view; verified milestones and rank changes queue refreshes. Skill-tree files are reused while their checksum matches and are regenerated after skill paths change or Slack removes the cached file. Opt-out refreshes the view to the public, nonparticipant Home. DM conversations and addressed private-channel threads use The Ledger's System personality. Clear self-directed progress questions in Ledger channels receive contextual replies with private-detail controls. Other ambient messages receive no reply.

Opt-in immediately queues Ledge Chat and the entry/current-rank channel. Verified history is imported asynchronously. Opt-out removes all registered game-channel memberships and cancels pending invitations, while retaining progress and silently accounting for eligible source activity. Returning members retain their original ruleset and receive one state summary. Peer kudos is the explicit consent exception: nonparticipants can receive thanks, but never receive XP for those kudos, including after joining later.

Promotion announces the member's ascent in their prior-rank channel without naming the next rank, invites them to the new-rank channel, then removes them from the prior channel and welcomes them after the invitation succeeds. Other earned lower channels remain as they are. Voluntary departures are respected. Self-service invitations restore only Ledge Chat/current rank. Rejoining an older earned channel requires an invitation by another participating member of that channel.

## Progression

Seven stable numbered slots separate identity from editable names and emoji. Slot seven starts inactive.

| Slot | Default rank | XP floor | Skills | Community |
| --- | --- | ---: | --- | --- |
| 1 | 🌱 Newbie | 0 | Opt-in | — |
| 2 | 🔨 Novice | 300 | 2 checkouts; verified First Build | — |
| 3 | ✨ Initiate | 600 | 4 checkouts, 2 shops | 1 mentoring session, 1 volunteer credit |
| 4 | 🔧 Apprentice | 1,500 | 8 checkouts, 3 shops, 1 completed shop | 3 sessions, 2 learners, 4 credits |
| 5 | ⚙️ Journeyman | 3,000 | 12 checkouts, 4 shops, 1 completed shop | 6 sessions, 3 learners, 8 credits, Boss Fight |
| 6 | 🌟 Adept | 5,000 | 16 checkouts, 5 shops, 2 completed shops | 12 sessions, 5 learners, 16 credits, develop another mentor, stewardship |
| 7 | Unconfigured | — | Inactive | Inactive |

All gates are cumulative and conjunctive. Promotion also requires a future `expirationTime` and verified paid/earned membership, including household coverage. Ambiguous prepaid/legacy coverage needs an independent admin/board attestation tied to that exact expiration. Expiration alone does **not** disqualify a kudos recipient with `activeMember` or `pending` status.

Distinct, non-revoked tool clearances count toward current skills. Shop completion uses staff-published frozen sets of enabled checkout-required tools. New equipment never erases an already awarded completion. Earned rank is retained after evidence corrections unless an independent moderator explicitly corrects the rank award.

| Source | XP |
| --- | ---: |
| First qualifying checkout with no prerequisites | 31 |
| First qualifying checkout with prerequisites | 100 |
| Grant another member a valid checkout | 67 total |
| Approved volunteer credit | 61 per credit, including fractions |
| Verified learning challenge | 100 once per catalog challenge |
| Boss Fight / stewardship | 500 instead of another 100 challenge XP |
| Successful recruitment | 11 once |
| Qualifying kudos received while participating | 17 |

Linked checkout volunteer credit counts toward service but earns no second XP award. Reversals append compensating XP entries. Recruitment requires confirmed sponsorship, first opt-in, and a newly completed verified learning/service milestone; imports, kudos, and repeat opt-ins cannot qualify.

Formal checkout teaching is imported automatically. Other mentoring needs learner acknowledgment and independent verification; workshops count as one session with multiple learners. Developing another mentor requires recorded guidance followed by independently verified teaching. Stewardship requires completed work and usable handoff evidence. Group quests require at least two contributors covering at least two predefined disciplines and individual acceptance. Appointments such as Tool Captain remain human decisions.

## Kudos

Only participants with permitted Ledger access can give kudos. Select another linked, active human Slack identity first. Nonparticipants trigger a warning and an explicit **Send kudos only** / **Send kudos and invite them to The Ledger** choice.

The required body accepts 1–2,000 characters of Slack `mrkdwn`, links, line breaks, Unicode emoji, and emoji shortcodes. It is stored and rendered verbatim; The Ledger generates only an introduction. Optional shop/tool selections describe the contribution and never require clearance. Changing shops clears the tool.

**Make public** defaults off. Checking it creates a second delivery to private Ledge Chat. Public attribution uses the giver's current configured rank emoji and the recipient's emoji only when currently participating. Screen-reader fallback text includes rank names. Public kudos is neither copied to rank channels nor suppressed by XP caps.

XP is limited to one qualifying kudos per giver/recipient pair per Monday–Sunday calendar week, and five per recipient per calendar day, using `America/New_York`. Additional thanks still deliver. A permanent submission-time XP decision and independent destination receipts prevent delivery retries from awarding XP again. The giver receives pending, delivered, or failed/cancelled destination status.

## Configuration and internals

See [administration](docs/ADMINISTRATION.md) for seven-slot editing, templates, catalog binding, reviews, and rollback. See [architecture](docs/ARCHITECTURE.md) for collection ownership, source contracts, privacy, event processing, and delivery semantics.

`ledger/assets` contains the six supplied rank images, unchanged. Their baked-in labels are displayed only while the configured name matches the artwork. Unicode/custom Slack emoji and live rank text continue working after renaming a rank. Replace/review artwork separately when changing baked-in labels.

## Verification

```powershell
python -m pytest
# Optional real transaction/concurrency test, using a disposable replica set:
$env:LEDGER_TEST_MONGO_URI = 'mongodb://localhost:27017/?replicaSet=rs0'
python -m pytest tests/test_mongo_integration.py
```

The ordinary suite runs without credentials. It covers rules, accounting, consent, Slack forms, signed HTTP ingress, retries, public kudos, scopes, channel races, mentoring, quests, and a real local HTTP chat-API stub. The Mongo integration test is skipped unless explicitly configured; it creates and removes only its own random `ledger_test_*` database. Workspace Slack behavior, broker delivery, replica-set performance, and pilot latency need deployment validation.

The sibling `ChangeStream2MQTT` change filters every `ledger_*` collection before logging or publishing, with defensive filtering at the dispatch and publish boundaries. Deploy that exclusion before enabling Ledger writes. Rails and React behavior is unchanged.

## Character sheets and reviewed member quests

Use `/ledger stats`, `/ledger progress`, `/ledger preferences`, and `/ledger achievements` for private detail. `/ledger-quests list` searches eligible titles; rank-3 members can use `/ledger-quests create` and Help draft with The Ledger. Independent reviewers approve immutable revisions and 0–500 XP rewards. Accepted quests survive rank-up and pay once per logical quest. Staff use `/ledger-admin delegates` to issue explicit scoped review authority. Audit-only observation defaults on for every eligible member in configured Ledger channels after an explanatory notice, independently of game participation. `/ledger preferences` opts out without joining. Arrival mentions have a separate preference and remain disabled by default deployment-wide. Model proposals cannot change accounting, rank or recognition delivery, regardless of flags. See [member guide, authority, and rollout](docs/ENGAGEMENT_QUESTS.md).

Review-channel notifications use `LEDGER_QUEST_REVIEW_CHANNEL_ID`. Configure a private staff channel containing The Ledger, separate from member game channels. Pending quests, completion evidence and other reviewable activities post there; review decisions update the saved message, with replacement if it was deleted. See [review notification operations](docs/ENGAGEMENT_QUESTS.md#review-channel-notifications).
