# GB10 inference and Cloudflare Tunnel

The supplied [compose.yaml](../compose.yaml) runs six services: web ingress, three independent workers, vLLM, and cloudflared. Existing Mongo and MQTT remain external dependencies. All commands below run from the repository root on the deployment host.

## Inference host and configuration

Run the complete stack on a Linux ARM64 NVIDIA DGX Spark/GB10 host with Docker Compose v2 and NVIDIA Container Toolkit configured for GPU containers. The [vllm-gb10 project](https://github.com/timothystewart6/vllm-gb10) publishes `ghcr.io/timothystewart6/vllm-gb10`; its image requires an explicit `vllm serve` command. An ordinary Windows/x86 Docker installation does not provide the required GB10 runtime.

Compose serves [nvidia/Qwen3.8-27B-NVFP4](https://huggingface.co/nvidia/Qwen3.8-27B-NVFP4). The model card's example targets multiple GPUs. This deployment instead starts with one GB10 GPU, an 8,192-token context, four concurrent sequences, FP8 KV cache, and 0.7 GPU memory utilization. These are conservative pilot settings, not a measured performance claim. Verify model loading and generation on the target machine; the model's mixed NVFP4/FP8 support depends on the selected image build. Adjust `VLLM_MAX_MODEL_LEN`, `VLLM_MAX_NUM_SEQS`, and `VLLM_GPU_MEMORY_UTILIZATION` after observing memory and response times.

Copy `.env.example` to `.env` and supply:

| Variable | Deployment value |
| --- | --- |
| `MLAB_URI`, `LEDGER_URI` | Reachable Mongo URIs with separate [restricted database credentials](MONGODB_ACCESS.md) |
| `SLACK_*` | Bot token, signing secret, workspace ID, and bot user ID from the installed app |
| `MQTT_*` | Reachable existing broker and its credentials/TLS setting |
| `LEDGER_LLM_BASE_URL` | `http://ledger-ai:8000/v1` for this Compose stack |
| `LEDGER_LLM_MODEL` | `nvidia/Qwen3.8-27B-NVFP4`; overrides rename the served API alias, not the model downloaded |
| `LEDGER_LLM_API_KEY` | A strong shared secret; Compose supplies it to vLLM as `VLLM_API_KEY` and the bot as its bearer key |
| `HF_TOKEN` | Optional Hugging Face token for model downloads, separate from the inference key |
| `VLLM_IMAGE`, `CLOUDFLARED_IMAGE` | Defaults use upstream `latest`; pin reviewed release tags/digests after a successful pilot |
| `CLOUDFLARE_TUNNEL_TOKEN` | Token for the remotely managed tunnel described below |

Compose requires nonblank inference and tunnel secrets. It does not invent, print, or provision them. Use deployment secret management for production. The `huggingface-cache` named volume persists model downloads; initial download, kernel compilation, and model loading can take a while. The health check allows twenty minutes for startup. Workers deliberately do not depend on AI health, so fallback delivery and opt-out cleanup remain available throughout.

`localhost` in `.env` refers to each container. Replace the local development Mongo/MQTT examples with addresses reachable from Docker. For a separate Python bot host, use the reachable vLLM `/v1` address and the same API key. Compose publishes no vLLM host port; exposing one for an external bot is a deliberate deployment override, kept off the Cloudflare Slack hostname.

## Cloudflare public hostname

Create a remotely managed Cloudflare Tunnel and put its connector token in `CLOUDFLARE_TUNNEL_TOKEN`. Compose passes it via `TUNNEL_TOKEN` to `cloudflare/cloudflared` running `tunnel --no-autoupdate run`. The token identifies the tunnel; hostname routing is configured in Cloudflare, not in the token or Compose. See [tunnel setup](https://developers.cloudflare.com/tunnel/get-started/) and [token environment parameters](https://developers.cloudflare.com/tunnel/reference/run-parameters/).

Configure a published application route:

| Field | Value |
| --- | --- |
| Public hostname | Your dedicated hostname, for example `ledger.example.org` |
| Origin service type | HTTP |
| Origin URL | `ledger-web:3000` (full service URL: `http://ledger-web:3000`) |
| Path | Leave blank to forward the hostname's paths unchanged |

Use this hostname in every URL in [slack-manifest.json](../slack-manifest.json), replacing `LEDGER_HOST`. The three callback URLs are `/slack/events`, `/slack/commands`, and `/slack/interactions`. Cloudflare terminates public HTTPS; the tunnel reaches Gunicorn on the Compose network. No inbound router port forwarding is needed. The bot's port 3000 is bound only to host loopback for local health checks.

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
docker compose up -d ledger-accounting ledger-delivery ledger-channels
docker compose ps
curl --fail http://127.0.0.1:3000/ready
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
], max_tokens=128))
PY
```

The adapter sends `chat_template_kwargs: {"enable_thinking": false}` with every request, matching the model's supported chat template. Server defaults also disable thinking. `--generation-config vllm` avoids model-repository sampling defaults overriding the service configuration; per-template temperature/token limits still apply. The [vLLM serving guide](https://docs.vllm.ai/en/latest/serving/online_serving/) and [reasoning guide](https://docs.vllm.ai/en/latest/features/reasoning_outputs/) describe these API options.

Finally exercise a DM, all commands, modal dropdowns/submissions, App Home, a public kudos, private-channel invitations/removals, and a file-backed skill tree in Slack. Stop only `ledger-ai` briefly during the staff pilot and confirm canned messages, unchanged accounting, and working opt-out cleanup, then restart it. A failed completion gets one attempt within the existing fifteen-second deadline; already composed fallback messages are reused on delivery retries.

Local automated checks validate the API contract, signed callback dispatch, manifest coverage, and Compose structure. A successful GB10 model load, authenticated Cloudflare route, and real Slack workspace installation must be verified on the deployment host; they are not simulated by those tests.
