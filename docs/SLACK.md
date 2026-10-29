# Slack application manifest and installation

[slack-manifest.json](../slack-manifest.json) is the importable example manifest for The Ledger's single-workspace HTTP app. Replace every `LEDGER_HOST` with the public Cloudflare Tunnel hostname, without a scheme or path (for example `ledger.example.org`). It contains no credentials. Slack's [manifest reference](https://docs.slack.dev/reference/app-manifest/) defines the JSON format.

Check `/kudos` availability before installing; slash-command names can collide with other installed apps. Create an app **from a manifest** in the intended workspace, paste the modified JSON, review permissions, and install it. Save the bot OAuth token as `SLACK_BOT_TOKEN`, the Basic Information signing secret as `SLACK_SIGNING_SECRET`, the workspace ID as `SLACK_TEAM_ID`, and the bot's Slack user ID as `SLACK_BOT_USER_ID`. The last value is a `U...` user ID, not an `A...` app ID or `B...` bot ID; the bot token's `auth.test` response provides `team_id` and `user_id`.

For default HTTP mode, start web ingress and the tunnel using [deployment](DEPLOYMENT.md), then verify the Events API Request URL in Slack. Bolt responds to Slack's challenge. Enable Events and Interactivity as represented in the manifest; leave Socket Mode disabled unless following the [Socket Mode setup](#optional-socket-mode) below. If updating an installed app's scopes, reinstall/re-authorize it before testing. The service uses a supplied bot token and does not implement an OAuth installation/refresh endpoint, so automatic token rotation is disabled in this example.

## Bot icon

Upload [icons/ledger-bot-512.png](../icons/ledger-bot-512.png) in the app's **Basic Information → Display Information → App icon** settings. This is The Ledger's anthropomorphic leatherbound grimoire artwork. Slack scales the uploaded icon for its clients; [icons/ledger-bot-36.png](../icons/ledger-bot-36.png) is the supplied mobile-size preview/export. The manifest sets a matching charcoal app-profile background. Icon upload is a separate installation step; it requires no additional runtime bot permissions. The [asset notes](../icons/README.md) include the original source, final prompt, and sizing guidance.

## HTTP endpoints and commands

| Configuration | HTTPS path |
| --- | --- |
| Events API Request URL | `/slack/events` |
| Every slash command | `/slack/commands` |
| Interactivity and option loading | `/slack/interactions` |

The manifest enables App Home and writable app messages. All eight commands have `should_escape: true` to preserve structured member mentions:

| Command | Function |
| --- | --- |
| `/ledger` | Consent, progress, sponsorship, channel invitations, feedback |
| `/ledger-skills` | Shop/tool skill trees and accessible text |
| `/ledger-quests` | Learning challenges, evidence, collaborative quests |
| `/ledger-mentor` | Success Buddies and verified mentoring |
| `/kudos` | Thanks for linked members with or without opt-in; optional emoji, public sharing and invitation |
| `/ledger-project` | Project gallery and feedback threads |
| `/ledger-admin` | Opted-in authorized staff/delegates only; role-scoped help and actions |
| `/ledger-anomaly` | Data trends, schema/detector reviews, and security analytics; channel readers receive aggregates, with staff evidence and administration restricted to verified admin/board DMs |

The anomaly commands share the existing `/slack/commands` endpoint, including when the analytics engine runs on another host. `ask <question>`, `status`, `detectors`, `schema`, and `security` are subcommands of `/ledger-anomaly`; they do not need separate Slack registrations. Security commands include `security summary`, `security incidents`, `security incident <id>`, `security evidence <id>`, `security resolve <id>`, and `security detectors list|review|propose|approve|disable`. Reviews, approvals, and disabling require an exact detector revision. See [anomaly command access and configuration](ANOMALY_SLACK.md) and [security monitoring](ANOMALY_SECURITY.md). Enable `ANOMALY_ENABLED` for requests and `ANOMALY_SECURITY_ENABLED` for security monitoring. Automatic security alerts go only to configured, currently authorized admin/board DM recipients.

All modals, buttons, checkboxes, and external shop/tool/member dropdowns use the interaction endpoint. They are interaction payloads, not additional event subscriptions or commands.

`/ledger-skills` hashes the exact accessible text equivalent and caches the generated PNG per member in `ledger_files`, along with the text checksum and cache timestamp. A later request reuses the Slack file through a Block Kit `slack_file` image when the text is unchanged and Slack still recognizes the file; changed text or a missing file triggers a new render and upload. Rank icons use the same collection and are checked before display; a permanently missing Slack file is evicted and replaced from the bundled PNG. Slack file retention or manual deletion therefore causes a fresh upload on the next request.

On promotion, no rank-up announcement is sent to shared Ledge Chat. The prior-rank channel receives a short ascent message that does not name or imply the next rank. The bot invites the member to the new-rank channel, then removes them from the prior-rank channel and posts the welcome after the invite succeeds. The shipped `rank_up.json` shared-audience variations generate these private channel messages from a generic stage summary and the member's name; the next-rank label is not passed into those generations.

## Saved opt-in and chat replies

The consent modal saves `ledger_participants.opted_in` synchronously and displays **Opt-in saved** after the transaction succeeds. Repeating `/ledger join` or clicking an old invitation while still opted in shows **Already opted in**, current rank/XP, and any pending history import. It does not reset consent, rules, progress, or invitations. Opting out and joining again still shows the consent form. Older images always reopened consent even for a saved participant; that alone did not mean consent was lost.

HTTP 200 for a chat event confirms receipt, not that a reply has been generated. The complete path is:

`ledger-web → ledger_inbox → ledger-accounting → ledger_outbox → ledger-interactive → Slack`

`ledger-channels` separately handles invitations/removals, `ledger-results` handles consolidated results, and `ledger-delivery` handles routine notices and artwork. Run these alongside accounting, interactive, and web. If accounting is absent, chat events and history imports remain queued; welcome messages wait for the missing import. Current workers report `HistoryImportPending`, defer the welcome without spending delivery retries, and log `check ledger-accounting`. Older images reported only `RuntimeError` and could exhaust their retries. Already-failed jobs need the targeted retry procedure in [operations](OPERATIONS.md#recovery); do not erase consent or re-award XP.

```sh
docker compose up -d --build --no-deps ledger-web ledger-accounting ledger-delivery ledger-channels ledger-results ledger-interactive
docker compose ps --all
docker compose logs --since=5m ledger-accounting ledger-delivery ledger-channels ledger-results ledger-interactive
```

Accounting logs `Slack event processed ... outcome=reply_queued` when it queues a reply; otherwise it reports a fixed reason such as `ignored_unlinked_identity`, `ignored_unjoined_channel`, or `ignored_unaddressed_channel_message`. No message body is logged. Pending `slack_event` inbox jobs point to accounting; pending `conversation`/`guidance`/`community_count_reply` jobs point to interactive; `summary_flush`/`summary_delivery` jobs point to results. Check workers use the same `LEDGER_URI`/`LEDGER_DATABASE` as web if queued jobs never appear to them. Check Slack errors such as `missing_scope` or `invalid_auth` in the responsible worker logs, and reinstall the Slack app after scope changes.

Linked human members may chat in DMs without opting in. In any channel the bot has joined, address The Ledger by name or Slack mention, or continue a thread containing a bot reply or a bot-authored root post. The first bot response to a top-level message is broadcast to its parent conversation; later replies remain in the thread. Only registered Ledger channels examine unaddressed messages containing `?` or recognize self-directed progress requests, with one exception: any human user, linked or not and regardless of Ledger participation, can ask supported questions about space usage or new-member totals in a DM or any channel where the bot is currently a member. Generic “how many people” wording must bind an attendance verb and the space, makerspace, or “here”; supported forms include “visited the makerspace,” “came to the space,” “came here,” and “checked in here.” Named destinations must end before the supported time period, punctuation, or question boundary. Destination filtering applies to every attendance form, including “how busy,” visitors, and check-ins, so website, page, class, course, workshop, and station contexts remain outside the physical-attendance path. The count itself comes from a deterministic fixed aggregate. For space-use answers, the interactive worker sends only that count and its approved timeframe to Qwen for varied wording such as “A total of 12 members used the space today.” Python accepts only a bounded affirmative factual grammar; changed, negated, approximate, bounded, or otherwise qualified totals, changed timeframes, identifier/storage wording, and extra numerical claims use the same plain member fallback. The original question and ambient history are never sent or retained. “Right now” means the preceding two hours and must remain a recent-use estimate rather than live occupancy. New-member counts stay deterministic, use `startDate`, and exclude merged duplicate records. Unrelated ambient messages in other channels are neither saved as chat context nor sent to inference; old ambient jobs are rejected before inference, and unrequested stored history is excluded from addressed replies there. Ordinary unaddressed chatter receives no reply. Nonparticipants may ask about The Ledger and XP generally and known shops/tools, with an optional `/ledger join` journey invitation; rules, specific ranks/quests and retained personal progress stay unavailable. Participants receive accurate current/lower rank details and next-rank requirements, with higher-rank names/details and inaccessible quests excluded from conversation facts. Unknown answers, including unavailable shop/tool facts, say "I don't know." Chat narration does not itself execute administrative commands or award XP; use the command/forms for actions. Ensure `message.im` and `app_mention` subscriptions, `message.groups` for private-channel replies and `message.channels` for joined public channels, and the matching manifest scopes are installed. `SLACK_BOT_USER_ID` must be the bot's `U...` user ID.

## Optional Socket Mode

Set `SLACK_SOCKET_MODE=true` and `SLACK_APP_TOKEN=xapp-...` in `.env` to receive
commands, events, and interactions over an outbound WebSocket instead of the
tunnel. In the Slack app settings, enable **Socket Mode**, then create an
**app-level token** under Basic Information with `connections:write`. This is
separate from the existing `SLACK_BOT_TOKEN`; keep the bot scopes, event
subscriptions, and interactivity enabled. See [Slack's Socket Mode guide](https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/).

Rebuild/recreate the web service after applying these settings:

```sh
docker compose up -d --build --no-deps ledger-web
```

The Docker entrypoint and `ledger serve` honor the flag. Socket Mode starts one
consumer per web container using Bolt's `websocket-client` adapter, with automatic
SDK reconnection and graceful shutdown;
it does not start consumers in Gunicorn workers or accounting/delivery workers.
Compose passes the app token only to `ledger-web`. Existing queue persistence,
workspace checks, authorization, and acknowledgement behavior still apply. Socket
Mode bypasses inbound tunnel routing, but does not fix slow handlers or Mongo errors.

`/health` stays available; `/ready` requires both databases and an active Slack
socket. HTTP `/slack/*` callbacks return 404 in Socket Mode. The signing secret is
required only for HTTP mode. Cloudflared may remain running, but is not needed for
Slack ingress in Socket Mode. Do not put the app token in the manifest or bot scopes.

### Repeated broken pipes

Slack periodically refreshes Socket Mode connections, but a new session failing
within milliseconds with `BrokenPipeError` on every retry is an outage, not normal
refresh behavior. Ledger uses Slack's supported `websocket-client` adapter in place
of the built-in socket implementation as a transport mitigation. Rebuild the image
and recreate `ledger-web` with the command above to install the new dependency;
restarting an old container does not apply this change. This does not establish the
cause of a deployed network failure or guarantee recovery through a broken proxy.

After deployment, check `docker compose logs --timestamps --since=10m ledger-web`,
confirm `/ready` returns 200, and invoke `/ledger` to verify end-to-end handling.
`/health` returning 200 only confirms the process is alive; Compose uses this
liveness check, so a healthy container can still have no Slack connection.
If immediate disconnects persist, investigate the host/container's outbound TLS
WebSocket path and any configured proxy/firewall. Collect installed versions with
`docker compose exec ledger-web python -c "from importlib.metadata import version; print({p: version(p) for p in ['slack-bolt', 'slack-sdk', 'websocket-client']})"`.
Avoid enabling WebSocket frame tracing in production: it can expose URLs and message
payloads. See [Slack's adapter reference](https://docs.slack.dev/tools/bolt-python/reference/adapter/socket_mode/websocket_client/index.html).

The checked-in manifest defaults to HTTP (`socket_mode_enabled: false`). If importing
it for a Socket Mode deployment, change that setting to `true` in the imported copy;
reimporting the default manifest would disable Socket Mode. To return to HTTP, set
`SLACK_SOCKET_MODE=false`, disable Socket Mode in Slack, restore the callback URLs
and signing secret, and recreate `ledger-web`. The normal two-worker Gunicorn
server and signed HTTP verification are then used again.

## Standalone Slack connectivity check

After rebuilding the web image, run the bot-token check inside the deployed container:

```sh
docker compose exec ledger-web ledger slack-check
```

This calls `auth.test` and requires the returned bot identity to match
`SLACK_TEAM_ID` and `SLACK_BOT_USER_ID`. It needs `SLACK_BOT_TOKEN` but no database,
MQTT, Google Docs, or inference service. JSON output includes dependency versions
and individual check results. Exit status is 0 for success and 1 for failure.
Exception messages, tokens, WebSocket URLs and received payloads are not printed.

To test the outbound WebSocket too, stop all normal Socket Mode consumers for this
app first, then use a temporary container with the same service environment/network:

```sh
docker compose stop ledger-web
docker compose run --rm --no-deps ledger-web ledger slack-check --socket
docker compose up -d --no-deps ledger-web
```

Run the last command even if the check fails. The socket check additionally requires
`SLACK_APP_TOKEN` with `connections:write`, opens a connection using `websocket-client`,
requires Slack's `hello`, and observes 20 seconds of repeated matching ping/pong
responses. It does not reconnect over failures. Slack can route events to this
temporary connection; use a maintenance window, avoid commands during the probe,
and retry commands after restoring ingress. An application envelope fails the check
and is never acknowledged or saved. Neither mode posts Slack messages.

Passing proves bot authentication/identity and, with `--socket`, sampled transport
health. It does not verify channel permissions, app-token/bot-token app pairing,
the running Bolt consumer, database persistence, workers, or end-to-end replies.
After restoring ingress, invoke `/ledger` manually to verify that complete flow.

## Troubleshooting "the app did not respond"

Slack needs an HTTP acknowledgment within three seconds of invoking a command ([Slack's command guide](https://docs.slack.dev/interactivity/implementing-slash-commands/)). Healthy `/health` and `/ready` responses do not verify this path: readiness checks Mongo connectivity and Ledger replica-set topology, not collection read/write authorization, callback reachability, or worker delivery.

For Socket Mode, check `docker compose logs ledger-web` for connection errors and
check `/ready`; no HTTP command POST is expected. For HTTP mode:

1. In the Slack app's **Slash Commands**, check each command's Request URL is exactly `https://YOUR_HOST/slack/commands`. Event subscriptions and interactivity have their own URLs above. Leave Socket Mode disabled in HTTP mode. Editing the repository manifest alone does not update an installed Slack app.
2. Send an **unsigned** probe through the public hostname (replace `YOUR_HOST`):

   ```sh
   curl -i --max-time 5 -X POST 'https://YOUR_HOST/slack/commands' \
     -H 'Content-Type: application/x-www-form-urlencoded' --data 'command=%2Fledger'
   ```

   Expect **401** from Bolt; this request cannot run a command. A redirect/login, challenge, 404, or gateway error indicates a routing/access issue. Cloudflared's origin is `http://ledger-web:3000`, with paths preserved; Slack must reach callback paths without an Access login or browser challenge. A 401 proves unsigned traffic reaches verification, not that Slack's signing secret is correct.
3. Reproduce one real `/ledger` invocation while watching:

   ```sh
   docker compose logs --follow --since=5m ledger-web cloudflared
   ```

   The supplied image logs method, path, HTTP status, and duration, without query strings, headers, or bodies. Callback failures and responses taking at least 2.5 seconds also produce a warning. Listener errors record exception class and numeric Mongo error code without the potentially sensitive exception text. To install these diagnostics from an older image, run `docker compose up -d --build --no-deps ledger-web` first.

   | Result for the real Slack request | Next check |
   | --- | --- |
   | No web access-log entry | Slack Request URL, Socket Mode, Cloudflare route/Access/WAF; ensure the image includes access logging |
   | 401 | `SLACK_SIGNING_SECRET` from this app's Basic Information, host clock, and unmodified request body |
   | 403 with `Workspace not allowed` | `SLACK_TEAM_ID` must match the installed workspace's `T...` ID |
   | 404 | Exact callback path; remove trailing slash or unintended path prefix |
   | 500/503 | Safe listener/ingress error record; `OperationFailure code=13` means Mongo authorization failed. Check source reads and Ledger writes using [the role examples](MONGODB_ACCESS.md), and complete initialization/bootstrap |
   | 200 taking around three seconds or more | Database lookup/transaction latency or synchronous Slack modal API calls; AI generation is outside ingress |
   | Prompt 200, but no later DM | Inspect `ledger-accounting` and `ledger-delivery` logs and the inbox/outbox; acknowledgment succeeded |

After changing `.env`, recreate the affected containers with `docker compose up -d --no-deps ledger-web ledger-accounting ledger-delivery ledger-channels ledger-results ledger-interactive`; a plain restart does not apply changed Compose environment values. Do not disable signature verification or acknowledge failed persistence to hide the error. Share only redacted diagnostic lines; keep request payloads, signing secrets, tokens, Mongo URIs, and `response_url` private.

## Event subscriptions

| Bot event | Handler purpose | Scope used |
| --- | --- | --- |
| `app_home_opened` | Publish the first-open placeholder and queue the personalized Character Sheet | No additional event scope |
| `app_mention` | Threaded responses when The Ledger is addressed | `app_mentions:read` |
| `message.im` | Onboarding/opt-out DMs and conversations; message edits/deletions | `im:history` |
| `message.groups` | Joined private-channel conversations and context edits/deletions | `groups:history` |
| `message.channels` | Joined public-channel questions, mentions and existing bot threads | `channels:history` |
| `member_joined_channel` | Check the join immediately and enqueue a registered-channel reconciliation | `groups:read` for private channels |
| `member_left_channel` | Record voluntary departure and respect re-invitation rules | `groups:read` for private channels |
| `user_change` | Refresh active human identity/deactivation state | `users:read` |

Slack delivers `message.im` and `message.groups` as `type: "message"`; `message_changed` and `message_deleted` are subtypes, not separate manifest entries. Channel events require the bot to belong to the channel. The worker immediately checks managed-channel joins and queues a durable channel reconciliation for every join event, including The Ledger's own join. Confirmed third-party bots are cached and excluded before removal work is queued. Direct join cleanup and reconciliation share one stable removal record per channel/user presence; a later join reopens a completed record instead of accumulating another document. Apply or reinstall the manifest so the deployed Events API subscription includes `member_joined_channel`. See Slack's [channel membership event contract](https://docs.slack.dev/reference/events/member_joined_channel/) and [App Home event contract](https://docs.slack.dev/reference/events/app_home_opened/).

The first Home-tab open publishes a brief processing placeholder and queues a personalized Character Sheet. A confirmed placeholder publication sets `ledger_homes.needs_replacement`; a confirmed generated view saves the complete view and represented rank. Startup and daily reconciliation queues opted-in ranked members whose saved Home is missing, still a placeholder, or represents an older rank. Home publications run in a low-priority outbox lane, and periodic repair jobs are delayed by five minutes. If `views.publish` identifies an invalid `slack_file` block, the worker logs its block index, file ID, asset key, Slack request ID, and safe `files.info` metadata; it clears the matching cached `file_id` so the next job attempt regenerates the asset. After a successful generated Home publication, the worker reads the largest available Slack profile image and asks Qwen for a brief description only when its downloaded-image checksum is new; the description and checksum are saved and logged in `ledger_homes`. In chat or DM, appearance questions about the caller or a named opted-in member use Qwen to rephrase the saved description in positive, flattering terms; missing descriptions receive a neutral unavailable response. Generated views are bound to the current member record and consent generation; an identity reassignment or rejoin causes a rebuild before the saved view is reused. Verified milestones, rank changes, and staff rank corrections queue Home refreshes. Ordinary XP ticks do not.

## Bot permissions

| Scope | Calls or events that need it |
| --- | --- |
| `commands` | All slash commands and their interactive flows |
| `app_mentions:read` | `app_mention` event |
| `chat:write` | `chat.postMessage` for DMs, notifications, kudos, project threads, and replies |
| `channels:read` | `conversations.info` verifies public-channel access; `conversations.list` resolves the anomaly channel and `conversations.members` verifies its readers |
| `channels:history` | Receives `message.channels` in public channels the bot has joined |
| `channels:manage` | `conversations.kick` for public-channel cleanup |
| `groups:read` | `conversations.info`, `conversations.list`, `conversations.members`, private-channel membership events; resolves private anomaly channels and verifies their readers |
| `groups:history` | `message.groups`, including edits/deletions; private project thread visibility |
| `groups:write` | `conversations.create` with `is_private`, `conversations.invite`, `conversations.kick` |
| `im:history` | `message.im`, including edits/deletions |
| `im:read` | `conversations.info` verifies an anomaly reply or security alert is going to the authorized recipient's own one-to-one DM |
| `im:write` | `conversations.open` for recipient DMs and file delivery |
| `users:read` | `users.info` for human/active checks, `user_change`, and complete bounded `users.list` name/alias reads for quest-inspiration redaction |
| `users.profile:read` | `users.profile.get` for profile images used by avatar and appearance features |
| `files:write` | Skill-tree image/text, rank art, avatars, and anomaly charts via `files.getUploadURLExternal` / `files.completeUploadExternal` (`files_upload_v2`); `files.delete` cleans up generated avatar artifacts |
| `files:read` | `files.info` checks cached image availability and submitted quest/avatar attachments |

The added `im:read` scope is required by [conversations.info](https://docs.slack.dev/reference/methods/conversations.info/) for DM metadata. Update the installed app's manifest and reinstall/re-authorize it so its bot token receives the scope, then verify `/ledger-anomaly status` in a permitted caller's DM. Source security events come from MongoDB; no Slack Audit Logs or workspace-admin scope is required. Application authorization and Prompt Matrix 52 continue to determine who may receive evidence; installing a scope does not grant a member a staff role.

`groups:write` is needed for [private-channel removal](https://docs.slack.dev/reference/methods/conversations.kick/) as well as [invitations](https://docs.slack.dev/reference/methods/conversations.invite/); `channels:manage` covers public-channel removal. Removal silently skips every Slack bot user, including The Ledger's configured bot ID and Slackbot. A failed human `conversations.kick` is logged as a best-effort cleanup failure and is not retried. Slack 429 responses establish a client-wide cooldown from `Retry-After`; later Slack API calls wait until that interval expires. [External file uploads](https://docs.slack.dev/reference/methods/files.getUploadURLExternal/) require `files:write`. `views.open`, `views.update`, `views.publish`, and `chat.getPermalink` need an authenticated bot but no extra scopes beyond this set for the supported flows.

No user-token scopes, workspace administration, email lookup, reaction write, or incoming webhooks are needed. An app-level token with `connections:write` is needed only when Socket Mode is enabled. Custom emoji shortcodes render in Slack without calling `emoji.list`. Ordinary app tokens do not bypass workspace policy on creating private channels or removing members; verify those permissions with workspace administrators during the pilot.

The bot must be invited to existing private game channels before `ledger bootstrap`. Bootstrap creates new private channels if none are supplied. Test join/leave reconciliation with real members: native manual invitations can briefly expose a private channel before removal, as documented in the accepted design.

## Progress and quest interactions

`/ledger stats`, `/ledger progress`, `/ledger preferences`, and `/ledger achievements` open deterministic private views. `/ledger-quests list` uses external-select title options, with hash-protected updates and revalidated selections/actions. `/ledger-quests create` is available to every active participant and supports individual/cooperative proposals, minimum rank, duration, up to 20 currently cleared available tools across shops, two to four cooperative disciplines, and one JPEG/PNG/GIF up to 10 MiB. Tool and photo choices are revalidated at submission and approval. Help and rewrite actions queue prose-only Qwen assistance; interaction acknowledgments never wait for inference and the participant must submit explicitly. Human review sets completion XP and the one-time proposer bonus. See [quest generation and participant proposals](QUEST_GENERATION.md).

## Kudos emoji and consent

The picker begins with silver star (`:kudo:`), gold star (`:kudos:`), fixed it! (`:fix_parrot:`), First place (`:first_place_medal:`), fistbump! (`:fistbump:`), used the force (`:duct_tape:`), Thank You (`:thankyou:`), and Teamwork! (`:teamwork:`). Custom shortcodes refer to the workspace's existing emoji.

The `/kudos` sending modal offers an optional emoji picker. Its selection is saved with the submission and survives shop/recipient changes and retries. Negative/offensive selections (including `:poop:`, `:-1:`, `:middle_finger:`, `:clown_face:`, aliases and tone variants) and unknown picker values are silently dropped. The selected emoji and a space precede "You have received kudos from <SENDER>" in the recipient DM; SENDER is a validated Slack mention. The public Ledge Chat header includes the same emoji, recipient and sender. Authored formatting and emoji in the message body remain untouched and are never sent to inference.

Both sending and receiving are available without game opt-in. Recipient participation still controls XP; one giver/recipient/week and five recipient/day qualifying XP caps still apply, and extra thanks still deliver. Nonparticipant senders can request public delivery and an invitation, but do not become recruitment sponsors. Requested acknowledgments/receipts are delivered without opt-in. Public-channel scopes and subscriptions have changed: update the manifest and reinstall the app before verifying joined-channel chat.

New kudos submissions send one giver result after requested destinations finish, or a plain pending/partial result at 60 seconds that later edits in place. Python renders recipient, destination outcomes, and actual once-only XP in a compact paragraph. Successful results may append a restrained sentence from `delivery.json`; the original kudos body and recipient progression are excluded from inference. Existing acknowledgement/receipt jobs retain their reserved flow. Customize with `/ledger-admin template delivery member` or explicitly adopt the updated file via `/ledger-admin template-library delivery member`. Saved choices/text remain stable on retries. Game results and the progress modal offer caller-only “Suggest a next step”; result replies use that DM's thread. See [result summaries](RESULT_SUMMARIES.md) and [prompt variables](PROMPTS.md).

Review notifications post and update through `chat.postMessage`/`chat.update`, both covered by existing `chat:write`. `LEDGER_QUEST_REVIEW_CHANNEL_ID` must name a private, non-external staff channel separate from game channels, with the bot invited. A deleted saved review message is replaced only when update returns `message_not_found`; permission and rate-limit failures use outbox retry.

Review reconciliation selects pending activities, unresolved failed/cancelled notice jobs, channel-mismatched addresses and indexed dirty notices closed while channel configuration was absent, including legacy addresses nested in contributions. Current fingerprints and already queued deliveries cause no activity transaction or rewrite. Resolved failed jobs retain their audit records with a reconciliation marker. New parent channel/dirty markers and indexes support selective recovery; run `python -m ledger init` after updating to initialize the indexes before normal service operation. Closed historical activities with no prior notice are not backfilled.

`/ledger-admin review` also lists pending cooperative contributions with quest IDs, member mentions and `verify-quest` commands, plus completion-ready shared projects with `complete-quest` commands. Each row requires the caller's current completion capability/scope and independence from contributors. Shared readiness requires two eligible verified contributors covering every discipline; current prerequisites and quest availability still apply. Delegates can discover authorized work without joining the private review channel; listing grants no additional authority or channel membership.

## Administrative access and invitations

Application Home/help, command responses, and generated private chat show administrative commands only to eligible opted-in humans. Source MLAB members role admin enables /ledger-admin at every rank, including slot zero. Board members retain global authority, resource managers retain shop scope, and active delegates retain granted review scope; all require current participation for this command. Rank or a role claim in chat never grants access. Ineligible invocations return an account-unavailable response without administrative help. Queued help is rechecked before delivery.

Use /ledger-admin help for available actions and /ledger-admin invite to open a recipient picker, optional sender name, and optional personal message. The picker excludes opted-in, revoked/suspended, merged and invalid human identities, and submission/delivery recheck eligibility. A blank sender uses the admin source first/last name. A supplied sender replaces that real name in all invitation text and blocks; no admin mention or sponsorship identifier is sent to the recipient. Personal text is delivered without AI rewriting. This is a consent invitation, never automatic enrollment or XP. The recipient still reviews the notice and explicitly opts in.

The invitation picker searches source first/last-name tokens case-insensitively, in either order, or a Slack user ID. It reads up to 500 matching source members, checks Slack identity uniqueness and participation in batches, and returns at most 100 eligible options; narrow the search if needed. Autocomplete database operations share a two-second [PyMongo deadline](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/connection-options/csot/). Database failures return empty options and log only the error class. A repeated “Nothing could be found” may indicate a database failure; check `Slack options lookup failed` logs and the source identity link.

`/ledger invite @member [chat|rank:<slot>]` is the existing invitation to a game channel for an opted-in member. Bare `/ledger invite` returns that usage immediately. To invite someone to opt in, an eligible admin uses `/ledger-admin invite`; a participant can use `/ledger sponsor @member`. Picker requests use `/slack/interactions`; slash commands use `/slack/commands`.

`/ledger sponsor` without a mention sends the caller a private, newest-first table of everyone they invited, with the invitation date, current opt-in state, latest opt-in date and latest opt-out date. Large registers continue across messages without dropping rows. `/ledger sponsor @member` shows those facts without resending when the caller already invited that member; otherwise it records and sends an eligible invitation. Each inviter has a separate history record, while the first canonical sponsor retains recruitment credit. Requested kudos invitations count; admin consent and game-channel invitations do not. Dates display in `America/New_York`; records remain UTC. The same caller-owned data is available through private DM questions such as “Who have I sponsored?” Shared-channel questions reveal no sponsorship facts.

See [recruitment and opt-in](RECRUITMENT.md) for participant reminders and invitation follow-ups.

Sponsor reports save one fact snapshot for retry consistency and recheck the caller's participation, consent generation and Slack identity before every page. Python renders every name, date, count and status. Qwen supplies only a short fact-free opener; invalid or unavailable generation uses a canned opener. Run `ledger init` after deployment to create the sponsor/consent indexes and idempotently backfill pair history from legacy canonical sponsor rows.

Source-role admins who opt in are invited to LEDGER_QUEST_REVIEW_CHANNEL_ID; opting out removes them. Board/resource/delegation roles are not automatically added. The destination must be private, unshared, contain The Ledger and be separate from game channels. Reconciliation backfills opted-in admins and cleans up role/identity/configuration changes. Membership confers no additional review authority.

Slack itself registers slash commands workspace-wide and does not offer application-controlled per-member command discovery. The app hides all its own administrative help for ineligible members and uses a generic registered description, but Slack's slash-command picker can still show /ledger-admin. See [Slack slash-command accessibility](https://docs.slack.dev/slack-marketplace/slack-marketplace-app-guidelines-and-requirements/#slash-commands).

SLACKIDS_INELIGIBLE in .env is an optional quoted, comma-separated set of Slack user IDs. Listed users may use /kudos and may send or receive ordinary kudos, but cannot opt in, receive invitations or unsolicited Ledger DMs, or join managed Ledger channels. Other Ledger commands are silently ignored, and invitation requests to them are silently suppressed. See recruitment and opt-in (RECRUITMENT.md) for the format and reconciliation guidance.

Kudos modal sends are durably reserved before Slack acknowledgment and processed by the interactive worker. Fixed DMs report acceptance or validation errors and offer caller-only Review kudos with the authored draft preserved. Pending and expired/lapsed members can receive kudos and requested consent invitations; suspended/revoked memberships and bot/deactivated Slack accounts cannot. Participation and once-only XP checks remain separate.

`/ledger-admin avatar @member` shows the selected participant’s avatar and saved generation statistics with a force-generate/regenerate button. A custom-avatar opt-out produces only an opt-out notice. `/ledger-admin avatar` opens a filtered participant picker; submit to view the selected participant’s avatar and saved data/statistics, then optionally click force-generate/regenerate to queue generation. These controls require current opted-in admin/board authority and preserve participant consent.
