import json
import re
import configparser

config = configparser.ConfigParser()
config.read('./config.ini')

UNITS = {
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
}

# knobs whose pg_settings min/max are unbounded (NULL) or unusable need a
# practical hardware-aware bound; default to a sane fallback when absent.
# keyed by knob name -> (min, max) in the knob's native unit space.
HARDWARE_BOUNDS = {
    'random_page_cost': (0.0, 100.0),
    'seq_page_cost': (0.0, 100.0),
    'cpu_tuple_cost': (0.0, 10.0),
    'cpu_index_tuple_cost': (0.0, 10.0),
    'cpu_operator_cost': (0.0, 10.0),
    'parallel_tuple_cost': (0.0, 10.0),
    'parallel_setup_cost': (0.0, 10000.0),
    'effective_cache_size': (1, 2147483647),
    'bgwriter_delay': (10, 10000),
    'bgwriter_lru_multiplier': (0.0, 10.0),
    'bgwriter_lru_maxpages': (0, 1000),
    'vacuum_cost_delay': (0, 100),
    'vacuum_cost_limit': (1, 10000),
    'autovacuum_naptime': (1, 3600),
    'autovacuum_vacuum_scale_factor': (0.0, 1.0),
    'autovacuum_analyze_scale_factor': (0.0, 1.0),
    'autovacuum_max_workers': (1, 32),
    'default_statistics_target': (1, 10000),
    'wal_writer_delay': (1, 10000),
    'deadlock_timeout': (1, 2147483647),
    'commit_delay': (0, 100000),
    'commit_siblings': (0, 1000),
    'join_collapse_limit': (1, 20),
    'from_collapse_limit': (1, 20),
    'geqo_threshold': (2, 10000),
    'geqo_effort': (1, 10),
    'geqo_pool_size': (0, 1000),
    'geqo_selection_bias': (1.5, 2.0),
    'max_parallel_workers': (0, 1024),
    'max_worker_processes': (0, 262143),
    'max_connections': (1, 262143),
    'max_wal_senders': (0, 1024),
    'max_replication_slots': (0, 1024),
    'min_wal_size': (80, 1073741824),
    'max_wal_size': (80, 2147483648),
    'checkpoint_timeout': (30, 3600),
    'checkpoint_completion_target': (0.0, 1.0),
    'checkpoint_warning': (0, 3600),
    'checkpoint_flush_after': (0, 256),
    'backend_flush_after': (0, 256),
    'bgwriter_flush_after': (0, 256),
    'wal_writer_flush_after': (0, 256),
    'wal_buffers': (-1, 262143),
    'shared_buffers': (16, 1073741823),
    'temp_buffers': (100, 1073741823),
    'work_mem': (64, 2147483647),
    'maintenance_work_mem': (1024, 2147483647),
    'autovacuum_work_mem': (-1, 2147483647),
    'max_parallel_workers_per_gather': (0, 1024),
    'max_parallel_maintenance_workers': (0, 1024),
    'max_standby_streaming_delay': (-1, 2147483647),
    'max_standby_archive_delay': (-1, 2147483647),
    'max_pred_locks_per_relation': (1, 2147483647),
    'max_pred_locks_per_transaction': (1, 2147483647),
    'max_locks_per_transaction': (1, 2147483647),
    'autovacuum_freeze_max_age': (100000, 2000000000),
    'autovacuum_multixact_freeze_max_age': (10000, 2000000000),
    'vacuum_freeze_table_age': (0, 2000000000),
    'vacuum_freeze_min_age': (0, 1000000000),
    'vacuum_multixact_freeze_table_age': (0, 2000000000),
    'vacuum_multixact_freeze_min_age': (0, 1000000000),
    'log_rotation_size': (0, 2097151),
    'log_min_duration_statement': (-1, 2147483647),
    'track_activity_query_size': (100, 1048576),
    'gin_pending_list_limit': (64, 2147483647),
    'temp_file_limit': (-1, 2147483647),
    'max_slot_wal_keep_size': (-1, 2147483647),
    'wal_keep_size': (0, 2147483647),
    'min_dynamic_shared_memory': (0, 2147483647),
    'maintenance_io_concurrency': (0, 1024),
    'effective_io_concurrency': (0, 1024),
    'hash_mem_multiplier': (1.0, 1000.0),
    'logical_decoding_work_mem': (64, 2147483647),
    'wal_compression': (0, 1),
    'wal_sync_method': (0, 3),
    'old_snapshot_threshold': (-1, 86400),
    'vacuum_cost_page_miss': (0, 10000),
    'vacuum_cost_page_dirty': (0, 10000),
    'vacuum_cost_page_hit': (0, 10000),
    'autovacuum_vacuum_threshold': (0, 2147483647),
    'autovacuum_analyze_threshold': (0, 2147483647),
    'autovacuum_vacuum_insert_threshold': (-1, 2147483647),
    'autovacuum_vacuum_cost_delay': (-1, 100),
    'autovacuum_vacuum_cost_limit': (-1, 10000),
    'wal_receiver_timeout': (0, 2147483647),
    'wal_sender_timeout': (0, 2147483647),
    'wal_writer_delay': (1, 10000),
    'max_sync_workers_per_subscription': (0, 64),
    'max_logical_replication_workers': (0, 64),
    'jit_above_cost': (-1, 2147483647),
    'jit_inline_above_cost': (-1, 2147483647),
    'jit_optimize_above_cost': (-1, 2147483647),
    'cursor_tuple_fraction': (0.0, 1.0),
    'client_connection_check_interval': (0, 2147483647),
    'vacuum_failsafe_age': (0, 2100000000),
    'vacuum_multixact_failsafe_age': (0, 2100000000),
    'vacuum_cost_page_hit': (0, 10000),
}


def convert_to_bytes(value, unit):
    """Convert a raw pg_settings value + unit into bytes (or leave unitless numeric)."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if unit and unit in UNITS:
        return int(num) * UNITS[unit]
    # unitless: return int when integral, else float
    return int(num) if num == int(num) else num


def is_degenerate(value):
    """True when a bound is unusable for tuning (None, zero, or near-infinite)."""
    if value is None:
        return True
    try:
        v = float(value)
    except (TypeError, ValueError):
        return True
    return v == 0 or v > 1e15


def extract_first_sentence(text):
    match = re.match(r'([^.]*[.])', text or '')
    if match:
        return match.group(1)
    return text or ''


def main():
    with open('knob selector/get_candidate_knobs/pg_source/system_view.json', 'r', encoding='utf-8') as f:
        system_view = json.load(f)

    with open('knob selector/get_candidate_knobs/pg_source/target_knobs.txt', 'r', encoding='utf-8') as f:
        target_knobs = [line.strip() for line in f if line.strip()]

    detailed = {}
    for name in target_knobs:
        if name not in system_view:
            print(f"warning: target knob {name} not found in system_view; skipping")
            continue
        sv = system_view[name]
        vartype = sv['vartype']

        # determine numeric bounds
        min_val = convert_to_bytes(sv['min_val'], sv['unit'])
        max_val = convert_to_bytes(sv['max_val'], sv['unit'])

        if vartype in ('integer', 'real'):
            if is_degenerate(min_val) or is_degenerate(max_val) or min_val == max_val:
                # unbounded or degenerate -> use hardware-aware fallback
                if name in HARDWARE_BOUNDS:
                    min_val, max_val = HARDWARE_BOUNDS[name]
                    # apply unit scaling to the fallback bound too
                    min_val = convert_to_bytes(min_val, sv['unit'])
                    max_val = convert_to_bytes(max_val, sv['unit'])
                else:
                    print(f"warning: {name} has no usable bounds; using default 0")
                    min_val = 0
                    max_val = 0
            entry = {
                'max': max_val,
                'min': min_val,
                'type': 'integer' if vartype == 'integer' else 'real',
                'description': (sv['short_desc'] or '') + (' ' + sv['extra_desc'] if sv['extra_desc'] else ''),
            }
        elif vartype == 'bool':
            entry = {
                'max': 1,
                'min': 0,
                'type': 'integer',
                'description': (sv['short_desc'] or '') + (' ' + sv['extra_desc'] if sv['extra_desc'] else ''),
            }
        elif vartype == 'enum':
            entry = {
                'enum_values': list(sv['enumvals'] or []),
                'type': 'enum',
                'description': (sv['short_desc'] or '') + (' ' + sv['extra_desc'] if sv['extra_desc'] else ''),
            }
        else:
            # string / other non-tunable types cannot be handled by the numeric
            # range pruner and config ranker; skip them to mirror the MySQL
            # candidate set (integer/enum only).
            print(f"skipping non-tunable knob {name} (vartype={vartype})")
            continue
        detailed[name] = entry

    # order by the target_knobs.txt file order
    ordered = {name: detailed[name] for name in target_knobs if name in detailed}
    with open('range pruner/knob_details_pg.json', 'w', encoding='utf-8') as f:
        json.dump(ordered, f, ensure_ascii=False, indent=4)

    # simplified candidate_knobs_pg (first-sentence description, no extra_desc)
    simplified = {}
    for name, entry in ordered.items():
        clean = {
            'type': entry['type'],
            'description': extract_first_sentence(entry['description']),
        }
        if entry.get('type') == 'enum':
            clean['enum_values'] = entry['enum_values']
        else:
            clean['max'] = entry['max']
            clean['min'] = entry['min']
        simplified[name] = clean

    with open('knob selector/candidate_knobs_pg', 'w', encoding='utf-8') as f:
        json.dump(simplified, f, ensure_ascii=False, indent=4)

    print(f"wrote {len(ordered)} knobs to range pruner/knob_details_pg.json")
    print(f"wrote {len(simplified)} knobs to knob selector/candidate_knobs_pg")


if __name__ == '__main__':
    main()
