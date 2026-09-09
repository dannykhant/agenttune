from openai import OpenAI
import configparser
import re
import json

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

knob_list_path = config['knob selector']['output_file']
with open(knob_list_path,"r") as f:
    knob_names = json.load(f) 

knob_details_path =  "./range pruner/renamed_knobs"
with open(knob_details_path, 'r') as f:
    knob_details = json.load(f) 

filtered = {name: knob_details[name] for name in knob_names if name in knob_details}
knobs = json.dumps(filtered, indent=4, ensure_ascii=False)
#print(knobs)

file_path = config['workload analyzer']['output_file']
with open(file_path, "r") as f:
    workload_features = f.read().strip()  
database_kernel=config['knob selector']['database_kernel']
hardware=config['knob selector']['hardware']
database_scale=config['knob selector']['database_scale']


def sanitize_ranges(parsed):
    """Clamp LLM-proposed ranges into the knob's legal config bounds.

    The JSON parse path (used by Gemini) skipped the bounds validation that the
    markdown path did, so out-of-bounds ranges (e.g. shared_buffers proposed in
    bytes below its minimum) reached the recommender and produced unusable
    configurations. When a proposed range is invalid, fall back to the full
    legal bounds so the recommender never sees a degenerate range.
    """
    sanitized = {}
    for knob_id, entry in parsed.items():
        config = knob_details.get(knob_id, {})
        config_min = config.get('min')
        config_max = config.get('max')
        if config_min is None or config_max is None:
            continue

        def to_num(v):
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, (int, float)):
                return v
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return int(f) if f == int(f) else f

        vartype = entry.get('type', config.get('type', 'integer'))
        lo = to_num(entry.get('min_value'))
        hi = to_num(entry.get('max_value'))
        step = to_num(entry.get('step'))

        valid = (lo is not None and hi is not None and lo < hi and
                 lo >= config_min and hi <= config_max)
        if not valid:
            lo, hi = config_min, config_max
        if vartype == 'real':
            if step is None or step <= 0 or step > (hi - lo):
                step = round((hi - lo) / 20.0, 4) or 1.0
        else:
            if step is None or step <= 0 or step > (hi - lo):
                step = max(1, int((hi - lo) / 20))

        clean = {
            'min_value': lo,
            'max_value': hi,
            'step': step,
            'type': vartype,
            'description': entry.get('description', config.get('description', '')),
        }
        if entry.get('special_value') is not None:
            clean['special_value'] = entry['special_value']
        sanitized[knob_id] = clean
    return sanitized


def extract_knob_intervals_with_ids(text):
    # Some models (e.g. Gemini) return a JSON object instead of the markdown
    # paragraph format below, parse that directly, keeping the markdown parser
    # as a fallback.
    text = text or ""
    stripped = text.strip()
    # The JSON object may be prefixed by prose; find the first fenced or
    # bare "{...}" block anywhere in the response.
    fenced = re.search(r"```[a-zA-Z]*\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    else:
        braced = re.search(r"\{.*?\}", stripped, re.DOTALL)
        if braced:
            stripped = braced.group(0).strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            parsed = {}
            def to_num(v):
                if isinstance(v, bool):
                    return int(v)
                if isinstance(v, (int, float)):
                    return v
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    return None
                return int(f) if f == int(f) else f
            for knob_id, spec in data.items():
                if not knob_id.startswith("knob") or not isinstance(spec, dict):
                    continue
                min_value = to_num(spec.get("min_value"))
                max_value = to_num(spec.get("max_value"))
                step = to_num(spec.get("step"))
                if min_value is None or max_value is None or step is None:
                    continue
                entry = {
                    "min_value": min_value,
                    "max_value": max_value,
                    "step": step,
                    "type": knob_details.get(knob_id, {}).get("type", "integer"),
                    "description": knob_details.get(knob_id, {}).get("description", ""),
                }
                if spec.get("special_value") is not None:
                    special = to_num(spec["special_value"])
                    if special is not None:
                        entry["special_value"] = special
                parsed[knob_id] = entry
            if parsed:
                return sanitize_ranges(parsed)

    # Split by Knob Paragraph
    knob_blocks = re.split(r'\n\d+\.\s+\*\*(knob\d+)\s+\((.*?)\)\*\*:', text)
    knobs = {}

    for i in range(1, len(knob_blocks), 3):
        knob_id = knob_blocks[i]  # e.g., "knob42"
        block_text = knob_blocks[i+2]

        min_match = re.search(r'\*\*min_value\*\*:\s*([\d]+)', block_text)
        max_match = re.search(r'\*\*max_value\*\*:\s*([\d]+)', block_text)
        step_match = re.search(r'\*\*step\*\*:\s*([\d]+)', block_text)
        special_match = re.search(r'\*\*special_value\*\*:\s*([\d]+)', block_text)

        if min_match and max_match and step_match:
            knobs[knob_id] = {
                "min_value": int(min_match.group(1)),
                "max_value": int(max_match.group(1)),
                "step": int(step_match.group(1)),
                "type": knob_details[knob_id]["type"],
                "description": knob_details[knob_id]["description"]
            }
            if special_match:
                knobs[knob_id]["special_value"] = int(special_match.group(1))

    return sanitize_ranges(knobs)

def call_open_source_llm(model,knob_list):
    client = OpenAI(
        api_key=config['knob selector']['api_key'], 
        base_url=config['knob selector']['base_url']
    )

    messages = [
    {"role": "system", "content": "You are an experienced database administrators, skilled in database knob tuning."},
    {
        "role": "user",
        "content": """
            Task Overview: 
            Given the knob name along with its suggestion and tuning task information, your job is to offer intervals for each knob that may lead to the best performance of the system and meet the hardware resource constraints. 
            In addition, if there is a special value (e.g., 0, -1, etc.), please mark it with “special value”.
            Knobs:
            {knob}
            Workload and Database information: 
            - Workload Features: {workload_features}
            - Database Kernel: {database_kernel}
            - Database Scale: {database_scale}
            - Hardware: {hardware}
            Output Format:
            "knob_name"{{
                "min_value": MIN_VALUE,
                "max_value": MAX_VALUE,
                "step": STEP_SIZE,
                "special_value": SPECIAL_VALUE
            }} 
            Now let us think step by step.        
        """.format(knob=knobs,  workload_features = workload_features, database_kernel=database_kernel, hardware=hardware, database_scale=database_scale)
    }
    ]

    completion = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature = 0
    )

    for choice in completion.choices:
        print(choice.message)
        print("--------------------------")
        result = extract_knob_intervals_with_ids(choice.message.content)
        output = config['range pruner']['output_file']
        with open(output,"w") as f:
            json.dump(result, f, indent=2)
            f.close()


if __name__ == '__main__':
    model = config['range pruner']['model']
    call_open_source_llm (model,knobs)