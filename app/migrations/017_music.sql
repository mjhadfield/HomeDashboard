-- The Music page: playlists Citadel designs and keeps on Spotify, what's been matched on Spotify, cached answers from
-- the music services, and Discover feedback. Settings and the Spotify login live in widget_settings ('music',
-- 'music-spotify'), like every other setting, so they move with the database.

-- A playlist's design. `ings` (ingredients, each with a share) and `rules` are JSON, validated by app/music/playlists.py.
CREATE TABLE music_playlists (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    mode         TEXT    NOT NULL DEFAULT 'fixed' CHECK (mode IN ('fixed', 'rot')),
    schedule     TEXT    NOT NULL DEFAULT '{}',              -- rotating: {"every": "daily"|"weekly"|"monthly", "day": 0-6 | 1-28, "at": "HH:MM", "until": "YYYY-MM-DD"?}
    public       INTEGER NOT NULL DEFAULT 1,
    ings         TEXT    NOT NULL DEFAULT '[]',
    rules        TEXT    NOT NULL DEFAULT '{}',
    template     TEXT    NOT NULL DEFAULT '',
    meta         TEXT    NOT NULL DEFAULT '{}',              -- e.g. a pre-gig playlist's gig, a setlist's source
    spotify_id   TEXT,
    spotify_url  TEXT,
    built_at     INTEGER,                                    -- last time the track list was (re)built
    saved_at     INTEGER,                                    -- last time it was written to Spotify
    last_error   TEXT    NOT NULL DEFAULT '',
    sort         INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

-- The current track list, in order. A locked track survives rebuilds; `uri` is empty until matched on Spotify.
CREATE TABLE music_playlist_tracks (
    playlist_id  INTEGER NOT NULL REFERENCES music_playlists(id) ON DELETE CASCADE,
    pos          INTEGER NOT NULL,
    key          TEXT    NOT NULL,                           -- normalised "artist|title", the same key music_matches uses
    title        TEXT    NOT NULL,
    artist       TEXT    NOT NULL,
    album        TEXT    NOT NULL DEFAULT '',
    mbid         TEXT,                                       -- album release-group id, for cover art
    src          TEXT    NOT NULL DEFAULT '',                -- which ingredient picked it
    why          TEXT    NOT NULL DEFAULT '',
    plays        INTEGER NOT NULL DEFAULT 0,
    uri          TEXT    NOT NULL DEFAULT '',
    locked       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, pos)
);

-- What each rotation put in, so "no repeats for N rotations" can look back; and songs taken out of a playlist by hand,
-- which it then leaves out for good.
CREATE TABLE music_playlist_history (
    playlist_id  INTEGER NOT NULL REFERENCES music_playlists(id) ON DELETE CASCADE,
    run          INTEGER NOT NULL,                           -- built_at of that rotation
    key          TEXT    NOT NULL,
    PRIMARY KEY (playlist_id, run, key)
);
CREATE TABLE music_playlist_bans (
    playlist_id  INTEGER NOT NULL REFERENCES music_playlists(id) ON DELETE CASCADE,
    key          TEXT    NOT NULL,
    PRIMARY KEY (playlist_id, key)
);

-- Song -> Spotify track. 'missing' is retried after a while (Spotify's catalogue changes); 'manual' is a pick made
-- by hand and never second-guessed.
CREATE TABLE music_matches (
    key          TEXT    PRIMARY KEY,
    status       TEXT    NOT NULL CHECK (status IN ('ok', 'missing', 'manual')),
    uri          TEXT    NOT NULL DEFAULT '',
    name         TEXT    NOT NULL DEFAULT '',
    artist       TEXT    NOT NULL DEFAULT '',
    album        TEXT    NOT NULL DEFAULT '',
    checked_at   INTEGER NOT NULL
);

-- Answers from Last.fm / Setlist.fm / MusicBrainz, kept so the same question isn't asked twice in a month.
CREATE TABLE music_cache (
    key          TEXT    PRIMARY KEY,                        -- "<service>:<what>"
    json         TEXT    NOT NULL,
    fetched_at   INTEGER NOT NULL
);

-- Discover: artists you've followed from there, or said "not for me" to.
CREATE TABLE music_feedback (
    artist       TEXT    PRIMARY KEY COLLATE NOCASE,
    verdict      TEXT    NOT NULL CHECK (verdict IN ('follow', 'nope')),
    at           INTEGER NOT NULL
);
