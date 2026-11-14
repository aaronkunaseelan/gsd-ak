"""Pi as gsd's second vendor: one argv and one reading of its JSONL.

`step.launch()` builds a claude argv or a pi argv for the same session, and
`step.usage()` reads either stream through here. `tags.py` keeps its own reading
for its tool-free batch call: a different argv, no session, and no tools.
"""

import json

BINARY = 'pi'

# gsd's three efforts in pi's vocabulary. This model maps no `medium` level, so
# the same ask is `high` there; `low` is mapped as itself.
THINKING = {'low': 'low', 'medium': 'high', 'high': 'high'}

# pi reports camelCase usage; gsd counts and stores claude's snake_case fields.
USAGE = {
    'input': 'input_tokens',
    'output': 'output_tokens',
    'cacheRead': 'cache_read_input_tokens',
    'cacheWrite': 'cache_creation_input_tokens',
}

VENDORS = ('claude', 'pi')

# The `stopReason`s with which a turn ends when it produced no response: a provider
# error, or an abort. pi exits 0 for the first in `--mode json`, so the turn itself
# is the failure signal.
PROVIDER_STOP = ('error', 'aborted')


def split(selector):
    """(vendor, model) from `pi:<provider/model>`, `claude:<model>` or a bare name.

    A bare name stays claude: every selector written before this vendor existed
    names a claude model, so no old argv or job config changes meaning.
    """
    vendor, _, name = selector.partition(':')
    if vendor in VENDORS and name:
        return vendor, name
    return 'claude', selector


def argv(run, model, session, effort):
    """One headless session with the same tools, isolation and prompt file as claude.

    pi reads the prompt file only when it exists and otherwise sends the path text,
    so the file is checked here where claude's `-file` flag would fail.
    """
    prompt = run / 'shared.md'
    if not prompt.is_file():
        raise ValueError('shared context file is missing; refusing to launch pi')
    return [
        BINARY,
        '-p',
        '--mode',
        'json',
        '--session-id',
        session,
        '--model',
        model,
        '--thinking',
        THINKING[effort],
        '--append-system-prompt',
        str(prompt),
        '--tools',
        'read,bash,write,edit',
        '--no-extensions',
        '--no-skills',
        '--no-context-files',
        '--no-prompt-templates',
        '--no-mcp',
        '--session-dir',
        str(run / 'sessions'),
    ]


def is_pi(text):
    """pi's JSON mode opens with a session header; claude's stream opens with `system`."""
    first = next((line for line in text.splitlines() if line.strip()), '')
    try:
        return json.loads(first).get('type') == 'session'
    except (ValueError, AttributeError):
        return False


def call_row(message, previous=None):
    """One usage row per assistant response, in the shape claude's rows already have."""
    previous = previous or {}
    usage = message.get('usage') or {}
    content = message.get('content') or []
    tools = [block for block in content if block.get('type') == 'toolCall']
    thinking = [block for block in content if block.get('type') == 'thinking']
    old = previous.get('usage', {})
    return {
        'message_id': previous.get('message_id'),
        'model': message.get('model') or previous.get('model'),
        'usage': {
            field: old.get(field, 0) + (usage.get(key) or 0) for key, field in USAGE.items()
        },
        'tool_use': previous.get('tool_use', False) or bool(tools),
        'thinking_blocks': previous.get('thinking_blocks', 0) + len(thinking),
        'thinking_chars': previous.get('thinking_chars', 0)
        + sum(len(block.get('thinking') or '') for block in thinking),
        'tool_calls': previous.get('tool_calls', [])
        + [{'id': block.get('id'), 'name': block.get('name')} for block in tools],
        'cost_usd': previous.get('cost_usd', 0)
        + ((usage.get('cost') or {}).get('total') or 0),
    }


def parse(text, *, exit_code=None, duration_ms=None):
    """(calls, result) for the dict gsd's runner and measurements already read.

    pi has no aggregate envelope: the calls are its `turn_end` records (a retried
    response repeats `responseId`), the success signal is `agent_settled.aborted`,
    the last turn's `stopReason` and the exit code, and cost, turns and duration are
    summed or counted here.
    """
    rows, aborted, last = {}, False, None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'agent_settled':
            aborted = aborted or bool(event.get('aborted'))
        elif event.get('type') == 'turn_end':
            message = event.get('message') or {}
            last = message
            key = message.get('responseId') or str(len(rows))
            row = call_row(message, rows.get(key))
            row['message_id'] = key
            rows[key] = row
    calls = list(rows.values())
    # A last turn that ended in a provider error or an abort is a failure even when
    # pi exited 0; an error followed by a successful retry is not one.
    stopped = bool(last) and last.get('stopReason') in PROVIDER_STOP
    failed = aborted or stopped or bool(exit_code)
    result = {
        'type': 'result',
        'subtype': 'error_during_execution' if failed else 'success',
        'is_error': failed,
        'usage': {
            field: sum(call['usage'][field] for call in calls) for field in USAGE.values()
        },
        'total_cost_usd': sum(call['cost_usd'] for call in calls),
        'num_turns': len(calls),
        'duration_ms': duration_ms,
    }
    if stopped and last.get('errorMessage'):
        # Where `step.failure_cause()` reads a failure's text; it names only the class.
        result['error'] = last['errorMessage']
    return calls, result
