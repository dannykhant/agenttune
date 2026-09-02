# AgentTune [SIGMOD 2026]
### An Agent-Based Large Language Model Framework for Database Knob Tuning

- **Paper:** [AgentTune](https://dl.acm.org/doi/10.1145/3769758)
- **Source:** [intlyy/AgentTune](https://github.com/intlyy/AgentTune)

## PostgreSQL support

In addition to the original MySQL pipeline, this repository includes additive
PostgreSQL support. Set `dbms=postgresql` in `config.ini` (under
`[configuration recommender]`) and run `./run_pg.bash`. The PostgreSQL-specific
files are:

- `configuration recommender/DB_client_pg.py` — PostgreSQL DB client (the
  original `DB_client.py` is left untouched).
- `knob selector/candidate_knobs_pg` and `range pruner/knob_details_pg.json` —
  PostgreSQL knob dataset, generated from a published `pg_settings` dump by
  `knob selector/get_candidate_knobs/get_candidate_knobs_pg.py`.
- `configuration recommender/inner_metric_pg` — PostgreSQL inner metrics.

When `dbms=mysql` (the default), behavior is unchanged and only the original
MySQL code paths are used.

## Running with Docker Compose (PostgreSQL 17)

Both AgentTune and PostgreSQL run as containers; nothing needs to be installed
on the host besides Docker. AgentTune controls the postgres container through
the shared docker socket (`docker restart agenttune-pg` / `docker exec`), so no
SSH is required.

```bash
docker compose up -d --build          # start agenttune + postgres:17
docker compose exec agenttune bash    # shell inside the AgentTune container
```

Inside the container (workdir `/app`, the repo is bind-mounted so `config.ini`
edits apply live):

```bash
python "workload analyzer/WorkloadParser.py"                       # 1. workload features
python "knob selector/anonymize.py" && python "knob selector/knob_select.py"
python "range pruner/anonymize.py" && python "range pruner/range_pruner.py"
python "configuration recommender/LLM_server.py" &                # 2. LLM server
python "configuration recommender/DB_client_pg.py"                # 3. tuning loop
```

The provided `config.ini` already targets this Docker setup:

```ini
[configuration recommender]
dbms=postgresql
DB_RestartMethod=docker
PG_ContainerName=agenttune-pg
PG_RestartCommand=docker restart agenttune-pg
DB_Host=postgres        # compose service name
DB_Port=5432
DB_User=postgres
DB_Name=agenttune
DB_Password=agenttune   # matches .env default
```

Overrides (password, database, ports) go in `.env` — copy `.env.example`.
Set the LLM `api_key`/`base_url`/`model` in `config.ini` (OpenAI-compatible;
Google Gemini works via `base_url=https://generativelanguage.googleapis.com/v1beta/openai/`).
For benchmarks on the host DB set `QueryDir` and `benchmark=SYSBENCH|TPCC|JOB|TPCDS`.
