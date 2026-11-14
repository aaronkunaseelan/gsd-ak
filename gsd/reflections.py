"""Optional model words, kept with the existing agent run receipt."""
import json

FIELD = 'reflection'
MAX_CHARS = 2000
OUTPUT_SCHEMA = {'type': 'object', 'properties': {FIELD: {'type': 'string'}}}


def text(result):
    value = (result.get('structured_output') or {}).get(FIELD, '')
    return value[:MAX_CHARS] if isinstance(value, str) else ''


def path(run, name):
    """One file per agent: a shared file would let one agent's words reach another."""
    return run / f'reflection-{name}.json'


def instruction(run, name):
    """The sentence a vendor with no structured output needs to use its own file.

    Its prompt cannot call this module, so the text carries the path as written.
    """
    return (
        '\nBefore you finish, write your reflection as JSON '
        f'{{"{FIELD}": "<a few words>"}} to {path(run, name)}.\n'
    )


def sidecar(run, name):
    """A vendor with no structured output leaves the words in the run folder instead.

    Absent, unreadable or malformed means none: the reflection is optional metadata.
    """
    path_ = path(run, name)
    try:
        value = json.loads(path_.read_text()).get(FIELD, '')
    except (OSError, ValueError, AttributeError):
        return ''
    return value[:MAX_CHARS] if isinstance(value, str) else ''


def collect(run):
    return [dict(agent=file.stem.removeprefix('agent-'), text=receipt[FIELD])
            for file in sorted(run.glob('agent-*.json'))
            if (receipt := json.loads(file.read_text())).get(FIELD)]
