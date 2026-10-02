-- A release date typed in by hand (Confirmed Pre-orders) must survive the next store sync, which otherwise copies the
-- store's own date -- often blank/TBC -- over it. date_set = 1 marks a date as the owner's: syncs leave it alone.
-- Clearing the date (back to TBC) unsets it, so a date the store publishes later can come through again.
ALTER TABLE feed_items ADD COLUMN date_set INTEGER NOT NULL DEFAULT 0;
