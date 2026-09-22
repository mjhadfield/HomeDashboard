-- The event log (what the dashboard did and what went wrong, shown in Settings → Maintenance) and per-job run state
-- (when each sync last ran / last succeeded, how many attempts in a row have failed) so a restart no longer re-runs
-- everything: a job is only due when its schedule says so.
CREATE TABLE event_log (
    id      INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,
    level   TEXT    NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    source  TEXT    NOT NULL,                 -- a widget id ('releases'), 'maintenance', 'scheduler', 'server', 'app'
    message TEXT    NOT NULL,
    detail  TEXT
);
CREATE INDEX idx_event_log_ts ON event_log (ts);
CREATE INDEX idx_event_log_source ON event_log (source, ts);

CREATE TABLE job_state (
    job           TEXT    PRIMARY KEY,        -- a widget id, or 'maint-backup' etc.
    last_run      INTEGER,                    -- last attempt (success or not)
    last_ok       INTEGER,                    -- last success
    last_seconds  REAL,
    streak        INTEGER NOT NULL DEFAULT 0, -- consecutive failed attempts (0 = healthy)
    failing_since INTEGER,
    message       TEXT    NOT NULL DEFAULT '' -- last result summary, or the last error
);

-- Existing installs: treat the last cached fetch as the last run, so the first start after this update doesn't re-run everything.
INSERT OR IGNORE INTO job_state (job, last_run, last_ok, streak, message)
    SELECT widget, fetched_at, fetched_at, 0, '' FROM widget_cache WHERE fetched_at IS NOT NULL;
