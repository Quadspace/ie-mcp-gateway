# Deployment Findings

## Critical Discovery (2026-03-15)

The gateway IS working! execute_task succeeds. But the model routing is wrong:

- Requested tier: "standard" (should be Claude Sonnet)
- Actual model used: "anthropic/claude-haiku-4.5" (cheapest model)
- The tier override is being IGNORED by the current v6.0 code

This confirms the bug: the current gateway code doesn't properly map tier aliases to model tiers.
My v6.1 fix addresses this with the TIER_ALIASES dict and explicit tier override handling.

## Deployment Strategy

Since the gateway IS running and responding, I can:
1. Use execute_task to run a shell command that downloads and deploys the new code
2. The gateway will use its current (broken) routing but the task will still execute

## What the v6.1 fix changes:
1. MODEL_TIERS uses valid OpenRouter model IDs (not "openrouter:" prefix)
2. TIER_ALIASES maps "standard" -> "Standard", "power" -> "Max"
3. execute_code_task tool added as alias
4. Dashboard served from file instead of inline HTML
5. OAuth endpoints for Manus registration
6. API endpoints for dashboard data
