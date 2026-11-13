"""Application-authored reports: the model cannot select source queries or audiences."""
from datetime import datetime, timedelta, timezone
import json
import os
import re
from urllib.parse import urlsplit
from uuid import uuid4, UUID

from .analytics import predict
from .detectors import DetectorRegistry, GUARDRAILS, SPEC_KEYS
from .privacy import timestamp
from .schema import fingerprint
from .state import stamp
from .slack import SECURITY_INTENT
from ..domain import Denied
from ..prompt_library import library_template, variables_for, render
from ..prompt_matrix import PromptMatrix


class AttachmentPending(RuntimeError):
    """Text was delivered; retry only pending chart attachments later."""
    retry_after_seconds = 120


def request_kind(text):
    """Intent only; raw Slack text never enters inference or a SQL/Mongo interface."""
    text = text.strip()
    words = text.split()
    if words and words[0].lower() == 'security':
        return 'security', words[1:]
    if SECURITY_INTENT.search(text):
        return 'security', []
    if words and words[0] in {"status", "detectors", "schema"}:
        return words[0], words[1:]
    if re.search(r"revok|revocation", text, re.I): return "revocation", []
    if re.search(r"retain|retention|24|long.term|sustain|stay", text, re.I): return "retention_24m", []
    if re.search(r"cancel|lapse|churn|expir|drop.out|risk", text, re.I): return "churn", []
    if re.search(r"schema|invalid|non.conform|integrity|data quality", text, re.I): return "quality", []
    if re.search(r"visit|check.?in|attendance|season|pattern", text, re.I): return "attendance", []
    return "overview", []


class Reports:
    def __init__(self, state, ledger, slack, access, registry, config, api=None, metabase=None):
        self.state, self.ledger, self.slack, self.access = state, ledger, slack, access
        self.registry, self.config, self.api, self.metabase = registry, config, api, metabase
        self.detectors = DetectorRegistry(state)
        self.matrix = PromptMatrix.from_env()

    def prompt(self, key, kind, summary):
        saved = self.state.get("ai_requests", key)
        if saved:
            return saved
        # Deterministic selection with recorded recent choice avoidance and immutable request text.
        recent = self.state.get("ai_history", kind, {}).get("choices", [])
        template = library_template(kind, "shared")
        choices = template["variations"]
        blocked = recent[:]
        while not [v for v in choices if v["id"] not in blocked]:
            blocked.pop(0)
        eligible = [v for v in choices if v["id"] not in blocked]
        choice = eligible[int(fingerprint(key)[:8], 16) % len(eligible)]
        values = variables_for({"summary": summary}, kind, "shared", template["audience_instruction"])
        self.matrix.refresh()
        matrix = self.matrix.snapshot()
        guardrails = GUARDRAILS if kind == "anomaly_propose" else GUARDRAILS.replace(
            "Return only a JSON object matching the requested detector specification.", "Explain only the supplied aggregate evidence in plain text.")
        system = matrix["text"] + "\n" + render(choice["system"], values) + "\n" + template["audience_instruction"] + "\n" + guardrails
        text = render(choice["user"], values)
        saved = {"system": system, "prompt": text, "template": template, "summary": summary,
                 "choice": choice["id"], "matrix_version": matrix["version"], "created_at": stamp()}
        self.state.put("ai_requests", key, saved)
        self.state.put("ai_history", kind, {"choices": (recent + [saved["choice"]])[-10:]})
        return saved

    def propose(self, key, actor):
        if self.api is None:
            raise ValueError("Inference is unavailable; no proposal was activated")
        saved = self.state.get("proposal_requests", key)
        if saved:
            return self.detectors.propose(saved["spec"], actor)
        summary = {"features": list(__import__('ledger.anomaly.analytics', fromlist=['FEATURES']).FEATURES),
                   "families": ["robust_deviation", "upper_threshold", "lower_threshold"],
                   "required_keys": sorted(SPEC_KEYS), "windows": [7, 28, 90], "minimum_evidence": 50,
                   "maximum_alert_rate": .05, "comparison_groups": ["member_history", "same_weekday"],
                   "references": ["5 Data Anomalies and Anomaly Detection Practices", "Advanced Techniques for Anomaly Detection", "Data Analytics using Python"],
                   "metrics": [v for _, v in self.state.rows("report_metrics")][:30]}
        prompt = self.prompt(key, "anomaly_propose", json.dumps(summary, separators=(",", ":")))
        try:
            output = self.api.complete([{"role": "system", "content": prompt["system"]}, {"role": "user", "content": prompt["prompt"]}],
                                       temperature=.3, max_tokens=1200)
        except Exception:
            raise ValueError("Inference is unavailable; no detector was proposed") from None
        spec = json.loads(output)
        version = self.detectors.propose(spec, actor)
        self.state.put("proposal_requests", key, {"spec": spec, "version": version})
        return version

    def build(self, job):
        p = job["payload"]
        capability = self.access.authorize(p["user_id"], p["channel"])
        kind, args = request_kind(p["text"])
        staff = capability == "staff_detail"
        if kind == 'security':
            from .security_reports import build
            return build(self, job, args, capability)
        if kind in {"detectors", "schema"}:
            if not staff or not self.access.staff(p["user_id"]):
                raise Denied("Detector and schema review require a verified staff DM.")
            verb = args[0] if args else "list"
            if kind == "schema":
                if verb == "approve" and len(args) == 2:
                    self.registry.approve(args[1], p["user_id"])
                current = self.state.get("schemas", "active", {})
                review = {"revision": current.get("version"), "provenance": self.registry.provenance(),
                          "pending": current.get("pending", {}), "collections": sorted(current.get("candidate", {}))}
                if verb == "review" and len(args) == 2:
                    name = args[1]
                    if name not in current.get("candidate", {}) and name not in current.get("collections", {}):
                        raise ValueError("Choose a known schema collection")
                    review["refreshed_definition"] = current.get("candidate", {}).get(name)
                    review["checking_definition"] = current.get("collections", {}).get(name)
                return {"text": json.dumps(review, sort_keys=True)[:30000], "charts": [], "capability": capability}
            if verb == "propose":
                version = self.propose(job["_id"], p["user_id"])
                text = "Detector proposed in shadow mode: " + version
            elif verb in {"approve", "disable"} and len(args) == 2:
                getattr(self.detectors, verb)(args[1], p["user_id"])
                text = f"Detector {args[1]} {verb} completed."
            elif verb == "resolve" and len(args) == 2:
                found = False
                with self.state.atomic():
                    for namespace in ("detector_findings", "behavior_findings"):
                        finding = self.state.get(namespace, args[1])
                        if finding:
                            finding.update(status="resolved", resolved_at=stamp(), resolved_by=p["user_id"])
                            self.state.put(namespace, args[1], finding)
                            found = True
                if not found:
                    raise ValueError("Unknown finding revision")
                text = "Finding resolved after human review: " + args[1]
            elif verb in {"list", "review"}:
                text = "\n".join(f"{key}: {r['status']} / {r['spec']['feature']} / backtest {r['evaluation']['passed']}" for key, r in self.state.rows("detectors")) or "No proposed detectors."
                if verb == "review" and len(args) == 2:
                    revision = self.state.get("detectors", args[1])
                    if not revision:
                        raise ValueError("Unknown detector revision")
                    text = json.dumps(revision, sort_keys=True) + "\nShadow evidence: " + json.dumps(self.state.get("detector_shadow", args[1], {}))
                open_findings = [(key, row) for namespace in ("detector_findings", "behavior_findings")
                                 for key, row in self.state.rows(namespace) if row.get("status") in {"active", "shadow"} and not row.get("resolved_at")]
                text += "\nOpen finding IDs for review: " + ", ".join(key for key, _ in open_findings[:20])
            else:
                raise ValueError("Use detectors list, propose, approve <revision>, disable <revision>, or resolve <finding ID>.")
            return {"text": text, "charts": [], "capability": capability}
        metrics = [v for _, v in self.state.rows("report_metrics") if kind == "overview" or v["report"] == kind]
        lines = ["Analytics report: " + kind.replace("_", " ")]
        if kind == "status":
            health = [v for _, v in self.state.rows("health")]
            lines.append(f"Pipeline components ready: {sum(v.get('status') == 'ready' for v in health)} / {len(health)}.")
            lines.append("Rails schema commit: " + self.registry.manifest.get("provenance", {}).get("rails_commit", "unknown"))
        elif kind == "quality":
            # Even structural violation counts are suppressed for small populations.
            records = [v for _, v in self.state.rows("findings") if v.get("status") == "open"]
            if staff or len({v["entity"] for v in records}) >= self.config.minimum_cohort:
                lines.append(f"Open schema/integrity findings: {len(records)}. Inspect the data-quality dashboard for rules and revisions.")
            else:
                lines.append("Small-population details are suppressed.")
        for row in metrics[:30]:
            lines.append(f"{row['dimension']} / {row['bucket']}: {row['value']:g} (n={row['sample_size']}, as of {row['as_of'][:10]}).")
        if kind == "retention_24m":
            lines.append("Positive-cohort commonalities are descriptive. Missing payment history cannot establish a nonqualifying comparison group.")
        if not metrics and kind not in {"status", "quality"}:
            lines.append("There is not enough verified, sufficiently large cohort evidence for this report yet.")
        # Member identification and evidence are rendered only in a verified staff DM, never sent to AI.
        mention = re.search(r"<@([UW][A-Z0-9]+)>", p["text"])
        if mention and staff:
            member = self.ledger.sources.identity(mention.group(1))
            if member:
                row = self.state.get("member_analytics", str(member["_id"]))
                if row:
                    lines.append("Member <@" + mention.group(1) + ">: " + json.dumps({"features": row["features"], "payer_type": row["payer_type"],
                        "coverage": row["sustained"], "as_of": row.get("as_of"), "confidence": row.get("confidence")}, sort_keys=True))
                    measured = timestamp(row.get("as_of"))
                    fresh = measured is not None and timedelta(0) <= datetime.now(timezone.utc) - measured <= timedelta(days=1)
                    for outcome in ("cancellation", "lapse"):
                        for horizon in (30, 90):
                            score = predict(self.state.get("models", f"{outcome}:{horizon}", {}), row["features"]) if row.get("confidence") == "verified" and fresh else None
                            if score is not None:
                                lines.append(f"Validated {outcome} association over {horizon} days: {score:.1%}; not a causal conclusion.")
        charts = []
        seeded = self.state.get("metabase", "bootstrap", {})
        periods = sorted({row["as_of"][:10] for row in metrics})
        period = ("Evidence as of " + ", ".join(periods)) if periods else "Current pipeline assessment"
        # Only a canonical report description and suppressed aggregates enter native Metabot.
        if self.metabase and staff and kind not in {"status", "quality"} and metrics:
            identity = fingerprint([os.environ.get("SLACK_TEAM_ID", ""), self.metabase.base_url,
                                    p["user_id"], p["channel"], p.get("thread_ts"), capability])
            conversation = self.state.get("metabot_conversations", identity)
            if not conversation:
                conversation = {"id": str(uuid4())}
                self.state.put("metabot_conversations", identity, conversation)
            self.access.authorize(p["user_id"], p["channel"])
            try:
                response = self.metabase.ask("Explain the " + kind + " report and create a chart from curated reporting views. "
                                            "Report associations, coverage gaps and uncertainty; never infer causation.", conversation["id"])
            except Exception as exc:
                self.state.put("health", "metabot", {"status": "unavailable", "error": type(exc).__name__, "updated_at": stamp()})
                response = {}
            # Native prose may explain numbers, but deterministic data above remains the source of truth.
            answer = response.get("answer", "")
            if isinstance(answer, str) and not any(t in answer for t in ("<@", "<!", "http://", "https://")):
                lines.append(answer[:1800])
            charts = response.get("chartrefs", [])[:3]
        elif self.api and metrics:
            prompt = self.prompt(job["_id"] + ":explanation", "anomaly_explain",
                json.dumps({"report": kind, "metrics": metrics[:30], "period": period,
                            "reference": "point, contextual and collective anomalies; absence of evidence remains unknown"}))
            try:
                explanation = self.api.complete([{"role": "system", "content": prompt["system"]},
                    {"role": "user", "content": prompt["prompt"]}], temperature=.1, max_tokens=384)
                if isinstance(explanation, str) and not any(s in explanation for s in ("<", ">", "http://", "https://")):
                    lines.append(explanation[:1800])
            except Exception as exc:
                self.state.put("health", "inference", {"status": "unavailable", "error": type(exc).__name__, "updated_at": stamp()})
        if not charts:
            cards = seeded.get("cards", {})
            for card_key, card_id in cards.items() if isinstance(cards, dict) else []:
                if kind in str(card_key):
                    charts.append({"type": "saved_card", "card_id": int(card_id)})
                    break
        dashboard_id = seeded.get("dashboards_by_report", {}).get(kind)
        public_url = os.environ.get("ANOMALY_METABASE_PUBLIC_URL", "").rstrip("/")
        parsed = urlsplit(public_url)
        if type(dashboard_id) is int and parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
            lines.append(f"Authenticated dashboard: {public_url}/dashboard/{dashboard_id}")
        lines.append(period + ".")
        lines.append("Missing historical evidence remains unknown. Statistical associations do not establish causation.")
        return {"text": "\n".join(lines)[:12000], "charts": charts, "capability": capability, "period": period}

    def deliver(self, job):
        p = job["payload"]
        capability = self.access.authorize(p["user_id"], p["channel"])
        result = self.state.get("query_results", job["_id"])
        if result is None:
            try:
                result = self.build(job)
            except ValueError as exc:
                # Only our bounded validation messages are allowed, never provider exception bodies.
                result = {"text": "The analytics request needs more evidence or a supported command. Use /ledger-anomaly status, security summary, security detectors list, detectors list, or schema review.", "charts": [], "capability": capability}
            self.state.put("query_results", job["_id"], result)
        if result["capability"] != capability:
            raise Denied("Analytics audience changed while queued.")
        receipt = self.state.get("query_receipts", job["_id"], {"text": False, "charts": []})
        self.access.authorize(p["user_id"], p["channel"])
        if not receipt["text"]:
            response = self.slack.chat_postMessage(channel=p["channel"], thread_ts=p.get("thread_ts"), text=result["text"],
                client_msg_id=str(UUID(fingerprint(job["_id"])[:32])), unfurl_links=False, unfurl_media=False)
            receipt.update(text=True, ts=response.get("ts"))
            self.state.put("query_receipts", job["_id"], receipt)
        pending_attachment = False
        for ref in result["charts"]:
            refid = fingerprint(ref)
            if refid in receipt["charts"] or self.metabase is None:
                continue
            try:
                content = self.metabase.render(ref)
                self.access.authorize(p["user_id"], p["channel"])
                if content:
                    self.slack.files_upload_v2(channel=p["channel"], thread_ts=p.get("thread_ts") or receipt.get("ts"),
                        file=content, filename="ledger-analytics.png", title="Ledger analytics — " + result.get("period", "current report"))
                receipt["charts"].append(refid)
                self.state.put("query_receipts", job["_id"], receipt)
            except Denied:
                raise
            except Exception as exc:
                from .metabase import MetabaseError
                terminal = isinstance(exc, MetabaseError) and any(f"HTTP {code}" in str(exc) for code in (400, 403, 404, 410, 422))
                self.state.put("attachment_failures", job["_id"] + ":" + refid,
                    {"error": type(exc).__name__, "at": stamp(), "status": "unavailable" if terminal else "pending"})
                if terminal:
                    receipt["charts"].append(refid)
                    self.state.put("query_receipts", job["_id"], receipt)
                else:
                    pending_attachment = True
        if pending_attachment:
            raise AttachmentPending("Factual text delivered; attachment retry pending")
        receipt["completed_at"] = stamp()
        self.state.put("query_receipts", job["_id"], receipt)
        return result
