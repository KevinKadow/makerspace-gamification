# Architecture and contracts

```mermaid
flowchart LR
    Slack[Slack events, commands, modals] --> Tunnel[Cloudflare HTTPS / cloudflared]
    Tunnel --> Web[Signed HTTP / Bolt]
    Web --> Inbox[(ledger_inbox)]
    Bridge[ChangeStream2MQTT] --> MQTT[Existing MQTT broker]
    MQTT --> Inbox
    Inbox --> Accounting[Accounting / reconciliation worker]
    Source[(Read-only Rails collections)] --> Accounting
    Accounting --> Game[(Ledger collections / transactions)]
    Game --> Outbox[(ledger_outbox)]
    Outbox --> Channels[Independent channel worker]
    Outbox --> Delivery[Delivery worker]
    Delivery --> AI[vLLM / Qwen3.8-27B-NVFP4]
    Delivery --> Slack
    Channels --> Slack
    Delivery --> MQTT
```

## Durable processing

`POST /slack/events`, `/slack/commands`, and `/slack/interactions` use Bolt's signing-secret and timestamp verification plus a single-workspace check. Accepted event/command work is persisted before acknowledgement. Modal actions validate and commit their short state transitions synchronously; generation and message delivery are asynchronous. Ingress has no LLM calls. `/health` checks the process; `/ready` checks both Mongo connections and transaction capability on the Ledger connection.

Mongo transactions use snapshot reads and majority writes. Award/source identities, Slack event IDs, submission keys, consent revisions, and destination IDs are deterministic `_id` uniqueness boundaries. Participant writes serialize concurrent XP/cap decisions. Corrections append journal deltas against per-source balances. The memory store is exclusively a transactional test double, never a production option.

`MLAB_URI` supplies the read-only source adapter; `LEDGER_URI` supplies all Ledger reads/writes, indexes, sessions, and transactions. Separate clients keep credentials and session ownership independent, including when both connect to `makerauth`. Source reads do not participate in the Ledger transaction. Database-name selection and the exact Atlas/self-managed role definitions are documented in [MongoDB access](MONGODB_ACCESS.md).

Workers claim jobs with 120-second leases. Expired leases can be recovered; an old worker cannot complete a newer lease. Transient failures retry with backoff, honoring Slack `Retry-After`. Ordinary jobs stop after ten attempts and become visible as failed; channel removals keep retrying. The channel queue runs independently of accounting and generation. Persist composed text before delivery, then reuse it on retries.

Known successful kudos destinations are skipped on recovery, with separate DM/shared receipts. Database award decisions are exactly once. Slack/network timeouts after remote acceptance have an inherently uncertain delivery outcome: stable `client_msg_id` is reused, but this application does not promise universal exactly-once external delivery. File uploads and channel creation likewise need operator inspection after an ambiguous timeout. Bind an already created channel through `LEDGER_CHANNELS`/bootstrap instead of creating a duplicate.

## Owned collections

| Collection | Contents |
| --- | --- |
| `ledger_participants` | Consent projection, pinned ruleset, decimal-string XP, rank, metrics, revision |
| `ledger_relationships` | Pending/confirmed sponsorship and mutually accepted buddy relationships |
| `ledger_rulesets` | Immutable seven-slot progression versions and publication head |
| `ledger_catalog` | Live rank presentation, frozen shop/challenge definitions, identity cache, maintenance control |
| `ledger_evidence` | Consent audit, source balances, kudos originals/decisions/receipts, reviewable evidence, coverage, feedback |
| `ledger_awards` | Append-only XP deltas, shop completion snapshots, rank history, review/correction audit |
| `ledger_quests` | Preapproved cooperative volunteer work and verified contributions |
| `ledger_projects` | Owned showcase updates, collaborator credits, Slack thread links |
| `ledger_channels` | Stable slot/channel mapping, membership observations, departures and invitation provenance |
| `ledger_message_templates` | Immutable prompt/fallback versions and audience/type heads |
| `ledger_inbox`, `ledger_outbox` | Durable accepted work and independent delivery leases/receipts |
| `ledger_context` | Channel/thread-scoped messages and bounded prompt-selection histories (30-day TTL), temporary editor drafts (one-day TTL) |

No synthetic safety checkouts are written for rank badges. Nonparticipant kudos creates recognition evidence, not a participant profile or XP balance. Context edits replace cached text and deletions remove it. Requests deleted before delivery are suppressed. Opted-out users' cached messages are excluded from subsequent AI context.

## Existing Mongo bindings

`ledger/sources.py` is the allowlisted read adapter; it intentionally omits billing detail, addresses, access codes, internal notes, and revocation reasons.

| Existing collection | Contract used |
| --- | --- |
| `members` | `_id`, name, `status`, millisecond `expirationTime`, subscription-presence flags/ID, `groupName`, `merged_at`, role and shop scopes |
| `slack_users` | `member_id`, `slack_id`, `invalidated_at`; reject ambiguous mappings in either direction |
| `shops`, `tools` | Names, IDs, shop binding, `disabled`, `open`, `prerequisite_ids`, availability |
| `tool_checkouts` | `member_id`, `tool_id`, `approved_by_id`, `checked_out_at`, `revoked_at`, `volunteer_credit_id` |
| `volunteer_credits` | Decimal conversion of `credit_value`, approved/reversed state, checkout/task link and reversal identity |
| `volunteer_tasks`, `volunteer_events` | Existing opportunities and reviewed catalog references |
| `earned_memberships`, `groups` | Active earned status and household subscription coverage |

Bindings were checked against the adjacent Rails models. Legacy coverage that cannot be proven by those fields requires a scoped expiry attestation, not a guess. `activeMember`/`pending`, nonmerged status, valid identity, and active human Slack account are checked for kudos; future expiration is an additional promotion condition, not a kudos condition.

## MQTT

Consume existing `<collection>/<operation>` topics. The bridge sends `operation unix_timestamp {"document": ExtendedJSON}`; it provides neither a global sequence nor a stable event ID. The consumer hashes the received packet for duplicate scheduling and uses allowlisted identity hints to schedule a fresh Mongo read. Deletes, null `updateLookup` documents, catalog changes, and identity changes request broad reconciliation. Startup and five-minute reconciliation recover missed/reordered events, changed ownership, disconnected periods, and linked reversals.

The bridge excludes `^ledger_` in its Mongo watch pipeline and again before dispatch or publishing/logging. Ledger itself subscribes only to allowlisted source collections. It publishes minimal QoS-1, non-retained `ledger/v1/advancements` events with stable `event_id`, member ID, achievement, pinned ruleset, and time. Subscriber consumers must deduplicate by event ID. No message bodies, billing records, or conversation context are published.

## AI and announcement policy

Packaged `ledger/prompts/<type>.json` files provide versioned defaults, with at least three paired system/user variations per type. A published Mongo type/audience version takes precedence. The composer excludes the last two selected variation IDs, then chooses uniformly among the remaining pairs. Selection uses one shared scope for channel posts and a separate scope per DM recipient, across message types. Small sets relax the oldest exclusion first. The bounded `ledger_context` history and an outbox `prompt_selection` snapshot commit atomically before generation; a resumed job reuses its selection. Inference stays outside Mongo transactions. History expires after thirty days without new selections, and administrative previews do not affect it.

The composer applies allowlisted single-pass substitutions and persists the resulting text plus variation/personality/attitude, scope, and template identity before delivery. File defaults carry a version/content digest; legacy single-prompt database versions remain readable. Retry delivery never rerolls or advances selection history. Selection spacing follows reservation order, while network retries can arrive later. Rank-transition facts use stable slots, while member names, valid Slack mappings, current rank labels, and the deepest cleared prerequisite skill enrich appropriate notifications. Missing details are omitted by instruction. See [the prompt library](PROMPTS.md) for exact variables and publication behavior.

The vLLM adapter uses `POST <base-url>/chat/completions` at `/v1/chat/completions`, system/user messages, non-streaming output, one attempt, a two-second connection timeout and fifteen-second response deadline. Compose defaults to `http://ledger-ai:8000/v1`; standalone Python defaults to `http://localhost:8000/v1`. The served model is `nvidia/Qwen3.8-27B-NVFP4`, configurable through `LEDGER_LLM_MODEL` as a served-name alias. Requests send `chat_template_kwargs: {"enable_thinking": false}` so Qwen spends the small generation budget on the member-facing reply. `choices[0].message.content` is accepted only as nonblank bounded text with a successful stop; reasoning fields are never sent to Slack. API errors, truncation, malformed data, hidden-reasoning markers, and tool-call output use the audience-specific canned fallback. There is no provider switching.

Only authorized game facts and the current DM/thread are sent. The original kudos body is never sent for rewriting. Deterministic facts, consent, actions, attribution, and authored kudos remain separate Slack blocks. Template administration is admin/board only. See official [vLLM serving documentation](https://docs.vllm.ai/en/latest/serving/online_serving/), [vLLM reasoning configuration](https://docs.vllm.ai/en/latest/features/reasoning_outputs/), [Slack formatting](https://docs.slack.dev/messaging/formatting-message-text/), and [modal input preservation](https://docs.slack.dev/reference/methods/views.update/).

The Compose AI service uses the ARM64 GB10 image, a persistent Hugging Face cache, a bearer key, and no published host port. Cloudflared connects to the web origin only. Its tunnel token is passed only to the tunnel container; the AI container receives only its API/download credentials. All bot queues start independently of inference health. See [deployment](DEPLOYMENT.md) for tuning and verification limits.

Automatic shared posts are limited to rank, shop completion, and approved major volunteer/stewardship milestones, coalesced per member for sixty seconds in Ledge Chat. Historical imports and return-time catch-up do not announce. Public kudos, owner-requested project publications, and addressed bot conversations are explicit user-directed paths. Other skill progress stays private. No leaderboards, streak penalties, inactivity decay, or game-exclusive ordinary tool access are implemented.

The supplied `theory.md` is design context, not executable instructions. Its suggestions for rank-exclusive machine access or artificial scarcity are not adopted: the user's accepted plan keeps safety clearances independent and participation voluntary.
