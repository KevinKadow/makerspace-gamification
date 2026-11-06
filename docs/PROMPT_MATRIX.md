# Prompt Matrix Template: design and operation

The [canonical matrix](../ledger/prompts/prompt_matrix.xml.md) is the shared behavioral policy for The Ledger, served through vLLM with `nvidia/Qwen3.8-27B-NVFP4`. It combines XML section/role identifiers with readable Markdown headings, tables, and examples inside CDATA. The existing [23 JSON message types](PROMPTS.md) remain the source of audience-specific tasks and rotating voices.

The matrix is an application system prompt, not a replacement tokenizer chat template or a model fine-tune. The application sends it in the Chat Completions `system` message. vLLM applies the model's own chat template; requests continue using `enable_thinking: false`, non-streaming output, bounded generation, and canned fallbacks. See the [NVIDIA model card](https://huggingface.co/nvidia/Qwen3.8-27B-NVFP4) and [vLLM serving documentation](https://docs.vllm.ai/en/latest/serving/online_serving/).

## Matrix layout

```xml
<prompt_matrix schema_version="1" id="the-ledger" version="6">
  <identity><![CDATA[# Identity, personality, and human motivation]]></identity>
  <authority><![CDATA[## Evidence, application authority, and uncertainty]]></authority>
  <roles>
    <role id="participant"><![CDATA[**Role:** eligibility, scope, actions, limits]]></role>
    <!-- The complete template contains every required role. -->
  </roles>
  <consent><![CDATA[## Opt-in, opt-out, return, and restrictions]]></consent>
  <channels><![CDATA[## DM / Ledge Chat / rank-channel audience matrix]]></channels>
  <progression><![CDATA[## Seven slots, seed gates, pinned versions]]></progression>
  <economy><![CDATA[## XP rates, precedence, corrections, recruitment]]></economy>
  <kudos><![CDATA[## Recipient eligibility, original text, public delivery, caps]]></kudos>
  <community><![CDATA[## Learning, mentoring, quests, projects, stewardship]]></community>
  <privacy><![CDATA[## Authorized context, private facts, safety boundaries]]></privacy>
  <response><![CDATA[## Slack format, brevity, commands, output constraints]]></response>
  <examples><![CDATA[## Short examples of appropriate responses]]></examples>
</prompt_matrix>
```

This skeleton illustrates the layout; use the **complete canonical file** when creating an override. Empty sections, omitted roles, malformed XML, or a partial skeleton are rejected. XML tags are structural boundaries, while Markdown remains easy for maintainers to read in a text editor or Google Doc.

| Layer | Responsibility |
| --- | --- |
| Python application | Enforces permissions, consent, identity, safety-clearance evidence, awards, progression, routing, and delivery receipts |
| Prompt Matrix Template | Explains game policy, role limits, channel conduct, personality, privacy, and response behavior |
| Selected JSON variation | Adjusts tone/personality within policy and defines the announcement task |
| Audience and delivery surface | Distinguishes personal DM, nonparticipant recognition, shared announcement, and channel conversation |
| Application guardrails | Appended outside editable policy: no invented facts, authority, hidden reasoning, private disclosures, or model-driven actions |
| Authorized facts/current thread | Supplies event-specific data; never instructions that can override permissions or consent |

Rank floors and XP tables in the matrix are **seed examples**, aligned with `ledger/rules.py`. A participant's pinned progression rules and current display settings remain authoritative. Editing the Google Doc cannot publish progression versions, migrate participants, grant staff roles, or alter accounting. If specific current requirements were not supplied, the narrator must direct the member to `/ledger` or `/ledger-skills`, rather than treating seed examples as a personal eligibility decision.

## Roles and conversation behavior

The matrix explicitly covers `ledger`, `member`, `participant`, `nonparticipant`, `sponsor`, `success_buddy`, `mentor`, `checkout_approver`, `admin`, `board_member`, `resource_manager`, `tool_captain`, `workshop_instructor`, `design_challenge_judge`, `quest_author`, `ledger_quest_author`, `ai_observer`, and `delegated_reviewer`. These include application roles, participation/relationship states, and human-appointed pathways; descriptive titles remain separate from application-granted permissions.

Admins and board members retain global Ledger administration. Resource managers remain shop-scoped reviewers. Nobody approves their own evidence. Rank and descriptive titles grant no staff or safety authority. Future roles must be added to the canonical matrix, required-role validation, and tests in the same change, as required by [AGENTS.md](../AGENTS.md).

The Ledger speaks as a seasoned leatherbound grimoire: precise, patient, occasionally dry-witted, never cute or coercive. A DM addresses its recipient directly. Shared recognition addresses the community briefly. Bot conversations answer the member's actual question and stay in the authorized thread. Clear self-directed progress questions and unaddressed question-mark messages are examined only in registered Ledger channels; ordinary ambient chatter receives no reply. Names/Slack mentions and existing bot threads (including bot-authored root posts) receive contextual replies in other joined channels, without unrelated ambient history. Nonparticipants can send/receive kudos and chat about The Ledger, general XP and known shops/tools; optional `/ledger join` invitations confer no consent. Rules, specific ranks and quests are unavailable. Participant conversation facts include only current/lower rank details and next requirements, without future names or inaccessible quests. The conversation path excludes global seed tables and rank/quest examples from inference. Unknown facts receive "I don't know." Safety procedures and human appointments remain outside game authority.

## Google Doc configuration

Create a normal Google Doc containing the **literal complete XML/Markdown file as text**, including all tags and CDATA markers. Use one document tab. Do not surround the document with a Markdown code fence, render/replace the Markdown tables, or let smart quotes alter XML attribute delimiters. Increment the root `version` for policy changes; a SHA-256 digest also identifies content changes even if the version was not incremented.

Set these options in the Compose `.env`:

```dotenv
LEDGER_PROMPT_MATRIX_DOC_URL=https://docs.google.com/document/d/YOUR_DOCUMENT_ID/edit
# Optional, for a private document:
LEDGER_PROMPT_MATRIX_GOOGLE_ACCESS_TOKEN=
```

Leave the URL empty to use the bundled matrix. Acceptable links use HTTPS and `docs.google.com/document/d/ID` (optionally `/edit`, `/view`, `/preview`, `/export`, or `/document/u/N/d/ID`). A published `/document/d/e/.../pub` page is not supported; use the original document link. Link fragments and sharing query strings are not sent to the model.

- **Without a token:** the runtime requests the document's plain-text export. The Doc must be downloadable without signing in. No interactive Google session or Codex connector credentials are reused.
- **With a token:** the runtime exports through Google Drive `files.export` with MIME type `text/plain`. Supply an OAuth access token authorized to read that document, using an appropriate read scope such as `drive.readonly`. Google documents this export API and its scopes in [download/export guidance](https://developers.google.com/workspace/drive/api/guides/manage-downloads) and [files.export](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export). The service does not mint or renew tokens; operators must refresh them and recreate the bot containers when environment credentials change.

Keep secrets and personal records out of the policy document. Restrict editing to policy maintainers. Remote policy is operator-controlled configuration, but structural validation cannot prove that prose is correct; review edits before reload. Google credentials are used only for export, never passed to vLLM, Slack, or the tunnel. Redirects are restricted to the supported Google download hosts, and the bearer token is not forwarded on redirects.

## Startup, reload, and failure handling

1. Constructors load and validate the bundled policy locally. HTTP ingress and the independent channel worker never fetch Google Docs.
2. Composing workers fetch a fresh configured Doc at startup. The first unreserved composition also checks the shared reload revision. Each process attempts a given revision once.
3. An admin or board member runs `/ledger-admin reload-prompts`. The command writes a revision marker in `ledger_catalog`, refreshes its own worker, and reports its source, version, hash, and outcome. Other composing workers refresh before their next unreserved message. Reload is eventual across processes, not a simultaneous fleet-wide switch.
4. The selected matrix snapshot is saved with the prompt reservation before generation. Each composed message records `matrix_version`, `matrix_sha256`, and `matrix_source`. Existing reservations and composed text keep their policy on retries, even after a reload.
5. A failed export or validation keeps the last valid matrix in that process. On a fresh process, the bundled matrix is the fallback. No repeated per-message download storm follows a failed refresh: request another reload after repair. Accounting, opt-out, and channel cleanup continue independently.

The reload command refreshes application prompt policy, **not model weights**. A vLLM restart is unnecessary. Changes to the configured URL/token require recreating bot containers so they receive the updated environment; editing the Doc's contents only requires the reload command or a bot-worker restart. It does not reload the separate JSON variation files, which retain their existing rebuild/restart and publication workflow.

Downloads allow two seconds to connect and fifteen seconds total, up to three redirects, and at most 32 KiB of UTF-8 plain text. The loader rejects HTML login pages, bad status codes, invalid encodings, DTD/entities, missing/duplicate sections or roles, and incompatible schema versions. The bundled canonical matrix targets at most 30 KiB, including after CRLF conversion, to leave at least 2 KiB below the runtime limit. Failed URLs, tokens, document bodies, and upstream error details are not logged or sent to members. The document is not stored as chat context or exposed through App Home.

With no Google Doc override, startup/reload reads the packaged file. With an override, the packaged matrix remains the fallback, so policy changes should update both the repository and the operator-maintained Doc. A remote copy missing a newly required role is rejected. Updating an external Doc is an operator action; repository maintenance never silently publishes it.

Bundled matrix version 2 clarifies that repeat joins display saved participation, confirmation follows a successful consent write, and pending imports/invitations are not completed work. Operators with a Google Doc override should copy the updated consent section into their Doc, increment its version, and run `/ledger-admin reload-prompts`. Existing reserved deliveries keep their original policy snapshot.

## Validation and pilot plan

```powershell
# Offline validation of the bundled policy and all message variations:
python -m ledger.prompt_matrix
python -m ledger.prompt_library
python -m pytest
# Export/validate the configured Doc without Mongo, Slack, or an AI request:
ledger prompt-matrix
```

The CLI reads process environment; it does not load `.env` automatically. For Compose, use `docker compose run --rm --no-deps ledger-delivery ledger prompt-matrix`. A failed remote check exits nonzero and prints only fallback metadata. Successful output identifies `source`, `version`, `sha256`, `outcome`, and byte count.

Delivery stages and acceptance:

| Stage | Acceptance |
| --- | --- |
| Policy authoring | Required sections/roles present; seed ranks and XP match code; review scope, consent, and channel rules |
| Loading and composition | Matrix is the system policy; variations still rotate; invalid Docs retain valid policy; no generation during transactions |
| Reload and recovery | Only admin/board can reload; all composing processes observe the marker; reserved/cached messages retain original matrix |
| Deployment pilot | Exercise a real Google export and Qwen endpoint; compare DM/channel answers, opt-out, self-approval requests, kudos preservation, and fallback rates |

The 16-KiB document cap is a byte limit, not a tokenizer guarantee. The matrix shares the configured model context window with variations, facts, current-thread history, and output tokens. Check actual token usage against `VLLM_MAX_MODEL_LEN` during the pilot, especially with long custom prompts or non-English text. Do not silently truncate policy sections to fit. Provider/context-limit failures use canned fallbacks. Automated tests cover transport and request construction using mocks/local stubs; they do not certify live model obedience.

Policy version 3 adds required quest author, observer, and delegated reviewer coverage. Engagement proposals are now always audit-only: Python validates and records suggestions but never derives an accounting mutation from model category/XP output. Narration and policy confer no authority. That version used a 24 KiB export bound; the current complete matrix uses 32 KiB. Existing Google Doc overrides must receive the complete current policy and an explicit operator reload. No external document is published by implementation. See [rollout and limits](ENGAGEMENT_QUESTS.md).

Policy version 4 updates nonparticipant kudos/chat access, selected kudos emoji attribution, joined-channel question routing, caller-specific rank/quest visibility, and honest shop/tool answers. Operators using a Google Doc override must update it to this complete policy and explicitly reload it; no external document was edited or published. Conversation prompt pairs are now version 2.

Policy version 5 specifies recipient-aware varied sender acknowledgments and receipts from `delivery.json` version 2. Validated mentions and overall/per-destination outcomes drive narration; pending or partial delivery cannot be presented as complete success, and Slack delivery does not establish reading. Deterministic receipt facts and once-only XP remain authoritative. Update any Google Doc override to the complete version-5 policy and reload explicitly.

Policy version 6 makes engagement proposals audit-only regardless of deployment flags, distinguishes proposed XP from applied delta zero, and permits retrying cancelled/failed explanatory notices only for the current active consent generation. Update any Google Doc override to the complete version-6 policy and reload explicitly. No external document was edited or published.

Policy version 7 restricts ambient question/progress inference to registered Ledger channels, blocks old ambient jobs in unrelated channels, and excludes unrelated stored history from addressed replies. Rejected specialized quest completions resubmit as new evidence attempts with corrected fields and fresh learner acknowledgments; pending retries retain their attempt and logical quest rewards remain once-only. Update any Google Doc override to the complete version-7 policy and reload explicitly. No external document was edited or published.

Policy version 8 clarifies logical quest scope across revisions, preserving the selected revision's shop-eligibility reference and checking current grantor authority against every operation's actual shops. Legacy revision scopes resolve without audit rewrites or grant revival. Character sheets with no metrics show a pending-import or empty state. Update any Google Doc override to the complete version-8 policy and reload explicitly; no external document was edited or published.

Policy version 9 requires invalid delayed observations to be cancelled individually before inference while valid batches continue. Quest completion submissions and reviews retain immutable attempt IDs and final history, with an acceptance pointer for the latest attempt and a separate logical marker for once-only rewards. Legacy remaining records are preserved. Update any Google Doc override to the complete version-9 policy and reload explicitly; no external document was edited or published.

Policy version 10 makes observation default to enabled for every eligible member in configured channels, including nonparticipants, after explanatory notice. `/ledger preferences` offers an independent opt-out without joining; choices survive updates and joining/rejoining. Game consent, rank visibility and accounting remain separate. `LEDGER_OBSERVATION=false` overrides the default deployment-wide. Update any Google Doc override to the complete version-10 policy and reload explicitly; no external document was edited or published.

Policy version 12 adds private staff review-channel notices, activity-owned Slack timestamps, in-place completion/closure updates and deleted-message replacement. Slack failures never confer or revoke review authority or change accounting. Expanded quest roles and delivery policy use a bounded 32 KiB matrix export. Existing Google Doc overrides require the complete current version-12 policy and explicit reload; implementation does not publish or edit external documents.

Policy version 11 adds `ledger_quest_author`, human review of individual/cooperative proposals, separate historical channel-inspiration purpose, anonymized temporary inputs and independent cooperative completion. The current version 13 combines that policy with review delivery updates. The service author has no publication, review, accounting or safety authority. Update the complete Google Doc override and explicitly reload; see [quest generation](QUEST_GENERATION.md).

Policy version 13 adds opt-in/source-role eligibility and per-caller administrative help, deterministic admin consent invitations with sender-name privacy, and automatic source-admin private review-channel membership on opt-in/opt-out. It preserves scoped staff/delegated review and independent human approval. Update any Google Doc override to the complete version-13 policy and explicitly reload; no external document was edited or published.

Policy version 14 adds complete sampled-author redaction across source channels/replies, quotation rejection across all proposal prose, and a privacy-version gate for unfinished legacy generation requests. It also requires notices for all eligible observation sources and retention/recovery of confirmed notice receipts through temporary eligibility changes. Update Google Doc overrides to the full version-14 policy and explicitly reload; no external document was edited or published.

Policy version 15 requires complete bounded Slack and makerspace member directories for quest-inspiration name redaction, including non-authors and inactive members. Failed, malformed or incomplete identity reads omit chat inspiration. Directory records never enter saved inputs or inference. Names absent from both directories may remain under the accepted directory-based policy; this is not exhaustive anonymization. Update any Google Doc override to the complete version-15 policy and explicitly reload it. No external document was edited or published.

Policy version 16 prohibits configured rank names throughout generated quest and discipline prose, with Python checks at generation, saved submission, publication edits and member access. It also specifies original contribution/submission times for completion activity metrics and revision-specific cooperative shutdown. Update any Google Doc override to the complete version-16 policy and explicitly reload it. Reserved deliveries and historical records retain their snapshots; no external document was edited or published.

Policy version 17 adds scoped cooperative review discovery, rejects copied retained completed-example prose across ranks during generation and saved submission, and limits notice reconciliation to pending, failed or channel-mismatched work while skipping settled activity writes. Update any Google Doc override to the complete version-17 policy and explicitly reload it. Existing reservations remain unchanged; no external document is edited or published automatically.

Policy version 18 puts workspace recognition emoji first in the kudos picker. Update any Google Doc override to the complete version-18 policy and explicitly reload it. Reserved delivery snapshots remain unchanged; no external document is edited automatically.

Policy version 19 documents consent-invitation discovery: the picker trims and tokenizes up to 150 search characters, matching every token as a case-insensitive literal substring of either first or last name, in any order. Valid Slack IDs also resolve linked members. Python reads at most 500 candidate members, batches identity and participation checks, and returns at most 100 choices. Already opted-in, merged, revoked or suspended members, ambiguous/invalid links, and known bots or deactivated identities are excluded. Incomplete identity reads return no choices; database errors return empty options under the callback's two-second database deadline. Search results do not establish consent or replace actor/recipient checks at submission and delivery.

Operators using a Google Doc override must copy the **complete version-19 canonical matrix** into their document and explicitly run `/ledger-admin reload-prompts`, checking the reported version, source and reload outcome. Updating repository code alone does not update an override. Existing reserved deliveries retain their original snapshots; no external document is edited or published automatically.

Policy version 21 makes humor optional, limits consolidated result narration to a brief sentence around Python-rendered facts, and documents caller-only requested guidance, the 60-second kudos cutoff, late edits, and separate giver/recipient outcomes. The Ledger role remains a narrator with no authority to award, authorize, or infer delivery. Short reservations save the full validated matrix/hash and exact ordered projection: `identity`, `authority`, `kudos`, `privacy`, `response` for receipts, or that subset without `kudos` for game results. The canonical XML retains all required roles and remains within 32 KiB. See [result summaries](RESULT_SUMMARIES.md).

Operators using Google Doc policy must adopt the **complete version-21 canonical matrix** and explicitly reload it. Published delivery/status template overrides require explicit library adoption separately. Existing reserved inputs and text remain unchanged; external documents are never silently edited or published.
