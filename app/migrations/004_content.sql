-- Content cards: artists you follow, the things found about them, and your booked gigs.

CREATE TABLE followed_artists (
    id                INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    mbid              TEXT,                                   -- MusicBrainz artist id (resolved lazily if unknown)
    status            TEXT    NOT NULL DEFAULT 'follow' CHECK (status IN ('follow', 'pin', 'mute')),
    source            TEXT    NOT NULL DEFAULT 'manual',      -- 'music-companion' | 'manual'
    weight            REAL    NOT NULL DEFAULT 0,             -- listening/collection/gig score, for ordering + capping
    mb_checked_at     INTEGER,                                -- last release check per source (NULL = never: first pass is a silent baseline)
    vinyl_checked_at  INTEGER,
    radar_checked_at  INTEGER,
    created_at        INTEGER NOT NULL
);

-- Everything discovered: releases, vinyl pressings, tour dates, headlines. `status` is the curation state and is
-- never overwritten by a refresh, so pins and dismissals survive.
CREATE TABLE feed_items (
    id           INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL CHECK (kind IN ('release', 'vinyl', 'radar', 'news')),
    source       TEXT    NOT NULL,                            -- 'musicbrainz' | 'discogs' | 'ticketmaster' | a feed id
    external_id  TEXT    NOT NULL,
    artist       TEXT,
    title        TEXT    NOT NULL,
    subtitle     TEXT    NOT NULL DEFAULT '',
    url          TEXT,
    date         TEXT,                                        -- YYYY-MM-DD, or YYYY-MM / YYYY when that's all the source knows
    meta         TEXT    NOT NULL DEFAULT '{}',
    status       TEXT    NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'seen', 'pinned', 'dismissed')),
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    UNIQUE (kind, source, external_id)
);
CREATE INDEX idx_feed_kind ON feed_items (kind, status, date);

-- Gigs you have tickets for.
CREATE TABLE gigs (
    id          INTEGER PRIMARY KEY,
    artist      TEXT    NOT NULL,
    venue       TEXT    NOT NULL DEFAULT '',
    city        TEXT    NOT NULL DEFAULT '',
    date        TEXT    NOT NULL,                             -- YYYY-MM-DD
    doors       TEXT    NOT NULL DEFAULT '',                  -- HH:MM
    ticket_url  TEXT,
    notes       TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_gigs_date ON gigs (date);
