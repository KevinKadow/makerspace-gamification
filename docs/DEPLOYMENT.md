# GB10 inference and Cloudflare Tunnel

The supplied [compose.yaml](../compose.yaml) runs nine services: web ingress, six independent workers, vLLM, and cloudflared. Results and interactive workers isolate consolidated DMs and conversations/guidance from routine delivery. Existing Mongo and MQTT remain external dependencies. All commands below run from the repository root on the deployment host.

## Inference host and configuration

Run the complete stack on a Linux ARM64 NVIDIA DGX Spark/GB10 host with Docker Compose v2 and NVIDIA Container Toolkit configured for GPU containers. The [vllm-gb10 project](https://github.com/timothystewart6/vllm-gb10) publishes `ghcr.io/timothystewart6/vllm-gb10`; its image requires an explicit `vllm serve` command. An ordinary Windows/x86 Docker installation does not provide the required GB10 runtime.

Compose serves [nvidia/Qwen3.8-27B-NVFP4](https://huggingface.co/nvidia/Qwen3.8-27B-NVFP4). The model card's example targets multiple GPUs. This deployment instead starts with one GB10 GPU, an 8,192-token context, four concurrent sequences, FP8 KV cache, and 0.7 GPU memory utilization. These are conservative pilot settings, not a measured performance claim. Verify model loading and generation on the target machine; the model's mixed NVFP4/FP8 support depends on the selected image build. Adjust `VLLM_MAX_MODEL_LEN`, `VLLM_MAX_NUM_SEQS`, and `VLLM_GPU_MEMORY_UTILIZATION` after observing memory and response times.

Copy `.env.example` to `.env` and supply:

| Variable | Deployment value |
| --- | --- |
| `MLAB_URI`, `LEDGER_URI` | Reachable Mongo URIs with separate [restricted database credentials](MONGODB_ACCESS.md) |
| `SLACK_*` | Bot token, signing secret, workspace ID, and bot user ID from the installed app |
| `MQTT_*` | Reachable existing broker and its credentials/TLS setting |
| `LEDGER_WEB_PORT` | Host loopback port for the web service; defaults to `3000`, or choose a free port such as `3001` |
| `LEDGER_LLM_BASE_URL` | `http://ledger-ai:8000/v1` for this Compose stack |
| `LEDGER_PROMPT_MATRIX_DOC_URL` | Optional normal Google Doc URL containing the complete [XML/Markdown prompt matrix](PROMPT_MATRIX.md) |
| `LEDGER_PROMPT_MATRIX_GOOGLE_ACCESS_TOKEN` | Optional OAuth bearer token for a private Doc; operators manage renewal |
| `LEDGER_LLM_MODEL` | `nvidia/Qwen3.8-27B-NVFP4`; overrides rename the served API alias, not the model downloaded |
| `LEDGER_LLM_API_KEY` | A strong shared secret; Compose supplies it to vLLM as `VLLM_API_KEY` and the bot as its bearer key |
| `HF_TOKEN` | Optional Hugging Face token for model downloads, separate from the inference key |
| `VLLM_IMAGE`, `CLOUDFLARED_IMAGE` | Defaults use upstream `latest`; pin reviewed release tags/digests after a successful pilot |
| `CLOUDFLARE_TUNNEL_TOKEN` | Token for the remotely managed tunnel described below |

Compose requires nonblank inference and tunnel secrets. It does not invent, print, or provision them. Use deployment secret management for production. The `huggingface-cache` named volume persists model downloads; initial download, kernel compilation, and model loading can take a while. The health check allows twenty minutes for startup. Workers deliberately do not depend on AI health, so fallback delivery and opt-out cleanup remain available throughout.

`localhost` in `.env` refers to each container. Replace the local development Mongo/MQTT examples with addresses reachable from Docker. For a separate Python bot host, use the reachable vLLM `/v1` address and the same API key. Compose publishes no vLLM host port; exposing one for an external bot is a deliberate deployment override, kept off the Cloudflare Slack hostname.

If another application uses host port 3000, set `LEDGER_WEB_PORT=3001` in `.env`. Apply a port change with `docker compose up -d --no-deps ledger-web`, then access the service at `http://127.0.0.1:3001`. The container listens on port 3000, so its health check and the Cloudflare Tunnel origin continue to use port 3000.

## Cloudflare public hostname

Create a remotely managed Cloudflare Tunnel and put its connector token in `CLOUDFLARE_TUNNEL_TOKEN`. Compose passes it via `TUNNEL_TOKEN` to `cloudflare/cloudflared` running `tunnel --no-autoupdate run`. The token identifies the tunnel; hostname routing is configured in Cloudflare, not in the token or Compose. See [tunnel setup](https://developers.cloudflare.com/tunnel/get-started/) and [token environment parameters](https://developers.cloudflare.com/tunnel/reference/run-parameters/).

Configure a published application route:

| Field | Value |
| --- | --- |
| Public hostname | Your dedicated hostname, for example `ledger.example.org` |
| Origin service type | HTTP |
| Origin URL | `ledger-web:3000` (full service URL: `http://ledger-web:3000`) |
| Path | Leave blank to forward the hostname's paths unchanged |

Use this hostname in every URL in [slack-manifest.json](../slack-manifest.json), replacing `LEDGER_HOST`. The three callback URLs are `/slack/events`, `/slack/commands`, and `/slack/interactions`. Cloudflare terminates public HTTPS; the tunnel reaches Gunicorn on the Compose network. No inbound router port forwarding is needed. The bot's configured host port (`LEDGER_WEB_PORT`, default 3000) is bound only to host loopback for local health checks.

Slack callbacks must reach those paths without a Cloudflare Access login, JavaScript challenge, redirect, cache response, or body rewrite. Scope any necessary Cloudflare exceptions to the dedicated callback paths. Slack signature/timestamp verification remains enabled in Bolt and is the callback authentication boundary. Keep normal outbound connectivity for the tunnel, Slack, Mongo, MQTT, and model downloads. Never point this hostname at `ledger-ai`.

## Start and verify

Create the Slack app using the [setup guide](SLACK.md) to obtain its credentials; complete request-URL verification after the web service and tunnel are reachable. Deploy the ChangeStream2MQTT exclusion first, as described in [operations](OPERATIONS.md).

```bash
docker compose config --quiet
docker compose build
docker compose up -d ledger-ai
docker compose run --rm --no-deps ledger-accounting ledger init
docker compose run --rm --no-deps ledger-accounting ledger dry-run
docker compose up -d ledger-web cloudflared
# Complete Slack request-URL verification, then create/bind the private channels.
docker compose run --rm --no-deps ledger-accounting ledger bootstrap
docker compose up -d ledger-accounting ledger-delivery ledger-channels ledger-engagement ledger-results ledger-interactive
docker compose ps
curl --fail "http://$(docker compose port ledger-web 3000)/ready"
```

Review `docker compose logs ledger-ai` for model startup failures and `docker compose logs cloudflared` for tunnel connectivity. `/health` checks the web process; `/ready` also checks both Mongo connections. Neither proves GPU generation or Slack delivery. After `ledger-ai` is healthy, test a real completion from the delivery container (this uses the configured bearer key without printing it):

```bash
docker compose exec -T ledger-delivery python - <<'PY'
import os
from ledger.messages import ChatAPI
api = ChatAPI(os.environ['LEDGER_LLM_BASE_URL'], os.environ['LEDGER_LLM_MODEL'], os.environ['LEDGER_LLM_API_KEY'])
print(api.complete([
    {'role': 'system', 'content': 'You are The Ledger. Reply in one short sentence.'},
    {'role': 'user', 'content': 'Welcome a maker who just completed their first build.'},
], max_tokens=128, deadline=10))
PY
```

The adapter sends `chat_template_kwargs: {"enable_thinking": false}` with every request, matching the model's supported chat template. Server defaults also disable thinking. `--generation-config vllm` avoids model-repository sampling defaults overriding the service configuration; per-template temperature/token limits still apply. The [vLLM serving guide](https://docs.vllm.ai/en/latest/serving/online_serving/) and [reasoning guide](https://docs.vllm.ai/en/latest/features/reasoning_outputs/) describe these API options.

Finally exercise a DM, all commands, modal dropdowns/submissions, App Home, a public kudos, private-channel invitations/removals, and a file-backed skill tree in Slack. Pilot the 60-second pending receipt, later edits, requested guidance, and rank artwork in result threads. Stop only `ledger-ai` briefly during the staff pilot and confirm factual/canned messages, unchanged accounting, and working opt-out cleanup, then restart it. Short result/guidance profiles get one ten-second attempt; other narration retains its existing bounds. Saved text is reused on delivery retries. See [result rollout](RESULT_SUMMARIES.md).

Local automated checks validate the API contract, signed callback dispatch, manifest coverage, and Compose structure. A successful GB10 model load, authenticated Cloudflare route, and real Slack workspace installation must be verified on the deployment host; they are not simulated by those tests.

## Rebuild after a repository update

After updating the deployment checkout, run:

```bash
bash scripts/rebuild.sh
# Allow longer model startup if necessary:
bash scripts/rebuild.sh --wait-timeout 2400
```

The [rebuild script](../scripts/rebuild.sh) requires Bash and Docker Compose v2 with `--wait-timeout` and `--ignore-buildable` support. It resolves the repository directory from its own location, loads normal Compose `.env`/override settings, checks the daemon and configuration, rebuilds application images with `--pull --no-cache`, and pulls configured registry images before stopping the running stack. Build or pull failure leaves existing containers running. Pin `VLLM_IMAGE` and `CLOUDFLARED_IMAGE` in `.env` to control registry updates; unpinned `latest` tags may change on every rebuild.

It then gives containers 60 seconds to shut down, removes the project's old containers and orphan services, and recreates all enabled services, including the engagement worker. The Hugging Face named cache volume and external Mongo/MQTT data are preserved. It does not prune Docker resources, reset records, or run `init`/`bootstrap`. Use the same Compose project settings as the existing deployment; optional profiles remain controlled by `COMPOSE_PROFILES`.

Expect downtime during recreation and model warmup. The default 1,800-second health deadline accommodates the AI's twenty-minute start period. Success requires all services to be running (healthy where health checks exist) and web `/ready` to pass both Mongo checks. Workers and cloudflared have no health checks; this does not prove broker connectivity, Slack delivery, tunnel connectivity, or successful model generation. A startup/readiness failure exits nonzero and shows service status, leaving containers available for diagnosis with `docker compose logs`. Fix the cause and rerun; rollback remains a deliberate operator action.

## Tools and staged engagement rollout

Compose enables automatic tool choice with the `qwen3_coder` parser and retains non-thinking generation. Verify the deployed image accepts these flags and performs a real tool_call_id-correlated round-trip before activation. Interfaces/delegation/prompts ship first. Observation is enabled by default for eligible members in configured channels after notice, including nonparticipants, with independent member opt-out through `/ledger preferences`. Welcomes remain disabled by default. An existing `.env` with `LEDGER_OBSERVATION=false` continues to disable observation; change it to `true` or remove the override and recreate services to adopt the default. Observation proposals are unconditionally audit-only, even if the compatibility audit flag is false; deduction/announcement flags cannot authorize model-driven accounting or recognition. Only separately authorized deterministic/human decisions may change accounting. The engagement worker is separate from accounting/channel cleanup. Update exact Mongo roles for volunteer_events/checkins/cards reads and quest indexes. See [switches and live acceptance checks](ENGAGEMENT_QUESTS.md).

`LEDGER_QUEST_REVIEW_CHANNEL_ID` optionally enables review-channel notifications. Use a private staff channel, not a registered member game channel, with The Ledger bot already invited. Configure it in `.env` and recreate application services. Existing `chat:write` covers message updates; no manifest scope change is needed. Verify one pending submission, in-place closure, and deleted-message replacement against Slack after deployment.
