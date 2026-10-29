# Administration

Admin and board roles come from the existing member record (`admin`, `board_member`). Resource managers (`resource_manager`) can review evidence only in their `resource_manager_shop_ids`; they cannot configure ranks/templates, attest coverage, or correct ranks. Reviewers cannot approve themselves. Staff need valid Slack/member mappings but need not join gamification to moderate.

## Ranks

`/ledger-admin ranks` opens all seven stable slots. Edit names, Unicode or `:workspace_emoji:` values, XP floors, and milestone JSON; then inspect the rendered preview and publish. Floors must increase, names must be unique, and enabled slots must be consecutive. Slot seven needs all fields and nonempty milestone requirements to activate. Slack renders workspace shortcodes; ensure the chosen emoji exists in the workspace.

Supported requirements: `checkouts`, `shops`, `completed_shops`, `first_build`, `mentoring`, `learners`, `volunteer`, `boss`, `develop_mentor`, `stewardship`. Requirements accumulate across earlier slots; removing a later duplicate requirement cannot bypass an earlier gate.

Each publication writes an immutable progression version. First opt-in pins the current version; existing and returning members keep their previous floors/gates. Names and emoji are live presentation settings for all members, with historical labels retained in rank audit records. Edits do not run promotions or demotions. Channel mappings use `rank:1` through `rank:7` and survive renames. Newly activated slots queue private-channel creation.

- `/ledger-admin history` lists versions.
- `/ledger-admin rollback <version-id>` publishes a fresh version with that prior configuration. It does not migrate participants.
- `/ledger-admin correct-rank @member <slot> <reason>` records an independent correction and holds further automatic advancement pending review.
- `/ledger-admin release-rank @member <reason>` resolves that hold and reevaluates pinned requirements. Correct erroneous source evidence before release.

## Message templates

The shared [Prompt Matrix Template](PROMPT_MATRIX.md) governs personality, roles, game rules, and channel/DM behavior. Admins and board members may use `/ledger-admin reload-prompts` to request a fresh configured Google Doc across composing workers. The reply identifies this worker's source/version/hash and load outcome; other workers refresh before their next new composition. A failed refresh retains valid policy. This command changes narration policy only; it cannot change permissions, awards, or pinned progression rules. Resource managers cannot reload policy.

`/ledger-admin template <type> <audience>` edits a JSON array of paired system/user prompt variations, audience instructions, canned fallback, temperature, and token budget. Each variation has an ID, personality, and attitude. Preview renders every pair with example facts before publication. `/ledger-admin template-test <type> <audience>` queues a live generation preview in the administrator's DM, reporting which variation was selected.

The packaged [prompt library](PROMPTS.md) supplies one JSON file per type, with three default voices plus two System variants in five completion types. It supports named substitutions such as `{member_full_name}`, `{member_slack_id}`, `{old_rank}`, `{new_rank}`, and `{highest_skill}`; variations can omit details. The bot avoids the last two variation IDs before randomly selecting a paired prompt set. Channel posts share one history; each DM recipient has their own. Keep variation IDs stable when editing voices. Publishing or rolling back a template does not reset history, and administrative previews do not consume it. The choice and text are saved for retries; custom sets with fewer alternatives relax the oldest exclusion first.

Use `/ledger-admin template-library <type> <audience>` to preview and publish a deployed JSON file over an existing database override. The editor splits larger sets into separate paired-variation JSON inputs. File edits require an application rebuild/restart; existing database overrides stay active until explicitly replaced. See [authoring, variables, and publication](PROMPTS.md) for the full workflow.

Audiences: `member`, `shared`, `recipient`, `nonparticipant`. Types include `onboarding`, `return`, `opt_out`, `invitation`, `checkout_earned`, `checkout_granted`, `volunteer_credit`, `kudos`, `recruitment`, `rank_up`, `shop_complete`, `boss`, `stewardship`, `challenge`, `first_build`, `develop_mentor`, `mentoring`, `correction`, `conversation`, `status`, `delivery`, `project`, `quest`.

Use `/ledger-admin template-history` and `/ledger-admin template-rollback <template-id>` for audit and rollback. Each rollback publishes a new immutable version. Queued messages reuse their already composed text. Consent, labels, canonical facts, attribution, and the original kudos body remain controlled by the application.

For a cultivation-style rank-up variation, its `user` prompt could be:

```text
{audience_instruction}
Write a warm two-line System announcement for {member_full_name} reaching {new_rank}.
You may mention their recorded skill {highest_skill}. Omit unavailable details and the old rank.
Do not invent facts or tool clearances; authoritative details follow separately.
```

## Catalogs and human verification

`/ledger-admin catalog` accepts a new immutable JSON definition. IDs must be unique. Review actual shop/tool IDs using `ledger dry-run` and the existing read-only Mongo catalog. For a full-shop milestone, the service snapshots enabled tools requiring a checkout:

```json
{"_id":"wood-v1","kind":"shop_completion","shop_id":"REVIEWED_SHOP_OBJECT_ID"}
```

For a Boss Fight, choose an existing volunteer opportunity and preapprove concrete acceptance criteria:

```json
{"_id":"class-design-v1","kind":"challenge","achievement":"boss","title":"Design and teach a new class","task_id":"REVIEWED_VOLUNTEER_TASK_ID","shop_id":"REVIEWED_SHOP_ID","criteria":"Publish safe lesson materials, deliver the class, collect learner feedback, and document revisions."}
```

Other achievement values are `first_build`, `challenge`, `mentoring`, `develop_mentor`, and `stewardship`. Stewardship also requires an existing volunteer task and a usable handoff in the submitted evidence. Use a new catalog ID for revisions. Seeded First Build and self-directed challenges are small, accessible options; shop safety rules always apply.

Members submit with `/ledger-quests submit <catalog-id>` or `/ledger-mentor log`. Non-checkout mentoring sends acknowledgment buttons to listed learners. The review queue is restricted to the reviewer's scope.

- `/ledger-admin review`
- `/ledger-admin approve <submission-id>`
- `/ledger-admin reject <submission-id> <reason>` (also compensates a previously approved award)
- `/ledger-admin quest` creates a cooperative quest with disciplines, acceptance criteria, and a real volunteer task.
- `/ledger-admin verify-quest <quest-id> @member` independently accepts one submitted contribution; the complete group must cover all predefined disciplines.
- `/ledger-admin coverage @member <reason>` attests ambiguous prepaid/legacy coverage against the current expiration, with no self-attestation.

The developing-mentor milestone requires approved guidance that includes the new mentor as a learner, followed by that person's teaching session approved by someone other than the original mentor or the new mentor. A workshop counts as one session regardless of learner count.

## Pilot and recovery controls

`/ledger-admin metrics` returns aggregate learning, mentoring, collaboration, feedback, notification, delivery-failure, and AI-fallback counts. Review feedback records through an authorized operator's Mongo read. `/ledger-admin reconcile` queues reconciliation.

Admins and board members use `/ledger-admin ticket-quests` to configure damaged/broken-ticket verification quests: enablement, announcement channel, Fix Tickets channel, quest duration, and first-claim XP with/without a JPEG. Defaults are 18 hours, 66 XP without an image, and 100 XP with one. A JPEG claimant reserves first place before verification, including the claimant's consent generation, Slack identity, and owning inbox job; transient image and workspace authentication/scope failures retry before XP is decided. If the claimant opts out and rejoins, their Slack/member mapping changes or disappears, or the owner job reaches terminal failure, reconciliation releases the stale reservation without awarding XP. Permanently unavailable or invalid images, and checks still failing after the retry budget, use the no-image award. JPEGs rejected by Pillow's decompression-bomb limit invalidate the claim, award no XP, release its reservation, and receive a brief Ledger-voice rejection. Once an image passes validation, Qwen generates a reusable caption; Slack reposts it as image alt text and the source ticket note appends it. New quest discovery is off by default; disabling it leaves already announced quests to finish normally. Unanswered quests close if the ticket is deleted, becomes terminal, expires, or leaves the eligible damaged/broken status. Optional Drive upload uses `LEDGER_TICKET_GOOGLE_ACCESS_TOKEN` and `LEDGER_TICKET_GOOGLE_FOLDER_ID` from deployment secrets; keep this upload token separate from the Prompt Matrix read token. Optional Rails note persistence uses the additional `gamification_ticket_note_writer` grants described in [MongoDB access](MONGODB_ACCESS.md). Without those grants, Ledger and Slack quest records still work.

`/ledger-admin pause` stops joining, game interactions, accounting, and ordinary delivery while keeping opt-out and channel cleanup available. Ordinary queued notifications encountered while paused are cancelled to avoid a stale announcement burst. Records remain intact. `/ledger-admin resume` reopens processing; use a deliberate reconciliation and review after repair. Use a previous application image to roll back code, while leaving a compatible ingress/channel worker available for opt-out cleanup.

## Delegated reviews and member quests

Use `/ledger-admin delegates` for explicit capability/scope grants, inspection, and reasoned revocation. Delegates can access authorized pending review actions without a staff role; configuration and finalized corrections remain staff-only. `/ledger-admin review` includes member quest publication and completion queues. `/ledger-admin publish-quest <revision>` / `reject-quest <revision>` open independent reward review. See [complete authority and lifecycle rules](ENGAGEMENT_QUESTS.md).

Quest scope accepts any revision ID and stores the quest's stable logical ID. The selected revision records the relevant shops, and reviews of later revisions still require those reviews' shops to be within the grantor's current scope. Legacy grants containing a revision ID resolve to the same logical quest without replacing their audit history.
