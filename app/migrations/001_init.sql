-- Citadel schema, v1.
-- Anything the owner configures in the UI lives here, so moving the dashboard is: copy data/hub.sqlite.

-- Enrolled devices. Anyone without a valid device cookie is a guest (no row needed).
CREATE TABLE devices (
    id          TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('owner', 'kiosk')),
    user_agent  TEXT,
    created_at  INTEGER NOT NULL,
    last_seen   INTEGER,
    revoked     INTEGER NOT NULL DEFAULT 0
);

-- One-time enrolment tokens (only a hash is stored).
CREATE TABLE enrol_tokens (
    token_hash  TEXT    PRIMARY KEY,
    role        TEXT    NOT NULL CHECK (role IN ('owner', 'kiosk')),
    name        TEXT    NOT NULL,
    expires_at  INTEGER NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);

-- Things the dashboard can start/stop. Commands are argv lists, never shell strings.
CREATE TABLE projects (
    id           INTEGER PRIMARY KEY,
    slug         TEXT    NOT NULL UNIQUE,            -- also names the systemd unit: hub-proj-<slug>
    name         TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL CHECK (kind IN ('web', 'desktop')),
    url          TEXT,                               -- may contain {host} (browser's host) / {ip} (server LAN ip)
    port         INTEGER,                            -- TCP port probed on 127.0.0.1 for health (web only)
    cwd          TEXT    NOT NULL,
    start_argv   TEXT    NOT NULL,                   -- JSON array
    env          TEXT    NOT NULL DEFAULT '{}',      -- JSON object
    lan_visible  INTEGER NOT NULL DEFAULT 1,         -- 0 = only reachable from this PC
    sort         INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1
);

-- Launchpad tiles.
CREATE TABLE links (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    subtitle    TEXT    NOT NULL DEFAULT '',
    url         TEXT    NOT NULL,
    icon        TEXT    NOT NULL DEFAULT 'globe',
    health_url  TEXT,                                -- optional; polled server-side for the status dot
    is_primary  INTEGER NOT NULL DEFAULT 0,
    show_guest  INTEGER NOT NULL DEFAULT 0,          -- shown on the guest page
    tier        TEXT    NOT NULL DEFAULT 'main' CHECK (tier IN ('main', 'more')),  -- 'more' = expanded view only
    sort        INTEGER NOT NULL DEFAULT 0
);

-- Per-widget settings (JSON). May contain secrets, hence the 0600 data dir.
CREATE TABLE widget_settings (
    widget  TEXT PRIMARY KEY,
    json    TEXT NOT NULL DEFAULT '{}'
);

-- Last good result per widget; a failed refresh keeps `data` and records `error`.
CREATE TABLE widget_cache (
    widget      TEXT PRIMARY KEY,
    data        TEXT,
    fetched_at  INTEGER,
    error       TEXT,
    error_at    INTEGER
);
