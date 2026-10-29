# Deployment, pilot, and rollback

## Before installation

1. Deploy the sibling ChangeStream2MQTT Ledger-prefix exclusion before any Ledger data is written. Run its existing test suite with `python -m unittest -q`.
2. Review `.env.example`, source bindings, Mongo replica-set support, and broker ACLs. Set `MLAB_URI` to a source-reader credential and `LEDGER_URI` to a Ledger-writer credential. Use the [MongoDB role examples](MONGODB_ACCESS.md) to grant access to the specific collections/actions used by each connection; keep secrets in the deployment secret store. The service never runs migrations on Rails collections.
3. Confirm `/kudos` is unused in the Slack workspace. Replace every `LEDGER_HOST` URL in the [manifest](../slack-manifest.json); import/install the app and supply the bot token, signing secret, workspace ID, and bot user ID. Use the [Slack setup guide](SLACK.md) for events, interactions, and scope mapping.
4. Follow the [GB10 and Cloudflare Tunnel deployment guide](DEPLOYMENT.md). Compose serves `nvidia/Qwen3.8-27B-NVFP4` with vLLM at `http://ledger-ai:8000/v1`, using `LEDGER_LLM_API_KEY` for both server and client. Configure the remotely managed tunnel to forward the manifest hostname to `http://ledger-web:3000`. `localhost` inside a container refers to that container; configure reachable Mongo/MQTT hosts too.
5. Run `ledger init`, inspect `ledger dry-run`, then `ledger bootstrap`. Bootstrap creates private Ledge Chat and active-rank channels or validates IDs from `LEDGER_CHANNELS`. Invite the bot into pre-existing channels first. Externally shared or public channels are rejected.
6. Publish reviewed shop-completion snapshots and volunteer challenge bindings. Review the seeded First Build and mentoring pathways. Bind existing opportunities; the system does not invent or modify source volunteer tasks.

Example existing-channel setting:

```json
{"chat":"C01234567","rank:1":"C12345678","rank:2":"C23456789"}
```

Bootstrap reuses saved mappings. Rank renames do not rename channels or change IDs. A newly enabled rank queues a private `ledger-rank-<slot>` channel; if its creation outcome is uncertain, inspect Slack and bind the existing channel before retrying.

## Processes

Compose runs web ingress and six independent workers: accounting, delivery, channels, engagement, results, and interactive, alongside vLLM and cloudflared. No bot process waits for vLLM health; model startup/outages use factual or canned fallbacks and leave consent/accounting/cleanup operational. `ledger worker` runs six queues in separate threads for local operation. Use `--queue inbox`, `--queue outbox`, `--queue channels`, `--queue engagement`, `--queue results`, or `--queue interactive` for independent processes; each MQTT-connected process requires a unique persistent client ID. Only accounting subscribes to source topics. Keep the channel worker running during provider incidents or code rollback.

Updated general delivery excludes summary and conversation/guidance jobs: deploy `ledger-results` and `ledger-interactive` together with it. Run `ledger init` for the new owned-evidence index; no Mongo privilege or collection is added. Pilot the fixed 60-second kudos cutoff, late edits, short Qwen deadlines, and rank uploads in result threads. Matrix version 21 and updated delivery/status library defaults require explicit adoption for published overrides and an explicit Google Doc policy reload. Reserved jobs keep their existing inputs. See [result summaries](RESULT_SUMMARIES.md).

MQTT connects/reconnects on its network thread, including the first attempt. An unreachable broker no longer prevents Slack workers from starting; periodic Mongo reconciliation continues and MQTT advancement jobs retain their ordinary retry policy. When `MQTT_CLIENT_ID` is unset or blank, the process name plus a random six-digit suffix is used. Configure a stable explicit ID when broker-side persistent-session continuity matters. Configure a broker host reachable from containers: Compose's default `localhost` is the bot container, not the Docker host. Startup logs identify active queues. A healthy web service does not establish worker health; `docker compose ps --all` must include all six workers. See [chat/consent diagnostics](SLACK.md#saved-opt-in-and-chat-replies).

Production ingress uses Gunicorn in the supplied Linux image. `ledger serve` is a local WSGI development server. Cloudflare terminates public HTTPS and cloudflared forwards all three Slack endpoints over the Compose network without changing the signed request body. Slack callbacks must not encounter an interactive Access login or browser challenge. Bolt verifies signatures/timestamps and answers URL-verification challenges. Do not enable payload debug logs in production.

## Staff-reviewed pilot

Start with staff volunteers and a few willing members. No bulk invitation is sent by startup. Use explicit sponsorship or let members DM the bot and accept consent.

Verify in the real workspace:

- Opt in, import verified history, invite Ledge Chat/current rank; opt out during an in-flight invite and confirm cleanup.
- Leave an old rank channel, verify self-service cannot restore it, and test an earned-channel peer reinvite.
- Give private and public kudos, including to a nonparticipant, with multiline Slack formatting and custom emoji. Confirm XP caps do not suppress thanks.
- Change recipient participation while its modal is open; confirm warning/choice and draft preservation. Check failed DM/public deliveries independently.
- Publish a new floor and rename a rank; verify cohort pinning, live emoji, unchanged channel IDs, historical labels, and rollback.
- Verify First Build, a workshop with multiple learners, a two-discipline quest, a mentoring successor, and a stewardship handoff.
- Exercise disabled/revoked source evidence, fractional volunteer credit, duplicate MQTT events, deletion, and reconnect reconciliation.
- Take the AI endpoint offline, then restore it. Confirm canned delivery, no raw provider errors, cached retry text, and unaffected cleanup/accounting.
- Inspect Mongo and broker permissions; ensure no Ledger source payload reaches the bridge logs or MQTT.

Target ordinary source-to-accounting updates within ten seconds during normal connected operation. Measure `last_reconciled` and inbox age separately from delivery time: AI may take fifteen seconds and automatic major posts intentionally wait sixty seconds. Offline recovery can take the thirteen-minute reconciliation interval. Alert on old pending jobs, expired working leases, failed jobs, or an increasing fallback rate. `/ledger-admin metrics` provides aggregate pilot counts; sample member feedback with staff authorization. Check that mentoring and learning improve, rather than merely counting XP.

Native Slack invitations can briefly admit an unauthorized person before the event/reconciliation removal runs; standard Slack cannot give this app a universal pre-invite veto. Keep channels private, restrict workspace app/admin roles appropriately, and test event visibility in the actual Slack plan.

## Recovery

Mongo inbox/outbox records survive worker restarts. Pending and expired working jobs are reclaimed. Do not reset participant XP to replay history; run `ledger reconcile`. Retry only the failed destination of a public kudos. An operator can set that specific failed outbox row to `pending` with `available_at` now after resolving the cause; leave its `_id`, evidence, and composed text intact. Successful receipts are skipped and XP is not recalculated from deliveries.

Inspect uncertain remote outcomes before manually replaying file uploads or channel creation. For `name_taken` during slot provisioning, find the private channel and run bootstrap with its ID. Do not delete successful receipts to force retries.

For rollback, `/ledger-admin pause` preserves records and keeps opt-out/channel cleanup working. Keep ingress and the channel worker available, replace the application image with the previous compatible image, verify readiness, then deliberately resume and reconcile. Do not roll back Mongo data by deleting new collections. Prompt/configuration rollback publishes new versions and preserves audit history; existing progression cohorts remain pinned.

Real Mongo transaction testing is opt-in via `LEDGER_TEST_MONGO_URI`; use a disposable replica set. The automated test owns only its randomly named `ledger_test_*` database. Test results from the in-memory suite do not substitute for this deployment check or a Slack pilot.

## Interactive-read rollout

Keep `LEDGER_OPTIMIZED_READS=false` while provisioning the additional `ledger_catalog` index privilege and running `ledger prepare-reads --verify`. Preparation creates additive owned indexes, backfills quest/acceptance display metadata, challenge/open-quest title keys and numeric context ordering, then refreshes sanitized catalog generations. Optimized quest discovery requires that preparation; it has no automatic unprepared-data fallback. Inspect shadow mismatches before enabling optimized reads across application processes. The Python default is `true`; Compose and `.env.example` deliberately hold this deployment gate at `false` and pass both read flags to every bot service. [Query optimization](QUERY_OPTIMIZATION.md) documents verification cases and counters.

Display-cache freshness is at most 300 seconds from the first source read. Accounting startup, shop/tool MQTT events and thirteen-minute reconciliation schedule refreshes; stale/partial/missing generations use live display reads. Source safety, prerequisites, caller clearances, consent and roles remain live. Twenty-four-hour TTL handles retired/incomplete generations without a new application delete privilege. Set `LEDGER_QUERY_METRICS=true` only when aggregate command/document/reply-byte/latency diagnostics are wanted; no bodies or filters are retained. Worker queue loops report process-wide totals every sixty seconds.

Rollback by setting `LEDGER_OPTIMIZED_READS=false` and recreating application processes. Keep indexes/display metadata and normal TTL cleanup; consent, source records and accounting need no data rollback. Use the current bundled schema-1 prompt policy. Update any Google Doc override explicitly and run `/ledger-admin reload-prompts`; no external document is edited automatically and reserved deliveries retain their existing prompt/policy/text.

## New rollout gates

Deploy progress/quests/delegation/prompts first, then verify real source permissions, Slack options/modal hashes, and Qwen tool transport. Observation defaults on in configured/registered Ledger channels for eligible members regardless of joining, after delivered notice. `/ledger preferences` is available without joining and saves an independent opt-out. Existing explicit `LEDGER_OBSERVATION=false` overrides continue to disable it; update the setting and rebuild/recreate to adopt the default. Observation is unconditionally audit-only; model category/XP output cannot trigger accounting, advancement or recognition delivery even with the audit flag false. Inspect proposals as audit evidence. Configure welcomes independently; cancelled/failed notices are reopened for the current eligible identity/generation when observation is re-enabled and the member has not disabled it. Monitor backlog, inference latency, budget rejections, deductions, delegation failures, and delivery/arrival outcomes through `/ledger-admin metrics`. Eligible members receive an explanatory DM before observation; nonparticipants first encountered in a configured channel queue this notice and subsequent periodic reconciliation retries it; active participants receive one proposal-availability notice. [Detailed policy and rollout](ENGAGEMENT_QUESTS.md) includes Google Doc update/reload requirements.

Configure `LEDGER_QUEST_REVIEW_CHANNEL_ID` as a private, non-external staff review channel, invite The Ledger bot, and keep it outside registered game channels. Review notices are deterministic outbox deliveries; inspect failed `review_notice` jobs when channel access or Slack delivery fails. Periodic reconciliation backfills pending work and retries terminal failures. Review decisions and accounting commit independently of Slack; completed/closed notices update `review_message_ts`, or post a replacement if the saved message was deleted.

Administrative invitations use /ledger-admin invite (eligible opted-in admins/board members, any rank). Set LEDGER_QUEST_REVIEW_CHANNEL_ID to a private unshared staff channel containing the bot and separate from game channels. Source-role admins join it on Ledger opt-in and leave it on opt-out. Channel jobs recheck live source role, identity and consent; opt-out removal and review access synchronization remain independent of inference/Google Docs and run while paused. Existing opted-in admins are backfilled during reconciliation. Failed invitations can be retried by reconciliation; stable consent checks block stale jobs after rejoining.
