"""
Apply the merge plan from the backstop audit to data/.registry_working.db.
Run from repo root: python scripts/apply_merges.py
"""
import sqlite3, re, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('data/.registry_working.db')
conn.row_factory = sqlite3.Row

def cur():
    return conn.cursor()

# ── helpers ──────────────────────────────────────────────────────────────────

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
    t = BRACKET_RE.sub(' ', title)
    t = t.lower()
    for phrase in PRESENTATION_WORDS:
        t = re.sub(r'\b' + re.escape(phrase) + r'\b', ' ', t)
    t = re.sub(r'\b(official|audio|video|clip|ver|version|stage|live)\b', ' ', t)
    t = re.sub(r'[^\w\s\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\uac00-\ud7af]', ' ', t, flags=re.UNICODE)
    return WHITESPACE_RE.sub(' ', t).strip()

REMIX_RE = re.compile(r'\bremix\b', re.IGNORECASE)
LANG_VER_RE = re.compile(
    r'\((korean|chinese|japanese|english|mandarin)\s+ver\.?\)'
    r'|\[(korean|chinese|japanese|english|mandarin)\s+ver\.?\]',
    re.IGNORECASE
)
MAKING_RE = re.compile(r'\bmaking\b', re.IGNORECASE)
SPED_UP_RE = re.compile(r'\bsped\s+up\b', re.IGNORECASE)
VOCAL_VER_RE = re.compile(r'\(vocal\s+ver\.?\)', re.IGNORECASE)
INSTRUMENTAL_RE = re.compile(r'\((instrumental|inst\.?|karaoke|acoustic|a\s+cappella)\)', re.IGNORECASE)
LYRIC_ASMR_RE = re.compile(r'lyric\s+asmr', re.IGNORECASE)

KNOWN_REMIX_PATTERNS = [
    'ofenbach ver', 'steve aoki ver', 'james carter ver', 'leez ver', 'ollounder ver',
    'yaeji ver', 'toppings drift ver', 'tell me bye ver', 'double pepperoni ver',
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
    if REMIX_RE.search(t) or SPED_UP_RE.search(title):
        return True
    return any(p in t for p in KNOWN_REMIX_PATTERNS)

def is_lang_ver_title(title):
    return bool(LANG_VER_RE.search(title))

def is_making_title(title):
    return bool(MAKING_RE.search(title))

def is_vocal_inst_title(title):
    return bool(VOCAL_VER_RE.search(title)) or bool(INSTRUMENTAL_RE.search(title))

def is_lyric_asmr(title):
    return bool(LYRIC_ASMR_RE.search(title))

def has_diff_feat(title_a, title_b):
    """Return True if the two titles have different feat. credits."""
    feat_re = re.compile(r'\(feat\.?\s+([^\)]+)\)', re.IGNORECASE)
    fa = feat_re.search(title_a)
    fb = feat_re.search(title_b)
    if fa and fb:
        return fa.group(1).lower().strip() != fb.group(1).lower().strip()
    # If one has feat. and other doesn't, potentially different versions.
    # Only flag as different if both have feat. but different ones.
    return False

def shortest_title(titles):
    return min(titles, key=len)

# ── build merge plan ──────────────────────────────────────────────────────────

c = cur()
c.execute("""
    SELECT s.song_id, s.channel_id, s.canonical_title, ch.display_name
    FROM songs s JOIN channels ch ON ch.channel_id = s.channel_id
    ORDER BY s.song_id
""")
rows = c.fetchall()

groups = defaultdict(list)
for row in rows:
    song_id, channel_id, canonical_title, display_name = row
    norm = normalize(canonical_title)
    if norm and len(norm) >= 2:
        groups[(channel_id, norm)].append((song_id, canonical_title, display_name))

dupes = [(k, v) for k, v in groups.items() if len(v) > 1]

merges = []   # (survivor, others_list, canonical_title)
excise_song_ids = []  # song_ids whose videos should be set to NULL

for (channel_id, norm), members in dupes:
    titles = [m[1] for m in members]
    display = members[0][2]

    if any(is_lyric_asmr(t) for t in titles):
        continue

    making_members = [m for m in members if is_making_title(m[1])]
    real_members = [m for m in members if not is_making_title(m[1])]

    if making_members:
        excise_song_ids.extend(m[0] for m in making_members)

    if len(real_members) < 2:
        continue

    remix_members = [m for m in real_members if is_remix_title(m[1])]
    lang_members = [m for m in real_members
                    if is_lang_ver_title(m[1]) and not is_remix_title(m[1])]
    vocal_inst_members = [m for m in real_members
                          if is_vocal_inst_title(m[1])
                          and not is_remix_title(m[1])
                          and not is_lang_ver_title(m[1])]

    core_members = [m for m in real_members
                    if m not in remix_members
                    and m not in lang_members
                    and m not in vocal_inst_members]

    if len(core_members) >= 2:
        # ── manual overrides ────────────────────────────────────────────────

        core_ids = set(m[0] for m in core_members)

        # 1. ATEEZ Ice On My Teeth: remove producer-ver Lyric Video songs 31, 32
        if 31 in core_ids or 32 in core_ids:
            core_members = [m for m in core_members if m[0] not in (31, 32)]

        # 2. EXO Love Me Right: [1943, 2867] - 2867 is Chinese ver with CJK in title
        if 1943 in core_ids and 2867 in core_ids:
            core_members = [m for m in core_members if m[0] != 2867]

        # 3. ZHOUMI Rewind: different feat. credits
        if 2889 in core_ids and 2890 in core_ids:
            core_members = [m for m in core_members if m[0] not in (2889, 2890)]

        # 4. LE SSERAFIM CRAZY: remove feat. PinkPantheress Visualizer (11541)
        if 11541 in core_ids:
            core_members = [m for m in core_members if m[0] != 11541]

        # 5. aespa LEMONADE: skip merge entirely (feat. Becky G vs no feat.)
        if 6504 in core_ids and 6505 in core_ids:
            core_members = [m for m in core_members if m[0] not in (6504, 6505)]

        # 6. LE SSERAFIM BOOMPALA: remove different-feat collab versions
        #    11447 (Ayra Starr), 11451 (GURU RANDHAWA), 11452 (SANTOS BRAVOS)
        #    11455 (Karaoke ver.) - also strip this
        boompala_collab = {11447, 11451, 11452, 11455}
        if core_ids & boompala_collab:
            core_members = [m for m in core_members if m[0] not in boompala_collab]

        # 7. LE SSERAFIM CRAZY Japanese ver.: 11525 has feat. JP THE WAVY, 11526 doesn't
        if 11525 in core_ids and 11526 in core_ids:
            core_members = [m for m in core_members if m[0] not in (11525, 11526)]

        # 8. LE SSERAFIM Eve Psyche English: different feat. credits
        if 11568 in core_ids and 11570 in core_ids:
            core_members = [m for m in core_members if m[0] not in (11568, 11570)]

        if len(core_members) >= 2:
            survivor = min(m[0] for m in core_members)
            others = [m[0] for m in core_members if m[0] != survivor]
            st = shortest_title([m[1] for m in core_members])
            merges.append((survivor, others, st))

# ── additional merges not caught by auto-detect ───────────────────────────────

# EXO Monster Chinese ver.: Performance Video + MV (both Chinese ver.)
merges.append((2814, [2816], "EXO 엑소 'Monster' MV (Chinese ver.)"))

print(f"Merges to apply: {len(merges)}")
print(f"Song rows to excise (making): {len(excise_song_ids)} -> {excise_song_ids}")
print()

# ── apply ─────────────────────────────────────────────────────────────────────

c2 = cur()

# Step 1: excise making/behind song rows
print("=== EXCISIONS ===")
for sid in excise_song_ids:
    c2.execute('SELECT video_id, title FROM videos WHERE song_id = ?', (sid,))
    vids = c2.fetchall()
    for v in vids:
        print(f"  Excise video {v[0]}: {v[1]}")
    c2.execute('UPDATE videos SET song_id = NULL WHERE song_id = ?', (sid,))

# Step 2: apply merges
print()
print("=== MERGES ===")
merge_count = 0
for survivor, others, title in merges:
    if not others:
        continue
    all_ids = [survivor] + others
    # Update videos
    placeholders = ','.join('?' for _ in others)
    c2.execute(
        f'UPDATE videos SET song_id = ? WHERE song_id IN ({placeholders})',
        [survivor] + others
    )
    # Update canonical_title
    c2.execute(
        'UPDATE songs SET canonical_title = ? WHERE song_id = ?',
        (title, survivor)
    )
    print(f"  Merged {all_ids} -> survivor={survivor} title={title!r}")
    merge_count += 1

# Step 3: cleanup orphan song rows
c2.execute("""
    DELETE FROM songs WHERE song_id NOT IN
    (SELECT DISTINCT song_id FROM videos WHERE song_id IS NOT NULL)
""")
deleted = conn.total_changes

conn.commit()

print()
print(f"Done. Applied {merge_count} merges, excised {len(excise_song_ids)} song rows.")

# Step 4: integrity check
c3 = cur()
c3.execute("""
    SELECT COUNT(*) FROM videos v WHERE v.song_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM songs s WHERE s.song_id = v.song_id)
""")
dangling = c3.fetchone()[0]
c3.execute("""
    SELECT COUNT(*) FROM songs s WHERE NOT EXISTS
    (SELECT 1 FROM videos v WHERE v.song_id = s.song_id)
""")
orphan_songs = c3.fetchone()[0]
c3.execute('SELECT COUNT(*) FROM songs')
songs_after = c3.fetchone()[0]
c3.execute('SELECT COUNT(*) FROM videos')
videos_after = c3.fetchone()[0]
c3.execute('SELECT COUNT(*) FROM videos WHERE song_id IS NULL')
null_after = c3.fetchone()[0]

print()
print("=== INTEGRITY CHECK ===")
print(f"  Dangling video->song refs (must be 0): {dangling}")
print(f"  Orphan songs with no videos (must be 0): {orphan_songs}")
print(f"  Songs after: {songs_after}")
print(f"  Videos after (must be unchanged 19823): {videos_after}")
print(f"  Videos with NULL song_id after: {null_after}")
