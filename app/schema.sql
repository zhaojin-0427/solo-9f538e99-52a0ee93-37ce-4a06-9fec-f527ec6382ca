PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS credentials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no  TEXT    NOT NULL UNIQUE,
    catalog_a   TEXT    NOT NULL,
    catalog_b   TEXT    NOT NULL,
    minutes     INTEGER NOT NULL CHECK (minutes BETWEEN 1 AND 10000),
    locked      INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (catalog_a <> catalog_b)
);

CREATE INDEX IF NOT EXISTS idx_credentials_active ON credentials(active);
CREATE INDEX IF NOT EXISTS idx_credentials_a ON credentials(catalog_a);
CREATE INDEX IF NOT EXISTS idx_credentials_b ON credentials(catalog_b);

CREATE TABLE IF NOT EXISTS plans (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    status             TEXT    NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'confirmed')),
    pair1_a            TEXT    NOT NULL,
    pair1_b            TEXT    NOT NULL,
    pair2_a            TEXT,
    pair2_b            TEXT,
    plan_total         INTEGER NOT NULL,
    solved_version     INTEGER NOT NULL,
    confirmed_version  INTEGER,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    confirmed_at       TEXT,
    CHECK (pair1_a <> pair1_b),
    CHECK ((pair2_a IS NULL AND pair2_b IS NULL) OR
           (pair2_a IS NOT NULL AND pair2_b IS NOT NULL AND pair2_a <> pair2_b))
);

CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);

CREATE TABLE IF NOT EXISTS plan_items (
    plan_id        INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    credential_id  INTEGER NOT NULL,
    voucher_no     TEXT    NOT NULL,
    catalog_a      TEXT    NOT NULL,
    catalog_b      TEXT    NOT NULL,
    minutes        INTEGER NOT NULL,
    PRIMARY KEY (plan_id, credential_id)
);

CREATE TABLE IF NOT EXISTS solve_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status          TEXT    NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'done', 'nosolution', 'error')),
    pair1_a         TEXT    NOT NULL,
    pair1_b         TEXT    NOT NULL,
    pair2_a         TEXT,
    pair2_b         TEXT,
    data_version    INTEGER NOT NULL,
    result_total    INTEGER,
    result_json     TEXT,
    message         TEXT,
    plan_id         INTEGER REFERENCES plans(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON solve_jobs(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Persisted branch-withdrawal trials.  One row per (library, original item
-- group) that would disappear or split if that library's catalogue numbers
-- were temporarily withdrawn.  The web page only reads these tables.
CREATE TABLE IF NOT EXISTS trial_results (
    library_code     TEXT    NOT NULL,
    group_key        TEXT    NOT NULL,
    outcome          TEXT    NOT NULL CHECK (outcome IN ('disappear', 'split')),
    original_size    INTEGER NOT NULL,
    removed_count    INTEGER NOT NULL,
    remaining_groups INTEGER NOT NULL,
    PRIMARY KEY (library_code, group_key)
);

CREATE INDEX IF NOT EXISTS idx_trial_results_group ON trial_results(group_key);

-- Membership of every catalogue number in its original same-item group (the
-- group as it stands without any library withdrawal).  Used to find the old
-- groups affected by a catalogue change without rescanning the whole feed.
CREATE TABLE IF NOT EXISTS trial_group_members (
    catalog_no TEXT PRIMARY KEY,
    group_key  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trial_group_members_key
    ON trial_group_members(group_key);

-- Catalogue numbers per library code (legacy numbers without a code are not
-- listed).  Lets the read-only page distinguish an unknown library from a
-- library with no affected groups.
CREATE TABLE IF NOT EXISTS trial_libraries (
    library_code  TEXT    PRIMARY KEY,
    catalog_count INTEGER NOT NULL
);

INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '0');
