-- Screen pairing: an unenrolled TV shows a short code; an owner types it in Settings to approve the screen.
-- Only the hash of the TV's poll secret is stored. Codes expire quickly and only ever mint 'kiosk' devices.
CREATE TABLE pairings (
    code         TEXT    PRIMARY KEY,               -- e.g. 'K7M4QX' (unambiguous alphabet)
    secret_hash  TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'claimed')),
    name         TEXT
);
