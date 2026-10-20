import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "jev_model_router.py"
SPEC = importlib.util.spec_from_file_location("jev_model_router", MODULE_PATH)
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


class JevModelRouterTests(unittest.TestCase):
    def setUp(self):
        refresh_patch = patch('model_benchmarks.schedule_refresh')
        refresh_patch.start()
        self.addCleanup(refresh_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        live_patch = patch.object(router, "live_codex_models", return_value=[])
        live_patch.start()
        self.addCleanup(live_patch.stop)
        claude_patch = patch.object(router, "claude_catalog", return_value=({}, "unavailable"))
        claude_patch.start()
        self.addCleanup(claude_patch.stop)
        openai_patch = patch.object(router, "openai_catalog", return_value=({}, "unavailable"))
        openai_patch.start()
        self.addCleanup(openai_patch.stop)
        for name, value in (("load_outcomes", []), ("load_scorecard", {})):
            evidence_patch = patch.object(router, name, return_value=value)
            evidence_patch.start()
            self.addCleanup(evidence_patch.stop)
        self.cache = Path(self.temp.name) / "models_cache.json"
        self.cache.write_text(json.dumps({
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "models": [
                {"slug": "gpt-6-astra", "description": "Frontier intelligence", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": e} for e in router.EFFORT_ORDER]},
                {"slug": "gpt-6-sol", "description": "Workhorse model", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": e} for e in router.EFFORT_ORDER]},
                {"slug": "gpt-5.6-terra", "description": "Balanced model", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": e} for e in router.EFFORT_ORDER[:-1]]},
                {"slug": "gpt-6-luna", "description": "Fast and affordable model", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": e} for e in router.EFFORT_ORDER[:-1]]},
                {"slug": "hidden", "description": "Workhorse model", "visibility": "hide"},
            ],
        }), encoding="utf-8")

    def test_catalog_includes_terra_and_effort_levels(self):
        catalog = router.candidate_catalog(router.visible_codex_models(self.cache), "large")
        self.assertEqual(catalog["codex_gpt_5_6_terra"]["efforts"][-1], "max")
        self.assertIn("claude_fable", catalog)
        self.assertNotIn("codex_hidden", catalog)

    def test_stale_catalog_excludes_codex(self):
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
        self.assertEqual(router.visible_codex_models(self.cache, now=now), [])

    def test_latest_version_per_role_preserves_terra_without_newer_terra(self):
        visible = [{"slug": slug} for slug in ("gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna",
                                               "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5")]
        self.assertEqual({m["slug"] for m in router.latest_codex_models(visible)},
                         {"gpt-6-sol", "gpt-6-luna", "gpt-5.6-terra"})

    def test_old_explicit_version_is_rejected(self):
        with patch.object(router, "live_codex_models", return_value=[
            {"slug": "gpt-6-sol", "description": "Workhorse model", "visibility": "list"},
            {"slug": "gpt-5.6-sol", "description": "Older coding model", "visibility": "list"},
        ]):
            with self.assertRaisesRegex(ValueError, "current visible catalog"):
                router.route("Fix a test", "code", "low", user_model="gpt-5.6-sol", cache_path=self.cache)

    def test_new_live_model_is_available_without_cache_update(self):
        added = {"slug": "gpt-7-sol", "description": "Workhorse model", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]}
        with patch.object(router, "live_codex_models", return_value=[added]):
            result = router.route("Check a simple patch", "code", "low", user_model="gpt-7-sol",
                                  user_effort="low", cache_path=self.cache)
        self.assertEqual((result["model"], result["catalog_source"]), ("gpt-7-sol", "app_server"))

    @patch.object(router, "ask_jev", return_value=("codex_gpt_6_luna", 0.9, "low", 0.9))
    def test_high_risk_review_raises_model_and_effort(self, _):
        result = router.route("Review access controls", "review", "high", cache_path=self.cache, key="test")
        self.assertEqual((result["model"], result["effort"]), ("gpt-6-astra", "high"))
        self.assertEqual((result["model_source"], result["effort_source"]), ("safety_floor", "safety_floor"))

    @patch.object(router, "ask_jev", return_value=("codex_gpt_6_sol", 0.4, "medium", 0.9))
    def test_low_model_confidence_falls_back(self, _):
        result = router.route("Fix failing tests", "code", "medium", cache_path=self.cache, key="test")
        self.assertEqual((result["model"], result["model_source"]), ("gpt-6-sol", "fallback"))
        self.assertEqual(result["fallback_reason"], "low Jev model confidence")

    @patch.object(router, "ask_jev", return_value=("claude_sonnet", 0.9, "ultra", 0.9))
    def test_unsupported_effort_is_clamped(self, _):
        result = router.route("Write a plan", "writing", "medium", cache_path=self.cache, key="test")
        self.assertEqual(result["effort"], "max")
        self.assertEqual(result["effort_delivery"], "recommendation_only_for_Agent_tool")

    def test_explicit_terra_and_effort_win(self):
        result = router.route("Review access controls", "review", "high", user_model="gpt-5.6-terra",
                              user_effort="xhigh", cache_path=self.cache, key="")
        self.assertEqual((result["model"], result["effort"], result["model_source"]),
                         ("gpt-5.6-terra", "xhigh", "user"))

    @patch.object(router, "ask_jev", return_value=("claude_opus", 0.9, "high", 0.9))
    def test_independent_review_excludes_original_provider(self, ask):
        result = router.route("Review another model's patch", "review", "high", reviewer_of="codex",
                              cache_path=self.cache, key="test")
        self.assertEqual(result["provider"], "claude")
        self.assertNotIn("codex_gpt_6_astra", ask.call_args.args[5])

    def test_missing_key_uses_visible_fallback(self):
        result = router.route("List related files", "scan", "low", scope="small", cache_path=self.cache, key="")
        self.assertEqual((result["model"], result["effort"]), ("haiku", None))
        self.assertEqual(result["fallback_reason"], "missing Jev credential")

    def test_secret_in_task_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "without credentials"):
            router.route("api_key: abc123", "scan", "low", cache_path=self.cache, key="test")

    def test_parses_new_claude_version_without_hardcoding_it(self):
        markdown = "\n".join([
            "| Feature | Claude Fable 5.2 | Claude Opus 5.5 | Claude Sonnet 5 | Claude Haiku 4.5 |",
            "| --- | --- | --- | --- | --- |",
            "| Description | Long work | Reasoning | Daily work | Fast work |",
            "| Claude API ID | `claude-fable-5-2` | `claude-opus-5-5` | `claude-sonnet-5` | `claude-haiku-4-5` |",
            "| Comparative latency | Slow | Moderate | Fast | Fastest |",
            "| [Pricing](https://example.com) | $10 / input MTok | $4 / input MTok | $2 / input MTok | $1 / input MTok |",
            "| Default effort | `high` | `medium` | `high` | Not supported |",
        ])
        models = router.parse_claude_overview(markdown)
        self.assertEqual(models["fable"]["api_model_id"], "claude-fable-5-2")
        self.assertEqual(models["opus"]["api_price"], "$4 / input MTok")

    def test_official_facts_are_provisional_not_performance_proof(self):
        facts = {"opus": {"api_model_id": "claude-opus-5-5", "api_price": "$4 / input MTok"}}
        with patch.object(router, "claude_catalog", return_value=(facts, "official_cache")):
            result = router.route("Review plan", "review", "high", user_model="opus", cache_path=self.cache)
        self.assertEqual(result["model_facts"]["api_model_id"], "claude-opus-5-5")
        self.assertEqual(result["optimization_status"], "provisional_until_task_evaluation")
        self.assertEqual(result["performance_evidence"], "vendor_claim_only")

    def test_openai_page_extracts_workload_strengths_and_price(self):
        markdown = """# GPT-7 Sol

Model ID: `gpt-7-sol`

Built for complex coding and agentic workflows.

`reasoning.effort` supports `low` and `high`.

## Model details

- 1,050,000 context window

## Pricing

| Input | $2 | 1M tokens |
| Output | $10 | 1M tokens |
"""
        facts = router.parse_openai_model_page(markdown, "gpt-7-sol")
        self.assertEqual(facts["strengths"], "Built for complex coding and agentic workflows.")
        self.assertEqual(facts["api_price"]["output"], "$10 / 1M tokens")
        with self.assertRaisesRegex(ValueError, "does not match"):
            router.parse_openai_model_page(markdown, "gpt-7-luna")

    def test_jev_receives_official_strengths_for_both_providers(self):
        candidates = {
            "claude_opus": {"provider": "claude", "model": "opus", "description": "Long work",
                            "capability": 3, "model_facts": {"strengths": "Agentic coding and knowledge work"},
                            "performance_evidence": "vendor_claim_only"},
            "codex_gpt_6_sol": {"provider": "codex", "model": "gpt-6-sol", "description": "Workhorse",
                                "capability": 2, "model_facts": {"strengths": "Complex coding and agentic workflows"},
                                "performance_evidence": "vendor_claim_only"},
        }
        answer = {"answers": {"model_choice": {"choice": "codex_gpt_6_sol", "confidence": 0.8},
                              "effort_choice": {"choice": "medium", "confidence": 0.8}}}
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps(answer).encode())

        with patch.object(router.urllib.request, "urlopen", side_effect=fake_urlopen):
            router.ask_jev("Review a patch", "review", "medium", "small", "balanced", candidates, "test")
        criteria = captured[0]["questions"]["model_choice"]["criteria"]
        self.assertIn("Agentic coding and knowledge work", criteria["claude_opus"])
        self.assertIn("Complex coding and agentic workflows", criteria["codex_gpt_6_sol"])


if __name__ == "__main__":
    unittest.main()
