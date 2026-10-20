#!/usr/bin/env python3
"""Choose a current Claude or Codex model and reasoning level for a bounded task."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MIN_CONFIDENCE = 0.55
CACHE_MAX_AGE = dt.timedelta(days=1)
CLAUDE_OVERVIEW_URL = "https://platform.claude.com/docs/en/models/overview.md"
OPENAI_MODEL_DOCS_URL = "https://developers.openai.com/api/docs/models"
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
DATA_DIR = Path(os.path.expanduser(os.environ.get("JEV_DATA_DIR", "~/.local/share/jev-model-router")))
CACHE_DIR = Path.home() / ".cache/jev-model-router"
OUTCOMES_PATH = DATA_DIR / "jev-outcomes.jsonl"
SCORECARD_PATH = DATA_DIR / "model-scorecard.md"
KINDS = ("scan", "mechanical", "writing", "research", "code", "review", "architecture", "coordination")
MIN_OUTCOMES = 5


def valid_number(value, integer=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0
            and (not integer or int(value) == value))


def validate_outcome(row: dict) -> None:
    if not isinstance(row, dict):
        raise ValueError("Outcome must be an object")
    task, note = row.get("task", ""), row.get("note", "")
    if (not isinstance(task, str) or not task.strip() or len(task) > 500
            or not isinstance(note, str) or len(note) > 2000
            or SECRET_PATTERN.search(task + " " + note)):
        raise ValueError("Use short outcome text without credentials or personal data")
    if (row.get("kind") not in KINDS or row.get("risk") not in {"low", "medium", "high"}
            or row.get("scope") not in {"small", "medium", "large"}
            or row.get("verdict") not in {"pass", "pass_with_fixes", "fail"}
            or row.get("effort") not in (*EFFORT_ORDER, None)):
        raise ValueError("Invalid outcome classification")
    if not re.fullmatch(r"claude-[a-z]+-\d+(?:-\d+)*|gpt-\d+(?:\.\d+)*(?:-[a-z0-9.-]+)?", row.get("model", "")):
        raise ValueError("Record an exact versioned model ID")
    for field in ("minutes", "cost_usd", "rework"):
        if row.get(field) is not None and not valid_number(row[field], field == "rework"):
            raise ValueError("Outcome metrics must be finite and nonnegative")


def record_outcome(row: dict, path: Path | None = None) -> dict:
    row = dict(row)
    if row.get("model") in CLAUDE_CATALOG:
        family = row["model"]
        resolved = [m for m in load_scorecard() if m.startswith(f"claude-{family}-")]
        if len(resolved) != 1:
            raise ValueError("Record an exact versioned model ID")
        row["model"] = resolved[0]
    validate_outcome(row)
    row.setdefault("recorded_at", dt.datetime.now(dt.timezone.utc).isoformat())
    row.setdefault("schema_version", 1)
    path = path or OUTCOMES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("Incomplete outcome append")
    finally:
        os.close(fd)
    return row


def load_outcomes(path: Path | None = None) -> list[dict]:
    rows = []
    try:
        with (path or OUTCOMES_PATH).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    validate_outcome(row)
                    rows.append(row)
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return rows


def outcome_summary(rows: list[dict]) -> dict:
    def average(field):
        values = [r[field] for r in rows if r.get(field) is not None]
        return {"count": len(values), "mean": sum(values) / len(values) if values else None}
    count = len(rows)
    return {"count": count, "pass_rate": sum(r["verdict"] == "pass" for r in rows) / count if count else None,
            "accepted_rate": sum(r["verdict"] != "fail" for r in rows) / count if count else None,
            "rework": average("rework"), "minutes": average("minutes"), "cost_usd": average("cost_usd")}


def load_scorecard(path: Path | None = None) -> dict:
    try:
        if path is None:
            data = json.loads((DATA_DIR / "model-benchmarks.json").read_text(encoding="utf-8"))
        else:
            content = path.read_text(encoding="utf-8")
            if path.suffix == ".json":
                data = json.loads(content)
            else:
                block = re.search(r"<!-- jev-scorecard -->\s*```json\s*(.*?)\s*```", content, re.S)
                data = json.loads(block.group(1))
        if data.get("schema_version") != 1:
            return {}
        models = data["models"]
        return models if isinstance(models, dict) and all(isinstance(m, dict) for m in models.values()) else {}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return {}


def attach_evidence(candidates: dict, kind: str, rows: list[dict], scorecard: dict) -> None:
    for item in candidates.values():
        model_id = item["model_facts"].get("api_model_id", item["model"])
        official = scorecard.get(model_id, {})
        item["official_performance"] = official
        matched = [r for r in rows if r["model"] == model_id]
        item["internal_by_kind"] = {k: outcome_summary([r for r in matched if r["kind"] == k])
                                    for k in sorted({r["kind"] for r in matched} | {kind})}
        has_official = bool(official.get("benchmarks") or official.get("relative_findings"))
        if has_official or matched:
            item["performance_evidence"] = ("official_benchmarks_and_local_outcomes" if has_official and matched
                                            else "official_benchmarks" if has_official else "local_outcomes")


def evidence_fallback(kind: str, risk: str, scope: str, candidates: dict,
                      rows: list[dict]) -> tuple[str, str]:
    minimum = (3 if kind in {"review", "architecture"} else 2) if risk == "high" else (2 if kind in {"code", "review", "architecture"} else 1)
    eligible = {n: i for n, i in candidates.items() if i["capability"] >= minimum}
    local = {}
    for name, item in eligible.items():
        model_id = item["model_facts"].get("api_model_id", item["model"])
        group = [r for r in rows if r["model"] == model_id and r["kind"] == kind
                 and r["risk"] == risk and r["scope"] == scope]
        if len(group) >= MIN_OUTCOMES:
            local[name] = outcome_summary(group)
    if local:
        def rank(name):
            s = local[name]
            return (-s["pass_rate"], -s["accepted_rate"],
                    s["cost_usd"]["mean"] if s["cost_usd"]["count"] >= MIN_OUTCOMES else math.inf,
                    s["rework"]["mean"] if s["rework"]["mean"] is not None else math.inf,
                    s["minutes"]["mean"] if s["minutes"]["mean"] is not None else math.inf)
        return min(local, key=rank), "internal_outcomes"
    scores, reason = benchmark_comparison(candidates, kind)
    for item in candidates.values():
        item["comparison_unavailable_reason"] = reason
    if scores:
        return max(eligible, key=lambda n: scores[n]), "official_benchmark"
    return fallback_model(kind, risk, candidates), "legacy_rules"


def benchmark_comparison(candidates: dict, kind: str) -> tuple[dict, str | None]:
    comparisons = {}
    for name, item in candidates.items():
        for b in item.get("official_performance", {}).get("benchmarks", []):
            if (kind not in b.get("kinds", []) or not valid_number(b.get("score"))
                    or b.get("effort") not in EFFORT_ORDER or not b.get("comparison_group")
                    or not b.get("comparable", False)):
                continue
            key = (b.get("name"), b["comparison_group"], b.get("unit"), b["effort"])
            comparisons.setdefault(key, {})[name] = b["score"]
    complete = [v for v in comparisons.values() if len(v) == len(candidates) and len(v) >= 2]
    if complete:
        return complete[0], None
    missing = [n for n, i in candidates.items() if not any(
        kind in b.get("kinds", []) and valid_number(b.get("score"))
        for b in i.get("official_performance", {}).get("benchmarks", []))]
    return {}, "비교 불가 사유: 모든 허용 후보에 공통 벤치마크·버전·측정 조건·추론 수준이 없음" + (
        "; 자료 없음: " + ", ".join(missing) if missing else "; 조건 또는 추론 수준이 다르거나 미확인")


def evidence_effort(model: dict, kind: str, risk: str, scope: str, rows: list[dict]) -> str:
    model_id = model["model_facts"].get("api_model_id", model["model"])
    levels = {}
    for effort in model["efforts"]:
        if risk == "high" and EFFORT_ORDER.index(effort) < EFFORT_ORDER.index("high"):
            continue
        group = [r for r in rows if r["model"] == model_id and r["kind"] == kind
                 and r["risk"] == risk and r["scope"] == scope and r["effort"] == effort]
        if len(group) >= MIN_OUTCOMES:
            levels[effort] = outcome_summary(group)
    if levels:
        return min(levels, key=lambda e: (-levels[e]["pass_rate"],
                   levels[e]["cost_usd"]["mean"] if levels[e]["cost_usd"]["count"] >= MIN_OUTCOMES else math.inf,
                   EFFORT_ORDER.index(e)))
    groups = {}
    for b in model.get("official_performance", {}).get("benchmarks", []):
        effort = b.get("effort")
        if (kind in b.get("kinds", []) and effort in model["efforts"]
                and valid_number(b.get("score")) and b.get("comparable")
                and (risk != "high" or EFFORT_ORDER.index(effort) >= EFFORT_ORDER.index("high"))):
            groups.setdefault((b.get("name"), b.get("comparison_group"), b.get("unit")), {})[effort] = b["score"]
    for scores in groups.values():
        if len(scores) >= 2 and supported_effort(fallback_effort(risk, scope), model["efforts"]) in scores:
            best = max(scores.values())
            selected = min((e for e, score in scores.items() if best - score <= 1.0), key=EFFORT_ORDER.index)
            model["effort_benchmark_reason"] = {"benchmark": next(k[0] for k, v in groups.items() if v is scores),
                "scores": scores, "tolerance_points": 1.0, "selected": selected}
            return selected
    return fallback_effort(risk, scope)
CLAUDE_CATALOG = {
    "haiku": ("Fast Claude model for short searches, formatting and mechanical work.", 1, ()),
    "sonnet": ("Current Claude daily work model for writing, research, coding and synthesis.", 2, EFFORT_ORDER[:-1]),
    "opus": ("Current Claude complex reasoning model for difficult design and high-stakes synthesis.", 3, EFFORT_ORDER[:-1]),
    "fable": ("Current Claude long-horizon frontier model for ambiguous work larger than one sitting; may require usage credits.", 4, EFFORT_ORDER[:-1]),
}


def parse_claude_overview(markdown: str) -> dict[str, dict]:
    """Extract vendor facts, without treating marketing copy as measured performance."""
    rows = {}
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5:
            rows[re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", cells[0])] = cells[1:]
    headings = rows.get("Feature", [])
    required = ("Description", "Claude API ID", "Comparative latency", "Pricing", "Default effort")
    if len(headings) != 4 or any(field not in rows for field in required):
        raise ValueError("Claude model overview format changed")
    models = {}
    for index, heading in enumerate(headings):
        match = re.fullmatch(r"Claude (Fable|Opus|Sonnet|Haiku) [\d.]+", heading)
        if not match:
            continue
        family = match.group(1).lower()
        model_id = rows["Claude API ID"][index].strip("`")
        if not re.fullmatch(r"claude-[a-z0-9-]+", model_id):
            raise ValueError("Invalid Claude model ID")
        models[family] = {
            "api_model_id": model_id,
            "display_name": heading,
            "description": rows["Description"][index],
            "strengths": rows["Description"][index],
            "comparative_latency": rows["Comparative latency"][index],
            "api_price": rows["Pricing"][index],
            "default_effort": rows["Default effort"][index].strip("`"),
            "evidence_url": CLAUDE_OVERVIEW_URL.removesuffix(".md"),
        }
    if not {"opus", "sonnet", "haiku"}.issubset(models):
        raise ValueError("Claude model overview is incomplete")
    return models


def claude_catalog(cache_path: Path, now: dt.datetime | None = None) -> tuple[dict[str, dict], str]:
    """Refresh official Claude metadata daily; fall back to a recent cache only."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cached = {}
    fetched = None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 3:
            raise ValueError("Claude cache schema changed")
        fetched = dt.datetime.fromisoformat(data["fetched_at"].replace("Z", "+00:00"))
        cached = data["models"]
        if dt.timedelta(0) <= now - fetched <= CACHE_MAX_AGE:
            return cached, "official_cache"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        request = urllib.request.Request(CLAUDE_OVERVIEW_URL, headers={"User-Agent": "jev-model-catalog/1.0"})
        with urllib.request.urlopen(request, timeout=5) as response:
            models = parse_claude_overview(response.read().decode("utf-8"))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"schema_version": 3, "fetched_at": now.isoformat(), "models": models}), encoding="utf-8")
        return models, "official_live"
    except (OSError, ValueError, UnicodeError, urllib.error.URLError):
        if fetched and dt.timedelta(0) <= now - fetched <= CACHE_MAX_AGE:
            return cached, "official_cache"
        return {}, "unavailable"
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|bearer)\s*[:=]\s*\S+|"
    r"sk-[a-z0-9_-]{12,}|apikey_[a-z0-9_-]{12,}|"
    r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"
)


def visible_codex_models(cache_path: Path, now: dt.datetime | None = None) -> list[dict]:
    """Use the CLI's recent visible catalog, not a pinned GPT generation."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched = dt.datetime.fromisoformat(data["fetched_at"].replace("Z", "+00:00"))
        age = (now or dt.datetime.now(dt.timezone.utc)) - fetched
        if not dt.timedelta(0) <= age <= CACHE_MAX_AGE:
            return []
        return [m for m in data["models"] if m.get("visibility") == "list"
                and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", m.get("slug", ""))]
    except (OSError, ValueError, KeyError, TypeError):
        return []


ACCOUNTS_PATH = Path.home() / ".config/ai-accounts/accounts.json"


def codex_accounts() -> tuple[dict, str]:
    try:
        block = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))["codex"]
        return block["accounts"], block.get("default", "")
    except (OSError, ValueError, KeyError, TypeError):
        return {}, ""


def resolve_codex_account(spec: str | None = None) -> dict:
    """Pick the Codex account: explicit flag > AI_CODEX_ACCOUNT env > CODEX_HOME env > registry default.
    The per-session choice of the Claude Code mod is an in-engine atom a script cannot read."""
    accounts, default = codex_accounts()

    def find(key):
        key = (key or "").strip().lower()
        return next((i for i, a in accounts.items() if key in {i.lower(), str(a.get("name", "")).lower()}), None)

    def build(acc_id, source):
        home = os.path.expanduser(accounts[acc_id].get("home") or "~/.codex")
        return {"id": acc_id, "name": accounts[acc_id].get("name", acc_id), "home": home, "source": source}

    if spec:
        found = find(spec)
        if not found:
            raise ValueError(f"Unknown Codex account: {spec}")
        return build(found, "flag")
    found = find(os.environ.get("AI_CODEX_ACCOUNT"))
    if found:
        return build(found, "env")
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        norm = os.path.normpath(os.path.expanduser(env_home))
        found = next((i for i, a in accounts.items()
                      if os.path.normpath(os.path.expanduser(a.get("home") or "~/.codex")) == norm), None)
        if found:
            return build(found, "env")
        return {"id": None, "name": None, "home": norm, "source": "env"}
    if find(default):
        return build(find(default), "registry_default")
    return {"id": None, "name": None, "home": str(Path.home() / ".codex"), "source": "fallback"}


def speed_tiers_of(item: dict) -> list[str]:
    """Normalize app-server speed info: wire id 'priority' is the Fast tier (config value 'fast')."""
    tiers = []
    for raw in list(item.get("additionalSpeedTiers") or []) + [
            t.get("id") for t in item.get("serviceTiers") or [] if isinstance(t, dict)]:
        name = {"priority": "fast"}.get(raw, raw)
        if isinstance(name, str) and re.fullmatch(r"[a-z0-9_-]+", name) and name not in tiers:
            tiers.append(name)
    return tiers


def live_codex_models(timeout: float = 4.0, codex_home: str | None = None) -> list[dict]:
    """Read account-available models from Codex app-server without starting a task."""
    env = dict(os.environ)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    process = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
    )
    deadline = time.monotonic() + timeout

    def send(message: dict) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def receive(request_id: int) -> dict:
        assert process.stdout is not None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError("Codex model/list request failed")
                return message["result"]
        raise TimeoutError("Codex model/list request timed out")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "jev-model-router", "version": "2.1"},
            "capabilities": {},
        }})
        receive(1)
        send({"method": "initialized", "params": {}})
        models = []
        cursor = None
        for page in range(8):
            request_id = page + 2
            params = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            send({"id": request_id, "method": "model/list", "params": params})
            result = receive(request_id)
            for item in result.get("data", []):
                slug = item.get("model", "")
                if item.get("hidden") or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", slug):
                    continue
                models.append({
                    "slug": slug,
                    "description": item.get("description", ""),
                    "visibility": "list",
                    "speed_tiers": speed_tiers_of(item),
                    "supported_reasoning_levels": [
                        {"effort": level.get("reasoningEffort")}
                        for level in item.get("supportedReasoningEfforts", [])
                    ],
                })
            cursor = result.get("nextCursor")
            if not cursor:
                break
        if not models:
            raise RuntimeError("Codex returned no visible models")
        return models
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def codex_catalog(cache_path: Path, codex_home: str | None = None) -> tuple[list[dict], str]:
    try:
        models = live_codex_models(codex_home=codex_home)
        if not models:
            raise RuntimeError("Codex returned no visible models")
        return models, "app_server"
    except (OSError, RuntimeError, TimeoutError, ValueError, BrokenPipeError):
        cached = visible_codex_models(cache_path)
        return cached, "cache" if cached else "claude_only"


def codex_model_version(slug: str) -> tuple[tuple[int, ...], str] | None:
    """Parse a GPT generation and its role; unknown names require manual routing."""
    match = re.fullmatch(r"gpt-(\d+(?:\.\d+)*)(?:-([a-z]+))?", slug)
    if not match:
        return None
    version = tuple(int(part) for part in match.group(1).split("."))
    return version, match.group(2) or "base"


def latest_codex_models(visible: list[dict]) -> list[dict]:
    """Keep the newest account-visible version of each GPT role."""
    parsed = [(item, codex_model_version(item["slug"])) for item in visible]
    versions = [version for _, parsed_model in parsed if parsed_model for version, _ in [parsed_model]]
    if not versions:
        return []
    newest_generation = max(versions)
    newest_by_role = {}
    for _, parsed_model in parsed:
        if parsed_model:
            version, role = parsed_model
            newest_by_role[role] = max(version, newest_by_role.get(role, ()))
    return [item for item, parsed_model in parsed
            if parsed_model and parsed_model[0] == newest_by_role[parsed_model[1]]
            and not (parsed_model[1] == "base" and parsed_model[0] < newest_generation)]


def parse_openai_model_page(markdown: str, slug: str) -> dict:
    """Take task strengths and API price from the exact official model page."""
    if not re.search(r"Model ID:\s*`" + re.escape(slug) + r"`", markdown):
        raise ValueError("OpenAI model page ID does not match the visible model")
    intro = markdown.split("Model ID:", 1)[1].split("## Model details", 1)[0].split("\n", 1)[-1]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", intro) if p.strip()]
    strengths = ""
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.splitlines()
                 if line.strip() and not re.search(r"reasoning\.effort|^Get started|^See \[", line, re.I)]
        if lines and lines[0][0].isalpha():
            strengths = " ".join(lines)
            break
    if not strengths:
        raise ValueError("OpenAI model page has no task strengths")
    prices = {}
    for metric in ("Input", "Cached input", "Output"):
        match = re.search(r"^\|\s*" + metric + r"\s*\|\s*(\$[\d.]+)\s*\|", markdown, re.MULTILINE)
        if match:
            prices[metric.lower().replace(" ", "_")] = match.group(1) + " / 1M tokens"
    context = re.search(r"^- ([\d,]+) context window$", markdown, re.MULTILINE)
    return {
        "api_model_id": slug,
        "strengths": strengths,
        "api_price": prices,
        "context_tokens": int(context.group(1).replace(",", "")) if context else None,
        "evidence_url": f"{OPENAI_MODEL_DOCS_URL}/{slug}",
    }


def openai_catalog(models: list[dict], cache_path: Path, now: dt.datetime | None = None) -> tuple[dict[str, dict], str]:
    """Refresh official pages for the latest visible Codex models once per day."""
    now = now or dt.datetime.now(dt.timezone.utc)
    slugs = sorted(item["slug"] for item in models)
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched = dt.datetime.fromisoformat(data["fetched_at"].replace("Z", "+00:00"))
        if (data.get("schema_version") == 2 and data.get("slugs") == slugs
                and dt.timedelta(0) <= now - fetched <= CACHE_MAX_AGE):
            return data["models"], "official_cache"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    facts = {}
    for slug in slugs:
        try:
            request = urllib.request.Request(f"{OPENAI_MODEL_DOCS_URL}/{slug}.md",
                                             headers={"User-Agent": "jev-model-catalog/1.0"})
            with urllib.request.urlopen(request, timeout=5) as response:
                facts[slug] = parse_openai_model_page(response.read().decode("utf-8"), slug)
        except (OSError, ValueError, UnicodeError, urllib.error.URLError):
            continue
    if facts:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"schema_version": 2, "fetched_at": now.isoformat(),
                                              "slugs": slugs, "models": facts}), encoding="utf-8")
        except OSError:
            pass
    return facts, "official_live" if facts else "unavailable"


def capability(description: str) -> int:
    text = description.lower()
    if "frontier" in text or "most demanding" in text or "most capable" in text or "hardest" in text:
        return 3
    if "workhorse" in text or "complex" in text or "balanced" in text or "legacy coding" in text:
        return 2
    return 1


def candidate_catalog(visible: list[dict], scope: str, claude_models: dict[str, dict] | None = None,
                      openai_models: dict[str, dict] | None = None) -> dict[str, dict]:
    catalog = {}
    for model, (description, level, efforts) in CLAUDE_CATALOG.items():
        if model == "fable" and scope != "large":
            continue
        facts = (claude_models or {}).get(model, {})
        vendor_description = facts.get("description", description)
        catalog[f"claude_{model}"] = {
            "provider": "claude", "model": model, "description": vendor_description,
            "capability": level, "efforts": efforts,
            "model_facts": facts, "performance_evidence": "vendor_claim_only" if facts else "none",
        }
    for item in latest_codex_models(visible):
        slug = item["slug"]
        description = item.get("description", "")
        facts = (openai_models or {}).get(slug, {})
        efforts = tuple(e["effort"] for e in item.get("supported_reasoning_levels", [])
                        if e.get("effort") in EFFORT_ORDER)
        catalog["codex_" + re.sub(r"[^a-z0-9]", "_", slug)] = {
            "provider": "codex", "model": slug, "description": description,
            "capability": capability(description + " " + facts.get("strengths", "")), "efforts": efforts,
            "model_facts": facts, "speed_tiers": list(item.get("speed_tiers") or []),
            "performance_evidence": "vendor_claim_only" if facts else "client_description_only",
        }
    return catalog


def get_key() -> str:
    value = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if value:
        return value
    try:
        return (Path.home() / ".config/typesafe/api_key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ask_jev(task: str, kind: str, risk: str, scope: str, priority: str,
            candidates: dict[str, dict], key: str) -> tuple[str, float, str, float]:
    criteria = {
        name: (f"Provider {item['provider']}; model {item['model']}; "
               f"documented strengths {item.get('model_facts', {}).get('strengths', item['description'])}; "
               f"provider description {item['description']}; "
               f"capability tier {item['capability']}/4; official model facts {item.get('model_facts', {})}; "
               f"performance evidence {item.get('performance_evidence', 'none')}. "
               f"official benchmark summary {json.dumps(item.get('official_performance', {}), ensure_ascii=False)}; "
               f"internal outcomes by kind {json.dumps(item.get('internal_by_kind', {}), ensure_ascii=False)}. "
               "Choose only if adequate for this task. Vendor claims are not task-specific evaluation results.")
        for name, item in candidates.items()
    }
    body = {
        "state": {
            "task_summary": task, "task_kind": kind, "risk": risk,
            "scope": scope, "priority": priority,
        },
        "model": "jev-latest",
        "questions": {
            "model_choice": {
                "type": "choice",
                "instructions": (
                    "Pick the adequate available model for this task. Consider modality, scope, risk, "
                    "need for independent review, speed and token spend. Compare the documented strengths of "
                    "each available model for this particular task instead of assuming a provider is better. "
                    "Do not prefer a new release solely because it is newer. Fable is for long "
                    "ambiguous work. Descriptions and API prices are vendor facts, not verified task performance "
                    "or actual subscription cost. Do not assume a new version wins without local evaluations. "
                    "Respect the stated priority, but do not sacrifice needed capability for speed or cost."
                    " Use local pass rates, rework, time and cost with their sample counts. Local tasks may differ; "
                    "pass_with_fixes is not a clean pass. Missing values are unknown, not zero. "
                    "Missing measurements are unknown, never evidence of low performance. Use benchmark ranking only if ALL allowed candidates share the benchmark version, harness and effort. Otherwise use capability tiers across providers."
                ),
                "criteria": criteria,
            },
            "effort_choice": {
                "type": "choice",
                "instructions": (
                    "Pick reasoning depth for this task independently of model brand. Use the lowest depth "
                    "that can handle ambiguity, dependencies, verification and risk. Avoid max or ultra "
                    "unless the task is unusually hard and substantial."
                ),
                "criteria": {
                    "low": "Short, bounded, reversible, low-risk work with clear steps.",
                    "medium": "Ordinary implementation, writing or analysis with limited ambiguity.",
                    "high": "Several interacting constraints or meaningful correctness risk.",
                    "xhigh": "Difficult debugging, architecture or high-risk review needing deeper reasoning.",
                    "max": "Exceptional difficult work where extra reasoning is likely worth its cost.",
                    "ultra": "Only a long, exceptionally demanding Codex task that benefits from autonomous delegation.",
                },
            },
        },
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "jev-model-router/2.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        answers = json.load(response)["answers"]
    model_answer, effort_answer = answers["model_choice"], answers["effort_choice"]
    model, effort = model_answer["choice"], effort_answer["choice"]
    model_confidence, effort_confidence = float(model_answer["confidence"]), float(effort_answer["confidence"])
    if (model not in candidates or effort not in EFFORT_ORDER
            or not 0 <= model_confidence <= 1 or not 0 <= effort_confidence <= 1):
        raise ValueError("invalid Jev choice")
    return model, model_confidence, effort, effort_confidence


def fallback_model(kind: str, risk: str, candidates: dict[str, dict]) -> str:
    if all(item["provider"] == "codex" for item in candidates.values()):
        # 공급자가 codex로 고정된 경우: 위험도에 맞는 최소 역량 중 가장 가벼운 모델.
        minimum = 3 if risk == "high" else 2
        suitable = [n for n, i in candidates.items() if i["capability"] >= minimum] or list(candidates)
        return min(suitable, key=lambda n: (candidates[n]["capability"], list(candidates).index(n)))
    if kind in {"code", "review"}:
        minimum = 3 if risk == "high" else 2
        suitable = [name for name, item in candidates.items()
                    if item["provider"] == "codex" and item["capability"] >= minimum]
        if suitable:
            return min(suitable, key=lambda name: (candidates[name]["capability"], list(candidates).index(name)))
        return "claude_opus" if risk == "high" else "claude_sonnet"
    if risk == "high":
        return "claude_opus"
    if kind in {"scan", "mechanical"}:
        return "claude_haiku"
    return "claude_sonnet"


def fallback_effort(risk: str, scope: str) -> str:
    if risk == "high":
        return "xhigh" if scope == "large" else "high"
    if risk == "low" and scope == "small":
        return "low"
    return "medium"


def supported_effort(wanted: str, supported: tuple[str, ...]) -> str | None:
    if not supported:
        return None
    index = EFFORT_ORDER.index(wanted)
    lower = [level for level in supported if EFFORT_ORDER.index(level) <= index]
    return lower[-1] if lower else supported[0]


def route(task: str, kind: str, risk: str, scope: str = "medium", priority: str = "balanced",
          reviewer_of: str | None = None, provider: str | None = None,
          user_model: str | None = None, user_effort: str | None = None,
          cache_path: Path | None = None, key: str | None = None,
          claude_cache_path: Path | None = None, outcomes_path: Path | None = None,
          scorecard_path: Path | None = None, codex_account: str | None = None) -> dict:
    if not task.strip() or len(task) > 500 or SECRET_PATTERN.search(task):
        raise ValueError("Use a short task summary without credentials or personal data")
    if user_effort and user_effort not in EFFORT_ORDER:
        raise ValueError("Unsupported explicit effort level")
    account = resolve_codex_account(codex_account)
    cache = cache_path or Path(account["home"]) / "models_cache.json"
    codex_models, catalog_source = codex_catalog(cache, account["home"])
    latest_codex = latest_codex_models(codex_models)
    openai_cache = CACHE_DIR / "openai_models.json"
    openai_models, openai_source = openai_catalog(latest_codex, openai_cache)
    claude_cache = claude_cache_path or CACHE_DIR / "claude_models.json"
    claude_models, claude_source = claude_catalog(claude_cache)
    candidates = candidate_catalog(latest_codex, scope, claude_models, openai_models)
    if scorecard_path is None:
        from model_benchmarks import schedule_refresh
        schedule_refresh([i["model_facts"].get("api_model_id", i["model"]) for i in candidates.values()])
    rows = load_outcomes(outcomes_path)
    attach_evidence(candidates, kind, rows, load_scorecard(scorecard_path))
    selection_basis = "user"
    if provider and not user_model:
        # 호출자가 공급자를 고정한 경우 그 안에서만 고른다.
        candidates = {name: item for name, item in candidates.items() if item["provider"] == provider}
        if not candidates:
            raise ValueError(f"No {provider} model is available")
    if reviewer_of and kind != "review":
        raise ValueError("--reviewer-of applies only to review tasks")
    if reviewer_of and not user_model:
        candidates = {name: item for name, item in candidates.items() if item["provider"] != reviewer_of}
        if not candidates:
            raise ValueError("Independent provider is unavailable")
        if risk == "high" and max(item["capability"] for item in candidates.values()) < 3:
            raise ValueError("Independent provider lacks high-risk review capability")
    if user_model:
        if user_model == "fable" and "claude_fable" not in candidates:
            candidates.update({"claude_fable": candidate_catalog([], "large", claude_models)["claude_fable"]})
        model_choice = next((name for name, item in candidates.items()
                             if user_model in {name, item["model"]}), None)
        if not model_choice:
            raise ValueError("Requested model is not in the current visible catalog")
        model_confidence, model_source = None, "user"
        effort_choice, effort_confidence = fallback_effort(risk, scope), None
        effort_source = "fallback"
    else:
        try:
            secret = key if key is not None else get_key()
            if not secret:
                raise ValueError("missing Jev credential")
            model_choice, model_confidence, effort_choice, effort_confidence = ask_jev(
                task, kind, risk, scope, priority, candidates, secret)
            if model_confidence < MIN_CONFIDENCE:
                raise ValueError("low Jev model confidence")
            model_source = "jev"
            selection_basis = "jev_with_evidence"
            effort_source = "jev" if effort_confidence >= MIN_CONFIDENCE else "fallback"
            if effort_source == "fallback":
                effort_choice = evidence_effort(candidates[model_choice], kind, risk, scope, rows)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            model_choice, selection_basis = evidence_fallback(kind, risk, scope, candidates, rows)
            model_confidence = None
            effort_choice, effort_confidence = evidence_effort(candidates[model_choice], kind, risk, scope, rows), None
            model_source = effort_source = "fallback"
            error = str(exc) if str(exc) in {"missing Jev credential", "low Jev model confidence", "invalid Jev choice"} else type(exc).__name__
    checks = []
    chosen = candidates[model_choice]
    if model_source != "user" and risk == "high":
        minimum = 3 if kind in {"review", "architecture"} else 2
        if chosen["capability"] < minimum:
            alternatives = [name for name, item in candidates.items()
                            if item["provider"] == chosen["provider"] and item["capability"] >= minimum]
            if alternatives:
                model_choice = min(alternatives, key=lambda name: (candidates[name]["capability"], list(candidates).index(name)))
            else:
                if "claude_opus" not in candidates:
                    raise ValueError("No model meets the high-risk capability floor")
                model_choice = "claude_opus"
            chosen = candidates[model_choice]
            model_source = "safety_floor"
            checks.append(f"high risk needs capability {minimum}/4")
    if user_effort:
        effort_choice, effort_source = user_effort, "user"
    elif risk == "high" and EFFORT_ORDER.index(effort_choice) < EFFORT_ORDER.index("high"):
        effort_choice, effort_source = "high", "safety_floor"
        checks.append("high risk needs at least high effort")
    applied_effort = supported_effort(effort_choice, chosen["efforts"])
    if user_effort and applied_effort != user_effort:
        raise ValueError("Requested effort is unsupported by the selected model")
    if applied_effort is None:
        effort_source = "not_supported"
    if effort_choice != applied_effort and applied_effort:
        checks.append(f"effort clamped to {applied_effort} supported by model")
    result = {
        "provider": chosen["provider"], "model": chosen["model"],
        "effort": applied_effort, "model_source": model_source,
        "effort_source": effort_source, "scope": scope, "priority": priority,
        "catalog_source": catalog_source, "checks": checks,
        "claude_catalog_source": claude_source, "openai_catalog_source": openai_source,
        "codex_account": {"id": account["id"], "name": account["name"], "source": account["source"]},
    }
    if reviewer_of:
        result["reviewer_of"] = reviewer_of
    if model_confidence is not None:
        result["model_confidence"] = model_confidence
    if effort_confidence is not None:
        result["effort_confidence"] = effort_confidence
    if model_source == "fallback":
        result["fallback_reason"] = error
    if chosen["provider"] == "claude":
        result["model_facts"] = chosen["model_facts"]
        result["alias_resolution_note"] = "Claude Code alias may resolve differently by provider or override; verify at runtime"
        result["effort_delivery"] = "not_supported" if applied_effort is None else "recommendation_only_for_Agent_tool"
        result["dispatch"] = "Claude Agent supports per-call model; effort inherits session unless launched separately"
        if chosen["model"] == "fable":
            result["usage_credit_notice"] = "Fable may request additional usage-credit consent"
    else:
        result["model_facts"] = chosen["model_facts"]
        result["effort_delivery"] = "explicit_codex_flag"
        result["dispatch"] = "Codex plugin rescue: pass --model and --effort"
        tiers = chosen.get("speed_tiers", [])
        fast_kind = kind in {"scan", "mechanical", "research"}
        tier = ("ultrafast" if "ultrafast" in tiers and (priority == "speed" or fast_kind)
                else "fast" if "fast" in tiers and priority == "speed" else None)
        result["speed_tiers_available"] = tiers
        result["speed_tier"] = tier
        result["codex_flags"] = f'-c service_tier="{tier}"' if tier else None
    result["performance_evidence"] = chosen["performance_evidence"]
    official = chosen.get("official_performance", {})
    internal = chosen.get("internal_by_kind", {}).get(kind, outcome_summary([]))
    model_id = chosen["model_facts"].get("api_model_id", chosen["model"])
    comparable_internal = outcome_summary([r for r in rows if r["model"] == model_id and r["kind"] == kind
                                          and r["risk"] == risk and r["scope"] == scope])
    reasons = {"internal_outcomes": "같은 유형·위험도·범위 5건 이상, 무수정 통과율 우선 후 비용 비교",
               "official_benchmark": "내부 표본 부족, 모든 허용 후보에 공통인 외부 시험 점수 순위",
               "legacy_rules": "내부 표본·공식 비교 점수 부족, 기존 유형·위험도 규칙",
               "jev_with_evidence": "Jev가 공식 수치·내부 요약을 참고해 선택",
               "user": "사용자가 지정한 모델 우선"}
    result["evidence"] = {
        "basis": selection_basis,
        "comparison_unavailable_reason": chosen.get("comparison_unavailable_reason") or benchmark_comparison(candidates, kind)[1],
        "effort_benchmark_reason": chosen.get("effort_benchmark_reason"),
        "official_count": len(official.get("benchmarks", [])) + len(official.get("relative_findings", [])),
        "official_benchmark_count": len(official.get("benchmarks", [])),
        "internal_count": internal["count"],
        "internal_summary": internal,
        "comparable_internal": comparable_internal,
        "official_metadata_count": int(bool(official.get("metadata_source"))),
        "official_summary": official,
        "selection_reason": reasons[selection_basis] + ("; 안전 하한 적용" if model_source == "safety_floor" else ""),
        "minimum_outcomes": MIN_OUTCOMES,
        "comparison_note": "내부 순위는 같은 kind/risk/scope 5건 이상, 외부 점수는 모든 허용 후보의 공통 시험·버전·조건·추론 수준이 있어야 비교",
    }
    if scorecard_path is None:
        from model_benchmarks import read_cache
        benchmark_cache = read_cache()
        result["evidence"]["benchmark_cache"] = {k: benchmark_cache.get(k) for k in ("refreshed_at", "attempted_at", "warnings")}
        if benchmark_cache.get("warnings"):
            print(f"benchmark warning: {len(benchmark_cache['warnings'])} source issues; cached measurements retained (see evidence.benchmark_cache)", file=sys.stderr)
    result["optimization_status"] = "provisional_until_task_evaluation"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["record", "refresh-benchmarks"])
    parser.add_argument("--task", help="Short sanitized task summary, max 500 characters")
    parser.add_argument("--kind", choices=["scan", "mechanical", "writing", "research", "code", "review", "architecture", "coordination"])
    parser.add_argument("--risk", choices=["low", "medium", "high"])
    parser.add_argument("--scope", choices=["small", "medium", "large"], default="medium")
    parser.add_argument("--priority", choices=["balanced", "speed", "cost", "quality"], default="balanced")
    parser.add_argument("--reviewer-of", choices=["claude", "codex"], help="For review, require another provider")
    parser.add_argument("--provider", choices=["claude", "codex"], help="Restrict candidates to one provider")
    parser.add_argument("--user-model", help="Explicit Claude alias or visible Codex model name")
    parser.add_argument("--user-effort", choices=EFFORT_ORDER)
    parser.add_argument("--codex-account", help="Codex account id or name (default: AI_CODEX_ACCOUNT env, then registry default)")
    parser.add_argument("--list-models", action="store_true", help="Show current route candidates and their effort levels")
    parser.add_argument("--model", help="Exact versioned model ID for record")
    parser.add_argument("--effort", choices=(*EFFORT_ORDER, "none"))
    parser.add_argument("--verdict", choices=["pass", "pass_with_fixes", "fail"])
    parser.add_argument("--minutes", type=float)
    parser.add_argument("--cost-usd", type=float)
    parser.add_argument("--rework", type=int)
    parser.add_argument("--note", default="")
    parser.add_argument("--fallback-only", action="store_true", help="Skip Jev and evaluate evidence fallback deterministically")
    parser.add_argument("--force", action="store_true", help="Force benchmark refresh before weekly expiry")
    parser.add_argument("--benchmark-models", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.command == "refresh-benchmarks":
        from model_benchmarks import refresh_benchmarks
        print(json.dumps(refresh_benchmarks(args.benchmark_models, force=args.force), ensure_ascii=False))
        return 0
    if args.command == "record":
        if not all([args.task, args.kind, args.risk, args.model, args.verdict]):
            parser.error("record requires --task, --kind, --risk, --model and --verdict")
        try:
            row = record_outcome({"task": args.task, "kind": args.kind, "risk": args.risk,
                                  "scope": args.scope, "model": args.model,
                                  "effort": None if args.effort == "none" else args.effort,
                                  "verdict": args.verdict, "minutes": args.minutes,
                                  "cost_usd": args.cost_usd, "rework": args.rework, "note": args.note})
            print(json.dumps(row, ensure_ascii=False))
            return 0
        except (ValueError, OSError) as exc:
            print(f"outcome recording rejected: {exc}", file=sys.stderr)
            return 2
    try:
        account = resolve_codex_account(args.codex_account)
    except ValueError as exc:
        print(f"model routing rejected: {exc}", file=sys.stderr)
        return 2
    cache = Path(account["home"]) / "models_cache.json"
    if args.list_models:
        codex_models, catalog_source = codex_catalog(cache, account["home"])
        latest_codex = latest_codex_models(codex_models)
        openai_models, openai_source = openai_catalog(latest_codex, CACHE_DIR / "openai_models.json")
        claude_models, claude_source = claude_catalog(CACHE_DIR / "claude_models.json")
        candidates = candidate_catalog(latest_codex, "large", claude_models, openai_models)
        from model_benchmarks import schedule_refresh
        schedule_refresh([i["model_facts"].get("api_model_id", i["model"]) for i in candidates.values()])
        attach_evidence(candidates, args.kind or "code", load_outcomes(), load_scorecard())
        print(json.dumps({"codex_account": {"id": account["id"], "name": account["name"], "source": account["source"]},
                          "catalog_source": catalog_source,
                          "claude_catalog_source": claude_source,
                          "openai_catalog_source": openai_source,
                          "models": candidates}, ensure_ascii=False, indent=2))
        return 0
    if not args.task or not args.kind or not args.risk:
        parser.error("--task, --kind and --risk are required unless --list-models is used")
    try:
        print(json.dumps(route(args.task, args.kind, args.risk, args.scope, args.priority,
                               args.reviewer_of, args.provider, args.user_model, args.user_effort,
                               key="" if args.fallback_only else None,
                               codex_account=args.codex_account), ensure_ascii=False))
        return 0
    except ValueError as exc:
        print(f"model routing rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
