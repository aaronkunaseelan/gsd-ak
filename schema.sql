-- gsd row versions. Payload fields are full snapshots; tombstones need only identity.

PRAGMA journal_mode = WAL;

PRAGMA foreign_keys = ON;

CREATE TABLE records (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    change_set_id TEXT NOT NULL,
    verb TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??*Z'),
    id TEXT NOT NULL,
    record_kind TEXT,
    canonical_title TEXT,
    status TEXT,
    source_updated_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    created_by TEXT,
    archived_at TEXT
) STRICT;

CREATE INDEX records_identity_seq ON records (id, seq DESC);

CREATE VIEW cur_records AS SELECT v.* FROM records v WHERE v.op = 'upsert' AND NOT EXISTS (SELECT 1 FROM records newer WHERE v.id = newer.id AND newer.seq > v.seq);

CREATE TRIGGER records_no_update BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT, 'append-only: records'); END;

CREATE TRIGGER records_no_delete BEFORE DELETE ON records BEGIN SELECT RAISE(ABORT, 'append-only: records'); END;

CREATE TRIGGER records_upsert_required BEFORE INSERT ON records WHEN NEW.op = 'upsert' AND (NEW.id IS NULL OR NEW.record_kind IS NULL OR NEW.created_at IS NULL OR NEW.updated_at IS NULL OR NEW.created_by IS NULL) BEGIN SELECT RAISE(ABORT, 'missing snapshot fields: records'); END;

CREATE TABLE people (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    change_set_id TEXT NOT NULL,
    verb TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??*Z'),
    id TEXT NOT NULL,
    display_name TEXT,
    primary_email TEXT,
    other_emails TEXT,
    organization TEXT,
    role_title TEXT,
    relationship_type TEXT,
    expected_response_window TEXT,
    status TEXT,
    summary TEXT,
    metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    source_updated_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    body_markdown TEXT
) STRICT;

CREATE INDEX people_identity_seq ON people (id, seq DESC);

CREATE VIEW cur_people AS SELECT v.* FROM people v WHERE v.op = 'upsert' AND NOT EXISTS (SELECT 1 FROM people newer WHERE v.id = newer.id AND newer.seq > v.seq);

CREATE TRIGGER people_no_update BEFORE UPDATE ON people BEGIN SELECT RAISE(ABORT, 'append-only: people'); END;

CREATE TRIGGER people_no_delete BEFORE DELETE ON people BEGIN SELECT RAISE(ABORT, 'append-only: people'); END;

CREATE TRIGGER people_upsert_required BEFORE INSERT ON people WHEN NEW.op = 'upsert' AND (NEW.id IS NULL OR NEW.display_name IS NULL OR NEW.other_emails IS NULL OR NEW.status IS NULL OR NEW.metadata_json IS NULL OR NEW.created_at IS NULL OR NEW.updated_at IS NULL) BEGIN SELECT RAISE(ABORT, 'missing snapshot fields: people'); END;

CREATE TABLE projects (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    change_set_id TEXT NOT NULL,
    verb TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??*Z'),
    id TEXT NOT NULL,
    name TEXT,
    slug TEXT,
    status TEXT,
    owner_person_id TEXT,
    priority INTEGER,
    desired_outcome TEXT,
    current_state TEXT,
    next_action TEXT,
    risk_summary TEXT,
    horizon TEXT,
    metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    source_updated_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    summary TEXT,
    body_markdown TEXT
) STRICT;

CREATE INDEX projects_identity_seq ON projects (id, seq DESC);

CREATE VIEW cur_projects AS SELECT v.* FROM projects v WHERE v.op = 'upsert' AND NOT EXISTS (SELECT 1 FROM projects newer WHERE v.id = newer.id AND newer.seq > v.seq);

CREATE TRIGGER projects_no_update BEFORE UPDATE ON projects BEGIN SELECT RAISE(ABORT, 'append-only: projects'); END;

CREATE TRIGGER projects_no_delete BEFORE DELETE ON projects BEGIN SELECT RAISE(ABORT, 'append-only: projects'); END;

CREATE TRIGGER projects_upsert_required BEFORE INSERT ON projects WHEN NEW.op = 'upsert' AND (NEW.id IS NULL OR NEW.name IS NULL OR NEW.status IS NULL OR NEW.priority IS NULL OR NEW.metadata_json IS NULL OR NEW.created_at IS NULL OR NEW.updated_at IS NULL) BEGIN SELECT RAISE(ABORT, 'missing snapshot fields: projects'); END;

CREATE TABLE items (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    change_set_id TEXT NOT NULL,
    verb TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??*Z'),
    id TEXT NOT NULL,
    kind TEXT,
    title TEXT,
    description TEXT,
    notes TEXT,
    project_id TEXT,
    parent_item_id TEXT,
    parent_project_item_id TEXT,
    source_kind TEXT,
    source_key TEXT,
    source_type TEXT,
    assigned_to TEXT,
    priority INTEGER,
    urgency TEXT,
    effort_band TEXT,
    estimated_minutes INTEGER,
    deadline_reason TEXT,
    thread_url TEXT,
    latest_deliverable_url TEXT,
    waiting_on TEXT,
    state TEXT,
    next_actor TEXT,
    calendar_event_id TEXT,
    schedule_status TEXT,
    planned_start TEXT,
    planned_end TEXT,
    source_updated_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    record_status TEXT,
    summary TEXT,
    desired_outcome TEXT,
    due_at TEXT,
    next_review_at TEXT,
    closed_at TEXT,
    body_markdown TEXT,
    next_actions TEXT
) STRICT;

CREATE INDEX items_identity_seq ON items (id, seq DESC);

CREATE VIEW cur_items AS SELECT v.* FROM items v WHERE v.op = 'upsert' AND NOT EXISTS (SELECT 1 FROM items newer WHERE v.id = newer.id AND newer.seq > v.seq);

CREATE TRIGGER items_no_update BEFORE UPDATE ON items BEGIN SELECT RAISE(ABORT, 'append-only: items'); END;

CREATE TRIGGER items_no_delete BEFORE DELETE ON items BEGIN SELECT RAISE(ABORT, 'append-only: items'); END;

CREATE TRIGGER items_upsert_required BEFORE INSERT ON items WHEN NEW.op = 'upsert' AND (NEW.id IS NULL OR NEW.kind IS NULL OR NEW.title IS NULL OR NEW.priority IS NULL OR NEW.state IS NULL OR NEW.next_actor IS NULL OR NEW.created_at IS NULL OR NEW.updated_at IS NULL OR NEW.record_status IS NULL) BEGIN SELECT RAISE(ABORT, 'missing snapshot fields: items'); END;

CREATE TABLE meetings (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    change_set_id TEXT NOT NULL,
    verb TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??*Z'),
    id TEXT NOT NULL,
    title TEXT,
    starts_at TEXT,
    ends_at TEXT,
    start_date TEXT,
    end_date TEXT,
    is_all_day INTEGER CHECK (is_all_day IN (0,1)),
    location TEXT,
    status TEXT,
    source_kind TEXT,
    source_key TEXT,
    summary TEXT,
    attendees_json TEXT CHECK (attendees_json IS NULL OR json_valid(attendees_json)),
    metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    source_updated_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    body_markdown TEXT,
    logistics_details TEXT,
    action_points TEXT,
    previous_action_points TEXT,
    duration_minutes INTEGER
) STRICT;

CREATE INDEX meetings_identity_seq ON meetings (id, seq DESC);

CREATE VIEW cur_meetings AS SELECT v.* FROM meetings v WHERE v.op = 'upsert' AND NOT EXISTS (SELECT 1 FROM meetings newer WHERE v.id = newer.id AND newer.seq > v.seq);

CREATE TRIGGER meetings_no_update BEFORE UPDATE ON meetings BEGIN SELECT RAISE(ABORT, 'append-only: meetings'); END;

CREATE TRIGGER meetings_no_delete BEFORE DELETE ON meetings BEGIN SELECT RAISE(ABORT, 'append-only: meetings'); END;

CREATE TRIGGER meetings_upsert_required BEFORE INSERT ON meetings WHEN NEW.op = 'upsert' AND (NEW.id IS NULL OR NEW.title IS NULL OR NEW.is_all_day IS NULL OR NEW.status IS NULL OR NEW.attendees_json IS NULL OR NEW.metadata_json IS NULL OR NEW.created_at IS NULL OR NEW.updated_at IS NULL) BEGIN SELECT RAISE(ABORT, 'missing snapshot fields: meetings'); END;

CREATE TABLE record_links (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    change_set_id TEXT NOT NULL,
    verb TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??*Z'),
    from_record_id TEXT NOT NULL,
    to_record_id TEXT NOT NULL,
    relation_kind TEXT NOT NULL,
    directionality TEXT,
    rank INTEGER,
    is_primary INTEGER CHECK (is_primary IN (0,1)),
    confidence_score TEXT,
    evidence_summary TEXT,
    metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT
) STRICT;

CREATE INDEX record_links_identity_seq ON record_links (from_record_id, to_record_id, relation_kind, seq DESC);

CREATE VIEW cur_record_links AS SELECT v.* FROM record_links v WHERE v.op = 'upsert' AND NOT EXISTS (SELECT 1 FROM record_links newer WHERE v.from_record_id = newer.from_record_id AND v.to_record_id = newer.to_record_id AND v.relation_kind = newer.relation_kind AND newer.seq > v.seq);

CREATE TRIGGER record_links_no_update BEFORE UPDATE ON record_links BEGIN SELECT RAISE(ABORT, 'append-only: record_links'); END;

CREATE TRIGGER record_links_no_delete BEFORE DELETE ON record_links BEGIN SELECT RAISE(ABORT, 'append-only: record_links'); END;

CREATE TRIGGER record_links_upsert_required BEFORE INSERT ON record_links WHEN NEW.op = 'upsert' AND (NEW.from_record_id IS NULL OR NEW.to_record_id IS NULL OR NEW.relation_kind IS NULL OR NEW.directionality IS NULL OR NEW.is_primary IS NULL OR NEW.metadata_json IS NULL OR NEW.created_by IS NULL OR NEW.created_at IS NULL OR NEW.updated_at IS NULL) BEGIN SELECT RAISE(ABORT, 'missing snapshot fields: record_links'); END;

CREATE TABLE source_refs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    change_set_id TEXT NOT NULL,
    verb TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL CHECK (at GLOB '????-??-??T??:??:??*Z'),
    source_ref_id INTEGER NOT NULL,
    record_id TEXT,
    source_system TEXT,
    source_surface TEXT,
    source_key TEXT,
    source_version TEXT,
    source_url TEXT,
    source_hash TEXT,
    evidence_qmd_event_id TEXT,
    role TEXT,
    confidence_score TEXT,
    excerpt TEXT,
    metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT
) STRICT;

CREATE INDEX source_refs_identity_seq ON source_refs (source_ref_id, seq DESC);

CREATE VIEW cur_source_refs AS SELECT v.* FROM source_refs v WHERE v.op = 'upsert' AND NOT EXISTS (SELECT 1 FROM source_refs newer WHERE v.source_ref_id = newer.source_ref_id AND newer.seq > v.seq);

CREATE TRIGGER source_refs_no_update BEFORE UPDATE ON source_refs BEGIN SELECT RAISE(ABORT, 'append-only: source_refs'); END;

CREATE TRIGGER source_refs_no_delete BEFORE DELETE ON source_refs BEGIN SELECT RAISE(ABORT, 'append-only: source_refs'); END;

CREATE TRIGGER source_refs_upsert_required BEFORE INSERT ON source_refs WHEN NEW.op = 'upsert' AND (NEW.source_ref_id IS NULL OR NEW.record_id IS NULL OR NEW.source_system IS NULL OR NEW.source_surface IS NULL OR NEW.source_key IS NULL OR NEW.role IS NULL OR NEW.metadata_json IS NULL OR NEW.created_by IS NULL OR NEW.created_at IS NULL OR NEW.updated_at IS NULL) BEGIN SELECT RAISE(ABORT, 'missing snapshot fields: source_refs'); END;

CREATE TRIGGER people_id_reference BEFORE INSERT ON people WHEN NEW.id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER projects_id_reference BEFORE INSERT ON projects WHEN NEW.id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER projects_owner_person_id_reference BEFORE INSERT ON projects WHEN NEW.owner_person_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.owner_person_id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER items_id_reference BEFORE INSERT ON items WHEN NEW.id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER items_project_id_reference BEFORE INSERT ON items WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.project_id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER items_parent_item_id_reference BEFORE INSERT ON items WHEN NEW.parent_item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.parent_item_id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER items_parent_project_item_id_reference BEFORE INSERT ON items WHEN NEW.parent_project_item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.parent_project_item_id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER meetings_id_reference BEFORE INSERT ON meetings WHEN NEW.id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER record_links_from_record_id_reference BEFORE INSERT ON record_links WHEN NEW.from_record_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.from_record_id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER record_links_to_record_id_reference BEFORE INSERT ON record_links WHEN NEW.to_record_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.to_record_id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

CREATE TRIGGER source_refs_record_id_reference BEFORE INSERT ON source_refs WHEN NEW.record_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.record_id) BEGIN SELECT RAISE(ABORT, 'unknown stable record id'); END;

-- Explicit sequences cannot replace existing history, even via INSERT OR REPLACE.
CREATE TRIGGER records_sequence_guard BEFORE INSERT ON records WHEN NEW.seq != -1 AND NEW.seq <= COALESCE((SELECT MAX(seq) FROM records), 0) BEGIN SELECT RAISE(ABORT, 'sequence must advance'); END;

-- Explicit sequences cannot replace existing history, even via INSERT OR REPLACE.
CREATE TRIGGER people_sequence_guard BEFORE INSERT ON people WHEN NEW.seq != -1 AND NEW.seq <= COALESCE((SELECT MAX(seq) FROM people), 0) BEGIN SELECT RAISE(ABORT, 'sequence must advance'); END;

-- Explicit sequences cannot replace existing history, even via INSERT OR REPLACE.
CREATE TRIGGER projects_sequence_guard BEFORE INSERT ON projects WHEN NEW.seq != -1 AND NEW.seq <= COALESCE((SELECT MAX(seq) FROM projects), 0) BEGIN SELECT RAISE(ABORT, 'sequence must advance'); END;

-- Explicit sequences cannot replace existing history, even via INSERT OR REPLACE.
CREATE TRIGGER items_sequence_guard BEFORE INSERT ON items WHEN NEW.seq != -1 AND NEW.seq <= COALESCE((SELECT MAX(seq) FROM items), 0) BEGIN SELECT RAISE(ABORT, 'sequence must advance'); END;

-- Explicit sequences cannot replace existing history, even via INSERT OR REPLACE.
CREATE TRIGGER meetings_sequence_guard BEFORE INSERT ON meetings WHEN NEW.seq != -1 AND NEW.seq <= COALESCE((SELECT MAX(seq) FROM meetings), 0) BEGIN SELECT RAISE(ABORT, 'sequence must advance'); END;

-- Explicit sequences cannot replace existing history, even via INSERT OR REPLACE.
CREATE TRIGGER record_links_sequence_guard BEFORE INSERT ON record_links WHEN NEW.seq != -1 AND NEW.seq <= COALESCE((SELECT MAX(seq) FROM record_links), 0) BEGIN SELECT RAISE(ABORT, 'sequence must advance'); END;

-- Explicit sequences cannot replace existing history, even via INSERT OR REPLACE.
CREATE TRIGGER source_refs_sequence_guard BEFORE INSERT ON source_refs WHEN NEW.seq != -1 AND NEW.seq <= COALESCE((SELECT MAX(seq) FROM source_refs), 0) BEGIN SELECT RAISE(ABORT, 'sequence must advance'); END;

CREATE TRIGGER records_record_kind_enum BEFORE INSERT ON records WHEN NEW.record_kind IS NOT NULL AND NEW.record_kind NOT IN ('person', 'project', 'item', 'meeting') BEGIN SELECT RAISE(ABORT, 'invalid record_kind'); END;

CREATE TRIGGER people_status_enum BEFORE INSERT ON people WHEN NEW.status IS NOT NULL AND NEW.status NOT IN ('active', 'inactive', 'unknown') BEGIN SELECT RAISE(ABORT, 'invalid status'); END;

CREATE TRIGGER projects_horizon_enum BEFORE INSERT ON projects WHEN NEW.horizon IS NOT NULL AND NEW.horizon NOT IN ('day', 'week', 'month', 'quarter', 'project', 'ongoing') BEGIN SELECT RAISE(ABORT, 'invalid horizon'); END;

CREATE TRIGGER projects_status_enum BEFORE INSERT ON projects WHEN NEW.status IS NOT NULL AND NEW.status NOT IN ('active', 'on_hold', 'done', 'cancelled', 'unknown', 'archived') BEGIN SELECT RAISE(ABORT, 'invalid status'); END;

CREATE TRIGGER projects_priority_range BEFORE INSERT ON projects WHEN NEW.priority IS NOT NULL AND (NEW.priority NOT BETWEEN 0 AND 100) BEGIN SELECT RAISE(ABORT, 'invalid priority'); END;

CREATE TRIGGER items_next_actor_enum BEFORE INSERT ON items WHEN NEW.next_actor IS NOT NULL AND NEW.next_actor NOT IN ('agent', 'user', 'external', 'none') BEGIN SELECT RAISE(ABORT, 'invalid next_actor'); END;

CREATE TRIGGER items_state_enum BEFORE INSERT ON items WHEN NEW.state IS NOT NULL AND NEW.state NOT IN ('inbox', 'ready', 'running', 'blocked', 'done', 'canceled') BEGIN SELECT RAISE(ABORT, 'invalid state'); END;

CREATE TRIGGER items_record_status_enum BEFORE INSERT ON items WHEN NEW.record_status IS NOT NULL AND NEW.record_status NOT IN ('open', 'done', 'cancelled') BEGIN SELECT RAISE(ABORT, 'invalid record_status'); END;

CREATE TRIGGER items_priority_range BEFORE INSERT ON items WHEN NEW.priority IS NOT NULL AND (NEW.priority NOT BETWEEN 0 AND 100) BEGIN SELECT RAISE(ABORT, 'invalid priority'); END;

CREATE TRIGGER items_estimated_minutes_range BEFORE INSERT ON items WHEN NEW.estimated_minutes IS NOT NULL AND (NEW.estimated_minutes < 0) BEGIN SELECT RAISE(ABORT, 'invalid estimated_minutes'); END;

CREATE TRIGGER meetings_status_enum BEFORE INSERT ON meetings WHEN NEW.status IS NOT NULL AND NEW.status NOT IN ('scheduled', 'completed', 'cancelled', 'unknown') BEGIN SELECT RAISE(ABORT, 'invalid status'); END;

CREATE TRIGGER meetings_duration_minutes_range BEFORE INSERT ON meetings WHEN NEW.duration_minutes IS NOT NULL AND (NEW.duration_minutes < 0) BEGIN SELECT RAISE(ABORT, 'invalid duration_minutes'); END;

CREATE TRIGGER record_links_directionality_enum BEFORE INSERT ON record_links WHEN NEW.directionality IS NOT NULL AND NEW.directionality NOT IN ('directed', 'undirected') BEGIN SELECT RAISE(ABORT, 'invalid directionality'); END;

CREATE TRIGGER record_links_confidence_score_range BEFORE INSERT ON record_links WHEN NEW.confidence_score IS NOT NULL AND (CAST(NEW.confidence_score AS REAL) NOT BETWEEN 0 AND 1) BEGIN SELECT RAISE(ABORT, 'invalid confidence_score'); END;

CREATE TRIGGER source_refs_role_enum BEFORE INSERT ON source_refs WHEN NEW.role IS NOT NULL AND NEW.role NOT IN ('canonical', 'states', 'derived_from', 'directed_by', 'context') BEGIN SELECT RAISE(ABORT, 'invalid role'); END;

CREATE TRIGGER source_refs_confidence_score_range BEFORE INSERT ON source_refs WHEN NEW.confidence_score IS NOT NULL AND (CAST(NEW.confidence_score AS REAL) NOT BETWEEN 0 AND 1) BEGIN SELECT RAISE(ABORT, 'invalid confidence_score'); END;

-- D7: pipeline cursor and change provenance. items.kind remains free SQL text.
CREATE TABLE change_sets (
  change_set_id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
  origin TEXT NOT NULL CHECK (origin IN ('pipeline','agent','owner','port')),
  step_id TEXT, window_until TEXT, note TEXT
) STRICT;
PRAGMA user_version = 1;
