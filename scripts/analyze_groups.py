import sqlite3, re, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('data/.registry_working.db')
c = conn.cursor()

c.execute("""
    SELECT s.song_id, s.channel_id, s.canonical_title, ch.display_name
    FROM songs s
    JOIN channels ch ON ch.channel_id = s.channel_id
    ORDER BY s.song_id
""")
rows = c.fetchall()

BRACKET_RE = re.compile(r'[\(\[\{][^\)\]\}]*[\)\]\}]')
WHITESPACE_RE = re.compile(r'\s+')

PRESENTATION_WORDS = [
    'official mv', 'official music video', 'official video', 'official audio',
    'official lyric video', 'official lyrics video', 'music video',
    'live clip', 'lyric video', 'dance practice', 'performance video',
    'performance film', 'choreography video', 'stage clip',
    'visual clip', 'mv', 'visualizer', 'visualiser',
]

def normalize(title):
    t = title
    t = BRACKET_RE.sub(' ', t)
    t = t.lower()
    for phrase in PRESENTATION_WORDS:
        t = re.sub(r'\b' + re.escape(phrase) + r'\b', ' ', t)
    t = re.sub(r'\b(official|audio|video|clip|ver|version|stage|live)\b', ' ', t)
    t = re.sub(r'[^\w\s\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\uac00-\ud7af]', ' ', t, flags=re.UNICODE)
    t = WHITESPACE_RE.sub(' ', t).strip()
    return t

groups = defaultdict(list)
for song_id, channel_id, canonical_title, display_name in rows:
    norm = normalize(canonical_title)
    if norm and len(norm) >= 2:
        groups[(channel_id, norm)].append((song_id, canonical_title, display_name))

dupes = [(k, v) for k, v in groups.items() if len(v) > 1]

REMIX_RE = re.compile(r'\bremix\b', re.IGNORECASE)
LANG_VER_RE = re.compile(
    r'\((korean|chinese|japanese|english|mandarin)\s+ver\.?\)|\[(korean|chinese|japanese|english|mandarin)\s+ver\.?\]',
    re.IGNORECASE
)
MAKING_RE = re.compile(r'\bmaking\b', re.IGNORECASE)
SPED_UP_RE = re.compile(r'\bsped\s+up\b', re.IGNORECASE)
VOCAL_VER_RE = re.compile(r'\(vocal\s+ver\.?\)', re.IGNORECASE)
INSTRUMENTAL_RE = re.compile(r'\((instrumental|inst\.?|karaoke|acoustic|a\s+cappella)\)', re.IGNORECASE)
LYRIC_ASMR_RE = re.compile(r'lyric\s+asmr', re.IGNORECASE)

KNOWN_REMIX_PATTERNS = [
    'ofenbach ver', 'steve aoki ver', 'james carter ver', 'leez ver', 'ollounder ver',
    'yaeji ver', 'toppings drift ver',
    'flava d remix', 'seeb remix', 'fenner remix', 'kenia os remix', 'timbaland remix',
    'tak remix', 'hitchhiker remix', 'nitepunk remix', 'zedd remix', 'yellow claw remix',
    'grimes remix', 'jafunk remix', 'cifika remix', 'darius remix', 'arkins remix',
    "snail's house remix", 'shindrum remix', 'no identity remix', 'imlay remix',
    'mar vista remix', 'pierre blanche remix', 'kun remix', 'chromeo remix',
    'marlon hoffstadt remix', 'young franco remix', 'sooyeon remix', 'jaebin remix',
    'dj seinfeld remix', 'silly silky remix', 'yunji remix', 'workout remix',
    '2spade remix', 'no1 ver', 'pika pika remix',
]

def is_remix_title(title):
    t = title.lower()
    if REMIX_RE.search(t):
        return True
    if SPED_UP_RE.search(title):
        return True
    for p in KNOWN_REMIX_PATTERNS:
        if p in t:
            return True
    return False

def is_lang_ver_title(title):
    return bool(LANG_VER_RE.search(title))

def is_making_title(title):
    return bool(MAKING_RE.search(title))

def is_vocal_inst_title(title):
    return bool(VOCAL_VER_RE.search(title)) or bool(INSTRUMENTAL_RE.search(title))

def is_lyric_asmr(title):
    return bool(LYRIC_ASMR_RE.search(title))

def shortest_title(titles):
    return min(titles, key=len)

merges = []
excise_song_ids = []
skip_list = []

for (channel_id, norm), members in dupes:
    titles = [m[1] for m in members]
    display = members[0][2]

    # Check for Lyric ASMR - leave split
    if any(is_lyric_asmr(t) for t in titles):
        skip_list.append({'reason': 'LYRIC ASMR', 'display': display, 'title': titles[0]})
        continue

    # Separate out making content (for excision)
    making_members = [m for m in members if is_making_title(m[1])]
    real_members = [m for m in members if not is_making_title(m[1])]

    if making_members:
        for m in making_members:
            excise_song_ids.append(m[0])

    if len(real_members) < 2:
        continue

    # Separate out language versions, remixes, vocal/inst versions
    remix_members = [m for m in real_members if is_remix_title(m[1])]
    lang_members = [m for m in real_members if is_lang_ver_title(m[1]) and not is_remix_title(m[1])]
    vocal_inst_members = [m for m in real_members if is_vocal_inst_title(m[1]) and not is_remix_title(m[1]) and not is_lang_ver_title(m[1])]

    core_members = [m for m in real_members
                    if m not in remix_members
                    and m not in lang_members
                    and m not in vocal_inst_members]

    if len(core_members) >= 2:
        survivor = min(m[0] for m in core_members)
        others = [m[0] for m in core_members if m[0] != survivor]
        st = shortest_title([m[1] for m in core_members])
        merges.append({
            'survivor': survivor,
            'others': others,
            'title': st,
            'display': display,
            'all_titles': [m[1] for m in core_members]
        })

print(f'Auto-classified merges: {len(merges)}')
print(f'Song rows to excise (making/behind): {len(excise_song_ids)} IDs={excise_song_ids}')
print(f'Skipped: {len(skip_list)}')
print()
print('=== EXCISE SONG IDs ===')
for sid in excise_song_ids:
    c.execute('SELECT video_id, title FROM videos WHERE song_id = ?', (sid,))
    for row in c.fetchall():
        print(f'  song_id={sid} video_id={row[0]}: {row[1]}')

print()
print('=== ALL MERGES ===')
for m in merges:
    all_ids = [m['survivor']] + m['others']
    print(f"{m['display']}: {all_ids} -> survivor={m['survivor']} title={m['title']!r}")
