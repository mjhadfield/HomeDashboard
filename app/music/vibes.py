"""Vibes: Last.fm's listener tags folded into a couple of dozen named styles ("Reggae & Ska", "Desert & Stoner Rock"), so
Discover can group your artists — and the new ones it finds — by mood rather than by how much you play them.

An artist's vibe is its strongest tag that names one; the catch-all tags ("metal", "rock", "alternative") only count when
nothing more specific does, so No Doubt (rock, ska, pop) lands in Reggae & Ska rather than Classic Rock."""
from __future__ import annotations

VIBES: list[tuple[str, str, frozenset[str]]] = [(vid, name, frozenset(tags)) for vid, name, tags in [
    ("heavy", "Heavy Metal", {"heavy metal", "nwobhm", "speed metal", "traditional heavy metal", "us power metal", "metal"}),
    ("power", "Power & Folk Metal", {"power metal", "folk metal", "viking metal", "pagan metal", "celtic metal", "battle metal", "symphonic power metal",
                                    "pirate metal", "medieval metal"}),
    ("thrash", "Thrash & Groove", {"thrash metal", "groove metal", "crossover thrash", "bay area thrash", "thrash"}),
    ("death", "Death & Melodeath", {"death metal", "melodic death metal", "technical death metal", "brutal death metal", "swedish death metal",
                                   "progressive death metal", "melodeath", "gothenburg metal"}),
    ("black", "Black Metal", {"black metal", "symphonic black metal", "atmospheric black metal", "melodic black metal", "blackgaze"}),
    ("gothic", "Gothic & Symphonic", {"gothic metal", "symphonic metal", "gothic rock", "love metal", "gothic", "darkwave", "female fronted metal", "dark rock"}),
    ("prog", "Prog & Math", {"progressive metal", "djent", "post-metal", "progressive rock", "math rock", "art rock", "experimental", "avant-garde metal"}),
    ("doom", "Doom & Sludge", {"doom metal", "sludge", "sludge metal", "stoner doom", "funeral doom", "drone"}),
    ("desert", "Desert & Stoner Rock", {"stoner rock", "desert rock", "stoner metal", "stoner", "fuzz", "heavy psych"}),
    ("core", "Metalcore & Post-hardcore", {"metalcore", "melodic metalcore", "deathcore", "post-hardcore", "trancecore", "electronicore", "screamo",
                                          "mathcore", "easycore", "nintendocore"}),
    ("nu", "Nu & Alt Metal", {"nu metal", "nu-metal", "alternative metal", "rapcore", "rap metal", "funk metal"}),
    ("industrial", "Industrial", {"industrial metal", "industrial", "industrial rock", "neue deutsche harte", "neue deutsche härte", "ebm", "aggrotech"}),
    ("hard", "Hard Rock & Hair Metal", {"hard rock", "glam metal", "hair metal", "sleaze rock", "aor", "arena rock", "southern rock", "southern metal"}),
    ("classic", "Classic Rock", {"classic rock", "blues rock", "psychedelic rock", "psychedelic", "glam rock", "jazz rock", "blues", "rock and roll", "rock"}),
    ("alt", "Grunge & Alt Rock", {"grunge", "alternative rock", "post-grunge", "alternative", "funk rock"}),
    ("indie", "Indie & Post-punk", {"indie", "indie rock", "indie pop", "britpop", "shoegaze", "post-punk", "new wave", "garage rock"}),
    ("punk", "Punk & Pop Punk", {"punk", "punk rock", "pop punk", "hardcore punk", "skate punk", "emo", "street punk", "oi"}),
    ("reggae", "Reggae & Ska", {"reggae", "ska", "ska punk", "ska-punk", "roots reggae", "dub", "rasta", "dancehall", "rocksteady", "2 tone",
                               "third wave ska", "reggae rock", "lovers rock"}),
    ("dnb", "Drum & Bass", {"drum and bass", "dnb", "drum n bass", "dubstep", "jungle", "liquid funk", "neurofunk", "breakbeat", "brostep"}),
    ("dance", "Dance & Electronic", {"electronic", "edm", "techno", "trance", "house", "electro", "hardstyle", "happy hardcore", "eurodance",
                                    "big beat", "electronica", "dance", "synthwave", "electro house", "rave"}),
    ("pop", "Pop", {"pop", "dance-pop", "synthpop", "synth-pop", "electropop", "rnb", "r&b", "teen pop", "disney"}),
    ("hiphop", "Hip-Hop", {"hip-hop", "hip hop", "hiphop", "rap", "gangsta rap", "west coast", "trap", "grime", "east coast rap"}),
    ("jazz", "Jazz, Swing & Soul", {"jazz", "vocal jazz", "swing", "soul", "funk", "lounge", "big band", "motown", "easy listening", "bossa nova"}),
    ("score", "Soundtracks & Orchestral", {"soundtrack", "video game music", "orchestral", "classical", "score", "film score", "ambient",
                                          "instrumental", "game soundtrack", "neoclassical"}),
    ("folk", "Folk & Country", {"folk", "country", "americana", "bluegrass", "singer-songwriter", "acoustic", "folk rock", "celtic"}),
]]
NAMES = {vid: name for vid, name, _ in VIBES}
TAG_VIBE = {t: vid for vid, _, tags in VIBES for t in tags}
assert len(TAG_VIBE) == sum(len(tags) for _, _, tags in VIBES), "a tag belongs to two vibes"
GENERIC = {"metal", "rock", "alternative"}          # only decide a vibe when nothing more specific does


def of(tags: list[str]) -> str | None:
    """The vibe an artist's tags (strongest first) put it in, or None."""
    for t in [t for t in tags if t not in GENERIC] + [t for t in tags if t in GENERIC]:
        if t in TAG_VIBE:
            return TAG_VIBE[t]
    return None


def fits(tags: list[str], vibe: str) -> bool:
    """Whether an artist's tags put it in this vibe at all — any of its specific tags will do."""
    specific = {TAG_VIBE[t] for t in tags if t in TAG_VIBE and t not in GENERIC}
    return vibe in specific if specific else of(tags) == vibe
