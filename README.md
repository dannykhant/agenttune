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

## Baseline integrity: run-blocking fixes only

This repository is used as the AgentTune SOTA baseline. The only deviations from
the upstream code are **bug fixes required to execute the published pipeline**
(PostgreSQL path). No tuning-strategy, prompt, or evaluation-criteria changes
were made:

- `configuration recommender/DB_client_pg.py`
  - `get_current_knob()`: report time knobs (e.g. `checkpoint_timeout`) in the
    same millisecond unit space used by the pruned ranges, so current values and
    ranges stay consistent.
  - `set_knobs_and_restart()`: convert normalized values back to PostgreSQL
    native units and clamp to `pg_settings` bounds before `ALTER SYSTEM SET`
    (otherwise `checkpoint_timeout` in ms crashes with "900000 s is outside the
    valid range").
  - `test_by_sysbench()`: auto-`cleanup`/`prepare` the sysbench tables when they
    are missing (otherwise the benchmark segfaults and reports 0 throughput).
- `configuration recommender/LLM_server.py`: add a monotonic tiebreaker to the
  history heap so equal-throughput entries don't crash `heapq`.
- `configuration recommender/config_rank.py`: when the LLM omits a knob from a
  recommendation, fill it with a numeric default (midpoint of its pruned range,
  or `special_value`) instead of the whole range-spec dict, which crashed the
  ranking average with `float + dict`.
- `range pruner/range_pruner.py`: clamp LLM-proposed knob ranges into the legal
  bounds from `knob_details` (both JSON and markdown parse paths) so the database
  is never set to unusable minimums.

These changes do not alter AgentTune's tuning algorithm or how configurations
are evaluated; they only make the baseline runnable as described in the paper.
