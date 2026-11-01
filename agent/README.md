# claas-agent — supplier node agent

The program a GPU owner runs to join the marketplace. **v0 is deliberately
execution-free**: it identifies itself, reports hardware, heartbeats, and
listens for jobs. Actually running buyer workloads is a separate, reviewed
step (see the EXECUTION PLAN in `claas_agent.py` and ROADMAP.md §6).

## Run (v0)

```bash
pip install requests
export CLAAS_SUPABASE_URL=https://YOUR-PROJECT.supabase.co
export CLAAS_SUPABASE_ANON_KEY=your-anon-key
export CLAAS_AGENT_EMAIL=you@example.com        # a Supabase Auth account
export CLAAS_AGENT_PASSWORD=...

python3 claas_agent.py --name "Dorm Rig 1" --region us-east
python3 claas_agent.py --list-gpus              # check detection without joining
```

The agent signs in with email+password and receives a JWT — all writes go
through RLS, so it can only ever touch its own node and its own assignments.
`CLAAS_SERVICE_KEY` overrides this for development (bypasses RLS — dev only).

## What v0 does NOT do (on purpose)

* Run containers (the `run_job` stub just logs).
* Open tunnels (`cloudflared` integration comes with execution).
* Touch money (wallets are service-role only).

The v0→v1 checklist lives in ROADMAP.md §6 — don't skip the review.
