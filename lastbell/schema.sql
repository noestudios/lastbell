-- lastbell schema (SQLite). Two join tables carry the design:
--   watcher_student     -> multi-watcher (many people per student)
--   credential_student  -> multi-account (many ParentVUE logins per household)

CREATE TABLE IF NOT EXISTS credentials (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('parentvue', 'studentvue')),
    holder      TEXT NOT NULL,           -- human label, e.g. "Mom's login"
    username    TEXT NOT NULL,
    secret_ref  TEXT NOT NULL,           -- pointer into keyring/secret store, NEVER the password
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS students (
    id           TEXT PRIMARY KEY,
    agu          TEXT NOT NULL UNIQUE,   -- dedupe key across credentials
    name         TEXT NOT NULL,
    initials     TEXT NOT NULL DEFAULT '',
    school       TEXT NOT NULL DEFAULT '',
    current_term TEXT NOT NULL DEFAULT ''  -- marking period as of the last pass
);

CREATE TABLE IF NOT EXISTS credential_student (
    credential_id TEXT NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
    student_id    TEXT NOT NULL REFERENCES students(id)    ON DELETE CASCADE,
    PRIMARY KEY (credential_id, student_id)
);

CREATE TABLE IF NOT EXISTS watchers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('guardian', 'student')),
    channels    TEXT NOT NULL DEFAULT '{}',   -- json: how to reach them
    quiet_hours TEXT NOT NULL DEFAULT '{}'    -- json: suppression window
);

CREATE TABLE IF NOT EXISTS watcher_student (
    watcher_id TEXT NOT NULL REFERENCES watchers(id) ON DELETE CASCADE,
    student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    PRIMARY KEY (watcher_id, student_id)
);

CREATE TABLE IF NOT EXISTS courses (
    id          TEXT PRIMARY KEY,
    edupoint_gu TEXT NOT NULL,
    student_id  TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    teacher     TEXT NOT NULL DEFAULT '',
    term        TEXT NOT NULL DEFAULT '',
    mark        TEXT NOT NULL DEFAULT '',
    percent     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'parentvue',  -- 'parentvue' | 'canvas'
    UNIQUE (student_id, edupoint_gu, term)
);

CREATE TABLE IF NOT EXISTS assignments (
    id          TEXT PRIMARY KEY,
    edupoint_gu TEXT NOT NULL,           -- stable natural key; the differ keys on this
    course_id   TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    assigned    TEXT,
    due_date    TEXT,
    graded_at   TEXT,
    score       REAL,
    points      REAL,
    status      TEXT NOT NULL DEFAULT 'due',
    source      TEXT NOT NULL DEFAULT 'parentvue',  -- 'parentvue' | 'canvas'
    superseded_by TEXT NOT NULL DEFAULT '',        -- gradebook twin's GUID (Canvas rows)
    UNIQUE (course_id, edupoint_gu)
);

-- Append-only change log powering "what changed and when".
CREATE TABLE IF NOT EXISTS grade_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    field         TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    seen_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Course-level counterpart: the overall mark/percent trajectory. Without
-- this the course grade is upserted in place and its history is lost — and
-- unlike parsing rules, history that wasn't captured can't be backfilled.
-- This is the series any future trend chart draws from.
CREATE TABLE IF NOT EXISTS course_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id           TEXT PRIMARY KEY,
    watcher_id   TEXT NOT NULL REFERENCES watchers(id) ON DELETE CASCADE,
    student_id   TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    alert_type   TEXT NOT NULL,
    channel      TEXT NOT NULL,
    send_at      TEXT,                    -- HH:MM: digest/summary delivery time (NULL = immediate)
    last_sent_on TEXT,                    -- date the last scheduled send went out (summary gating)
    urgent_now   INTEGER NOT NULL DEFAULT 0  -- urgent alert types skip the digest wait
);

CREATE TABLE IF NOT EXISTS alerts (
    id         TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Deferred deliveries (Phase 4): digests and quiet-hours holdbacks. A row is
-- one event line owed to one watcher over one channel; the flusher groups rows
-- into a single message per (watcher, channel). Unsent rows retry every tick.
CREATE TABLE IF NOT EXISTS outbox (
    id         TEXT PRIMARY KEY,
    watcher_id TEXT NOT NULL REFERENCES watchers(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL,
    student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL,
    detail     TEXT NOT NULL,
    queued_at  TEXT NOT NULL DEFAULT (datetime('now')),
    send_after TEXT NOT NULL,             -- local ISO timestamp when eligible
    sent_at    TEXT
);

-- Small facts about the install itself (not student data). Today: when the
-- last poll finished, so the dashboard can say how fresh it is.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
