-- Reminders the owner has dismissed from the top strip. `key` names one occurrence (e.g. "bin:blue:2026-10-29",
-- "release:812"), so next week's bin or the next race comes back on its own. Old rows are tidied nightly.
CREATE TABLE alert_dismissals (
    key           TEXT    PRIMARY KEY,
    dismissed_at  INTEGER NOT NULL
);
