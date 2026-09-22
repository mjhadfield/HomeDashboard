-- Releases now asks MusicBrainz for albums only, up to a year ahead (the old query took the first 50 hits of every release
-- type, so a busy artist could crowd an upcoming album out). Look at every followed artist again with the new query. The
-- re-look is a silent baseline, so nothing that was already known lights up as "New"; pins and dismissals are untouched.
UPDATE followed_artists SET mb_checked_at = NULL;
