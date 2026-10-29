"""Optional model words, kept with the existing agent run receipt."""
import json

FIELD = 'reflection'
MAX_CHARS = 2000
OUTPUT_SCHEMA = {'type': 'object', 'properties': {FIELD: {'type': 'string'}}}


def text(result):
    value = (result.get('structured_output') or {}).get(FIELD, '')
    return value[:MAX_CHARS] if isinstance(value, str) else ''


def collect(run):
    return [dict(agent=path.stem.removeprefix('agent-'), text=receipt[FIELD])
            for path in sorted(run.glob('agent-*.json'))
            if (receipt := json.loads(path.read_text())).get(FIELD)]
