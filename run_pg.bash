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

# Start the LLM server in the background so the tuning client can run after it.
python "./configuration recommender/LLM_server.py" &
LLM_PID=$!
trap 'kill $LLM_PID 2>/dev/null || true; wait $LLM_PID 2>/dev/null || true' EXIT

# Wait until the LLM server accepts connections on the configured port.
LLM_PORT=$(python -c "import configparser; c = configparser.ConfigParser(); c.read('./config.ini'); print(c.get('configuration recommender', 'LLM_server_port'))")
LLM_HOST=$(python -c "import configparser; c = configparser.ConfigParser(); c.read('./config.ini'); print(c.get('configuration recommender', 'LLM_server_IP', fallback='localhost'))")
echo "Waiting for LLM server on ${LLM_HOST}:${LLM_PORT}..."
for _ in $(seq 1 60); do
    if (echo > "/dev/tcp/${LLM_HOST}/${LLM_PORT}") 2>/dev/null; then
        echo "LLM server is up."
        break
    fi
    sleep 1
done

python "./configuration recommender/DB_client_pg.py"
