"""The Music page: Spotify playback control, playlists Citadel designs from your own listening data, Discover, and
gig setlists.

    store.py        settings (Settings → Music page), the response cache, change notifications
    services/       one module per outside service (Spotify, Last.fm, Setlist.fm, MusicBrainz, Music Companion) —
                    drop a module in to add one; Settings and the status list pick it up by themselves
    library.py      read-only queries against Music Companion's database (your scrobbles, records and gigs)
    matcher.py      song -> Spotify track, remembered
    ingredients.py  the things a playlist can be made of; also a registry
    playlists.py    designs, building, saving to Spotify, rotation
    discover.py     artists you'd probably like, and ones you've drifted from
    gigs.py         predicted and real setlists
    rotator.py      the background loop that rebuilds rotating playlists on schedule
"""
