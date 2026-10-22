"""Shared item-state semantics for published views and routing cards."""


def is_open(state):
    return state not in ('done', 'canceled')


def history(db):
    """Adjacent graph versions, before any reader's time window is applied."""
    for row in db.execute('''
        SELECT h.*, p.name AS project FROM (
            SELECT *, lag(state) OVER versions AS previous_state,
                   lag(op) OVER versions AS previous_op
            FROM items
            WINDOW versions AS (PARTITION BY id ORDER BY seq)
        ) h LEFT JOIN cur_projects p ON p.id = h.project_id
        ORDER BY h.seq
    '''):
        row = dict(row)
        row['completion'] = (row['op'] == 'upsert' and not is_open(row['state'])
                             and (row['previous_state'] != row['state'] or row['previous_op'] == 'delete'))
        row['was_open'] = row['previous_op'] == 'upsert' and is_open(row['previous_state'])
        row['is_open'] = row['op'] == 'upsert' and is_open(row['state'])
        yield row
