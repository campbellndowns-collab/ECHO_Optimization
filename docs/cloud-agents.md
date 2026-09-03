# Cloud Agents on this repository

This repo is set up so **several Cloud Agents can run at the same time** on GitHub.
Each agent gets its own VM, its own git branch, and the same install defined in
[`.cursor/environment.json`](../.cursor/environment.json).

## What was added for agents

| Path | Role |
|---|---|
| `.cursor/environment.json` | Repo-managed Cloud Agent environment (highest precedence) |
| `.cursor/Dockerfile` | Python 3.12 + Node 20 + compilers |
| `scripts/cloud-agent-install.sh` | Idempotent `pip` + `npm ci` |
| `scripts/cloud-agent-run-api.sh` | FastAPI on port 43127 |
| `scripts/cloud-agent-run-web.sh` | Next.js on port 43128 |
| `.cursor/rules/echo-optimizer.mdc` | Shared agent rules (formulas, private data, branches) |

## One-time: connect GitHub

1. [cursor.com/dashboard](https://cursor.com/dashboard) → **Settings → Integrations**.
2. **Connect** GitHub (or **Manage Connections**).
3. Grant the Cursor GitHub App access to `campbellndowns-collab/ECHO_Optimization`.
4. Paid Cursor plan; read-write on the repo.

Connecting the app does not attach GitHub to an already-running “start from scratch”
agent. Start **new** agents from this GitHub repository after the code is on `main`.

## Start several agents at once

1. Push this project to GitHub `main` (see below if this tree is still only local).
2. Open [cursor.com/agents](https://cursor.com/agents).
3. Start an agent, select **`campbellndowns-collab/ECHO_Optimization`**, branch **`main`**.
4. Repeat for each parallel task. Every run is an isolated VM.

Give each agent a **narrow task** that does not heavily overlap files (for example
one agent on the UI, one on the API, one on docs). They will each open a
`cursor/…` branch and can open separate PRs.

Do **not** point two agents at the same branch. Do **not** tell an agent to
force-push `main`.

### From Cursor desktop

Under the agent input, choose **Cloud**, pick this GitHub repo, send the prompt.

### From GitHub

On a PR or issue, comment `@cursor` (after the GitHub app is installed).

## After the first GitHub agent

On [Cloud Agents environments](https://cursor.com/dashboard/cloud-agents), you can
save a snapshot / enable **Builds** so later agents skip a cold `pip`/`npm` install.
That is optional. The committed `environment.json` is enough for agents to boot.

## Private optimizer data

Public GitHub must not contain the warehouse or PyThrust motor/APC files.

Without this data on the VM, agents can still edit code, run frontend lint, and
start the API. **Catalog, `/evaluate`, and optimization jobs will not be numerically
useful** until you copy:

```text
legacy/streamlit/pythrust/                          # setuav-pythrust 0.2.2, not PyPI
legacy/streamlit/component_data/component_warehouse.sqlite
legacy/streamlit/component_data/propulsion_performance_cache.sqlite   # optional
```

How to give agents that data (pick one):

1. Keep a **private** GitHub repo with the datasets and add it under Cloud Agent
   secrets / a private snapshot you control — do not merge it into the public repo.
2. After an agent starts, upload a zip of those paths into the VM (same layout as
   local `PyThrust_Cursor`).
3. Build a Cloud Agent snapshot on a machine that already has the data, then reuse
   that snapshot for later agents.

Set `DRONE_OPTIMIZER_DATA_ROOT` to the directory that contains `component_data/`
and `pythrust/` if it is not `legacy/streamlit`.

## Local check of the install script

```bash
bash scripts/cloud-agent-install.sh
bash scripts/cloud-agent-install.sh   # must succeed a second time
```
