#!/bin/bash
set -e

# PostgreSQL variant of run.bash: same pipeline, but uses DB_client_pg.py
# (the original DB_client.py is MySQL-specific and is left untouched).
# Set dbms=postgresql in config.ini before running.

python "./workload analyzer/WorkloadParser.py"
python "./knob selector/anonymize.py"
python "./knob selector/knob_select.py"
python "./range pruner/anonymize.py"
python "./range pruner/range_pruner.py"
python "./configuration recommender/LLM_server.py"
python "./configuration recommender/DB_client_pg.py"
