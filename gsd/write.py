"""One atomic writer for flag verbs, batch proposals and reviewer amendments."""

import json
import math
import re
import sqlite3
import uuid
from . import db as store

NOTES = ('why', 'confidence', 'importance', 'grp')
ROLES = ('canonical', 'states', 'derived_from', 'directed_by', 'context', 'decides', 'commits')
DEFAULTS = {
    'records': {'created_by': 'owner'},
    'people': {'aliases': '[]', 'status': 'unknown', 'metadata_json': '{}'},
    'projects': {'status': 'unknown', 'priority': 50, 'metadata_json': '{}'},
    'items': {
        'kind': 'task',
        'priority': 50,
        'state': 'inbox',
        'next_actor': 'user',
    },
    'meetings': {
        'is_all_day': 0,
        'status': 'unknown',
        'attendees_json': '[]',
        'metadata_json': '{}',
    },
    'record_links': {'directionality': 'directed', 'is_primary': 0, 'metadata_json': '{}'},
    'source_refs': {'source_surface': 'qmd', 'role': 'states', 'metadata_json': '{}'},
}


REFERENCES = {
    'owner_person_id': 'person',
    'project_id': 'project',
    'from_record_id': None,
    'to_record_id': None,
    'record_id': None,
}


class Invalid(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('validation failed')


def upsert_identity(db, op, kind):
    rid = op.get('id')
    record = None
    if rid is not None and (not isinstance(rid, str) or not rid):
        fail('id', rid, 'expected nonempty id')
    if rid:
        # Existing references may be short; genuinely new ids must be UUID4.
        try:
            resolved = store.resolve(db, rid)
            record = store.current(db, 'records', (resolved,))
            rid = resolved
        except ValueError:
            try:
                u = uuid.UUID(rid)
                if u.version != 4 or str(u) != rid:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                fail('id', rid, 'unknown/ambiguous reference; new ids must be UUID4')
            if store.current(db, 'records', (rid,), True):
                fail('id', rid, 'record is tombstoned')
        if record and record['record_kind'] != kind:
            fail('upsert', kind, 'cannot change record kind')
    else:
        rid = str(uuid.uuid4())
    return rid, record


def write_citations(db, op, rid, cid, actor, verb):
    cites = op.get('cite', [])
    if not isinstance(cites, list):
        fail('cite', cites, 'expected array')
    for cite in cites:
        if not isinstance(cite, dict) or set(cite) - {'ref', 'role', 'excerpt'}:
            fail('cite', cite, 'expected ref, optional role and excerpt')
        qid = qmd_ref(db, cite.get('ref'))
        refid = db.execute('SELECT COALESCE(MAX(source_ref_id),0)+1 FROM source_refs').fetchone()[0]
        evidence = base('source_refs', actor)
        evidence.update(
            source_ref_id=refid,
            record_id=rid,
            source_system=qid.split(':')[0],
            source_key=qid,
            evidence_qmd_event_id=qid,
            role=cite.get('role', 'states'),
            excerpt=cite.get('excerpt'),
        )
        insert(db, 'source_refs', evidence, cid, verb, actor)


def err(field, value, why):
    return {'field': field, 'value': value, 'why': why}


def fail(field, value, why):
    raise Invalid([err(field, value, why)])


def rules(db, table):
    """Read fixed vocabularies and required columns from the actual SQL triggers."""
    sql = '\n'.join(
        r[0]
        for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
        )
    )
    enums = {
        f: re.findall(r"'([^']*)'", vs)
        for f, vs in re.findall(r'NEW\.(\w+) NOT IN \(([^)]+)\)', sql)
    }
    table_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    for f, values in re.findall(r'CHECK \((\w+) IN \(([^)]+)\)\)', table_sql):
        enums[f] = [s if s else int(n) for s, n in re.findall(r"'([^']*)'|([0-9]+)", values)]
    required = []
    for r in db.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (table + '_upsert_required',)
    ):
        required = re.findall(r'NEW\.(\w+) IS NULL', r[0])
    if table == 'items':
        enums['kind'] = ['task', 'question']
    return enums, required


def validate(db, table, data):
    errors = []
    types = store.columns(db, table)
    enums, required = rules(db, table)
    for f, v in data.items():
        if f not in types or f in store.META:
            errors.append(err(f, v, 'unknown or managed field'))
            continue
        if v is None:
            if f in required or f in store.IDENTITY[table]:
                errors.append(err(f, v, 'required field cannot be null'))
            continue
        typ = types[f]
        if (typ == 'TEXT' and not isinstance(v, str)) or (
            typ == 'INTEGER' and not isinstance(v, int)
        ):
            errors.append(err(f, v, f'must be {typ.lower()}'))
            continue
        if f.endswith(('_at', '_date')):
            try:
                store.notion_date_utc(v)
            except (ValueError, OverflowError):
                errors.append(err(f, v, 'expected ISO date or timestamp'))
        if f in enums and v not in enums[f]:
            errors.append(err(f, v, 'expected one of ' + ', '.join(map(str, enums[f]))))
        if f in ('priority', 'duration_minutes', 'is_all_day', 'is_primary'):
            high = 100 if f == 'priority' else 1 if f in ('is_all_day', 'is_primary') else None
            if v < 0 or (high is not None and v > high):
                errors.append(
                    err(f, v, f'expected 0..{high}' if high is not None else 'must be nonnegative')
                )
        if f == 'confidence_score':
            try:
                good = math.isfinite(float(v)) and 0 <= float(v) <= 1
            except (TypeError, ValueError):
                good = False
            if not good:
                errors.append(err(f, v, 'expected number in 0..1'))
        if f == 'aliases':
            try:
                aliases = json.loads(v)
                if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                    raise ValueError()
            except (ValueError, TypeError):
                errors.append(err(f, v, 'expected JSON list of strings'))
        if f.endswith('_json'):
            try:
                json.loads(v)
            except (ValueError, TypeError):
                errors.append(err(f, v, 'invalid JSON text'))
    for f in required:
        if f not in data:
            errors.append(err(f, None, 'required field missing'))
    for f, kind in REFERENCES.items():
        if data.get(f) is not None:
            r = store.current(db, 'records', (data[f],))
            if not r or (kind and r['record_kind'] != kind):
                errors.append(err(f, data[f], 'requires a current ' + (kind or 'record') + ' id'))
    if errors:
        raise Invalid(errors)


def reference(db, text, kind=None):
    if not isinstance(text, str) or not text:
        fail('id', text, 'expected record id or unambiguous name')
    try:
        return store.resolve(db, text, kind)
    except ValueError as e:
        fail('id', text, str(e))


def qmd_ref(db, ref):
    if not isinstance(ref, str):
        fail('ref', ref, 'expected QMD id')
    if store.has_table(db, 'draft_ref'):
        r = db.execute('SELECT qmd_id FROM draft_ref WHERE short=?', (ref,)).fetchone()
        if r:
            ref = r[0]
    if not re.fullmatch(r'[a-z][a-z0-9_-]*:[^\s:][^\s]*', ref):
        fail('ref', ref, 'expected PREFIX:ID')
    return ref


def citation_flag(value):
    m = re.search(r':(' + '|'.join(ROLES) + r')(?::|$)', value)
    if not m:
        return {'ref': value}
    return {'ref': value[: m.start()], 'role': m.group(1), 'excerpt': value[m.end() :] or None}


def base(table, actor):
    at = store.now()
    r = dict(DEFAULTS[table], created_at=at, updated_at=at)
    if table in ('records', 'record_links', 'source_refs'):
        r['created_by'] = actor
    return r


def insert(db, table, data, cid, verb, actor, op='upsert'):
    if op == 'upsert':
        validate(db, table, data)
    store.append(db, table, data, cid, verb, actor, op=op)


def upsert(db, op, cid, actor, verb='upsert'):
    kind = op['upsert']
    if not isinstance(kind, str) or kind not in store.KINDS:
        fail('upsert', kind, 'expected person, project, item or meeting')
    table = store.KINDS[kind]
    fields = op.get('set', {})
    if not isinstance(fields, dict):
        fail('set', fields, 'expected object')
    forbidden = set(fields) & (store.META | {'id', 'created_at', 'updated_at'})
    unknown = set(fields) - set(store.columns(db, table))
    if forbidden or unknown:
        raise Invalid(
            [err(f, fields[f], 'unknown or managed field') for f in sorted(forbidden | unknown)]
        )
    rid, record = upsert_identity(db, op, kind)
    previous = store.current(db, table, (rid,))
    data = {k: v for k, v in (previous or base(table, actor)).items() if k not in store.META}
    data.update(fields, id=rid, updated_at=store.now())
    # Resolve stable reference fields, preserving schema text fields such as assigned_to.
    for f, k in REFERENCES.items():
        if k and f in fields and fields[f] is not None:
            data[f] = reference(db, fields[f], k)
    validate(db, table, data)
    if kind == 'item':
        cites = op.get('cite', [])

        def quoted(roles):
            return isinstance(cites, list) and any(
                isinstance(c, dict)
                and c.get('role') in roles
                and isinstance(c.get('excerpt'), str)
                and c['excerpt'].strip()
                for c in cites
            )

        if (
            data.get('state') in ('done', 'canceled')
            and data.get('state') != (previous or {}).get('state')
            and not quoted({'decides'})
        ):
            fail(
                'state',
                data['state'],
                'closure needs a decides citation with a non-empty excerpt in this op; otherwise leave the item open',
            )
        if (
            data.get('assigned_to')
            and data.get('assigned_to') != (previous or {}).get('assigned_to')
            and not quoted({'commits', 'decides'})
        ):
            fail(
                'assigned_to',
                data['assigned_to'],
                'assignment needs a commits or decides citation with a non-empty excerpt in this op; otherwise leave assigned_to empty and put candidates in waiting_on',
            )
    title = store.name(data)
    if not record or title != record['canonical_title']:
        rec = {k: v for k, v in (record or base('records', actor)).items() if k not in store.META}
        rec.update(id=rid, record_kind=kind, canonical_title=title, updated_at=store.now())
        insert(db, 'records', rec, cid, verb, actor)
    insert(db, table, data, cid, verb, actor)
    write_citations(db, op, rid, cid, actor, verb)
    return rid


def operation(db, op, cid, actor, verb=None):
    if not isinstance(op, dict):
        fail('ops', op, 'each operation must be an object')
    tags = set(op) & {'upsert', 'link', 'end_link', 'tombstone'}
    if len(tags) != 1:
        fail('ops', op, 'exactly one operation required')
    tag = next(iter(tags))
    allowed = {
        'upsert': {'upsert', 'id', 'set', 'cite'},
        'link': {'link'},
        'end_link': {'end_link'},
        'tombstone': {'tombstone'},
    }[tag]
    if set(op) - allowed:
        fail('ops', op, 'unknown operation fields')
    verb = verb or tag.replace('_', '-')
    if tag == 'upsert':
        return upsert(db, op, cid, actor, verb)
    if tag in ('link', 'end_link'):
        value = op[tag]
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not all(isinstance(x, str) and x for x in value)
        ):
            fail(tag, value, 'expected [FROM, TO, RELATION]')
        frm, to = [reference(db, x) for x in value[:2]]
        if (
            tag == 'link'
            and value[2] in ('belongs_to_project', 'assigned_to_person')
            and store.current(db, 'records', (frm,))['record_kind'] == 'item'
        ):
            column = {'belongs_to_project': 'project_id', 'assigned_to_person': 'assigned_to'}[
                value[2]
            ]
            fail('link', value[2], f'retired item relation; set {column} instead')
        identity = (frm, to, value[2])
        previous = store.current(db, 'record_links', identity)
        if tag == 'end_link' and not previous:
            fail('end_link', value, 'no current link')
        data = dict(zip(store.IDENTITY['record_links'], identity))
        if tag == 'link':
            data = {
                k: v
                for k, v in (previous or base('record_links', actor)).items()
                if k not in store.META
            } | data
            data['updated_at'] = store.now()
        insert(
            db, 'record_links', data, cid, verb, actor, 'delete' if tag == 'end_link' else 'upsert'
        )
        return frm
    rid = reference(db, op['tombstone'])
    rec = store.current(db, 'records', (rid,))
    for table in (store.KINDS[rec['record_kind']], 'records'):
        insert(db, table, {'id': rid}, cid, verb, actor, 'delete')
    return rid


def validate_notes(db, change):
    errors = []
    if any(k in change for k in NOTES) and not store.has_table(db, 'draft_change'):
        errors.append(err('changes', None, 'proposal notes require a draft store'))
    for f in ('confidence', 'importance'):
        v = change.get(f)
        if v is not None and (
            not isinstance(v, (int, float))
            or isinstance(v, bool)
            or not math.isfinite(v)
            or not 0 <= v <= 1
        ):
            errors.append(err(f, v, 'expected number in 0..1'))
    for f in ('why', 'grp'):
        if change.get(f) is not None and not isinstance(change[f], str):
            errors.append(err(f, change[f], 'expected text or null'))
    if errors:
        raise Invalid(errors)


def batch(db, payload, actor='owner', change_id=None):
    if (
        not isinstance(payload, dict)
        or set(payload) != {'changes'}
        or not isinstance(payload['changes'], list)
    ):
        fail('changes', None, 'expected {"changes": [...]}')
    results = []
    for change in payload['changes']:
        cid = change.get('id', change_id or str(uuid.uuid4())) if isinstance(change, dict) else None
        try:
            db.execute('BEGIN IMMEDIATE')
            if isinstance(change, dict) and set(change) & {'needs_owner', 'question'}:
                fail(
                    'change',
                    None,
                    'needs_owner and question are retired (D14); leave uncertain fields unassigned or unchanged and explain in why',
                )
            if not isinstance(change, dict) or set(change) - {'id', 'ops', *NOTES}:
                fail('change', change, 'unknown change fields or not an object')
            if not isinstance(cid, str) or not cid.strip():
                fail('id', cid, 'expected nonempty change id')
            if db.execute('SELECT 1 FROM change_sets WHERE change_set_id=?', (cid,)).fetchone():
                fail('id', cid, 'change id already exists; resend only failed changes')
            validate_notes(db, change)
            ops = change.get('ops')
            if not isinstance(ops, list) or not ops:
                fail('ops', ops, 'expected nonempty array')
            store.change_set(db, cid, actor)
            errors, ids = [], []
            for i, op in enumerate(ops):
                db.execute('SAVEPOINT operation')
                try:
                    ids.append(operation(db, op, cid, actor))
                    db.execute('RELEASE operation')
                except Invalid as e:
                    db.execute('ROLLBACK TO operation')
                    db.execute('RELEASE operation')
                    errors.extend(dict(e_, field=f'ops[{i}].{e_["field"]}') for e_ in e.errors)
                except sqlite3.IntegrityError as e:
                    db.execute('ROLLBACK TO operation')
                    db.execute('RELEASE operation')
                    errors.append(err(f'ops[{i}]', op, str(e)))
            if errors:
                raise Invalid(errors)
            if store.has_table(db, 'draft_change'):
                db.execute(
                    'INSERT INTO draft_change(change_id,why,confidence,importance,grp) VALUES (?,?,?,?,?)',
                    (
                        cid,
                        change.get('why'),
                        change.get('confidence'),
                        change.get('importance'),
                        change.get('grp'),
                    ),
                )
            db.commit()
            results.append({'id': cid, 'ok': True, 'records': list(dict.fromkeys(ids))})
        except Invalid as e:
            db.rollback()
            results.append({'id': cid, 'ok': False, 'errors': e.errors})
        except sqlite3.IntegrityError as e:
            db.rollback()
            results.append({'id': cid, 'ok': False, 'errors': [err('change', None, str(e))]})
    return results
