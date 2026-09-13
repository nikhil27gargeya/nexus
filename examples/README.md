# Captured runs

Real Claude Sonnet 5 runs on live Sentry, Datadog, Mixpanel and Stripe data, saved as the raw event stream the demo
page consumes. Each folder has:

- `events.jsonl`: every event of the run (steps, tool results summary, verdict, checks, outcome)
- `verify.txt`: output of `nexus verify`, which re-read each cited evidence ID directly from the live apps

| Run | Agent | Outcome | Verify |
| --- | --- | --- | --- |
| [`01-fathom-remediation`](01-fathom-remediation) | 7 steps, `causal_harm`, confidence 0.90, all 5 checks pass | Remediation | 3/3 |
| [`02-quanta-standard-offer`](02-quanta-standard-offer) | 4 steps, `no_harm`, confidence 0.95, stopped at check 2 | Standard offer | 1/1 |

Replay either one in the terminal with no keys:

```bash
cd backend
uv run python -c "import json,sys; [print(json.loads(l)['type'], json.loads(l)['data'].get('summary','')) for l in open(sys.argv[1])]" ../examples/01-fathom-remediation/events.jsonl
```

Or in the browser: `uv run nexus serve` and open http://127.0.0.1:8765. With no local runs in `replays/`, the page
replays these, without calling Claude.
