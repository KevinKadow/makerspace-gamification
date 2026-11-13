# Recruitment and opt-in

The Ledger is opt-in. A sponsorship invitation is only an invitation to learn about The Ledger; it does not create an account, grant channel access, or award XP. The recipient must explicitly choose **Opt in to The Ledger** or run `/ledger join`. Invitation history and the first sponsor's recruitment credit are separate from consent. Recruitment XP is awarded only after the invitee opts in and completes a qualifying, verified learning or service milestone.

Operators may set `SLACKIDS_INELIGIBLE` in `.env` to a quoted, comma-separated set of Slack user IDs, for example `'{"U0123456789","U0345678901"}'`. Restart Ledger services to load changes, then run `/ledger-admin reconcile` to remove any listed identities who already belong to managed channels. Listed identities cannot opt in or receive sponsorship/admin invitations. The Ledger sends them no unsolicited or command-response DMs, and silently ignores their Slack commands except `/kudos`. They remain eligible to send or receive ordinary kudos. If a sender chooses to invite one of these recipients in the kudos form, kudos still delivers but no invitation is created and no message reveals the exclusion.

## Invite a maker

An opted-in participant can invite an eligible linked member with `/ledger sponsor @member`. Sending kudos with **Send kudos and invite them to The Ledger** also records an invitation. The `/kudos` message remains the sender's authored text; the invitation does not enroll the recipient.

Use `/ledger sponsor` with no options to view your own invitations, newest first, with each invitee's invitation date, current opt-in state, and latest opt-in/out dates. The report is private and does not disclose another sponsor's history.

When an invitation DM is delivered, a participant with a reminder from the previous seven days receives an updated reminder DM thanking them for growing The Ledger. When their invitee accepts, that same recent reminder is updated with a Qwen-generated thank-you naming the new participant and suggesting a personal welcome in Ledge Chat. Acceptance takes precedence over delayed invitation-sent updates. Reminder receipts are bound to the Slack identity and consent generation that received them; a relink opens a new DM instead of editing the former account's channel. The reminder timestamp and original sent date remain attached to the reminder. These follow-ups do not create additional XP.

## Operator reminders

Run the one-shot reminder script from the application environment:

```powershell
python -m ledger.sponsorship_reminder --days never
python -m ledger.sponsorship_reminder --days 30 --limit 25 --verbose
```

`--days never` selects opted-in participants with no recorded invitations. `--days N` selects participants who have invited before but have no invitation in the last N days. N must be a positive integer. Candidates must have a valid linked human Slack identity. A new DM is sent if there is no reminder or its sent date is at least seven days old; a more recent reminder is updated in place and keeps its original sent date.

Useful options:

- `--variants path.json` selects a JSON variants file. The default sample is `ledger/sponsorship_reminder_variants.json`; it includes never-inviter reminders, previous-inviter reminders, and accepted-invitation thank-you prompts. Initial reminders use the composer's saved prompt selection and never repeat the same variant index twice consecutively for one member; the selected index and timestamp are saved after delivery. The accepted-invitation template is saved with the participant's latest reminder receipt so the follow-up uses that run's selected file.
- `--dry-run` (also `--dryrun`) lists eligible members, planned send/update actions, and the prompt intended for Qwen without sending messages or writing to MongoDB.
- `--dry-run --generate` generates and displays proposed text instead of the prompt, still without sending or writing.
- `--limit N` caps the number of participants processed; `--member ID` limits the run to one internal member ID or linked Slack user ID.
- `--verbose` and `--debug` write Mongo errors, Slack errors, and Slack 429 backoff notices to STDERR. The live-run totals on STDOUT report new messages sent, reminders updated, failed message operations, and Mongo documents updated.

The script uses `MLAB_URI` only for source reads and `LEDGER_URI` for owned collection reads/writes, along with the configured Slack bot and Qwen endpoints. It never reads or writes source collections. Review the dry-run output before a first live run.

`--verbose` and `--debug` show Mongo and Slack error types and 429 retry delays on STDERR; the final STDOUT summary counts sent messages, in-place updates, failed message operations, and participant documents updated. During a maintenance pause, a live run prints one warning and stops before the next Slack API operation. Dry runs ignore the pause and never send or write.

The canonical recruitment narration policy is in the [Prompt Matrix Template](../ledger/prompts/prompt_matrix.xml.md). If the deployment uses a Google Doc override, copy the corresponding policy update there and explicitly reload it with `/ledger-admin reload-prompts`; the script does not edit or publish that document.
