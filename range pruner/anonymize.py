import json
import configparser
def rename_knobs(input_file, output_file):
    with open(input_file, 'r') as f:
        original_data = json.load(f)

    new_data = {}
    for idx, (old_key, value) in enumerate(original_data.items(), start=1):
        new_key = f"knob{idx}"
        new_data[new_key] = value

    with open(output_file, 'w') as f:
        json.dump(new_data, f, indent=4, ensure_ascii=False)



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

dbms = config.get('configuration recommender', 'dbms', fallback='mysql')
if dbms == 'postgresql':
    input_file = "./range pruner/knob_details_pg.json"
else:
    input_file = config['range pruner']['knob_details']
output_file="./range pruner/renamed_knobs"
rename_knobs(
    input_file,
    output_file
)