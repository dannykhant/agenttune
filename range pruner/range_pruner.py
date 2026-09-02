from openai import OpenAI
import configparser
import re
import json

config = configparser.ConfigParser()
config.read('./config.ini')

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


def extract_knob_intervals_with_ids(text):
    # Some models (e.g. Gemini) return a JSON object instead of the markdown
    # paragraph format below; parse that directly, keeping the markdown parser
    # as a fallback.
    text = text or ""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
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
                return parsed

    # Split by Knob Paragraph
    knob_blocks = re.split(r'\n\d+\.\s+\*\*(knob\d+)\s+\((.*?)\)\*\*:', text)
    knobs = {}
    validation_errors = []
    
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
        
        #Safe Check
        error_messages=[]
        config_min = knob_details[knob_id]["min"]
        config_max = knob_details[knob_id]["max"]
        min_val = int(min_match.group(1))
        max_val = int(max_match.group(1))

        if isinstance(min_val, int) and not (config_min <= min_val <= config_max):
            error_messages.append(f"min_val{min_val}out of bounds{config_min}-{config_max}]")
            
        if isinstance(max_val, int) and not (config_min <= max_val <= config_max):
            error_messages.append(f"max_val{max_val}out of bounds[{config_min}-{config_max}]")
            
        if isinstance(min_val, int) and isinstance(max_val, int) and min_val > max_val:
            error_messages.append(f"min_val{min_val}larger than{max_val}")

        #Record validation errors
        if error_messages:
            validation_errors.append({
                "parameter": knob_name,
                "errors": error_messages,
                "received": {
                    "min": min_val,
                    "max": max_val
                }
            })
            continue
            
    if validation_errors:
        print("Error:")
        for error in validation_errors:
            print(json.dumps(error, indent=2, ensure_ascii=False))

    return knobs

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