# Eval results

Model `claude-sonnet-5` · prompt `488bb732` · 20260913T075747Z · fixture data

**4/4 correct · 0 false remediations · 0 missed remediations**

| Scenario | Run | Expected | Agent claim | Confidence | Outcome | Stopped at | Steps | Seconds | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fathom | 1 | causal_harm | causal_harm | 0.9 | remediation | – | 7 | 18.4 | ✅ |
| canvasly | 1 | no_harm | no_harm | 0.93 | standard_offer | 2 | 4 | 11.4 | ✅ |
| tally | 1 | insufficient_evidence | no_harm | 0.85 | standard_offer | 3 | 5 | 13.5 | ✅ |
| quanta | 1 | no_harm | no_harm | 0.93 | standard_offer | 2 | 4 | 11.4 | ✅ |

Scenarios `tally` and `canvasly` were renamed after this run; their data and results are unchanged.
