#!/bin/bash
set -euo pipefail

./recreate_pg_db.bash
./run_pg.bash