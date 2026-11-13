# Participant avatars

Personalized fantasy avatars default on for active participants. `/ledger
preferences` offers an opt-out, one owned JPEG/PNG/WebP reference strictly below
2,000,000 bytes, and reference removal. Opt-out restores the rank image and
cancels generation/notices, tombstones the current pair, and deletes its bot-owned
files after the default Home is confirmed. Preferences survive leaving/rejoining. An uploaded
reference replaces the Slack photo; its original user-owned file is retained.
The first confirmed avatar DM explains the opt-out location.

## Generation and privacy

Joining, promotion, downward rank correction, newly recorded shop completion, and independently verified
individual/cooperative/catalog quest completions reserve jobs in the existing
Mongo transactions. Duplicate events do not reserve duplicate jobs. Pending
requests coalesce for sixty seconds; milestones during generation reserve a
follow-up. Join jobs wait for history import. The avatar worker reserves a New
York calendar-day backfill on startup and checks for a new day at most once every
five minutes while its loop is available. Reservation checks are throttled even
after database failures, in both avatar-only and combined workers. Backfill
pages at most 100 participants, skips active jobs/opt-outs, and repairs missing,
deleted or wrong-rank avatars.

Context contains rank, saved objective image description, configured Slack
bio/interests, up to forty tool names, ten active accepted and ten recent
completed quests, and twelve own retained messages of at most 600 characters.
Observed posts require the current original text, configured channel, observation
notice and generation. Observation opt-out excludes channel posts; own retained
messages to The Ledger may still personalize enabled avatars. Other authors,
bot replies, expired/deleted/prior-consent messages and sensitive credential,
billing, access-code or internal-note text are excluded. The deployed narrator
tokenizer fits the prompt. Successful composition removes raw chat and
composition messages from the job, retaining the resulting visual prompt.

Dominant shop means the most distinct currently non-revoked, available tool
clearances, with latest checkout then shop ID as tie-breakers. These are checkout
clearance records, not equipment-use frequency. Models grant no XP, safety
clearance, staff role or accounting authority.

Four reference slots are identity (custom reference or Slack photo), prior
character continuity, bundled current-rank style, and shop. Missing optional
images are omitted. Unavailable custom references never substitute the profile
photo. References are normalized to bounded PNGs. The default shop reference is a
local neutral workshop drawing; operators can mount `<shop-id>.png` assets in
`LEDGER_AVATAR_SHOP_ASSET_DIR` on the avatar worker. The shop ID is the source
`shops` document's `_id`. If that file is missing, the worker tries the actual
shop name in lowercase with only letters and digits retained, followed by
`.png`: for example, `Wood Shop & CNC` becomes `woodshopcnc.png`. The ID file
takes precedence. If neither file exists or the normalized name is empty, the
worker uses the neutral workshop drawing.

The runtime generates one 1280-square PNG, forty diffusion steps, true CFG 1.0,
and a reserved seed. Pillow normalizes orientation, flattens transparency,
strips metadata and exports RGB JPGs at exactly 1254-square and 512-square,
quality 95. Both confirmed Slack uploads must exist before atomic pair
activation. Failure preserves the old pair. Files are shared only with the
participant's Ledger DM. Slack may show upload shares before the final composed
notification; that notification follows activation and displays the 512 JPG
with a link to the full-resolution file.
Confirmed upload receipts survive an opt-out during upload and reserve cleanup.
Cleanup waits for the generation to finish or cancel, and protects current files.
Failed candidates that never activated bypass Home replacement confirmation;
activated candidates still require it, including their upload-receipt cleanup.
Activated jobs never regenerate when their original worker lease is reclaimed.

Home and character-sheet rendering resolve the current avatar from Mongo. Home
snapshots include avatar revision; normal Home opens detect stale revisions.
Missing, wrong-rank or opted-out avatars use the rank artwork. After confirmed
Home replacement, retryable cleanup deletes superseded bot-generated files and
temporary generation artifacts, never the current pair or user-owned reference.
Old DM links disappear after obsolete files are deleted.
Runtime receipt deletion uses a separate durable `avatar_runtime_ack` delivery
job. It retries with capped backoff until the supervisor recovers, independently
of Slack and local-file cleanup, without consuming their retry budget. Keep the
runtime URL/key configured until pending acknowledgment jobs have drained.
When forced regeneration cancels a working candidate, its cleanup waits until
the supervisor reports that request is no longer working. Supervisor outages
defer this cleanup without spending retries. A `missing` receipt also defers
cleanup until the pipeline records completion for the cancelled job's current
lease: the request may still be on its way to the supervisor. Cancellation,
lease expiry and a previous attempt's completion do not establish that the
current attempt has exited. If a worker crashes without recording completion,
this cleanup remains deferred until operator recovery establishes that the old
worker cannot dispatch another request. The acknowledgment endpoint also
refuses deletion while inference holds the runtime lock, so a late receipt
cannot outlive a prematurely completed acknowledgment.

## Storage and metrics

`ledger_avatars` is Ledger-owned and uses `LEDGER_URI`:

| ID prefix | Contents |
| --- | --- |
| `current:<member>` | Rank/revision, username, exact Qwen-image prompt, `avatar`/`avatar512` Slack references, sizes/checksums, token usage, seconds and UTC `job_end_time` |
| `job:<job-id>` | Frozen policy/prompt selection, sanitized context, reference checksums, model/settings/seed, upload stages and bounded attempt metrics |
| `reference:<member>` | User-owned file ID, validation checksum, objective description; removal creates a tombstone |
| `state:<member>`, `trigger:<hash>` | Sequence and deterministic milestone deduplication |
| `notice:<member>` | Confirmed latest DM receipt and first-notification timestamp |

Jobs remain in `ledger_outbox`; the global runtime lease uses `ledger_catalog`.
The role examples explicitly grant find/insert/update/createIndex on
`ledger_avatars`. No new source write or wildcard privilege is introduced.
Existing Slack scopes cover profile reads, file upload/deletion and DMs.

Each attempt prints structured JSON to STDOUT with job ID, Slack username,
outcome, duration in seconds, UTC end time and token usage, and stores the same
metrics in Mongo. Narrator usage is request-local input/output/total tokens.
Diffusion has no ordinary output-token count: provider usage, encoder counts and
diffusion metrics are separate; unavailable counts are null, never fabricated.
Attempt duration includes loading/uploads, excludes queue delay and subsequent
Home/DM delivery. Successful `job_end_time` denotes activation; terminal failed
attempts also have end times.

## Deployment and lifecycle

The optional `avatars` Compose profile adds `ledger-avatars` and `ledger-image`.
The narrator stays running. Ordinary delivery handles notices/cleanup and shares
the avatar spool volume. No Docker socket or public inference route is added.

Deployment information checked on October 9, 2026. Qwen-Image-2.1 support landed
in [vLLM-Omni PR #8099](https://github.com/vllm-project/vllm-omni/pull/8099) on
October 7, superseding the closed PR #7759. The
[official serving recipe](https://recipes.vllm.ai/Qwen/Qwen-Image-2.1) still
contains older prerequisites; check the implementation in the chosen image.

1. Set `VLLM_OMNI_IMAGE` in `.env`. A candidate for Linux ARM64/GB10 testing is
   `vllm/vllm-omni:qwen-image21-arm64-cu129`. Docker Hub metadata confirms Linux
   ARM64 and the digest below; generation on the target GB10 still needs testing.
   [Image tag](https://hub.docker.com/r/vllm/vllm-omni/tags?name=qwen-image21-arm64-cu129).

   ```dotenv
   VLLM_OMNI_IMAGE=vllm/vllm-omni@sha256:0903b0620d64d03de6acae489bc12dafd89006892f005270b69f2b32d9ea8661
   ```

   An [immutable digest](https://docs.docker.com/dhi/explore/security-concepts/digests/)
   fixes the exact image contents; tags can move. Retain the tested digest for
   production. The example `latest` value in `.env.example` and Compose is a
   discovery placeholder. To inspect the tag before choosing a newer digest:

   ```bash
   docker buildx imagetools inspect vllm/vllm-omni:qwen-image21-arm64-cu129
   ```

2. Build the supervisor image on the deployment host:

   ```bash
   docker compose --profile avatars build ledger-image
   ```

   `Dockerfile.image` checks the `QwenImage21Pipeline` import. A successful build
   establishes that the implementation imports; it does not validate GPU kernels,
   memory capacity or multiple-reference generation.
3. Set a separate `LEDGER_AVATAR_RUNTIME_KEY` of at least 24 characters. Configure
   Slack custom-profile IDs in `LEDGER_AVATAR_BIO_FIELD_ID` and
   `LEDGER_AVATAR_INTERESTS_FIELD_ID`; unset fields are omitted. Set
   `LEDGER_AVATAR_CONTEXT_LIMIT` to the narrator window (default 8192).

   `LEDGER_AVATAR_RUNTIME_COMMAND` optionally accepts JSON argv, never shell
   code. Default: `vllm serve Qwen/Qwen-Image-2.1 --omni --host 127.0.0.1 --port
   8091`. Overrides must preserve that loopback API address. Test any offload or
   quantization flags on the selected runtime first.

   Set `LEDGER_AVATAR_MODEL_REVISION` to the actual checkpoint revision for audit
   metadata. This setting does not change the checkpoint loaded by the default
   command. To freeze every model component, mount a pinned local snapshot and
   use its path in the runtime command; a serving revision flag may not reach
   every component loader.
4. Optionally preload the model weights before enabling workers. Docker stores
   the software image separately; the Hugging Face cache stores model weights.
   Without preloading, the model loader downloads missing weights when the first
   generation loads the model. After the image build, this command downloads the
   current `Qwen/Qwen-Image-2.1` snapshot into the service's persistent cache:

   ```bash
   docker compose --profile avatars run --rm --no-deps \
     --entrypoint python3 ledger-image \
     -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen-Image-2.1')"
   ```

   For a pinned checkpoint, pass its full commit hash as `revision` and configure
   the runtime to load that snapshot. See the
   [Hugging Face download guide](https://huggingface.co/docs/huggingface_hub/main/guides/download).
   Compose mounts the named `huggingface-cache` volume at
   `/root/.cache/huggingface` for both narrator and image services. A model already
   in the host's `~/.cache/huggingface` is not automatically visible in this volume;
   using the host cache requires an explicit bind mount for both services.
5. Apply the updated Mongo role and run `ledger init`. Verify generation with one
   to four reference images on the target GPU alongside the narrator before
   enabling avatar workers. Start the services with:

   ```bash
   docker compose --profile avatars up -d --build ledger-image ledger-avatars ledger-delivery
   ```

   When upgrading the supervisor protocol, deploy the application workers and
   `ledger-image` together. Preserve the cache and spool volumes across restarts.

The authenticated supervisor provides `/generate`, `/status`, `/unload`, `/ack`
and `/health`. Disk receipts let a repeated request ID with identical inputs
recover its generated image after a lost response. Changed inputs with the same
ID are rejected. Its lock rejects concurrent generation, unload or receipt
acknowledgment with HTTP 409. Acknowledgment retries durably until inference has
stopped, preventing a late receipt from surviving a prematurely completed `/ack`.
GPU process logs are suppressed to keep prompts/images out of logs. Failure or
timeout terminates the entire process group; Compose init reaps descendants.

The worker renews its job/global leases every thirty seconds. Lease expiry or a
lost HTTP response never authorizes overlapping GPU work: the supervisor remains
single-flight until completion/termination. When no runnable jobs remain, the
worker requests unloading. Delayed retries do not keep the model loaded. An
independent supervisor watchdog unloads after 120 idle seconds if a worker
disconnects; SIGTERM also unloads the GPU subprocess.

For another GPU/host, run the same supervisor there and configure
`LEDGER_AVATAR_RUNTIME_URL` and matching key in the application environment.
Use a private network, HTTPS across untrusted networks, and host/Compose GPU
selection. All workers for one Ledger database must use the **same logical
supervisor**. Drain/stop the old supervisor before switching hosts; never deploy
independent simultaneous supervisors. OOM preserves the old avatar and retries;
move the image service to another GPU rather than automatically unloading the
narrator.

Prompt Matrix version 49 and the paired `avatar.json` set cover avatar
policy. XML schema/required roles remain unchanged. Google Doc overrides require
the same policy update and explicit reload; no external document is edited or
published, and existing reserved jobs retain their policy snapshot.

Run `python -m ledger.prompt_matrix`, `python -m ledger.prompt_library` and
`python -m pytest`. Deployment checks must exercise real multiple-reference
generation, narrator coexistence, sequential inference, OOM recovery, GPU memory
release, both JPG sizes, participant file access, first-DM opt-out text, Home
replacement, reference validation and deletion. Those checks need deployment
hardware and Slack credentials; automated tests do not claim to perform them.

Administrators and board members who are opted in can use `/ledger-admin avatar @member` to inspect a participant’s current avatar, saved visual prompt, reference manifest, model/settings, token usage, duration, completion time and attempt metrics. Raw conversations, policy snapshots and credentials are excluded. The force-generate/regenerate button invalidates queued or running candidates and reserves an immediate replacement on the sequential avatar queue, keeping the current pair until successful activation. Repeated clicks on the same button reserve only one replacement. Custom-avatar opt-out is honored at inspection, selection, enqueue and delivery. `/ledger-admin avatar` opens a participant picker filtered to eligible opted-in participants with personalized avatars enabled. Submission opens the selected participant’s avatar and saved generation details; only the optional force-generate/regenerate button queues generation. No rank, XP or consent is changed.


## Routing repair and recovery

All worker lanes must run the routing fix together: Mongo claims intersect the
lane's allowed kinds with excluded kinds (including anomaly jobs). Do not leave
old results or interactive workers running while retrying avatar jobs.

On the deployment host, retain the same Compose project and environment:

```bash
export COMPOSE_FILE=compose.yaml:compose.anomaly.yaml
export COMPOSE_PROFILES=avatars
bash scripts/rebuild.sh
docker compose ps --all
```

The rebuild builds and preflights before stopping services, preserves named
volumes, and recreates all enabled workers. `ledger init` is not a worker-start
command. Avatar prompt composition uses `LEDGER_AVATAR_PROMPT_TIMEOUT_SECONDS`
(default 60, finite and positive); ordinary chat deadlines stay unchanged.

Before manually retrying failed jobs, inventory their reserved inputs and
receipts from the avatar worker's shared spool. This read-only command prints
identifiers and presence flags, never prompts, reference bytes or credentials:

```bash
docker compose exec -T ledger-avatars python - <<'PY'
import hashlib, json, os
from pathlib import Path
from ledger.storage import connect
store = connect(os.environ['LEDGER_URI'], os.environ.get('LEDGER_DATABASE'))
root = Path(os.environ.get('LEDGER_AVATAR_SPOOL', '/var/lib/ledger-avatars'))
for job in store.select('ledger_outbox', {'kind': 'avatar_generate',
                                        'status': {'$in': ['pending', 'working', 'failed']}}):
    row = store.get('ledger_avatars', 'job:' + job['_id']) or {}
    prefix = root / hashlib.sha256(job['_id'].encode()).hexdigest()
    print(json.dumps({'job_id': job['_id'], 'status': job['status'],
        'last_error': job.get('last_error'), 'activated': row.get('status') == 'activated',
        'reserved': bool(row.get('selection')), 'prompt_saved': bool(row.get('prompt')),
        'manifest_present': Path(str(prefix) + '.refs.json').is_file(),
        'images_present': {n: Path(str(prefix) + '.' + n + '.jpg').is_file()
                           for n in ('avatar', 'avatar512')},
        'uploads_confirmed': {n: bool(row.get(n)) for n in ('avatar', 'avatar512')}}))
PY
```

Pending jobs with intact reserved inputs retry automatically. Completed uploads
and activated avatars must remain intact. A missing manifest only blocks a job
that still needs image generation; confirmed uploads can finish activation
without the local images. `AvatarReferenceUnavailable` means restore the exact
original manifest from the original worker/spool or use `/ledger-admin avatar
@member` and its explicit force-regenerate button. That workflow rechecks current
consent and staff authorization and reserves a new generation. Do not substitute
new references into an old reservation or bulk-reset attempts on missing-input
jobs. Terminal failed jobs do not retry automatically; use the explicit
regeneration workflow unless an operator has verified and restored all original
inputs for a controlled retry. Leave runtime acknowledgement jobs on their
existing durable retry path.

For a live verification, follow the relevant workers, then request one authorized
regeneration and track its job ID:

```bash
docker compose logs --follow --since=5m ledger-avatars ledger-results ledger-interactive ledger-delivery ledger-image
```

Generation must run only in `ledger-avatars`. `avatar_phase` logs identify
`reference_preparation`, `prompt_composition`, `image_request`, `upload` and
`acknowledgement`, with outcome, exception class and elapsed milliseconds. An
`image_request` success confirms a runtime response; upload receipts and activated
status confirm completion. Acknowledgements run in delivery. The image supervisor
may otherwise be quiet. Logs exclude provider exception messages, prompts,
credentials and image bytes. Verify this on the host with live Slack/inference;
local tests cannot establish deployment connectivity.


### Image runtime HTTP failures

`AvatarRuntimeHTTPError code=503` means the supervisor reported a failure; it does
not identify GPU exhaustion by itself. Inspect `ledger-image` logs for
`avatar_runtime phase=model_load` versus `phase=inference`. Load failures report
the subprocess exit code before cleanup; inference failures report the upstream
HTTP status. Neither includes exception messages, prompts, command arguments or
image data. A signal-like exit code alone is not proof of an OOM event.

Rebuild/recreate both `ledger-image` and `ledger-avatars` to install these
diagnostics. Preserve the image and avatar spool volumes and existing job IDs;
pending jobs keep their normal retry behavior. Use
`docker compose --profile avatars logs --since=15m ledger-image ledger-avatars`
with the same Compose files/project used at deployment. If loading fails, check
GPU memory on the host (`nvidia-smi`) and the configured image/command. Do not
increase the prompt timeout for an image-stage failure: prompt composition has
already completed. These diagnostics localize a failure; a live retry is still
needed to establish its cause.
