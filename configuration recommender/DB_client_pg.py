import requests
import json
import psycopg2
import os
import sys
import time
import re
import paramiko
import configparser

config = configparser.ConfigParser()
config.read('./config.ini')

import sys as _sys
_dbms = next((a.split('=', 1)[1] for a in _sys.argv if a.startswith('--dbms=')), None)
if _dbms and config.has_section(_dbms):
    _src = config[_dbms]
    for _sec in config.sections():
        if _sec in ('postgres', 'mysql'):
            continue
        for _k in list(config[_sec]):
            if _k in _src:
                config[_sec][_k] = _src[_k]

db_ip = config['configuration recommender']['DB_IP']
ip_password = config['configuration recommender']['DB_IP_Password']
db_config = {
    'user': config['configuration recommender']['DB_User'],
    'password': config['configuration recommender']['DB_Password'],
    'host': config['configuration recommender']['DB_Host'],
    'dbname': config['configuration recommender']['DB_Name'],
    'port': config['configuration recommender']['DB_Port']
}

pg_data_dir = config['configuration recommender']['PG_DataDir']
pg_restart_command = config['configuration recommender']['PG_RestartCommand']

# how to run commands on the PostgreSQL host: ssh (default) or docker
# (docker exec / docker restart against the sibling postgres container)
db_restart_method = config.get('configuration recommender', 'DB_RestartMethod', fallback='ssh')
pg_container_name = config.get('configuration recommender', 'PG_ContainerName', fallback='agenttune-pg')

dbms = config.get('configuration recommender', 'dbms', fallback='mysql')
if dbms == 'postgresql':
    candidate_knobs_path = "./knob selector/candidate_knobs_pg"
else:
    candidate_knobs_path = config['knob selector']['candidate_knobs']

with open(candidate_knobs_path, 'r') as f:
    original = json.load(f)
    original_keys = list(original.keys())

with open(config['range pruner']['output_file'], 'r') as f:
    selected_knobs = json.load(f)

def get_current_metric():

    conn = psycopg2.connect(**db_config)
    cursor = conn.cursor()

    cursor.execute("SHOW server_version_num")
    server_version = int(cursor.fetchone()[0])

    # PostgreSQL 17 moved the checkpoint counters out of pg_stat_bgwriter into
    # the new pg_stat_checkpointer view, renaming the columns.
    if server_version >= 170000:
        checkpoint_source = "pg_stat_checkpointer"
        cp_timed, cp_req, cp_write, cp_sync, cp_buffers = \
            "num_timed", "num_requested", "write_time", "sync_time", "buffers_written"
        # buffers_backend / buffers_backend_fsync were removed in PostgreSQL 17
        bgwriter_backend = ""
    else:
        checkpoint_source = "pg_stat_bgwriter"
        cp_timed, cp_req, cp_write, cp_sync, cp_buffers = \
            "checkpoints_timed", "checkpoints_req", "checkpoint_write_time", "checkpoint_sync_time", "buffers_checkpoint"
        bgwriter_backend = """
    UNION ALL SELECT 'buffers_backend', buffers_backend FROM pg_stat_bgwriter
    UNION ALL SELECT 'buffers_backend_fsync', buffers_backend_fsync FROM pg_stat_bgwriter"""

    sql = """
    SELECT 'xact_commit', sum(xact_commit) FROM pg_stat_database
    UNION ALL SELECT 'xact_rollback', sum(xact_rollback) FROM pg_stat_database
    UNION ALL SELECT 'blks_read', sum(blks_read) FROM pg_stat_database
    UNION ALL SELECT 'blks_hit', sum(blks_hit) FROM pg_stat_database
    UNION ALL SELECT 'tup_returned', sum(tup_returned) FROM pg_stat_database
    UNION ALL SELECT 'tup_fetched', sum(tup_fetched) FROM pg_stat_database
    UNION ALL SELECT 'tup_inserted', sum(tup_inserted) FROM pg_stat_database
    UNION ALL SELECT 'tup_updated', sum(tup_updated) FROM pg_stat_database
    UNION ALL SELECT 'tup_deleted', sum(tup_deleted) FROM pg_stat_database
    UNION ALL SELECT 'temp_files', sum(temp_files) FROM pg_stat_database
    UNION ALL SELECT 'temp_bytes', sum(temp_bytes) FROM pg_stat_database
    UNION ALL SELECT 'deadlocks', sum(deadlocks) FROM pg_stat_database
    UNION ALL SELECT 'checkpoints_timed', {cp_timed} FROM {checkpoint_source}
    UNION ALL SELECT 'checkpoints_req', {cp_req} FROM {checkpoint_source}
    UNION ALL SELECT 'checkpoint_write_time', {cp_write} FROM {checkpoint_source}
    UNION ALL SELECT 'checkpoint_sync_time', {cp_sync} FROM {checkpoint_source}
    UNION ALL SELECT 'buffers_checkpoint', {cp_buffers} FROM {checkpoint_source}
    UNION ALL SELECT 'buffers_clean', buffers_clean FROM pg_stat_bgwriter
    UNION ALL SELECT 'maxwritten_clean', maxwritten_clean FROM pg_stat_bgwriter{bgwriter_backend}
    UNION ALL SELECT 'buffers_alloc', buffers_alloc FROM pg_stat_bgwriter
    UNION ALL SELECT 'seq_scan', sum(seq_scan) FROM pg_stat_user_tables
    UNION ALL SELECT 'idx_scan', sum(idx_scan) FROM pg_stat_user_tables
    UNION ALL SELECT 'n_tup_ins', sum(n_tup_ins) FROM pg_stat_user_tables
    UNION ALL SELECT 'n_tup_upd', sum(n_tup_upd) FROM pg_stat_user_tables
    UNION ALL SELECT 'n_tup_del', sum(n_tup_del) FROM pg_stat_user_tables
    UNION ALL SELECT 'n_tup_hot_upd', sum(n_tup_hot_upd) FROM pg_stat_user_tables
    UNION ALL SELECT 'n_live_tup', sum(n_live_tup) FROM pg_stat_user_tables
    UNION ALL SELECT 'n_dead_tup', sum(n_dead_tup) FROM pg_stat_user_tables
    UNION ALL SELECT 'idx_scan_indexes', sum(idx_scan) FROM pg_stat_user_indexes
    UNION ALL SELECT 'idx_tup_read', sum(idx_tup_read) FROM pg_stat_user_indexes
    UNION ALL SELECT 'idx_tup_fetch', sum(idx_tup_fetch) FROM pg_stat_user_indexes
    UNION ALL SELECT 'wal_records', wal_records FROM pg_stat_wal
    UNION ALL SELECT 'wal_fpi', wal_fpi FROM pg_stat_wal
    UNION ALL SELECT 'wal_bytes', wal_bytes FROM pg_stat_wal
    UNION ALL SELECT 'wal_buffers_full', wal_buffers_full FROM pg_stat_wal
    UNION ALL SELECT 'wal_write', wal_write FROM pg_stat_wal
    UNION ALL SELECT 'wal_sync', wal_sync FROM pg_stat_wal
    UNION ALL SELECT 'lock_count', count(*) FROM pg_locks
    UNION ALL SELECT 'lock_waits', count(*) FROM pg_locks WHERE NOT granted
    """.format(checkpoint_source=checkpoint_source, cp_timed=cp_timed, cp_req=cp_req,
               cp_write=cp_write, cp_sync=cp_sync, cp_buffers=cp_buffers,
               bgwriter_backend=bgwriter_backend)
    cursor.execute(sql)
    result = cursor.fetchall()
    knobs = {}
    for name, value in result:
        try:
            knobs[name] = int(value)
        except (TypeError, ValueError):
            knobs[name] = 0

    cursor.close()
    conn.close()
    return knobs


def get_current_knob():

    conn = psycopg2.connect(**db_config)
    cursor = conn.cursor()

    parameters = []
    for key in selected_knobs.keys():
        index = int(key.replace("knob", "")) - 1
        param_name = original_keys[index]
        parameters.append(param_name)

    sql = "SELECT name, setting, unit, vartype FROM pg_settings WHERE name = ANY(%s)"
    cursor.execute(sql, (parameters,))
    result = cursor.fetchall()

    units = {
        'B': 1,
        'kB': 1024,
        'MB': 1024 ** 2,
        'GB': 1024 ** 3,
        'TB': 1024 ** 4,
        '8kB': 8 * 1024,
        '16kB': 16 * 1024,
        '32kB': 32 * 1024,
        '64kB': 64 * 1024,
        '128kB': 128 * 1024,
        '256kB': 256 * 1024,
        '512kB': 512 * 1024,
        'ms': 1,
        's': 1000,
        'min': 60000,
        'us': 1,
    }

    knobs = {}
    for name, setting, unit, vartype in result:
        if vartype in ('integer', 'real'):
            try:
                value = int(setting)
            except ValueError:
                value = float(setting)
            if unit and unit in units:
                knobs[name] = value * units[unit]
            else:
                knobs[name] = value
        else:
            knobs[name] = setting

    cursor.close()
    conn.close()

    json_data = json.dumps(knobs, indent=4)
    print(json_data)
    return knobs


def get_knobs_detail():
    f = open(config['range pruner']['output_file'], 'r')
    content = json.load(f)

    result = {}
    count = 0
    for i in content.keys():
        result[i] = content[i]
        count += 1

    return result


def get_knob_contexts():
    """Map knob name -> pg_settings.context (postmaster/user/sighup)."""
    conn = psycopg2.connect(**db_config)
    cursor = conn.cursor()
    cursor.execute("SELECT name, context FROM pg_settings")
    result = dict(cursor.fetchall())
    cursor.close()
    conn.close()
    return result


def get_knob_native_bounds():
    """Map knob name -> {unit, min, max} as reported by pg_settings.

    pg_settings stores values/bounds in the knob's native unit (e.g. seconds
    for checkpoint_timeout, kB for work_mem), while the tuning pipeline works
    in a normalized space (bytes for memory, ms for time). The apply step needs
    these native bounds to convert back and clamp before ALTER SYSTEM SET.
    """
    conn = psycopg2.connect(**db_config)
    cursor = conn.cursor()
    cursor.execute("SELECT name, unit, min_val, max_val FROM pg_settings")
    result = {}
    for name, unit, min_val, max_val in cursor.fetchall():
        def to_num(v):
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return int(f) if f == int(f) else f
        result[name] = {'unit': unit, 'min': to_num(min_val), 'max': to_num(max_val)}
    cursor.close()
    conn.close()
    return result


def run_remote_command(command):
    """Run a shell command on the PostgreSQL host.

    ssh (default): wraps in `sshpass -p {pwd} ssh {db_ip} "<command>"`.
    docker: `command` is already a full docker CLI invocation (e.g.
        `docker exec -u postgres agenttune-pg rm -f ...` or
        `docker restart agenttune-pg`) run against the shared socket.
    """
    if db_restart_method == 'docker':
        return os.system(command)
    head_command = 'sshpass -p {} ssh {} '.format(ip_password, db_ip)
    return os.system(head_command + '"{}"'.format(command))


def wait_for_db(timeout=90):
    """Block until the database accepts connections (e.g. after a restart)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(**db_config)
            conn.close()
            return True
        except Exception:
            time.sleep(2)
    return False


def set_knobs_and_restart(knob):
    # load knobs
    temp_config = {}
    knobs_detail = get_knobs_detail()
    for key in knobs_detail.keys():
        if key in knob.keys():
            if knobs_detail[key]['type'] in ('integer', 'real'):
                temp_config[key] = knob.get(key)
            elif knobs_detail[key]['type'] == 'enum':
                value = str(knob.get(key))
                if value in knobs_detail[key]['enum_values']:
                    temp_config[key] = value
                else:
                    print(f"Warning: {value} not found in enum values for {key}")

    # resolve anonymized knob ids to real parameter names
    named_config = {}
    for knobs in temp_config:
        index = int(knobs.replace("knob", "")) - 1
        knob_name = original_keys[index]
        named_config[knob_name] = temp_config[knobs]

    contexts = get_knob_contexts()
    needs_restart = any(contexts.get(name) == 'postmaster' for name in named_config)

    # The pipeline works in a normalized unit space (bytes for memory, ms for
    # time). pg_settings reports values in native units (kB, s, ...), so the
    # normalized values must be converted back before ALTER SYSTEM SET, and
    # clamped to the native bounds as a safety net against out-of-range LLM
    # recommendations (e.g. checkpoint_timeout 900000 ms -> 900 s).
    units = {
        'B': 1,
        'kB': 1024,
        'MB': 1024 ** 2,
        'GB': 1024 ** 3,
        'TB': 1024 ** 4,
        '8kB': 8 * 1024,
        '16kB': 16 * 1024,
        '32kB': 32 * 1024,
        '64kB': 64 * 1024,
        '128kB': 128 * 1024,
        '256kB': 256 * 1024,
        '512kB': 512 * 1024,
        'ms': 1,
        's': 1000,
        'min': 60000,
        'us': 1,
    }
    native_bounds = get_knob_native_bounds()
    for name, value in list(named_config.items()):
        if not isinstance(value, (int, float)):
            continue
        info = native_bounds.get(name)
        unit = info['unit'] if info else None
        factor = units.get(unit, 1)
        native_value = value / factor
        if info is not None and info['min'] is not None and info['max'] is not None:
            native_value = min(max(native_value, info['min']), info['max'])
        if isinstance(native_value, float) and native_value.is_integer():
            native_value = int(native_value)
        named_config[name] = native_value

    conn = psycopg2.connect(**db_config)
    conn.autocommit = True
    cursor = conn.cursor()
    for name, value in named_config.items():
        cursor.execute("ALTER SYSTEM SET {} = %s".format(name), (value,))
    cursor.close()
    conn.close()

    time.sleep(10)

    if needs_restart:
        state = run_remote_command(pg_restart_command)
        if state != 0:
            print('database restarting failed')
            return -1
        wait_for_db()
        print('database has been restarted')
    else:
        conn = psycopg2.connect(**db_config)
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("SELECT pg_reload_conf()")
        cursor.close()
        conn.close()
        print('configuration reloaded')

    return 0


def reset_knobs():
    """Restore default configuration by removing postgresql.auto.conf and restarting."""
    if db_restart_method == 'docker':
        remove_command = 'docker exec -u postgres {} rm -f {}/postgresql.auto.conf'.format(pg_container_name, pg_data_dir)
    else:
        remove_command = 'rm -f {}/postgresql.auto.conf'.format(pg_data_dir)
    run_remote_command(remove_command)
    state = run_remote_command(pg_restart_command)
    if state != 0:
        print('database restarting failed')
        return -1
    wait_for_db()
    print('database has been restarted')
    return 0


def run_sql_queries(query_dir, cursor):
    """Run all .sql files under query_dir; return total elapsed seconds."""
    query_files = [os.path.join(query_dir, f) for f in os.listdir(query_dir) if f.endswith('.sql')]
    total_time = 0
    for query_file in query_files:
        print(f"Running {query_file}")
        start = time.time()
        with open(query_file, 'r') as f:
            cursor.execute(f.read())
        conn = cursor.connection
        conn.commit()
        elapsed_time = time.time() - start
        print(f"Time taken: {elapsed_time:.2f} seconds")
        total_time += elapsed_time
    return total_time


def test_by_job(knob):
    temp_config = {}
    knobs_detail = get_knobs_detail()
    for key in knobs_detail.keys():
        if key in knob.keys():
            if knobs_detail[key]['type'] in ('integer', 'real'):
                temp_config[key] = knob.get(key)
            elif knobs_detail[key]['type'] == 'enum':
                value = str(knob.get(key))
                if value in knobs_detail[key]['enum_values']:
                    temp_config[key] = value
                else:
                    print(f"Warning: {value} not found in enum values for {key}")

    state = set_knobs_and_restart(knob)
    if state != 0:
        return -1

    conn = psycopg2.connect(**db_config)
    cursor = conn.cursor()
    query_dir = config['configuration recommender'].get('QueryDir', '')
    total_time = run_sql_queries(query_dir, cursor)
    cursor.close()
    conn.close()
    return total_time


def test_by_tpcc(knob):
    temp_config = {}
    knobs_detail = get_knobs_detail()
    for key in knobs_detail.keys():
        if key in knob.keys():
            if knobs_detail[key]['type'] in ('integer', 'real'):
                temp_config[key] = knob.get(key)
            elif knobs_detail[key]['type'] == 'enum':
                value = str(knob.get(key))
                if value in knobs_detail[key]['enum_values']:
                    temp_config[key] = value
                else:
                    print(f"Warning: {value} not found in enum values for {key}")

    state = set_knobs_and_restart(knob)
    if state != 0:
        return 0

    log_file = './configuration recommender/log/' + '{}.log'.format(int(time.time()))
    command = 'pgbench -h {} -p {} -U {} -d {} -c 32 -j 8 -T 120 -n'.format(
        db_config['host'],
        db_config['port'],
        db_config['user'],
        db_config['dbname']
    )
    os.environ['PGPASSWORD'] = db_config['password']
    os.system(command + ' > "{}" '.format(log_file))

    tps = 0
    with open(log_file, 'r') as f:
        for line in f:
            if 'tps' in line and 'including connections establishing' in line:
                try:
                    tps = float(line.split('=')[1].split()[0])
                    break
                except (IndexError, ValueError):
                    pass
    return tps


def test_by_sysbench(knob):
    temp_config = {}
    knobs_detail = get_knobs_detail()
    for key in knobs_detail.keys():
        if key in knob.keys():
            if knobs_detail[key]['type'] in ('integer', 'real'):
                temp_config[key] = knob.get(key)
            elif knobs_detail[key]['type'] == 'enum':
                value = str(knob.get(key))
                if value in knobs_detail[key]['enum_values']:
                    temp_config[key] = value
                else:
                    print(f"Warning: {value} not found in enum values for {key}")

    state = set_knobs_and_restart(knob)
    if state != 0:
        return 0

    tables = 50
    conn = psycopg2.connect(**db_config)
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM information_schema.tables "
                   "WHERE table_schema = 'public' AND table_name LIKE 'sbtest%'")
    sbtest_count = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    sysbench_args = '--db-driver=pgsql --threads=32 --pgsql-host={} --pgsql-port={} --pgsql-user={} --pgsql-password={} --pgsql-db={} --tables={} --table-size=1000000'.format(
        db_config['host'], db_config['port'], db_config['user'],
        db_config['password'], db_config['dbname'], tables)

    if sbtest_count < tables:
        print('sysbench tables missing, preparing {} tables'.format(tables))
        os.system('sysbench {} oltp_read_write cleanup > /dev/null 2>&1'.format(sysbench_args))
        os.system('sysbench {} oltp_read_write prepare > "{}" 2>&1'.format(
            sysbench_args, './configuration recommender/log/sysbench_prepare.log'))

    log_file = './configuration recommender/log/' + '{}.log'.format(int(time.time()))
    command_run = 'sysbench {} --time=120 --report-interval=60 oltp_read_write run'.format(sysbench_args)
    os.system(command_run + ' > "{}" '.format(log_file))

    qps = sum([float(line.split()[8]) for line in open(log_file, 'r').readlines() if 'qps' in line][-int(120 / 60):]) / (int(120 / 60))
    tps = float(qps / 20.0)
    return tps


def test_by_tpcds(knob):
    temp_config = {}
    knobs_detail = get_knobs_detail()
    for key in knobs_detail.keys():
        if key in knob.keys():
            if knobs_detail[key]['type'] in ('integer', 'real'):
                temp_config[key] = knob.get(key)
            elif knobs_detail[key]['type'] == 'enum':
                value = str(knob.get(key))
                if value in knobs_detail[key]['enum_values']:
                    temp_config[key] = value
                else:
                    print(f"Warning: {value} not found in enum values for {key}")

    state = set_knobs_and_restart(knob)
    if state != 0:
        return -1

    conn = psycopg2.connect(**db_config)
    cursor = conn.cursor()
    query_dir = config['configuration recommender'].get('QueryDir', '')
    total_time = run_sql_queries(query_dir, cursor)
    cursor.close()
    conn.close()
    return total_time


def unknown_benchmark(name):
    print(f"Unknown benchmark: {name}")


if __name__ == "__main__":

    knob = get_current_knob()
    throughput = test_by_sysbench(knob)
    metric = get_current_metric()
    data = {
        "knob": knob,
        "throughput": throughput,
        "metric": metric
    }
    data1 = [data]

    url = 'http://{}:{}/process'.format(config['configuration recommender']['LLM_server_IP'], config['configuration recommender']['LLM_server_port'])

    response = requests.post(url, json=data1)

    result = response.json()
    print(result)

    iteration = 0
    best_knob = []
    best_metric = []
    best_throughput = 0
    while iteration < int(config['configuration recommender']['iteration']):
        data_list = []
        for knob in result:
            if not knob:
                continue
            benchmark = config['configuration recommender']['benchmark'].strip().upper()
            benchmark_switch = {
                "SYSBENCH": test_by_sysbench,
                "TPCC": test_by_tpcc,
                "JOB": test_by_job,
                "TPCDS": test_by_tpcds
            }
            throughput = benchmark_switch.get(benchmark, lambda: unknown_benchmark(benchmark))(knob)
            if throughput == 0:
                metric = []
            else:
                metric = get_current_metric()
            data = {
                "knob": knob,
                "throughput": throughput,
                "metric": metric
            }
            data_list.append(data)
            with open('./configuration recommender/record/benmark_history', "a") as f:
                json.dump(data, f, indent=4)
                f.close()
            if throughput > best_throughput:
                best_knob = knob
                best_metric = metric
                best_throughput = throughput

        url = 'http://{}:{}/process'.format(config['configuration recommender']['LLM_server_IP'], config['configuration recommender']['LLM_server_port'])
        response = requests.post(url, json=data_list)

        result = response.json()
        iteration = iteration + 1

    with open("./configuration recommender/record/optimal configuration", "w", encoding="utf-8") as f:
        print("best_knob:", best_knob, file=f)
        print("best_throughput:", best_throughput, file=f)
