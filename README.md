# jev-model-router

Pick a Claude or Codex (OpenAI) model and a reasoning level for one bounded task.
Claude 또는 Codex 모델과 추론 수준을 작업 하나 단위로 고르는 CLI다.

The router reads the live model catalogs, asks the Jev decision API to choose,
and falls back to local rules when the API key is missing or the answer is weak.
It prints one JSON object that a coding agent can pass to its sub-agent call.

## How it chooses

1. Build candidates from the official Claude model list and the models your Codex account can use.
2. Attach evidence: official benchmark scores (only when every candidate shares the same test, version and effort) and your own recorded outcomes (only with 5 or more comparable results).
3. Ask Jev (`api.typesafe.ai`) to choose a model and an effort level. Answers below 0.55 confidence are rejected.
4. Without a key, or on any API error, use built-in rules by task kind, risk and scope. High-risk work always gets a safety floor.

## Requirements

- Python 3.10+ (standard library only)
- Optional: `codex` CLI logged in, to list Codex models available to your account
- Optional: a Jev API key, for API-based choice

## Usage

```bash
python3 jev_model_router.py --task "add unit tests for the parser" \
  --kind code --risk medium --scope medium --priority balanced
```

Useful options:

- `--provider claude|codex`: limit candidates to one provider
- `--reviewer-of claude|codex`: for review work, force the other provider
- `--fallback-only`: skip the API and use local rules only
- `--list-models`: print the current candidates

Record how a task went, so later choices can use your own results:

```bash
python3 jev_model_router.py record --task "parser tests" --kind code --risk medium \
  --scope medium --model claude-sonnet-5-5 --effort medium --verdict pass --minutes 12
```

Refresh the official benchmark cache: `python3 jev_model_router.py refresh-benchmarks`

## Configuration

| Item | Where |
|---|---|
| Jev API key | `TYPESAFE_API_KEY` env, or `~/.config/typesafe/api_key` |
| Data folder (outcomes, benchmark cache, scorecard) | `JEV_DATA_DIR` env, default `~/.local/share/jev-model-router` |
| Model catalog cache | `~/.cache/jev-model-router` |
| Several Codex accounts (optional) | `~/.config/ai-accounts/accounts.json`, see `examples/accounts.example.json`; pick with `--codex-account` or `AI_CODEX_ACCOUNT` |

Never commit your key, `accounts.json` or your outcomes file. `.gitignore` already blocks them.

## Privacy

- `--task` and `--note` go into the API request and your local outcomes file. Write short, sanitized summaries.
- The router rejects text that looks like a credential or an email address.
- Outcome files are created with mode `0600`.

## Notes

- Some output messages are in Korean.
- Benchmark numbers come from vendor pages and LiveBench public results, kept only in your local cache. This repository does not redistribute that data.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT
