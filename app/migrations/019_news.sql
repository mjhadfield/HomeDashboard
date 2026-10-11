-- The News page learns what you like (👍/👎 per story) and remembers what you never want to see again.
-- Stories themselves stay in feed_items (kind = 'news'); their topics, vote and publish time live in its meta.

-- One row per topic a vote has touched. `key` is "<kind>:<casefolded label>" (kind p = a name in a headline,
-- c = the feed's own category, w = a plain word, s = the source); `label` keeps the casing for display.
-- `weight` decays with time when read (see app/newsrank.py), from `updated_at`.
CREATE TABLE news_topic_weights (
    key         TEXT    PRIMARY KEY,
    label       TEXT    NOT NULL,
    weight      REAL    NOT NULL DEFAULT 0,
    votes       INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL
);

-- "Never show": a topic or a source, everywhere or in one section only.
CREATE TABLE news_rules (
    id          INTEGER PRIMARY KEY,
    term        TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'topic' CHECK (kind IN ('topic', 'source')),
    section     TEXT,                                   -- NULL = every section
    created_at  INTEGER NOT NULL,
    UNIQUE (term, kind, section)
);
