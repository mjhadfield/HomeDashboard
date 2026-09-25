-- A vinyl item can now be marked "Purchased" — a confirmed pre-order, tracked separately (and reachable to
-- add by hand too, for records bought somewhere this app doesn't watch) until it arrives and is removed.
-- SQLite can't ALTER a CHECK constraint, so the table is rebuilt: same columns, same index, one more allowed
-- status. Every existing row (and its current status) carries across unchanged — this is the first migration
-- in this app that rebuilds a table rather than ALTER TABLE ADD/DROP COLUMN.
CREATE TABLE feed_items_new (
    id           INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL CHECK (kind IN ('release', 'vinyl', 'radar', 'news')),
    source       TEXT    NOT NULL,
    external_id  TEXT    NOT NULL,
    artist       TEXT,
    title        TEXT    NOT NULL,
    subtitle     TEXT    NOT NULL DEFAULT '',
    url          TEXT,
    date         TEXT,
    meta         TEXT    NOT NULL DEFAULT '{}',
    status       TEXT    NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'seen', 'pinned', 'dismissed', 'purchased')),
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    UNIQUE (kind, source, external_id)
);

INSERT INTO feed_items_new (id, kind, source, external_id, artist, title, subtitle, url, date, meta, status, first_seen, last_seen)
    SELECT id, kind, source, external_id, artist, title, subtitle, url, date, meta, status, first_seen, last_seen FROM feed_items;

DROP TABLE feed_items;
ALTER TABLE feed_items_new RENAME TO feed_items;
CREATE INDEX idx_feed_kind ON feed_items (kind, status, date);
