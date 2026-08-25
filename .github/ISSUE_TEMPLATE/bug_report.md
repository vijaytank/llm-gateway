---
name: Bug report
about: Report something broken in the gateway, adapter, UI, brain, or wizard
labels: bug
---

**Component**
Which part? `gateway/` (LiteLLM proxy) · `adapter/` (Anthropic↔OpenAI) · `ui/`
(web dashboard) · `brain/` (routing/scoring/circuits) · `wizard/` (setup) ·
docker stack · docs

**What happened?**
A clear description. If routing behaved unexpectedly, say what you expected
the fallback chain / score / circuit state to do.

**Steps to reproduce**

1.
2.
3.

**Environment**
- OS:
- Install method: docker compose (`core`/`full` profile) / bare metal + wizard
- Version (git SHA or tag):

**Logs**
Relevant output from `docker compose logs gateway|adapter|ui` (redact API keys
and the master key — never paste real secrets).

**Additional context**
Anything else, including your `gateway_config.yaml` `providers:` section with
keys/env names only.
