#!/usr/bin/env python3
"""
EHF Catalog Auto-Builder
Fetches Google Sheets data, rebuilds index.html, deploys to Netlify.
Runs via GitHub Actions every 720 minutes (12 hours) — schedule set in auto-deploy.yml.
Skips deploy when the sheet is unchanged, to conserve Netlify credits.
"""

import csv, re, io, hashlib, json, os, sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── CONFIG ──────────────────────────────────────────────
SHEET_ID   = '1PBVR3cDRCU4hyt577lYBVmv4f3hgp0jbJZbjyNTOR5k'
BASE_URL   = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet='
HASH_FILE  = 'last_hash.txt'
HTML_FILE  = 'index.html'

NETLIFY_TOKEN   = os.environ.get('NETLIFY_ACCESS_TOKEN', '')
NETLIFY_SITE_ID = os.environ.get('NETLIFY_SITE_ID', '')
SLACK_AUDIT_WEBHOOK = os.environ.get('SLACK_AUDIT_WEBHOOK', '')  # Slack Incoming Webhook URL for build alerts
# Invoice system (Google Apps Script) health endpoint. The build audit calls this
# to confirm the invoice system is alive, healthy for all reps, and on a matching
# version. Set INVOICE_EXEC_URL to the /exec URL; leave blank to skip the check.
INVOICE_EXEC_URL = os.environ.get('INVOICE_EXEC_URL',
    'https://script.google.com/macros/s/AKfycbw_3jfvJY1Y2UVXs_XODHDbUMTNzB36kwraR_UZle-l8Rq94pHB_qEgo4DPxsvR-D22fg/exec')
COUNTS_FILE = 'last_counts.json'  # remembers each tab's product count between runs
SNAPSHOT_FILE = 'inventory_snapshot.json'  # last build's full product state (for diffing)
HISTORY_FILE = 'inventory_history.json'    # running log of all changes over time

# ── HELPERS ──────────────────────────────────────────────

VERSION_FILE = 'version.txt'
BUILD_VERSION = ''

def get_next_version():
    """Read the current version, bump the minor number, persist, and return the new string.
    e.g. '3.4' -> '3.5'. Always increments so newest is obvious."""
    try:
        cur = open(VERSION_FILE).read().strip()
    except Exception:
        cur = '3.4'
    try:
        major, minor = cur.split('.')
        minor = str(int(minor) + 1)
        new_v = f'{major}.{minor}'
    except Exception:
        new_v = '3.5'
    try:
        open(VERSION_FILE, 'w').write(new_v)
    except Exception:
        pass
    return new_v


def fetch_sheet(tab):
    """Fetch a sheet tab as CSV. Uses gviz endpoint with sheet name.
    Retries once and logs row count so failures are visible."""
    from urllib.parse import quote
    url = BASE_URL + quote(tab)
    for attempt in range(2):
        try:
            req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            raw = urlopen(req, timeout=45).read().decode('utf-8')
            rows = list(csv.reader(io.StringIO(raw)))
            print(f'  Fetched "{tab}": {len(rows)} rows')
            if len(rows) > 1:
                return rows
            # Empty result — retry once
            if attempt == 0:
                print(f'  "{tab}" returned {len(rows)} rows, retrying...')
                continue
            return rows
        except Exception as e:
            print(f'  WARNING: fetch "{tab}" attempt {attempt+1} failed: {e}')
            if attempt == 0:
                continue
            return []
    return []

def get_drive_id(url):
    if not url: return ''
    m = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    return m.group(1) if m else ''

def thumb(url):
    fid = get_drive_id(url)
    return f'https://drive.google.com/thumbnail?id={fid}&sz=w600' if fid else ''

# Hosts whose links expire or aren't publicly viewable — never use as images.
_BAD_IMG_HOSTS = ('slack-files.com', 'slack.com/files')

def is_valid_pic(url):
    """Accept any real http(s) image/CDN URL, except known-expiring hosts.
    Drive links must have an extractable file id (so we can thumbnail them)."""
    url = str(url or '').strip()
    if not url or not url.lower().startswith('http'):
        return False
    if any(b in url for b in _BAD_IMG_HOSTS):
        return False
    if 'drive.google.com' in url:
        return bool(get_drive_id(url))
    # Any other real URL is accepted (leadconnectorhq, filesafe, googleusercontent,
    # storage.googleapis, shopify cdn, images.*, cloudfront, etc.)
    return True

def get_pic(raw):
    raw = raw.strip()
    if not is_valid_pic(raw): return ''
    if 'drive.google.com' in raw: return thumb(raw)
    return raw

def is_valid_coa(url):
    """Accept any real http(s) COA URL except known-expiring hosts (Slack)."""
    url = str(url or '').strip()
    if not url or not url.lower().startswith('http'):
        return False
    if any(b in url for b in _BAD_IMG_HOSTS):
        return False
    return True

def esc(s):
    return str(s).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').strip()

def clean_price(s):
    s = str(s or '').strip()
    if s.upper() in ('N/A', 'NA', 'TBD', '-', '—'):
        return ''
    return s.replace('$', '').replace(',', '')

# ── STRAIN DEFINITIONS ───────────────────────────────────
ST_MAP = {
    'VENOM OG':           ('indica',        'Indica'),
    'TAHOE OG':           ('indica',        'Indica'),
    'TRUMP RUNTZ':        ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'G47':                ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'JUNGLE CAKE':        ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'WHITE GUMMIES':      ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'BODHIS CHARMZ':      ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'ORANGE CREAM POP':   ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'DIRTY SPRITE':       ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'BUBBA KUSH':         ('indica',        'Indica'),
    'GUMBO':              ('indica',        'Indica'),
    'MOCHI':              ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'TROP CHERRY':        ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'HASHBURGER':         ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'TRIPLE BAKE CAKE':   ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'SUPER BOOF CHERRY':  ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'TRIPLE BURGER':      ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'PURPLE RUNTZ':       ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'VICE CITY':          ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'SUGAR COOKIES':      ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'ANIMAL FACE':        ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'LEMON CHERRY GELATO':('hybrid-indica', 'Hybrid / Indica-Lean'),
    'GUAVA':              ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'SUPER SILVER HAZE':  ('sativa',        'Sativa'),
    'CHERRY LIME RUNTZ':  ('hybrid',        'Hybrid'),
    'TRUFFLE TART':       ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'MODIFIED GRAPES':    ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'GRAPE CANDY':        ('hybrid',        'Hybrid'),
    'SOUR SUNDAE':        ('hybrid',        'Hybrid'),
    'GARLIC COOKIES':     ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'PINK CERTZ':         ('hybrid',        'Hybrid'),
    'CEREAL MILK':        ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'GRAPE GAS':          ('indica',        'Indica'),
    'APPPLE TART':        ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'CANDY GAS':          ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'PUNCH BREATH':       ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'ESCOBARS':           ('hybrid',        'Hybrid'),
    'MAC 1':              ('hybrid',        'Hybrid / Sativa-Lean'),
    'BLACK CHERRY GELATO':('hybrid-indica', 'Hybrid / Indica-Lean'),
    'KANDY KUSH':         ('indica',        'Indica'),
    'CALI BURGER':        ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'ZANGRIA':            ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'SHERBANGER':         ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'FROZEN GRAPES':      ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'ZOOTOPIA':           ('hybrid',        'Hybrid'),
    'PIE FACE':           ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'MELONAID':           ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'BLUE DREAM':         ('hybrid-sativa', 'Hybrid / Sativa-Lean'),
    'BISCOTTI X JEALOUSY':('hybrid-indica', 'Hybrid / Indica-Lean'),
    'RAINBOW RUNTZ':      ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'BUBBLEGUM GUSHERS':  ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'CANDY RUNTZ':        ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'ICE CREAM CAKE':     ('hybrid-indica', 'Hybrid / Indica-Lean'),
    'WHITE CHERRY GELATO':('hybrid-indica', 'Hybrid / Indica-Lean'),
}

def auto_st(name):
    """Auto-assign strain type for unknown strains based on name patterns."""
    n = name.upper()
    if any(x in n for x in ['KUSH','OG','BUBBA','INDICA']): return ('indica','Indica')
    if any(x in n for x in ['HAZE','DREAM','SATIVA','EXPRESS']): return ('sativa','Sativa')
    if any(x in n for x in ['GELATO','CAKE','COOKIES','CREAM','ICE']): return ('hybrid-indica','Hybrid / Indica-Lean')
    if any(x in n for x in ['MELON','GUAVA','TROP','FRUIT','CITRUS']): return ('hybrid-sativa','Hybrid / Sativa-Lean')
    return ('hybrid','Hybrid')

def auto_anim(name):
    """Auto-assign animation class based on name patterns."""
    n = name.upper()
    if name == 'ESCOBARS': return 'agold'
    if any(x in n for x in ['G47','VICE CITY','SPACE','ALIEN','COSMIC']): return 'asp'
    if any(x in n for x in ['FIRE','BURGER','HOT','FUEL','GAS','GARLIC','DIESEL','SOUR','PUNCH']): return 'af'
    if any(x in n for x in ['ICE','CREAM','SNOW','FROZEN','COLD','WHITE','MINT','FROST']): return 'ai'
    if any(x in n for x in ['GRAPE','PURPLE','PLUM','VIOLET','BERRY','KUSH']): return 'ap'
    if any(x in n for x in ['MELON','GUAVA','GREEN','JUNGLE','ANIMAL','TROP','SILVER','MOCHI']): return 'ag'
    if any(x in n for x in ['RAINBOW','CANDY','RUNTZ','SKITTLE','ZKITTLE','FRUIT','CITRUS','LEMON','CHERRY','LIME']): return 'arb'
    return 'ag'  # default: green

KNOWN_PREV = {
    'MAC 1','BLACK CHERRY GELATO','TRUMP RUNTZ','KANDY KUSH','JUNGLE CAKE',
    'WHITE GUMMIES','G47','CALI BURGER','HASHBURGER','ZANGRIA','SHERBANGER',
    'FROZEN GRAPES','ZOOTOPIA','TROP CHERRY','PIE FACE','SUGAR COOKIES',
    'VICE CITY','ANIMAL FACE','LEMON CHERRY GELATO','GUAVA','MELONAID',
    'BLUE DREAM','BISCOTTI X JEALOUSY','MODIFIED GRAPES','RAINBOW RUNTZ',
    'GRAPE CANDY','BUBBA KUSH','BUBBLEGUM GUSHERS','CANDY RUNTZ','ICE CREAM CAKE',
    'CEREAL MILK','WHITE CHERRY GELATO','GRAPE GAS','APPPLE TART','CANDY GAS',
    'ESCOBARS','VENOM OG','TAHOE OG','BODHIS CHARMZ','ORANGE CREAM POP',
    'DIRTY SPRITE','GUMBO','MOCHI','TRIPLE BAKE CAKE','SUPER BOOF CHERRY',
    'TRIPLE BURGER','PURPLE RUNTZ','SUPER SILVER HAZE','CHERRY LIME RUNTZ',
    'TRUFFLE TART','SOUR SUNDAE','GARLIC COOKIES','PINK CERTZ','PUNCH BREATH',
}

# ── FLOWER PARSER ────────────────────────────────────────
# Flower subcategories priced PER UNIT (3.5g cans) instead of by weight tiers.
FLOWER_UNIT_SECTIONS = {'MINI SODA CANS','MINI SODA CAN','MINI TUNA CANS','MINI TUNA CAN','QUARTER OUNCE JAR','QUARTER OUNCE JARS'}


# Map flower section headers -> (strain filter key, display label) so products
# inherit strain type for the Indica/Sativa/Hybrid filter WITHOUT showing dividers.
FLOWER_SECTION_STRAIN = {
    'INDICA': ('indica','Indica'), 'INDICA DOMINANT': ('indica','Indica'),
    'SATIVA': ('sativa','Sativa'), 'SATIVA DOMINANT': ('sativa','Sativa'),
    'HYBRID': ('hybrid','Hybrid'), 'HYBRID DOMINANT': ('hybrid','Hybrid'),
}

def parse_flower(rows):
    items = []
    skip_names = {'PRODUCT NAME','CALL (408) 444-HEMP',''}
    current_mode = 'weight'   # 'weight' = LB/half/qtr/oz tiers, 'unit' = per-can
    current_unit_label = ''
    current_strain = None
    for row in rows:
        if len(row) < 2: continue
        name = row[0].strip()
        # Stop at the bottom reference table (sold-out archive) — not real inventory.
        if 'NOT IN STOCK' in name.upper() or 'FOR REFERENCE ONLY' in name.upper():
            break
        if not name or name in skip_names or name.startswith('Last Updated'): continue
        if is_junk_row(name) and name.upper() not in WEIGHT_SECTIONS and name.upper() not in FLOWER_UNIT_SECTIONS: continue

        # Detect a section header: a row whose name matches a known unit-section,
        # OR a row with a name but no THCa value and no prices (a divider).
        upper = strip_stars(name).upper()
        thca_s = row[1].strip().replace('%','') if len(row)>1 else ''
        has_thca = False
        try:
            float(thca_s); has_thca = True
        except: has_thca = False
        row_prices = [c for c in row[2:8] if '$' in c] if len(row) > 2 else []

        if upper in FLOWER_UNIT_SECTIONS:
            current_mode = 'unit'
            current_unit_label = strip_stars(name).title()  # e.g. 'Mini Soda Cans'
            items.append({'sec':True,'n':name})
            continue
        # Known weight-based section dividers (explicit list — avoids
        # mistaking a real strain with blank data for a header)
        WEIGHT_SECTIONS = {'INDICA','SATIVA','HYBRID','INDICA DOMINANT',
                           'SATIVA DOMINANT','HYBRID DOMINANT','FLOWER','PREMIUM FLOWER',
                           'EXOTIC','TOP SHELF','SMALLS'}
        if upper in WEIGHT_SECTIONS:
            current_mode = 'weight'
            current_unit_label = ''
            # Capture strain type so products inherit it for filtering.
            if upper in FLOWER_SECTION_STRAIN:
                current_strain = FLOWER_SECTION_STRAIN[upper]
                # Do NOT emit a visible divider for Indica/Sativa/Hybrid.
                continue
            current_strain = None
            items.append({'sec':True,'n':name})
            continue

        vid    = row[8].strip() if len(row)>8 else ''
        coa    = row[9].strip() if len(row)>9 else ''
        pic_raw= row[7].strip() if len(row)>7 else ''
        pic    = get_pic(pic_raw)              # '' -> "Picture Coming Soon" on site
        coa    = coa if is_valid_coa(coa) else ''  # '' -> "COA Coming Soon" on site

        try: thca = float(thca_s)
        except: thca = None                    # None -> "Testing Pending" on site
        qty = row[2].strip() if len(row)>2 else ''

        # Skip only rows with NO usable data at all (no thca, no price, no pic, no coa).
        row_has_price = any('$' in c for c in row[2:8]) if len(row) > 2 else False
        if thca is None and not row_has_price and not pic and not coa:
            continue

        if current_mode == 'unit':
            # Per-unit (per 3.5g can) pricing. Find the first $ price in the row.
            unit_price = ''
            for c in row[2:8]:
                if '$' in c and c.strip().upper() != 'N/A':
                    unit_price = c.strip(); break
            special = qty.upper() == 'MADE TO ORDER'
            if name in ST_MAP:
                st, sl = ST_MAP[name]
            elif current_strain:
                st, sl = current_strain
            else:
                st, sl = auto_st(name)
            isnew = 'true' if name not in KNOWN_PREV else 'false'
            # Unique display name so duplicates (same flavor in flower + cans)
            # don't collide in the popup's name-based lookup.
            disp_name = f'{name} ({current_unit_label})' if current_unit_label else name
            unit_size = '3.5g' if 'CAN' in current_unit_label.upper() else ('7g' if 'QUARTER' in current_unit_label.upper() else '')
            items.append({
                'n':disp_name,'thca':thca,'qty':qty,'pic':pic,'vid':vid,'coa':coa,
                'st':st,'sl':sl,'isnew':isnew,'special':special,
                'unitmode':True,'unitprice':unit_price,'size':unit_size,
                'lb':0,'half':0,'qtr':0,'oz':0,'hideThca':True,
            })
        else:
            def _pv(cell):
                v = cell.strip()
                if v.upper() in ('N/A','NA','',''): return ''
                return clean_price(cell)
            lb   = _pv(row[3]) if len(row)>3 else ''
            half = _pv(row[4]) if len(row)>4 else ''
            qtr  = _pv(row[5]) if len(row)>5 else ''
            oz   = _pv(row[6]) if len(row)>6 else ''
            try: lb_f = float(lb)
            except: lb_f = 0
            try: half_f = float(half)
            except: half_f = 0
            try: qtr_f = float(qtr)
            except: qtr_f = 0
            try: oz_f = float(oz)
            except: oz_f = 0
            special = qty.upper() == 'MADE TO ORDER'
            if name in ST_MAP:
                st, sl = ST_MAP[name]
            elif current_strain:
                st, sl = current_strain
            else:
                st, sl = auto_st(name)
            isnew = 'true' if name not in KNOWN_PREV else 'false'
            items.append({
                'n':name,'thca':thca,'qty':qty,'lb':lb_f,'half':half_f,
                'qtr':qtr_f,'oz':oz_f,'pic':pic,'vid':vid,'coa':coa,
                'st':st,'sl':sl,'isnew':isnew,'special':special,'unitmode':False,
            })
        # De-duplicate identical product names (sheet sometimes lists a flavor twice)
    seen = {}
    for it in items:
        if it.get('sec'): continue
        nm = it['n']
        if nm in seen:
            seen[nm] += 1
            it['n'] = f"{nm} #{seen[nm]}"
        else:
            seen[nm] = 1
    return items

def build_flower_js(items):
    lines = ['const FLOWER=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        # Section header row
        if p.get('sec'):
            lines.append(f'{{sec:true,n:"{esc(p["n"])}"}}{comma}')
            continue
        thca_js = str(p['thca']) if p.get('thca') is not None else 'undefined'
        obj = (f'{{n:"{esc(p["n"])}",thca:{thca_js},qty:"{esc(p["qty"])}",lb:{p.get("lb",0)},'
               f'half:{p.get("half",0)},qtr:{p.get("qtr",0)},oz:{p.get("oz",0)},'
               f'pic:"{esc(p["pic"])}",vid:"{esc(p["vid"])}",coa:"{esc(p["coa"])}",'
               f'st:"{p["st"]}",sl:"{p["sl"]}",isnew:{p["isnew"]}')
        if p.get('unitmode'):
            obj += f',unitmode:true,unitprice:"{esc(p.get("unitprice",""))}",size:"{esc(p.get("size","3.5g"))}"'
        if p.get('hideThca'): obj += ',hideThca:true'
        if p.get('special'): obj += ',special:true'
        obj += '}'
        lines.append(obj + comma)
    lines.append('];')
    return '\n'.join(lines)

def build_anim_js(items):
    lines = ['const ANIM={']
    for i,p in enumerate(items):
        cls = auto_anim(p['n'])
        lines.append(f"  '{esc(p['n'])}':'{cls}'" + (',' if i < len(items)-1 else ''))
    lines.append('};')
    return '\n'.join(lines)

# ── SECTION PRICE LADDERS ────────────────────────────────────────────────────
# WHY THIS IS HARD-DECLARED AND NOT READ FROM THE SHEET:
# The builder reads the sheet through Google's gviz CSV endpoint. gviz types each
# COLUMN, and the tier columns are full of currency, so it types them numeric —
# then BLANKS any cell in them holding text. Every section's header labels
# ("100 UNIT PRICE", "50 UNIT PRICE") are exactly such cells, so they arrive
# empty. The old parser therefore kept whatever ladder it read first, which is
# how Doobies and Hotties were published at 1,000 units for $1,700 when the sheet
# says 100. The prices were right and the quantities were off by 10x.
#
# TO ADD OR CHANGE A SECTION: add its name here with the unit count of each price
# column, LEFT TO RIGHT, matching the sheet's column order. Any section that is
# missing here AND whose labels cannot be read is reported in the build audit.
SECTION_LADDERS = {
    'KING SIZE PRE ROLLS':  (1000, 500, 100, 1),
    'KING SIZE PRE ROLL':   (1000, 500, 100, 1),
    'DOOBIES':              (100,  50,  10,  1),
    'HOTTIES':              (100,  50,  10,  1),
    'SINGLE MINI PRE ROLL': (1000, 500, 100, 1),
    'SINGLE MINI PRE ROLLS':(1000, 500, 100, 1),
}


def ladder_from_map(section, current):
    """Rebuild tier columns from SECTION_LADDERS, keeping the column positions we
    already know and replacing only the quantities. Returns None if the section
    is not declared or the tier count does not match."""
    key = str(section or '').strip().upper()
    units = SECTION_LADDERS.get(key)
    if not units or not current or len(units) != len(current):
        return None
    return [(f'{n}u', col) for n, (_lbl, col) in zip(units, current)]


# ── PREROLL PARSER ───────────────────────────────────────
SECTION_NAMES = {'KING SIZE PRE ROLLS','DOOBIES','HOTTIES','SINGLE MINI PRE ROLL'}

# All pre-roll section headers get the same large/bold brand styling.
PREROLL_BRAND_HEADERS = {'DOOBIES','HOTTIES','SINGLE MINI PRE ROLL','SINGLE MINI PRE ROLLS',
                         'KING SIZE PRE ROLLS','KING SIZE PRE ROLL'}
PREROLL_CATEGORY_HEADERS = set()

# Preroll sections and their size labels (so buyers know king-size vs mini).
PREROLL_SIZE_LABELS = {
    'KING SIZE PRE ROLLS': '2.5g+ with tube',
    'KING SIZE PRE ROLL': '2.5g+ with tube',
    'DOOBIES': '1g Doobie',
    'HOTTIES': '1g Hottie',
    'SINGLE MINI PRE ROLL': '1g Mini',
    'SINGLE MINI PRE ROLLS': '1g Mini',
}
PREROLL_KING_SECTIONS = {'KING SIZE PRE ROLLS','KING SIZE PRE ROLL'}

def _is_header_row(row):
    """A header row contains 'PRICE' in some column (tier headers) or starts with
    PRODUCT NAME. Section headers like ***DOOBIES*** ALSO carry tier labels."""
    joined = ' '.join(str(c).upper() for c in row)
    return 'PRICE' in joined and ('UNIT' in joined or 'PRODUCT NAME' in joined)

def parse_preroll(rows):
    items = []
    _URL_COL_HINTS['pic'] = _URL_COL_HINTS['coa'] = -1  # reset per-tab
    tier_cols = []          # (label, idx) tier columns for the CURRENT section
    current_size = ''
    current_is_king = False
    current_note = ''
    for row in rows:
        if not row: continue
        if not row[0].strip():
            # A tier-header row can arrive with a BLANK first cell when the
            # section title beside it is a MERGED cell in the sheet. Skipping it
            # leaves the PREVIOUS section's tier labels in force — which is how
            # a "100 UNIT PRICE" column ends up displayed as "1,000 units".
            tier_cols = adopt_tier_header(row, tier_cols)
            continue
        name = row[0].strip()
        upper = name.upper()
        # Top header row
        if name.strip().upper().startswith('PRODUCT NAME'):
            tier_cols = find_tier_columns(row)
            set_url_columns_from_header(row)
            continue
        if is_junk_row(name):
            continue
        cann = row[1].strip() if len(row)>1 else ''
        # Section headers: either the cann column is blank, OR it literally says
        # "CANNABINOID" (combined section+column-header rows like ***DOOBIES***),
        # OR the name is wrapped in *** ***.
        is_section = (not cann) or cann.upper() == 'CANNABINOID' or (name.startswith('*') and name.endswith('*'))
        if is_section:
            clean = strip_stars(name)
            cu = clean.upper()
            is_brand = cu in PREROLL_BRAND_HEADERS
            current_size = PREROLL_SIZE_LABELS.get(cu, '')
            current_is_king = cu in PREROLL_KING_SECTIONS
            current_note = '🫙 Glass jar · 5 mini pre-rolls' if ('DOOBIE' in cu or 'HOTTIE' in cu) else ''
            # Each section carries its OWN price ladder. Resolve it here, and
            # refuse to inherit the section above if this one can't be read.
            tier_cols = ladder_for_section(row, tier_cols, 'PreRoll', clean)
            items.append({'sec':True,'n':clean,'brand':is_brand})
            continue
        pic_idx, coa_idx = find_url_columns(row)
        pic_raw = row[pic_idx].strip() if pic_idx != -1 else ''
        coa     = row[coa_idx].strip() if coa_idx != -1 else ''
        pic = get_pic(pic_raw)
        coa = coa if is_valid_coa(coa) else ''
        # Extract ALL price tiers for this product (1000u/500u/100u/10u/1u as present).
        tiers = extract_tiers(row, tier_cols)
        cann_list = [x.strip() for x in re.split(r'[/,]', cann) if x.strip()]
        items.append({'sec':False,'n':name,'cann':cann,'cannList':cann_list,
                      'qty':row[2].strip() if len(row)>2 else '',
                      'tiers':tiers,'pic':pic,'coa':coa,'note':current_note,
                      'size':current_size,'king':current_is_king})
    return items

def tiers_js(p):
    """Emit a JS array of {label,qty,price} tier objects for a product."""
    tiers = p.get('tiers') or []
    parts = []
    for t in tiers:
        parts.append('{label:"%s",qty:%d,price:"%s"}' % (esc(t['label']), t['qty'], esc(t['price'])))
    return '[' + ','.join(parts) + ']'

def cann_js(p):
    """Emit a JS array literal of the product's cannabinoid options."""
    lst = p.get('cannList') or []
    inner = ','.join('"'+esc(x)+'"' for x in lst)
    return '['+inner+']'

def build_preroll_js(items):
    lines = ['const PREROLL=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        if p['sec']:
            brand = 'true' if p.get('brand') else 'false'
            lines.append(f'{{sec:true,n:"{esc(p["n"])}",brand:{brand}}}{comma}')
        else:
            note = p.get('note','')
            king = 'true' if p.get('king') else 'false'
            lines.append(f'{{n:"{esc(p["n"])}",cann:"{esc(p["cann"])}",cannList:{cann_js(p)},size:"{esc(p.get("size",""))}",king:{king},tiers:{tiers_js(p)},pic:"{esc(p["pic"])}",coa:"{esc(p["coa"])}",note:"{esc(note)}"}}{comma}')
    lines.append('];')
    return '\n'.join(lines)

# ── FIXED-BLEND SECTIONS ─────────────────────────────────────────────────────
# Most products let the buyer CHOOSE which cannabinoid they want. A few ship as a
# pre-set combination — the listed cannabinoids are the ingredients inside that
# device, not a menu. Those must render read-only, or the popup implies a choice
# that does not exist and stamps a fake "selection" onto the quote line.
# Matched as a substring against the cleaned, upper-cased section name.
FIXED_BLEND_SECTIONS = ('ESCO BAR',)


def is_fixed_blend(section):
    u = ' '.join(str(section or '').upper().split())
    return any(k in u for k in FIXED_BLEND_SECTIONS)


# ── VAPE PARSER ──────────────────────────────────────────
VAPE_SECTIONS = {'2G DISPOSABLE VAPE\nBLINKERS BLEND',
                 '2G DISPOSABLE VAPE\nLIVE RESIN DIAMONDS\nPACKS POD',
                 '3G DISPOSABLE ESCO BARS','1G VAPE CARTRIDGE EHF'}

def parse_vape(rows):
    items = []
    _URL_COL_HINTS['pic'] = _URL_COL_HINTS['coa'] = -1  # reset per-tab
    tier_cols = []
    current_fixed = False   # is the current section a pre-set blend?
    for row in rows:
        if not row: continue
        if not row[0].strip():
            tier_cols = adopt_tier_header(row, tier_cols)
            continue
        name = row[0].strip()
        if name.strip().upper().startswith('PRODUCT NAME') or _row_has_tier_header(row):
            row_tiers = find_tier_columns(row)
            if row_tiers: tier_cols = row_tiers
            set_url_columns_from_header(row)
            if name.startswith('*') and name.endswith('*'):
                _sec = strip_stars(' '.join(name.split()))
                current_fixed = is_fixed_blend(_sec)
                items.append({'sec':True,'n':_sec})
            continue
        if is_junk_row(name):
            continue
        cann = row[1].strip() if len(row)>1 else ''
        if not cann or cann.upper() == 'CANNABINOID':
            clean_name = strip_stars(' '.join(name.split()))
            current_fixed = is_fixed_blend(clean_name)
            tier_cols = adopt_tier_header(row, tier_cols)
            items.append({'sec':True,'n':clean_name})
            continue
        pic_idx, coa_idx = find_url_columns(row)
        pic_raw = row[pic_idx].strip() if pic_idx != -1 else ''
        coa     = row[coa_idx].strip() if coa_idx != -1 else ''
        tiers = extract_tiers(row, tier_cols)
        pic = get_pic(pic_raw)
        coa = coa if is_valid_coa(coa) else ''
        items.append({'sec':False,'n':name,'cann':cann,
                      'cannList':[x.strip() for x in re.split(r'[/,]', cann) if x.strip()],
                      'qty':row[2].strip() if len(row)>2 else '',
                      'tiers':tiers,'pic':pic,'coa':coa,
                      'fixedBlend':current_fixed})
    return items

def build_vape_js(items):
    lines = ['const VAPE=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        if p['sec']:
            lines.append(f'{{sec:true,n:"{esc(p["n"])}"}}{comma}')
        else:
            fb = ',fixedBlend:true' if p.get('fixedBlend') else ''
            lines.append(f'{{n:"{esc(p["n"])}",cann:"{esc(p["cann"])}",cannList:{cann_js(p)},size:"",tiers:{tiers_js(p)},pic:"{esc(p["pic"])}",coa:"{esc(p["coa"])}",note:""{fb}}}{comma}')
    lines.append('];')
    return '\n'.join(lines)

# ── EDIBLES PARSER ───────────────────────────────────────
EDIBLES_SECTIONS = {'SWEET TOOTH','SWEETH TOOTH','CBD CANDY','EHF','PESO PESO'}

def parse_edibles(rows):
    items = []
    _URL_COL_HINTS['pic'] = _URL_COL_HINTS['coa'] = -1  # reset per-tab
    tier_cols = []
    pieces_col = cat_col = -1
    for row in rows:
        if not row: continue
        if not row[0].strip():
            tier_cols = adopt_tier_header(row, tier_cols)
            continue
        name = row[0].strip()
        if name.strip().upper().startswith('PRODUCT NAME') or _row_has_tier_header(row):
            row_tiers = find_tier_columns(row)
            if row_tiers: tier_cols = row_tiers
            set_url_columns_from_header(row)
            pc, cc = find_pieces_columns(row)
            if pc != -1: pieces_col = pc
            if cc != -1: cat_col = cc
            if name.startswith('*') and name.endswith('*'):
                items.append({'sec':True,'n':strip_stars(' '.join(name.split()))})
            continue
        if is_junk_row(name):
            continue
        cann = row[1].strip() if len(row)>1 else ''
        if not cann or cann.upper() == 'CANNABINOID':
            row_tiers = find_tier_columns(row)
            if row_tiers:
                tier_cols = row_tiers
                set_url_columns_from_header(row)
                pc, cc = find_pieces_columns(row)
                if pc != -1: pieces_col = pc
                if cc != -1: cat_col = cc
            items.append({'sec':True,'n':strip_stars(' '.join(name.split()))})
            continue
        pic_idx, coa_idx = find_url_columns(row)
        pic_raw = row[pic_idx].strip() if pic_idx != -1 else ''
        coa     = row[coa_idx].strip() if coa_idx != -1 else ''
        tiers = extract_tiers(row, tier_cols)
        pic = get_pic(pic_raw)
        coa = coa if is_valid_coa(coa) else ''
        # Read pieces + category from the sheet columns (fallback to hardcoded map)
        raw_pieces = row[pieces_col].strip() if 0 <= pieces_col < len(row) else ''
        raw_cat    = row[cat_col].strip()    if 0 <= cat_col    < len(row) else ''
        if raw_pieces or raw_cat:
            pieces = format_pieces(raw_pieces, raw_cat)
        else:
            pieces = piece_label(name)  # fallback to built-in map
        items.append({'sec':False,'n':name,'cann':cann,
                      'cannList':[x.strip() for x in re.split(r'[/,]', cann) if x.strip()],
                      'qty':row[2].strip() if len(row)>2 else '',
                      'tiers':tiers,'pic':pic,'coa':coa,
                      'note':'','pieces':pieces})
    return items
def build_edibles_js(items):
    lines = ['const EDIBLES=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        if p['sec']:
            lines.append(f'{{sec:true,n:"{esc(p["n"])}"}}{comma}')
        else:
            lines.append(f'{{n:"{esc(p["n"])}",cann:"{esc(p["cann"])}",cannList:{cann_js(p)},size:"",tiers:{tiers_js(p)},pieces:"{esc(p.get("pieces",""))}",pic:"{esc(p["pic"])}",coa:"{esc(p["coa"])}",note:"{esc(p.get("note",""))}"}}{comma}')
    lines.append('];')
    return '\n'.join(lines)

# ── GENERIC SECTION PARSER (Extracts, Syrup, Topicals, GelCaps) ──────────────
PIECES = {
    "WAFFLE": ("snack", "cereal"),
    "FRUITY": ("snack", "cereal"),
    "COTTON CANDY": ("snack", "cereal"),
    "BIRTHDAY CAKE": ("snack", "cereal"),
    "APPLE KUSH": ("snack", "cereal"),
    "TAJIN SANDIA": ("snack", "cereal"),
    "CINNAMON": ("snack", "cereal"),
    "BANANA SPLITS": ("snack", "cereal"),
    "SMORES": ("snack", "cereal"),
    "PINEAPPLE EXPRESS": ("snack", "cereal"),
    "CUBES": ("10", "gummy"),
    "DELTA BEARS": ("10", "gummy"),
    "DELTA STRIPS": ("10", "gummy"),
    "DELTA BURSTS": ("10", "gummy"),
    "DELTA ROPE": ("1 rope", "gummy with candy"),
    "DELTA DROPS": ("10", "gummy"),
    "CRUNCHY BELT PURPLE PUNCH": ("1 belt", "gummy with candy"),
    "CRUNCHY BELT WILD CHERRY": ("1 belt", "gummy with candy"),
    "CRUNCHY BELT LOUD LEMON": ("1 belt", "gummy with candy"),
    "CRUNCHY BELT HONEY LEMON": ("1 belt", "gummy with candy"),
    "CRUNCHY BELT STRAWBERRY SHERBERT": ("1 belt", "gummy with candy"),
    "CRUNCHY BELT GRAPE LEMONADE": ("1 belt", "gummy with candy"),
    "CRUNCHY BELT RAINBOW": ("1 belt", "gummy with candy"),
    "GOOEY BURSTS": ("10", "gummy"),
    "LITTLES": ("10", "candy"),
    "VERY BEARY": ("10", "gummy"),
    "RAINBOW STRIPS": ("10", "gummy"),
    "APPLE RINGS": ("6", "gummy"),
    "SOUR WATERMELON SHARKS": ("6", "gummy"),
    "STRAWBERRY PUFFS": ("6", "gummy"),
    "PEACH RINGS": ("6", "gummy"),
    "ALL STAR MIX": ("10", "gummy"),
    "SOUR OCTOPUS": ("6", "gummy"),
    "WATERMELON WORMS": ("6", "gummy"),
    "GUMMY SHARKS": ("6", "gummy"),
    "SOUR GLOW WORMS": ("6", "gummy"),
    "PINEAPPLE RINGS": ("6", "gummy"),
    "TRIPS AHOY PEANUT BUTTER": ("2", "cookie"),
    "TRIPS AHOY RED VELVET": ("2", "cookie"),
    "TRIPS AHOY CANDY BLAST": ("2", "cookie"),
    "TRIPS AHOY CHEWY": ("2", "cookie"),
    "TRIPS AHOY CHUNKY": ("2", "cookie"),
    "ASTRO FOOD COOKIES": ("10", "cookie"),
    "ASTRO FOOD ORBIT O'S": ("10", "cookie"),
    "POT TARTS STRAWBERRY": ("1", "pastry"),
    "COOKIE CRISP BAR": ("1", "cereal bar"),
    "RICE CEREAL TREATS ORIGINAL": ("1", "cereal bar"),
    "RICE CEREAL TREATS BIRTHDAY CAKE": ("1", "cereal bar"),
    "CHEDDAR SNACK CRACKERS": ("snack", "chips"),
    "CAP'N CHRONIC ORIGINAL": ("snack", "cereal"),
    "CAP'N CHRONIC BERRIES": ("snack", "gummy"),
    "LOUDEST FLAKES ORIGINAL": ("snack", "gummy"),
    "LOUDEST FLAKES BANANA CREME": ("snack", "gummy"),
    "FRUITY CEREAL": ("snack", "gummy"),
    "FRUITY LOOP CEREAL": ("snack", "gummy"),
    "TRIX CEREAL": ("snack", "gummy"),
    "CANNABIS TOAST CRUNCH CHURROS": ("snack", "cereal"),
    "CANNABIS TOAST CRUNCH CEREAL": ("snack", "cereal"),
    "STONEY CHARMS CEREAL": ("snack", "cereal"),
    "DOWEEDOS SPICY SWEET CHILI": ("snack", "chips"),
    "DOWEEDOS FLAMAS": ("snack", "chips"),
    "DOWEEDOS TAPATIO": ("snack", "chips"),
    "DOWEEDOS NACHO CHEESE": ("snack", "chips"),
    "DOWEEDOS COOL RANCH": ("snack", "chips"),
    "CORN CHIPS ORIGINAL": ("snack", "chips"),
    "CORN CHIPS FLAMIN' HOT": ("snack", "chips"),
    "CORN CHIPS CHILLI CHEESE": ("snack", "chips"),
    "TAKIS FUEGO": ("snack", "chips"),
    "CHEESE PUFFS FLAMIN' HOT": ("snack", "chips"),
    "CHEESE PUFFS ORIGINAL": ("snack", "chips"),
    "CHEESE PUFFS CRUNCHY": ("snack", "chips"),
    "CHEESE PUFFS XXTRA FLAMIN' HOT": ("snack", "chips"),
}

def piece_label(name):
    """Return a display label like '~10 pieces · gummy' or 'Full Bag · chips'."""
    import re as _re
    key = _re.sub(r'\s+', ' ', name).strip().upper()
    entry = PIECES.get(key)
    if not entry:
        # Try without spaces (handles "PINE APPLE" vs "PINEAPPLE")
        nospace = key.replace(' ', '')
        for k, v in PIECES.items():
            if k.replace(' ', '') == nospace:
                entry = v
                break
    if not entry:
        # Auto-default obvious snacks
        SNACK_WORDS = ['CHIPS','FUNYUNS','PUFFS','TAKIS','DOWEEDOS','CEREAL',
                       'FLAKES','CRACKERS','CORN CHIP','POTATO']
        if any(w in key for w in SNACK_WORDS):
            return 'Full Bag'
        return ''
    val, note = entry
    v = val.strip().lower()
    if v == 'snack':
        count = 'Full Bag'
    elif 'rope' in v:
        count = '1 Rope'
    elif 'belt' in v:
        count = '1 Belt'
    elif v.isdigit():
        n = int(v)
        count = '1 piece' if n == 1 else f'~{n} pieces'
    else:
        count = val
    if note:
        return f'{count} \u00b7 {note}'
    return count

def find_pieces_columns(header):
    """Locate PIECES PER UNIT and CATEGORY columns by header label."""
    pieces_idx = cat_idx = -1
    for i, h in enumerate(header):
        hl = h.strip().lower()
        if 'piece' in hl:
            pieces_idx = i
        elif hl in ('category','subcategory','sub category','type'):
            cat_idx = i
    return pieces_idx, cat_idx

def format_pieces(val, cat):
    """Build display label from raw pieces value + category, e.g. '~10 pieces \u00b7 gummy'."""
    val = (val or '').strip()
    cat = (cat or '').strip()
    if not val and not cat:
        return ''
    v = val.lower()
    if v == 'snack':
        count = 'Full Bag'
    elif 'rope' in v:
        count = '1 Rope'
    elif 'belt' in v:
        count = '1 Belt'
    elif v.isdigit():
        n = int(v)
        count = '1 piece' if n == 1 else f'~{n} pieces'
    elif val:
        count = val
    else:
        count = ''
    cat_disp = cat.lower() if cat else ''
    if count and cat_disp:
        return f'{count} \u00b7 {cat_disp}'
    return count or cat_disp

# Header-based column locator: set once per sheet from the PRODUCT NAME row.
_URL_COL_HINTS = {'pic': -1, 'coa': -1}

def set_url_columns_from_header(header):
    """Detect PICTURE and COA columns by their header labels (reliable regardless
    of whether both URLs are on the same domain)."""
    pic = coa = -1
    for i, h in enumerate(header):
        hl = str(h).strip().lower()
        if pic == -1 and ('picture' in hl or 'photo' in hl or 'image' in hl):
            pic = i
        if coa == -1 and 'coa' in hl and 'date' not in hl:
            coa = i
    _URL_COL_HINTS['pic'] = pic
    _URL_COL_HINTS['coa'] = coa
    return pic, coa

def find_url_columns(row):
    """Find picture and COA columns.
    1) Prefer the header-detected columns (set via set_url_columns_from_header).
    2) Fall back to URL sniffing, but handle the case where BOTH pic and COA are
       the same domain (e.g. two drive.google.com links) by taking them in order."""
    # Header-based first (most reliable)
    ph, ch = _URL_COL_HINTS['pic'], _URL_COL_HINTS['coa']
    if ph != -1 or ch != -1:
        # Only trust header cols if the cells actually hold something
        pic_ok = ph != -1 and ph < len(row) and row[ph].strip()
        coa_ok = ch != -1 and ch < len(row) and row[ch].strip()
        if pic_ok or coa_ok:
            return (ph if pic_ok else -1), (ch if coa_ok else -1)

    # Fallback: collect ALL url-bearing cells in order, then assign
    url_cols = []
    for i, cell in enumerate(row):
        c = cell.strip()
        if ('http' in c) and ('drive.google.com' in c or 'leadconnectorhq.com' in c
                or 'storage.googleapis.com' in c or 'filesafe.space' in c
                or 'shopify.com' in c or 'cdn.' in c or '.pdf' in c.lower()
                or 'images.' in c):
            url_cols.append(i)
    if not url_cols:
        return -1, -1
    if len(url_cols) == 1:
        # Single URL — assume it's the picture (COA optional now)
        return url_cols[0], -1
    # Two or more URLs: first = picture, second = COA (matches sheet column order)
    return url_cols[0], url_cols[1]

def _price_val(s):
    """Extract the leading dollar amount from a price string like '$100/10 UNITS' -> 100.0"""
    import re
    m = re.search(r'\$?\s*([\d,]+\.?\d*)', s.replace(',', ''))
    return float(m.group(1)) if m else 0.0

def strip_stars(name):
    """Remove surrounding *** from a section name: '***DOOBIES***' -> 'DOOBIES'."""
    return str(name).strip().strip('*').strip()

def is_junk_row(name):
    """True if this row is a repeated header or metadata — NOT a real product and
    NOT a section header. Note: '***DOOBIES***' style names are SECTION HEADERS,
    not junk — only a repeated 'PRODUCT NAME...' header or call/metadata is junk."""
    n = ' '.join(str(name).split()).strip().upper()
    if not n: return True
    if n.startswith('PRODUCT NAME'): return True   # repeated header row (even 'PRODUCT NAME ***KING***')
    if n.startswith('CALL ') or 'HEMP TO ORDER' in n: return True
    if n.startswith('LAST UPDATED'): return True
    return False


def is_tier_header_row(row):
    """True if this row carries price-tier COLUMN LABELS ('100 UNIT PRICE',
    'SINGLE UNIT PRICE') rather than product data.

    WHY THIS EXISTS: every pre-roll section has its OWN price ladder — King Size
    runs 1000/500/100/1 while Doobies and Hotties run 100/50/10/1. Those repeated
    header rows frequently arrive from the CSV export with a BLANK first cell,
    because the coloured section title sitting beside them is a merged cell and
    gviz only puts the value in the first row of a merge. The old parser skipped
    any row with an empty first cell, so the section below silently inherited the
    ladder above it and every quantity printed 10x too high.
    """
    for c in row:
        u = ' '.join(str(c).upper().split())
        if 'PRICE' in u and ('UNIT' in u or 'SINGLE' in u):
            return True
    return False


def adopt_tier_header(row, current):
    """If `row` carries tier labels, return the new tier columns (and refresh the
    picture/COA column hints). Otherwise return `current` unchanged."""
    if not is_tier_header_row(row):
        return current
    found = find_tier_columns(row)
    if found:
        set_url_columns_from_header(row)
        return found
    return current


def is_full_header_row(row):
    """True if this row is a section's own COLUMN-HEADER row — it names the
    CANNABINOID / QUANTITY AVAILABLE columns. Such a row is expected to carry its
    section's price ladder."""
    joined = ' '.join(str(c).upper() for c in row)
    return 'CANNABINOID' in joined and ('QUANTITY AVAILABLE' in joined or 'QTY AVAILABLE' in joined)


# Sections whose header row we saw but could not read a ladder from. Their
# products are published with NO prices rather than the previous section's
# numbers — a blank price is a phone call, a wrong price is a lawsuit.
UNRESOLVED_LADDERS = []


def ladder_for_section(row, current, tab, section):
    """Resolve the price ladder for a new section.

    If the section's own header row carries readable tier labels, use them.
    If it is plainly a header row but the labels are missing or unreadable,
    return [] — DO NOT fall through to the previous section's ladder. Inheriting
    silently is what published Doobies at "1,000 units for $1,700" when the sheet
    said 100."""
    found = find_tier_columns(row)
    if found:
        set_url_columns_from_header(row)
        return found
    mapped = ladder_from_map(section, current)
    if mapped:
        set_url_columns_from_header(row)
        return mapped
    if is_full_header_row(row):
        UNRESOLVED_LADDERS.append(f'{tab} / {section or "(unnamed section)"}')
        return []
    return current


def classify_row(row, header_has_cannabinoid_col=True):
    """Unified, defensive row classifier used by all parsers.
    Returns one of: 'junk', 'section', 'product', 'empty'.
    - empty:   no name at all
    - junk:    repeated 'PRODUCT NAME' header, CALL/metadata, 'LAST UPDATED'
    - section: has a name but no cannabinoid value (a divider/header), incl. ***X***
    - product: has a name AND a cannabinoid value
    This is intentionally forgiving: anything with a cannabinoid is a product,
    anything without is a section header (unless it's junk)."""
    if not row or not str(row[0]).strip():
        return 'empty'
    name = str(row[0]).strip()
    if is_junk_row(name):
        return 'junk'
    cann = str(row[1]).strip() if len(row) > 1 else ''
    if not cann:
        return 'section'
    return 'product'


def any_price_in_row(row):
    """True if any cell in the row holds a $ price (used by the audit + parsers)."""
    return any('$' in str(c) for c in row)


def find_price_columns(header):
    """Locate box-price and unit-price columns by their header labels.
    Returns (box_idx, unit_idx) or (-1,-1) if not found by label."""
    box_idx = unit_idx = -1
    for i, h in enumerate(header):
        hl = h.strip().lower()
        if 'box' in hl and 'price' in hl:
            box_idx = i
        elif ('single' in hl or 'per unit' in hl or 'single unit' in hl) and 'price' in hl:
            unit_idx = i
    return box_idx, unit_idx


def find_tier_columns(header):
    """Scan a header row and return an ordered list of (label, column_index) for
    every quantity-tier price column. Handles all the variants in the sheet:
      '1000 UNITS PRICE', '500 UNITS PRICE', '100 UNIT PRICE', '10UNIT PRICE',
      'SINGLE UNIT PRICE', 'PRICE PER UNIT', '50 UNIT PRICE', etc.
    The tier label shown to buyers is derived from the number (e.g. '1000u', '1u').
    """
    import re as _re
    tiers = []
    for i, h in enumerate(header):
        hl = str(h).strip()
        hu = ' '.join(hl.upper().split())   # collapse newlines inside a wrapped header cell
        if 'PRICE' not in hu:
            continue
        # Single-unit variants. NOTE: a bare 'PRICE' header (Edibles' legacy
        # free-text column like "$100/10 UNITS") is NOT a clean tier — skip it.
        if 'SINGLE' in hu or 'PER UNIT' in hu:
            tiers.append(('1u', i))
            continue
        if hu == 'PRICE':
            continue  # legacy combined column — ignore
        # Numbered tiers: pull the leading number (1000, 500, 100, 50, 10)
        m = _re.search(r'(\d+)\s*UNIT', hu)
        if m:
            n = m.group(1)
            tiers.append((f'{n}u', i))
    return tiers


def extract_tiers(row, tier_cols):
    """Given a row and the (label, idx) tier columns, return an ordered list of
    {'label','qty','price'} for the tiers that actually have a price in this row.
    Ordered biggest-qty first (best wholesale price shown first)."""
    out = []
    for label, idx in tier_cols:
        if idx >= len(row):
            continue
        cell = str(row[idx]).strip()
        if not cell or cell.upper() in ('N/A', 'NA', 'TBD', '-'):
            continue
        if '$' not in cell and not _re_price_digit(cell):
            continue
        # Skip combined free-text values like "$100/10 UNITS" — not a clean tier price.
        if '/' in cell or 'UNIT' in cell.upper():
            continue
        price = cell
        qty = int(label[:-1]) if label[:-1].isdigit() else 1
        out.append({'label': label, 'qty': qty, 'price': price})
    # Sort by qty descending (1000u first ... 1u last)
    out.sort(key=lambda t: -t['qty'])
    return out


def _re_price_digit(s):
    import re as _re
    return bool(_re.search(r'\d', str(s)))


def find_prices(row, pic_idx, coa_idx, box_col=-1, unit_col=-1):
    """Return (box_price, unit_price).
    Strategy:
    1. If labeled columns were found in the header, use them directly.
    2. Otherwise collect all $-cells, DISCARD any 'combined' price that contains
       '/' or 'UNIT' (e.g. '$100/10 UNITS' — that's the stale legacy column),
       then take the two clean prices: larger = box-of-10, smaller = single unit.
    """
    # 1. Labeled columns (most reliable)
    if box_col != -1 or unit_col != -1:
        box  = row[box_col].strip()  if 0 <= box_col  < len(row) else ''
        unit = row[unit_col].strip() if 0 <= unit_col < len(row) else ''
        if box or unit:
            if box and unit and _price_val(box) == _price_val(unit):
                return box, ''
            return box, unit
    # 2. Fallback: gather clean $-prices, dropping any combined "/UNITS" style value
    clean = []
    for i in range(2, len(row)):
        if i == pic_idx or i == coa_idx: continue
        cell = row[i].strip()
        if '$' not in cell: continue
        up = cell.upper()
        # Skip the stale combined price like "$100/10 UNITS"
        if '/' in cell or 'UNIT' in up:
            continue
        clean.append(cell)
    if not clean:
        # No clean prices — fall back to whatever single price exists
        for i in range(2, len(row)):
            if i == pic_idx or i == coa_idx: continue
            if '$' in row[i]:
                return row[i].strip(), ''
        return '', ''
    if len(clean) == 1:
        return clean[0], ''
    clean_sorted = sorted(clean, key=_price_val, reverse=True)
    box, unit = clean_sorted[0], clean_sorted[1]
    if _price_val(box) == _price_val(unit):
        return box, ''
    return box, unit

def parse_generic(rows, const_name):
    """Parse Extracts/Syrup/Topicals/GelCaps. Detects pic/COA columns by URL and
    captures ALL price tiers (50u/10u/1u etc.) by header label."""
    items = []
    _URL_COL_HINTS['pic'] = _URL_COL_HINTS['coa'] = -1  # reset per-tab
    tier_cols = []
    for row in rows:
        if not row: continue
        if not row[0].strip():
            tier_cols = adopt_tier_header(row, tier_cols)
            continue
        name = row[0].strip()
        # Header rows (top or combined section+header) carry the tier labels.
        if name.strip().upper().startswith('PRODUCT NAME') or _row_has_tier_header(row):
            row_tiers = find_tier_columns(row)
            if row_tiers:
                tier_cols = row_tiers
            set_url_columns_from_header(row)
            # If it's ALSO a *** section *** name, emit the divider.
            if name.startswith('*') and name.endswith('*'):
                items.append({'sec':True,'n':strip_stars(' '.join(name.split()))})
            continue
        if is_junk_row(name):
            continue
        cann = row[1].strip() if len(row)>1 else ''
        if not cann or cann.upper() == 'CANNABINOID':
            clean_name = strip_stars(' '.join(name.split()))
            tier_cols = adopt_tier_header(row, tier_cols)
            items.append({'sec':True,'n':clean_name})
            continue
        pic_idx, coa_idx = find_url_columns(row)
        pic_raw = row[pic_idx].strip() if pic_idx != -1 else ''
        coa     = row[coa_idx].strip() if coa_idx != -1 else ''
        tiers = extract_tiers(row, tier_cols)
        pic = get_pic(pic_raw)
        coa = coa if is_valid_coa(coa) else ''
        cann_list = [x.strip() for x in re.split(r'[/,]', cann) if x.strip()]
        items.append({'sec':False,'n':name,'cann':cann,'cannList':cann_list,'size':'',
                      'tiers':tiers,'pic':pic,'coa':coa,
                      'qty':row[2].strip() if len(row)>2 else ''})
    return items


def _row_has_tier_header(row):
    """True if this row carries price-tier column labels (e.g. a combined
    section+header row like ***EXTRACTS*** ... 50 UNITS PRICE)."""
    joined = ' '.join(str(c).upper() for c in row)
    return 'PRICE' in joined and 'UNIT' in joined and row[0].strip().startswith('*')

def build_generic_js(items, const_name):
    lines = [f'const {const_name}=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        if p['sec']:
            lines.append(f'{{sec:true,n:"{esc(p["n"])}"}}{comma}')
        else:
            lines.append(f'{{n:"{esc(p["n"])}",cann:"{esc(p["cann"])}",cannList:{cann_js(p)},size:"{esc(p.get("size",""))}",tiers:{tiers_js(p)},pic:"{esc(p["pic"])}",coa:"{esc(p["coa"])}",note:""}}{comma}')
    lines.append('];')
    return '\n'.join(lines)

# ── FRONT-END PATCHES (applied to index.html on every build) ─────────────────
# build.py normally only swaps the product data arrays. These two fixes live in
# the page's JavaScript, so they are applied here — idempotent, and re-applied
# automatically if index.html is ever restored from an older copy.
FRONTEND_PATCHES = [
    (
        "self-serve checkout — pay now",
        # The catalog already had a full cart, contact form and shipping picker;
        # it just ended at "request a quote". This adds a second path: a big
        # total, and a Pay Now button that mints a real invoice and sends the
        # customer to Clover with one exact figure on screen.
        #
        # The quote path is untouched. Some buyers want to negotiate; they still
        # can. This is for the ones who want to pay and get on with it.
        """      <button id="coSubmit" class="co-btn" onclick="submitQuote()">Submit Quote Request</button>""",
        """      <div id="coPayNote" class="co-stock-note" style="display:none">✅ <b>Paying reserves your order.</b> We confirm stock before your card is charged for anything — if something is unavailable we will call you, and refund in full if you would rather not substitute.</div>
      <div id="coTotalBox" class="co-totalbox" style="display:none">
        <div class="co-total-lbl">Estimated total</div>
        <div class="co-total-num" id="coTotalNum">$0.00</div>
        <div class="co-total-sub" id="coTotalSub"></div>
      </div>
      <button id="coPayBtn" class="co-btn co-btn-pay" style="display:none" onclick="payNow()">Pay Now &amp; Reserve This Order</button>
      <button id="coSubmit" class="co-btn co-btn-quote" onclick="submitQuote()">Or request a quote instead</button>"""
    ),
    (
        "show the total when a shipping speed is picked",
        # pickShip() set SELECTED_SHIP and stopped, so nothing ever revealed the
        # total or the Pay button. One line, and it is the line the whole flow
        # hangs on.
        """function pickShip(radio){
  SELECTED_SHIP = {
    method: radio.value,
    shipEstimate: Number(radio.dataset.est)||0,
    grandTotal: Number(radio.dataset.total)||0
  };
}""",
        """function pickShip(radio){
  SELECTED_SHIP = {
    method: radio.value,
    shipEstimate: Number(radio.dataset.est)||0,
    grandTotal: Number(radio.dataset.total)||0
  };
  if (typeof showCheckoutTotal === 'function') showCheckoutTotal();
}"""
    ),
    (
        "checkout styles + payNow",
        """</body>""",
        """<style>
.co-totalbox{background:#12261B;border-radius:14px;padding:18px 20px;margin:14px 0 12px;text-align:center}
.co-total-lbl{font-size:10px;letter-spacing:.26em;text-transform:uppercase;color:#C9A227;font-weight:700}
.co-total-num{font-family:Georgia,serif;font-size:38px;font-weight:700;color:#fff;line-height:1.1;margin-top:6px}
.co-total-sub{font-size:12px;color:#8FA896;margin-top:8px;line-height:1.6}
.co-btn-pay{background:linear-gradient(96deg,#C9A227,#A5872B)!important;color:#1A1400!important;
  font-size:16px!important;font-weight:800!important;padding:17px!important}
.co-btn-quote{background:transparent!important;color:#8FA896!important;border:1px solid rgba(255,255,255,.18)!important;
  font-size:13px!important;font-weight:600!important;margin-top:9px!important}
.co-paywait{background:#12261B;border-radius:14px;padding:24px;text-align:center;color:#fff}
.co-payamt{font-family:Georgia,serif;font-size:34px;font-weight:700;color:#C9A227;margin:10px 0}
.co-paystep{text-align:left;font-size:13.5px;color:#C6D6C9;line-height:1.9;margin:14px 0}
.co-paybtn{display:inline-block;background:#C9A227;color:#1A1400;text-decoration:none;border-radius:10px;
  padding:15px 28px;font-weight:800;font-size:15px;margin-top:8px}
</style>
<script>
/* ── SELF-SERVE PAYMENT ────────────────────────────────────────────────────
   Shows the total the moment a shipping speed is picked, then mints a real
   invoice and hands the customer to Clover with one exact figure.

   The amount matters: Clover's widget is open-amount and carries no reference
   field, so the invoice number is encoded in the CENTS. A payment of exactly
   $3,240.25 can only be INV-100025. That is why the figure is repeated three
   times on the way out — it has to be typed correctly. */
/* ONE id per cart, not per click. A new id on every attempt meant a lost
   response — network drop, phone sleeping — would mint a SECOND invoice when
   the customer tried again. The id resets only when the cart does. */
var CHECKOUT_SUBMIT_ID = 'co-'+Date.now()+'-'+Math.random().toString(36).slice(2,9);
function newCheckoutId(){
  CHECKOUT_SUBMIT_ID = 'co-'+Date.now()+'-'+Math.random().toString(36).slice(2,9);
}

/* The free-shipping threshold lives on the server. Hardcoding it here as well
   meant the two could drift, and the customer would be shown one total and
   charged another. */
var CO_FREE_OVER = 5000, CO_DEPOSIT_PCT = 50;
(async function loadCheckoutConfig(){
  try{
    var r=await fetch(APPS_SCRIPT_URL,{method:'POST',
      headers:{'Content-Type':'text/plain;charset=utf-8'},
      body:JSON.stringify({action:'catalogConfig',secret:CATALOG_SECRET})});
    var d=await r.json();
    if(d&&d.ok){
      CO_FREE_OVER=Number(d.freeShipOver)||CO_FREE_OVER;
      CO_DEPOSIT_PCT=Number(d.depositPct)||CO_DEPOSIT_PCT;
      if(d.payEnabled===false){ var b=document.getElementById('coPayBtn'); if(b) b.dataset.off='1'; }
    }
  }catch(e){}
})();

function showCheckoutTotal(){
  var box=document.getElementById('coTotalBox');
  var pay=document.getElementById('coPayBtn');
  if(!box||!pay) return;
  if(!SELECTED_SHIP){ box.style.display='none'; pay.style.display='none'; return; }
  var sub=cartSubtotal();
  var ship=Number(SELECTED_SHIP.shipEstimate)||0;
  var free=(CO_FREE_OVER>0 && sub>=CO_FREE_OVER);
  if(free) ship=0;
  var total=sub+ship;
  document.getElementById('coTotalNum').textContent=money(total);
  document.getElementById('coTotalSub').innerHTML=
    money(sub)+' product'+(free?' &middot; <b style="color:#4ADE80">free shipping</b>'
                               :(' &middot; '+money(ship)+' shipping'))+
    // The exact figure is set when the invoice number is issued — it carries a
    // few cents that tie the payment to the order. Saying "estimated" here
    // stops the number appearing to change on the next screen.
    '<br>Your exact total is confirmed on the next screen.';
  box.style.display='block';
  pay.style.display = pay.dataset.off ? 'none' : 'block';
  // Swap the stock wording — "confirmed after submission" is not true once
  // somebody has handed over money.
  var sn=document.querySelector('.co-stock-note:not(#coPayNote)');
  var pn=document.getElementById('coPayNote');
  if(sn) sn.style.display='none';
  if(pn) pn.style.display='block';
}

async function payNow(){
  var name=document.getElementById('coName').value.trim();
  var shop=document.getElementById('coShop').value.trim();
  var phone=document.getElementById('coPhone').value.trim();
  var email=document.getElementById('coEmail').value.trim();
  var shipTo=document.getElementById('coShipTo').value.trim();
  var shipMethod=document.querySelector('input[name="shipMethod"]:checked');

  if(!name||!phone||!email){ alert('Please enter your name, phone, and email.'); return; }
  if(!shop){ alert('Please enter your business name.'); return; }
  if(!shipMethod){ alert('Please choose a shipping speed.'); return; }
  var items=cartItemsForPayload();
  if(!items.length){ alert('Your cart is empty.'); return; }

  var btn=document.getElementById('coPayBtn');
  btn.disabled=true; btn.textContent='Reserving your order…';

  var payload={
    action:'catalogCheckout',
    secret:CATALOG_SECRET,
    rep:getRep(),
    submitId:CHECKOUT_SUBMIT_ID,
    customer:{name:name, shop:shop, phone:phone, email:email,
              addr1:shipTo, city:'', state:'',
              zip:document.getElementById('coZip').value.replace(/[^0-9]/g,'')},
    items:items,
    shippingMethod:shipMethod.value,
    shipping: SELECTED_SHIP ? SELECTED_SHIP.shipEstimate : 0,
    notes:document.getElementById('coNotes').value.trim()
  };

  try{
    var res=await fetch(APPS_SCRIPT_URL,{method:'POST',
      headers:{'Content-Type':'text/plain;charset=utf-8'},
      body:JSON.stringify(payload)});
    var d=await res.json();
    if(!d.ok) throw new Error(d.msg||'could not reserve the order');

    document.getElementById('checkoutBody').innerHTML=
      '<div class="co-paywait">'+
        '<div style="font-size:11px;letter-spacing:.24em;text-transform:uppercase;color:#8FA896">'+
          'Order '+d.invoiceNo+' reserved</div>'+
        '<div class="co-payamt">'+money(d.total)+'</div>'+
        '<div style="font-size:13px;color:#C6D6C9">Pay this exact amount, including the cents.</div>'+
        '<div class="co-paystep">'+
          '<b>1.</b> Tap the button below to open our secure payment page.<br>'+
          '<b>2.</b> Enter <b style="color:#C9A227">'+money(d.total)+'</b> as the amount — the cents '+
            'identify your order, so please do not round it.<br>'+
          '<b>3.</b> Complete the payment. We will confirm by email.'+
        '</div>'+
        '<a class="co-paybtn" href="'+d.payUrl+'" target="_blank" rel="noopener">'+
          'Pay '+money(d.total)+'</a>'+
        '<div style="font-size:12px;color:#8FA896;margin-top:16px;line-height:1.7">'+
          'Prefer to pay a deposit? Send <b>'+money(d.deposit)+'</b> and we will invoice the balance '+
          'before shipping.<br>Questions: (408) 444-HEMP</div>'+
        (d.pdfUrl?('<div style="margin-top:14px"><a href="'+d.pdfUrl+'" target="_blank" '+
          'style="color:#C9A227;font-size:13px">View your invoice</a></div>'):'')+
      '</div>';
  }catch(err){
    btn.disabled=false; btn.textContent='Pay Now & Reserve This Order';
    alert('Sorry, we could not reserve that order. Please call or text (408) 444-HEMP and we will take care of you.');
  }
}
</script>
</body>"""
    ),
    (
        "unit-mode add-to-cart",
        # Mini soda cans / tuna cans / quarter-ounce jars have NO weight tiers —
        # lb/half/qtr/oz are all 0 — so the pricing block rendered empty and the
        # popup had no quantity box and no Add button. Nothing to click.
        """  const wRow=(lbl,val)=>val?`<div class="pop-wrow">
      <span class="pop-wlbl">${lbl}</span>
      <span class="pop-wamt">${fmt(val)}</span>
      <span class="pop-wqty">
        <button class="pop-qbtn" onclick="popQty('${nmE}','${lbl}',-1)">−</button>
        <input class="pop-qin" id="q_${lbl.replace(/[^A-Za-z]/g,'')}" value="1" inputmode="numeric" onchange="popQtyFix(this)">
        <button class="pop-qbtn" onclick="popQty('${nmE}','${lbl}',1)">+</button>
      </span>
      <button class="pop-wadd" onclick="addQtyToCart('${nmE} — ${lbl}','${fmt(val)}','flower','${lbl.replace(/[^A-Za-z]/g,'')}')">+ Add</button>
    </div>`:'';
  const pricing=`<div class="pop-pricing">
      <div class="pop-price-hd">Select size & quantity, then add to quote:</div>`+
    wRow('Pound',p.lb)+
    wRow('½ Pound',p.half)+
    wRow('¼ Pound',p.qtr)+
    wRow('Ounce',p.oz)+
    '</div>';""",
        """  const wRow=(lbl,val)=>{
      if(!val) return '';
      // Weight tiers arrive as numbers; unit-mode products arrive already
      // formatted ("$15"), so only run fmt() on the numeric case.
      const amt=(typeof val==='number')?fmt(val):String(val);
      const key=lbl.replace(/[^A-Za-z]/g,'');
      return `<div class="pop-wrow">
      <span class="pop-wlbl">${lbl}</span>
      <span class="pop-wamt">${amt}</span>
      <span class="pop-wqty">
        <button class="pop-qbtn" onclick="popQty('${nmE}','${lbl}',-1)">−</button>
        <input class="pop-qin" id="q_${key}" value="1" inputmode="numeric" onchange="popQtyFix(this)">
        <button class="pop-qbtn" onclick="popQty('${nmE}','${lbl}',1)">+</button>
      </span>
      <button class="pop-wadd" onclick="addQtyToCart('${nmE} — ${lbl}','${amt}','flower','${key}')">+ Add</button>
    </div>`;
    };
  // Unit-mode products price per can/jar, not by weight. Without this branch the
  // pricing block came out empty and the product could not be added at all.
  const pricing=p.unitmode
    ? `<div class="pop-pricing">
      <div class="pop-price-hd">Choose quantity, then add to quote:</div>`+
      wRow(p.size?('Per Unit '+p.size):'Per Unit', p.unitprice)+
      '</div>'
    : `<div class="pop-pricing">
      <div class="pop-price-hd">Select size & quantity, then add to quote:</div>`+
    wRow('Pound',p.lb)+
    wRow('½ Pound',p.half)+
    wRow('¼ Pound',p.qtr)+
    wRow('Ounce',p.oz)+
    '</div>';""",
    ),
    (
        "fixed-blend popup (Esco Bars)",
        # A pre-set blend is not a menu. Showing tickboxes implies a choice that
        # does not exist and stamps a fabricated "selection" onto the quote line.
        """        ${(p.cannList&&p.cannList.length)?`<div class="cann-picker">
          <div class="cann-picker-hd">Available cannabinoids — pick one or more:</div>
          <div class="cann-picker-note">These are the options available. Choose what you want (or all).</div>
          <div class="cann-picker-grid">
            ${p.cannList.map((x,i)=>`<label class="cann-opt"><input type="checkbox" class="cann-cb" data-cann="${x}" ${i===0?'checked':''}> <span>${x}</span></label>`).join('')}
          </div>
        </div>`:''}""",
        """        ${(p.cannList&&p.cannList.length)?(p.fixedBlend?`<div class="cann-picker">
          <div class="cann-picker-hd">Blend — pre-set, not selectable</div>
          <div class="cann-picker-note">These cannabinoids are the ingredients inside this device. It ships as a fixed combination.</div>
          <div class="cann-avail" style="margin-top:8px;justify-content:flex-start">${p.cannList.map(x=>`<span class="cann-chip">${x}</span>`).join('')}</div>
        </div>`:`<div class="cann-picker">
          <div class="cann-picker-hd">Available cannabinoids — pick one or more:</div>
          <div class="cann-picker-note">These are the options available. Choose what you want (or all).</div>
          <div class="cann-picker-grid">
            ${p.cannList.map((x,i)=>`<label class="cann-opt"><input type="checkbox" class="cann-cb" data-cann="${x}" ${i===0?'checked':''}> <span>${x}</span></label>`).join('')}
          </div>
        </div>`):''}""",
    ),
    (
        "fixed-blend card label",
        """    ? `<div class="cann-avail"><span class="cann-avail-lbl">Available:</span>${item.cannList.map(x=>`<span class="cann-chip">${x}</span>`).join('')}</div>`""",
        """    ? `<div class="cann-avail"><span class="cann-avail-lbl">${item.fixedBlend?'Blend:':'Available:'}</span>${item.cannList.map(x=>`<span class="cann-chip">${x}</span>`).join('')}</div>`""",
    ),
]


def apply_frontend_patches(html):
    """Apply the JS fixes above. Returns (html, problems).
    A patch that is already applied is fine and silent. A patch that can neither
    be found NOR is already applied is reported loudly — it means index.html
    drifted and the fix is silently missing from the live site."""
    problems = []
    for name, old, new in FRONTEND_PATCHES:
        if new in html:
            continue                      # already applied
        if old in html:
            html = html.replace(old, new, 1)
            print(f'    front-end patch applied: {name}')
        else:
            problems.append(f'*Front-end patch "{name}" could not be applied* — the target code in '
                            f'index.html did not match and the fix is NOT on the live site. '
                            f'index.html has probably been edited by hand. Send Claude index.html.')
    return html, problems

# ── NETLIFY DEPLOY ───────────────────────────────────────

# ── SHORT LINKS AND CACHE RULES ──────────────────────────────────────────────
# Apps Script web apps cannot live on a custom domain — Google serves them from
# script.google.com and they will not answer to a CNAME. What CAN be done is a
# branded entry point that redirects, so reps type something they can remember
# and the browser lands on the real URL.
#
# These are 302s, not proxies. A proxy would break the app: Apps Script runs
# inside a sandboxed iframe and its google.script.run calls depend on being on
# the script.google.com origin.
APPS_SCRIPT_EXEC = ('https://script.google.com/macros/s/'
                    'AKfycbw_3jfvJY1Y2UVXs_XODHDbUMTNzB36kwraR_UZle-l8Rq94pHB_qEgo4DPxsvR-D22fg/exec')

REDIRECTS = f"""# Branded short links for the team. 302 so the address is never cached by a
# browser — if the deployment URL changes, updating this file is enough.
/sales      {APPS_SCRIPT_EXEC}                 302
/order      {APPS_SCRIPT_EXEC}                 302
/invoice    {APPS_SCRIPT_EXEC}                 302
/quote      {APPS_SCRIPT_EXEC}                 302

# The console. Deliberately NOT /admin — a guessable path to an owner console
# gated by one shared code is an invitation. Change this string whenever you
# would change the code itself.
/ops-console  {APPS_SCRIPT_EXEC}?page=admin    302
"""

HEADERS = """/*
  Cache-Control: public, max-age=0, must-revalidate
  Netlify-CDN-Cache-Control: public, max-age=0, must-revalidate
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin

/dashboard.html
  X-Robots-Tag: noindex, nofollow
"""

def ensure_site_config():
    """Write _headers and _redirects if the repo has not got them, so a deploy
    can never publish a site without cache rules or short links."""
    for name, body in (('_redirects', REDIRECTS), ('_headers', HEADERS)):
        if not os.path.exists(name):
            try:
                open(name, 'w').write(body)
                print(f'  Created {name}')
            except Exception as e:
                print(f'  Could not write {name}: {e}')



def deploy_to_netlify(html_content):
    """Deploy the catalog plus the dashboard and its data files in one Netlify deploy.
    The dashboard becomes available at /dashboard.html and reads the JSON alongside it."""
    if not NETLIFY_TOKEN or not NETLIFY_SITE_ID:
        print('  No Netlify credentials — skipping deploy')
        return False

    # Gather every file to publish.
    #
    # A Netlify API deploy publishes ONLY the files listed here — anything absent
    # is gone from the live site. _headers and _redirects were never in this list,
    # so every build silently wiped the cache rules and any short links. They are
    # included now, and generated below if the repo does not have them.
    files = {'/index.html': html_content.encode('utf-8')}
    ensure_site_config()
    for extra in ('dashboard.html', 'dashboard_data.json', 'inventory_history.json',
                  '_headers', '_redirects'):
        if os.path.exists(extra):
            try:
                files['/' + extra] = open(extra, 'rb').read()
            except Exception as e:
                print(f'  (skipping {extra}: {e})')

    # Declare all files with their sha1 hashes.
    digests = {path: hashlib.sha1(data).hexdigest() for path, data in files.items()}
    headers_json = {'Authorization': f'Bearer {NETLIFY_TOKEN}', 'Content-Type': 'application/json'}
    body = json.dumps({'files': digests}).encode()
    req = Request(
        f'https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}/deploys',
        data=body, headers=headers_json, method='POST'
    )
    resp = urlopen(req, timeout=60).read()
    deploy = json.loads(resp)
    deploy_id = deploy.get('id', '')
    if not deploy_id:
        print('  ERROR: No deploy ID returned')
        return False

    # Netlify tells us which files it still needs; upload those.
    required = deploy.get('required', [])
    headers_bin = {'Authorization': f'Bearer {NETLIFY_TOKEN}', 'Content-Type': 'application/octet-stream'}
    for path, data in files.items():
        # Upload if Netlify asked for this hash, or always upload index.html to be safe.
        if not required or digests[path] in required or path == '/index.html':
            req2 = Request(
                f'https://api.netlify.com/api/v1/deploys/{deploy_id}/files{path}',
                data=data, headers=headers_bin, method='PUT'
            )
            urlopen(req2, timeout=120)
    print(f'  Deployed to Netlify (deploy {deploy_id}) — {len(files)} file(s): ' +
          ', '.join(sorted(p.lstrip("/") for p in files)))
    return True

# ── INJECT INTO HTML ─────────────────────────────────────
def inject(html, const_name, new_js, next_const):
    """Replace a JS const array. Finds const NAME=[ ... up to next const."""
    s = html.find(f'const {const_name}=[')
    if s == -1:
        print(f'  WARNING: const {const_name}=[ not found in HTML — skipping')
        return html
    e = html.find(f'const {next_const}=', s + len(const_name))
    if e == -1:
        print(f'  WARNING: end marker "const {next_const}" not found — skipping')
        return html
    # Walk back over whitespace/newlines before next const so we don't double them
    while e > 0 and html[e-1] in ('\n', '\r', ' ', '\t'):
        e -= 1
    print(f'  Injected const {const_name}: {len(new_js)} chars')
    return html[:s] + new_js + '\n\n' + html[e:]

# ── MAIN ─────────────────────────────────────────────────


_PRICE_LABELS = {'lb':'LB','half':'½LB','qtr':'¼LB','oz':'OZ',
                 'price':'Box','unit':'Unit','case':'Case','unitprice':'Unit'}

def _product_prices(p):
    """Return an ordered dict-like list of (label, value) prices for a product."""
    out = []
    if p.get('unitmode'):
        if p.get('unitprice'): out.append(('Unit', str(p.get('unitprice'))))
        return out
    for k in ('lb','half','qtr','oz','price','unit','case'):
        v = p.get(k)
        if v: out.append((_PRICE_LABELS[k], str(v)))
    return out

def _product_price_str(p):
    """A single comparable price string (stable key for diffing)."""
    return '|'.join(f'{lbl}:{val}' for lbl, val in _product_prices(p))

def _price_diff_pretty(old_str, new_str):
    """Given two 'LB:1275|½LB:705' strings, show only what changed, human-readable."""
    def _parse(s):
        d = {}
        for part in str(s).split('|'):
            if ':' in part:
                k, v = part.split(':', 1); d[k] = v
        return d
    o, n = _parse(old_str), _parse(new_str)
    diffs = []
    for k in n:
        if k in o and o[k] != n[k]:
            diffs.append(f'{k} ${o[k]}→${n[k]}')
    return ', '.join(diffs) if diffs else 'pricing updated'


def snapshot_products(parsed):
    """Build a flat {tab::name: {price, stock, cann}} snapshot of everything live."""
    snap = {}
    for tab, (rows, items) in parsed.items():
        for p in items:
            if p.get('sec'):
                continue
            nm = ' '.join(str(p.get('n','')).split())
            key = f'{tab}::{nm}'
            snap[key] = {
                'tab': tab,
                'name': nm,
                'price': _product_price_str(p),
                'stock': ' '.join(str(p.get('qty','')).split()) or 'unknown',
                'cann': p.get('cann',''),
            }
    return snap


def _is_out_of_stock(stock_str):
    s = str(stock_str).upper()
    return 'SOLD OUT' in s or 'OUT OF STOCK' in s or s.strip() in ('0','0 LBS','NONE')


def diff_inventory(new_snap):
    """Compare the new snapshot to the last one. Returns a dict of change lists.
    Also appends changes to the running history file with a timestamp."""
    import json as _json
    old_snap = {}
    if os.path.exists(SNAPSHOT_FILE):
        try: old_snap = _json.load(open(SNAPSHOT_FILE))
        except Exception: old_snap = {}

    added, removed, price_changes, went_oos, back_in = [], [], [], [], []

    # First run — nothing to diff against
    first_run = not old_snap

    for key, cur in new_snap.items():
        prev = old_snap.get(key)
        if prev is None:
            if not first_run:
                added.append(cur)
            continue
        # price change
        if cur['price'] != prev.get('price') and cur['price'] and prev.get('price'):
            price_changes.append({'tab':cur['tab'],'name':cur['name'],
                                  'old':prev.get('price',''),'new':cur['price']})
        # stock transitions
        was_oos = _is_out_of_stock(prev.get('stock',''))
        now_oos = _is_out_of_stock(cur['stock'])
        if now_oos and not was_oos:
            went_oos.append(cur)
        elif was_oos and not now_oos:
            back_in.append(cur)

    for key, prev in old_snap.items():
        if key not in new_snap:
            removed.append(prev)

    changes = {
        'added': added, 'removed': removed, 'price_changes': price_changes,
        'went_oos': went_oos, 'back_in': back_in, 'first_run': first_run,
    }

    # Persist the new snapshot for next time
    try: _json.dump(new_snap, open(SNAPSHOT_FILE,'w'), indent=1)
    except Exception: pass

    # Append to running history (only if there were real changes)
    if not first_run and any([added, removed, price_changes, went_oos, back_in]):
        hist = []
        if os.path.exists(HISTORY_FILE):
            try: hist = _json.load(open(HISTORY_FILE))
            except Exception: hist = []
        entry = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'version': BUILD_VERSION,
            'added': [f"{c['tab']}: {c['name']}" for c in added],
            'removed': [f"{c['tab']}: {c['name']}" for c in removed],
            'price_changes': [f"{c['tab']}: {c['name']} ({c['old']} -> {c['new']})" for c in price_changes],
            'went_oos': [f"{c['tab']}: {c['name']}" for c in went_oos],
            'back_in': [f"{c['tab']}: {c['name']}" for c in back_in],
        }
        hist.append(entry)
        hist = hist[-500:]  # keep the last 500 change-events
        try: _json.dump(hist, open(HISTORY_FILE,'w'), indent=1)
        except Exception: pass

    return changes


def audit_tier_ladders(parsed):
    """Report the price ladder each section actually resolved to.

    A section inheriting the ladder above it is silent and expensive: the prices
    stay right while every QUANTITY prints wrong. This surfaces the ladder per
    section so a wrong one is obvious in the build log and the Slack audit."""
    lines, problems = [], []
    for tab in ('PreRoll', 'Vape', 'Edibles', 'Extracts', 'Syrup', 'Topicals', 'GelCaps'):
        if tab not in parsed:
            continue
        rows, items = parsed[tab]
        section = '(top of tab)'
        ladders = {}
        for p in items:
            if p.get('sec'):
                section = p.get('n', '')
                continue
            t = p.get('tiers') or []
            if not t:
                continue
            key = '/'.join(str(x['qty']) for x in t)
            ladders.setdefault(section, set()).add(key)
        for sec, keys in ladders.items():
            for k in keys:
                lines.append(f'{tab} · {sec or "(unnamed)"}: {k}')
            if len(keys) > 1:
                problems.append(f'*{tab} / {sec}* — products in the SAME section resolved to '
                                f'different quantity ladders ({", ".join(sorted(keys))}). '
                                f'One of them is inheriting the wrong header row. '
                                f'Check the tier labels on that section header in the sheet.')
    return lines, problems


def audit_build(parsed):
    """Detailed self-audit. Returns (report_lines, problems, warnings).
    Checks every tab for: silently dropped rows, big count changes vs last run,
    missing images, missing prices, missing COAs, duplicate product names,
    and empty tabs. The Slack message explains what each finding means and
    what to check, so a non-technical reader knows exactly what to do."""
    import json as _json
    problems = []      # serious — something is broken or data is being lost
    warnings = []      # worth knowing — not broken, but a shop owner would notice
    report = []        # per-tab summary line
    counts = {}
    details = {}       # tab -> dict of counts (imgs, prices, coas, etc.)

    for tab, (rows, items) in parsed.items():
        prods = [x for x in items if not x.get('sec')]
        shown = set(' '.join(str(x.get('n','')).split()).upper() for x in prods)
        secs  = set(' '.join(str(x.get('n','')).split()).upper() for x in items if x.get('sec'))
        counts[tab] = len(prods)

        # --- Silent drops: a row with cannabinoid + price that never became a product ---
        dropped = []
        for r in rows:
            if not r or not str(r[0]).strip():
                continue
            nm = ' '.join(str(r[0]).split()).upper()
            if nm.startswith('PRODUCT NAME') or 'NOT IN STOCK' in nm or 'FOR REFERENCE ONLY' in nm:
                continue
            if is_junk_row(r[0]):
                continue
            clean = strip_stars(nm)
            if clean in shown or clean in secs or nm in shown or nm in secs:
                continue
            # account for renamed unit products (e.g. "X (Mini Soda Cans)")
            if any(clean in s for s in shown):
                continue
            cann = str(r[1]).strip() if len(r) > 1 else ''
            has_price = any('$' in str(c) for c in r[2:8])
            if cann and has_price:
                dropped.append(str(r[0]).strip().replace(chr(10), ' '))

        # --- Quality checks on the products that DID make it ---
        _clean = lambda s: ' '.join(str(s).split())
        no_img   = [_clean(p['n']) for p in prods if not str(p.get('pic','')).strip()]
        # A product has a price if EITHER (a) any of the legacy per-unit keys are
        # populated (Flower still uses lb/half/qtr/oz), OR (b) it has a non-empty
        # `tiers` list with at least one priced tier (PreRoll/Vape/Edibles/Extracts/
        # Syrup/Topicals/GelCaps store prices this way now). Missing this second
        # branch was the bug that made every tiered product look like "Call for
        # Pricing" in the audit even though the catalog showed correct prices.
        _LEGACY_PRICE_KEYS = ('price','unit','case','lb','half','qtr','oz','unitprice')
        def _has_price(p):
            if any(str(p.get(k,'')).strip() for k in _LEGACY_PRICE_KEYS):
                return True
            tiers = p.get('tiers') or []
            return any(str(t.get('price','')).strip() for t in tiers if isinstance(t, dict))
        no_price = [_clean(p['n']) for p in prods if not _has_price(p)]
        no_coa   = [_clean(p['n']) for p in prods if not str(p.get('coa','')).strip()]

        # duplicate product names within a tab — a REAL dupe is the same name AND
        # the same product line/size (e.g. two "STRAWBERRY" both 1g Doobie).
        # Same name in DIFFERENT lines (Strawberry Doobie vs Strawberry Hottie) is
        # NOT a dupe — the size tag distinguishes them for buyers.
        seen = {}
        for p in prods:
            nm = _clean(p.get('n',''))
            key = (nm, str(p.get('size','')).strip())  # name + line/size
            seen[key] = seen.get(key, 0) + 1
        dupes = [f'{nm} ({sz})' if sz else nm
                 for (nm, sz), c2 in seen.items() if c2 > 1]

        details[tab] = {
            'products': len(prods), 'dropped': len(dropped),
            'no_img': len(no_img), 'no_price': len(no_price),
            'no_coa': len(no_coa), 'dupes': len(dupes),
        }

        # --- Escalate to problems / warnings ---
        if dropped:
            problems.append(f'*{tab}* — {len(dropped)} row(s) with a cannabinoid AND a price '
                            f'did NOT show up on the site (silently dropped). '
                            f'Check these rows in the sheet: ' +
                            ', '.join(dropped[:6]) + (' …' if len(dropped) > 6 else ''))
        if dupes:
            warnings.append(f'*{tab}* — {len(dupes)} duplicate name(s): ' +
                            ', '.join(dupes[:5]) + (' …' if len(dupes) > 5 else '') +
                            '  (buyers will see the same product listed twice)')
        if len(prods) == 0:
            problems.append(f'*{tab}* — 0 products! This tab is EMPTY on the site. '
                            f'Check the sheet tab has data and correct headers.')
        if no_img and len(prods) > 0:
            pct = len(no_img) / len(prods) * 100
            lvl = problems if pct >= 50 else warnings
            lvl.append(f'*{tab}* — {len(no_img)}/{len(prods)} products have NO image ({pct:.0f}%). '
                       f'These show a grey placeholder. Likely cause: the image link in the sheet '
                       f'is blank, a Slack link (expires), or a Drive file not shared "Anyone with the link". '
                       f'Examples: ' + ', '.join(no_img[:4]) + (' …' if len(no_img) > 4 else ''))
        if no_price:
            warnings.append(f'*{tab}* — {len(no_price)} product(s) show "Call for Pricing" (no price in sheet): ' +
                            ', '.join(no_price[:4]) + (' …' if len(no_price) > 4 else ''))

        # per-tab report line
        flags = []
        if dropped:  flags.append(f'{len(dropped)} dropped')
        if no_img:   flags.append(f'{len(no_img)} no-img')
        if no_price: flags.append(f'{len(no_price)} no-price')
        if dupes:    flags.append(f'{len(dupes)} dupe')
        report.append(f'{tab}: {len(prods)} products' + (f'  ⚠️ ' + ', '.join(flags) if flags else '  ✓'))

    # --- Price-ladder audit: catches a section inheriting the wrong header ---
    ladder_lines, ladder_problems = audit_tier_ladders(parsed)
    for sec in UNRESOLVED_LADDERS:
        problems.append(f'*{sec}* — this section\'s header row was found but its price-tier '
                        f'labels could not be read, so its products were published with NO '
                        f'prices ("Call for Pricing") rather than inheriting the wrong ones. '
                        f'Check that the tier labels on that header row read like '
                        f'"100 UNIT PRICE" and are plain text.')
    problems.extend(ladder_problems)
    if ladder_lines:
        report.append('— price ladders (units per tier) —')
        report.extend(ladder_lines)

    # --- Compare product counts to the previous run ---
    prev = {}
    if os.path.exists(COUNTS_FILE):
        try: prev = _json.load(open(COUNTS_FILE))
        except Exception: prev = {}
    for tab, n in counts.items():
        p = prev.get(tab)
        if p is not None and p > 0:
            change = n - p
            drop_pct = (p - n) / p * 100
            if drop_pct >= 20:
                problems.append(f'*{tab}* — product count DROPPED {p} → {n} '
                                f'({drop_pct:.0f}% fewer than last build). '
                                f'Did you remove products on purpose? If not, something broke the parser.')
            elif change >= max(5, p * 0.5):
                warnings.append(f'*{tab}* — product count jumped {p} → {n} (+{change}). '
                                f'Expected if you added inventory; worth a glance if not.')
    try: _json.dump(counts, open(COUNTS_FILE, 'w'))
    except Exception: pass

    return report, problems, warnings


def check_invoice_health(catalog_version, live_version=None):
    """Call the invoice system's ?page=health endpoint.

    Version model (fully automatic — no hand-set numbers anywhere):
      • The catalog owns the version and publishes it to dashboard_data.json.
      • The invoice READS that same published version at health-check time.
      • So both systems always reference ONE source of truth. "In sync" simply
        means the invoice can successfully read the catalog's published version.

    Timing note: during a build, the NEW version has not been deployed yet, so the
    invoice still sees the PREVIOUS live version. That is expected and is NOT a
    desync. The caller passes `live_version` = the version that is currently live
    (captured before dashboard_data.json is overwritten), and we compare the
    invoice's seen-version against THAT.

    Returns: {reachable, version, live_version, next_version, in_sync, ok,
              problems, warnings, stats}.
    Never raises — a down invoice system becomes a reported problem, not a crash.
    """
    result = {'reachable': False, 'version': None, 'live_version': None,
              'next_version': catalog_version, 'in_sync': None,
              'ok': None, 'problems': [], 'warnings': [], 'stats': {}}

    # The version that is CURRENTLY live (previous build). Prefer the value passed
    # by the caller; fall back to reading the local file if not provided.
    if live_version is None:
        try:
            if os.path.exists('dashboard_data.json'):
                with open('dashboard_data.json') as _f:
                    live_version = str((json.load(_f) or {}).get('version', '') or '')
        except Exception:
            live_version = ''
    live_version = str(live_version or '')
    result['live_version'] = live_version or None

    if not INVOICE_EXEC_URL:
        return result
    try:
        url = INVOICE_EXEC_URL + ('&' if '?' in INVOICE_EXEC_URL else '?') + 'page=health'
        req = Request(url, headers={'User-Agent': 'EHF-build-audit/1.0'})
        raw = urlopen(req, timeout=30).read().decode('utf-8')
        data = json.loads(raw)
        result['reachable'] = True
        result['version']  = data.get('version')          # catalog version the invoice can see
        result['ok']       = data.get('ok')
        result['problems'] = list(data.get('problems', []) or [])
        result['warnings'] = list(data.get('warnings', []) or [])
        result['stats']    = data.get('stats', {}) or {}

        seen = str(result['version'] or '')
        if not seen:
            # Invoice reached, but it could not read the catalog version URL.
            result['in_sync'] = None
        elif live_version:
            # Both known: in sync when the invoice sees the current live version.
            result['in_sync'] = (seen == live_version)
        else:
            # No local live version to compare (e.g. first ever build). If the
            # invoice can see any version, treat as in sync — same source of truth.
            result['in_sync'] = True
    except Exception as e:
        result['problems'].append(f'Invoice system unreachable: {e}')
    return result


def send_slack_audit(report, problems, warnings=None, changes=None, invoice=None):
    """Post a detailed audit to Slack that a non-technical reader can act on.
    Structure:
      • Headline with version + overall status
      • PROBLEMS (things broken / data lost) — with what to check
      • WARNINGS (worth a look, not broken)
      • Per-tab summary line
      • A short 'what this audit checks' legend
    """
    if not SLACK_AUDIT_WEBHOOK:
        return
    import json as _json
    warnings = warnings or []
    vtag = f'v{BUILD_VERSION}' if BUILD_VERSION else ''
    total = 0
    for r in report:
        try: total += int(r.split(':')[1].strip().split(' ')[0])
        except Exception: pass

    # A clean build should be one line. Nobody reads a wall of green ticks, and a
    # message people skim is a message that hides the one line that mattered.
    if not problems and not warnings:
        clean = f':white_check_mark: *Catalog {vtag} built clean* — {total} products, {len(report)} tabs, nothing to fix.'
        if invoice is not None and invoice.get('reachable') and not invoice.get('ok'):
            clean += '\n:red_circle: _But the invoice system reported issues — see the console._'
        try:
            req = Request(SLACK_AUDIT_WEBHOOK, data=_json.dumps({'text': clean}).encode('utf-8'),
                          headers={'Content-Type': 'application/json'}, method='POST')
            urlopen(req, timeout=15)
            print('Slack audit posted. (clean, one line)')
        except Exception as e:
            print(f'Slack audit failed (non-fatal): {e}')
        return

    if problems:
        headline = f':rotating_light: *Catalog {vtag} — {len(problems)} problem(s)*'
    else:
        headline = f':warning: *Catalog {vtag} — {len(warnings)} thing(s) to review*'

    parts = [headline]

    # ── Invoice system health + automatic version sync ──
    if invoice is not None:
        parts.append('')
        if not invoice.get('reachable'):
            parts.append('*:electric_plug: Invoice system:* :red_circle: could not reach it to check. '
                         'Approvals/quotes may still work, but this build could not confirm. '
                         '(Deploy Code.gs with the health endpoint if this is the first run.)')
        else:
            seen = invoice.get('version')          # catalog version the invoice can see (currently live)
            live = invoice.get('live_version')     # what this build knows is currently live
            nxt  = invoice.get('next_version')     # version this build will publish
            in_sync = invoice.get('in_sync')
            inv_ok = ':white_check_mark:' if invoice.get('ok') else ':red_circle:'
            inv_word = 'healthy' if invoice.get('ok') else 'ISSUES FOUND'
            stats = invoice.get('stats', {})
            reps = stats.get('repCount')
            rep_txt = f' · {reps} reps' if reps is not None else ''

            if in_sync is True:
                sync_line = (f':white_check_mark: reading catalog v{seen or "?"} '
                             f'(auto-synced — single source of truth)')
            elif in_sync is False:
                sync_line = (f':rotating_light: *version mismatch* — invoice is reading catalog '
                             f'v{seen or "?"}, but the live catalog is v{live or "?"}. '
                             f'This usually means the last catalog deploy did not publish, or the '
                             f'invoice URL points at a stale/cached file. Check that '
                             f'exclusive-hemp-farms.com/dashboard_data.json shows v{live or "?"}.')
            else:
                sync_line = (':grey_question: reached the invoice, but it could not read the catalog '
                             'version URL (exclusive-hemp-farms.com/dashboard_data.json). '
                             'Version sync unconfirmed.')

            if invoice.get('ok') and in_sync is True:
                parts.append(f'_Invoice system healthy{rep_txt}, versions in sync._')
            else:
                parts.append(f'*:electric_plug: Invoice system:* {inv_ok} {inv_word}{rep_txt} — {sync_line}')
            # After THIS build deploys, the live version becomes nxt; the invoice will
            # read that automatically on its next check. Note it so the flow is clear.
            # (Version drift after a deploy is expected and not worth a line.)
            for p in invoice.get('problems', []):
                parts.append(f'   :red_circle: {p}')
            for w in invoice.get('warnings', [])[:8]:
                parts.append(f'   :large_yellow_circle: {w}')
            if len(invoice.get('warnings', [])) > 8:
                parts.append(f'   _…and {len(invoice["warnings"])-8} more invoice warning(s)._')

    if problems:
        parts.append('')
        parts.append('*:red_circle: PROBLEMS (something is broken or products are missing):*')
        for p in problems:
            parts.append(f'• {p}')

    if warnings:
        parts.append('')
        parts.append('*:large_yellow_circle: WARNINGS (worth a look — not broken):*')
        for w in warnings:
            parts.append(f'• {w}')

    # Only the tabs that actually have something wrong. A clean tab needs no line.
    bad_tabs = [r for r in report if '⚠️' in r]
    if bad_tabs:
        parts.append('')
        parts.append('*Tabs with something wrong:*')
        for r in bad_tabs:
            parts.append(f'• {r}')

    # Inventory change report
    if changes and not changes.get('first_run'):
        c = changes
        if any([c['added'], c['removed'], c['price_changes'], c['went_oos'], c['back_in']]):
            parts.append('')
            parts.append('*:package: Catalog changes since last build:*')
            if c['added']:
                parts.append(f":new: *{len(c['added'])} added:* " +
                             ', '.join(x['name'] for x in c['added'][:8]) +
                             (' …' if len(c['added'])>8 else ''))
            if c['removed']:
                parts.append(f":x: *{len(c['removed'])} removed:* " +
                             ', '.join(x['name'] for x in c['removed'][:8]) +
                             (' …' if len(c['removed'])>8 else ''))
            if c['price_changes']:
                parts.append(f":heavy_dollar_sign: *{len(c['price_changes'])} price change(s):*")
                for pc in c['price_changes'][:8]:
                    parts.append(f"   • {pc['name']}: {_price_diff_pretty(pc['old'], pc['new'])}")
                if len(c['price_changes'])>8:
                    parts.append('   • …')
            if c['went_oos']:
                parts.append(f":red_circle: *{len(c['went_oos'])} went out of stock:* " +
                             ', '.join(x['name'] for x in c['went_oos'][:8]) +
                             (' …' if len(c['went_oos'])>8 else ''))
            if c['back_in']:
                parts.append(f":green_circle: *{len(c['back_in'])} back in stock:* " +
                             ', '.join(x['name'] for x in c['back_in'][:8]) +
                             (' …' if len(c['back_in'])>8 else ''))
        # (No changes needs no line.)

    parts.append('')
    parts.append('_Copy this message into your Claude chat if you want it fixed._')

    text = '\n'.join(parts)
    # Slack has a ~40k char limit per message; trim defensively.
    if len(text) > 38000:
        text = text[:38000] + '\n… (truncated)'

    try:
        req = Request(
            SLACK_AUDIT_WEBHOOK,
            data=_json.dumps({'text': text}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST')
        urlopen(req, timeout=15)
        status = 'ISSUES' if problems else ('warnings' if warnings else 'clean')
        print(f'Slack audit posted. ({status})')
    except Exception as e:
        print(f'Slack audit failed (non-fatal): {e}')


def main():
    global BUILD_VERSION
    print(f'\n=== EHF Catalog Builder v8 (per-section price ladders) — {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} ===')
    # Bump the version FIRST so it is available to every downstream write:
    # dashboard_data.json (which the invoice reads for sync), Slack audit, HTML
    # footer stamp. Setting it late produced dashboard_data.json with an empty
    # "version" field, which broke the invoice's version-sync check.
    BUILD_VERSION = get_next_version()
    print(f'  Build version: v{BUILD_VERSION}')

    # ── Fetch all sheet tabs ──
    print('Fetching sheets...')
    flower_rows   = fetch_sheet('THCa Flower')
    preroll_rows  = fetch_sheet('PreRoll')
    vape_rows     = fetch_sheet('Vape')
    edibles_rows  = fetch_sheet('Edibles')
    extracts_rows = fetch_sheet('Extracts')
    syrup_rows    = fetch_sheet('Syrup')
    topicals_rows = fetch_sheet('Topicals')
    gelcaps_rows  = fetch_sheet('GelCaps/Tinctures')

    print(f'  Flower rows fetched: {len(flower_rows)}')
    if not flower_rows:
        print('ERROR: Could not fetch sheet data. Check that the sheet is set to Anyone with link can view.')
        sys.exit(1)

    # ── Hash sheet data to detect changes ──
    all_raw = str(flower_rows)+str(preroll_rows)+str(vape_rows)+str(edibles_rows)+str(extracts_rows)+str(syrup_rows)+str(topicals_rows)+str(gelcaps_rows)
    data_hash = hashlib.md5(all_raw.encode()).hexdigest()
    prev_hash = ''
    if os.path.exists(HASH_FILE):
        prev_hash = open(HASH_FILE).read().strip()
    force = os.environ.get('FORCE_REBUILD','').lower() in ('1','true','yes')
    if data_hash == prev_hash and not force:
        print('No changes detected in sheet data. Skipping deploy.')
        return
    reason = 'Forced rebuild' if force else f'Changes detected (hash {prev_hash[:8] or "none"} → {data_hash[:8]})'
    print(f'{reason}. Rebuilding...')

    # ── Parse all tabs ──
    flower_items   = parse_flower(flower_rows)
    preroll_items  = parse_preroll(preroll_rows)
    vape_items     = parse_vape(vape_rows)
    edibles_items  = parse_edibles(edibles_rows)
    extracts_items = parse_generic(extracts_rows, 'EXTRACTS')
    syrup_items    = parse_generic(syrup_rows, 'SYRUP')
    topicals_items = parse_generic(topicals_rows, 'TOPICALS')
    gelcaps_items  = parse_generic(gelcaps_rows, 'GELCAPS')

    print(f'  Flower: {len(flower_items)} | PreRoll: {len([x for x in preroll_items if not x.get("sec")])} | Vape: {len([x for x in vape_items if not x.get("sec")])} | Edibles: {len([x for x in edibles_items if not x.get("sec")])}')

    # ── SELF-AUDIT: account for every row, compare to last run, alert Slack on problems ──
    _audit_report, _audit_problems, _audit_warnings = audit_build({
        'Flower':   (flower_rows,   flower_items),
        'PreRoll':  (preroll_rows,  preroll_items),
        'Vape':     (vape_rows,     vape_items),
        'Edibles':  (edibles_rows,  edibles_items),
        'Extracts': (extracts_rows, extracts_items),
        'Syrup':    (syrup_rows,    syrup_items),
        'Topicals': (topicals_rows, topicals_items),
        'GelCaps':  (gelcaps_rows,  gelcaps_items),
    })
    print('  --- BUILD AUDIT ---')
    for _line in _audit_report:
        print(f'    {_line}')
    if _audit_problems:
        print('  !!! AUDIT PROBLEMS !!!')
        for _p in _audit_problems:
            print(f'    [PROBLEM] {_p}')
    if _audit_warnings:
        print('  --- audit warnings ---')
        for _w in _audit_warnings:
            print(f'    [warn] {_w}')

    # ── Inventory change tracking (added/removed/price/stock) ──
    _parsed_for_diff = {
        'Flower':   (flower_rows,   flower_items),
        'PreRoll':  (preroll_rows,  preroll_items),
        'Vape':     (vape_rows,     vape_items),
        'Edibles':  (edibles_rows,  edibles_items),
        'Extracts': (extracts_rows, extracts_items),
        'Syrup':    (syrup_rows,    syrup_items),
        'Topicals': (topicals_rows, topicals_items),
        'GelCaps':  (gelcaps_rows,  gelcaps_items),
    }
    _snap = snapshot_products(_parsed_for_diff)
    _changes = diff_inventory(_snap)
    _c = _changes
    print('  --- INVENTORY CHANGES ---')
    print(f"    +{len(_c['added'])} added, -{len(_c['removed'])} removed, "
          f"{len(_c['price_changes'])} price changes, "
          f"{len(_c['went_oos'])} went OOS, {len(_c['back_in'])} back in stock")

    # Capture the version that is CURRENTLY live (previous build) BEFORE we
    # overwrite dashboard_data.json — the invoice reads this same live value.
    _prev_live_version = ''
    try:
        if os.path.exists('dashboard_data.json'):
            with open('dashboard_data.json') as _pf:
                _prev_live_version = str((json.load(_pf) or {}).get('version', '') or '')
    except Exception:
        _prev_live_version = ''

    # Write a JSON the dashboard can read
    try:
        import json as _dj
        _dj.dump({'version': BUILD_VERSION,
                  'built': datetime.now(timezone.utc).isoformat(),
                  'counts': {t: len([x for x in it if not x.get('sec')])
                             for t,(r,it) in _parsed_for_diff.items()},
                  'changes': _changes}, open('dashboard_data.json','w'), indent=1)
    except Exception as _e:
        print(f'    dashboard_data.json write failed: {_e}')

    # Check the invoice system's health + version sync (best-effort; never blocks).
    _invoice = check_invoice_health(BUILD_VERSION, _prev_live_version)
    if _invoice.get('reachable'):
        print(f"  --- INVOICE HEALTH --- invoice sees catalog v{_invoice.get('version')} "
              f"(live now: v{_invoice.get('live_version')}, this build: v{BUILD_VERSION}) "
              f"ok={_invoice.get('ok')} in_sync={_invoice.get('in_sync')} "
              f"({len(_invoice.get('problems',[]))} problems, {len(_invoice.get('warnings',[]))} warnings)")
    else:
        print("  --- INVOICE HEALTH --- unreachable")

    send_slack_audit(_audit_report, _audit_problems, _audit_warnings, _changes, _invoice)

    # ── Build JS arrays ──
    flower_js  = build_flower_js(flower_items)
    anim_js    = build_anim_js(flower_items)
    preroll_js = build_preroll_js(preroll_items)
    vape_js    = build_vape_js(vape_items)
    edibles_js = build_edibles_js(edibles_items)
    extracts_js= build_generic_js(extracts_items, 'EXTRACTS')
    syrup_js   = build_generic_js(syrup_items, 'SYRUP')
    topicals_js= build_generic_js(topicals_items, 'TOPICALS')
    gelcaps_js = build_generic_js(gelcaps_items, 'GELCAPS')

    # ── Load and patch HTML ──
    if not os.path.exists(HTML_FILE):
        print(f'ERROR: {HTML_FILE} not found in repo.')
        print(f'ACTION REQUIRED: Make sure {HTML_FILE} exists in the GitHub repo root.')
        sys.exit(0)  # exit 0 so workflow shows yellow, not red
    html = open(HTML_FILE, encoding='utf-8').read()

    html, _fe_problems = apply_frontend_patches(html)
    for _fp in _fe_problems:
        print(f'    [PROBLEM] {_fp}')
        _audit_problems.append(_fp)

    html = inject(html, 'FLOWER',   flower_js,   'PREROLL')
    html = inject(html, 'PREROLL',  preroll_js,  'VAPE')
    html = inject(html, 'VAPE',     vape_js,     'EDIBLES')
    html = inject(html, 'EDIBLES',  edibles_js,  'EXTRACTS')
    html = inject(html, 'EXTRACTS', extracts_js, 'SYRUP')
    html = inject(html, 'SYRUP',    syrup_js,    'TOPICALS')
    html = inject(html, 'TOPICALS', topicals_js, 'GELCAPS')
    html = inject(html, 'GELCAPS',  gelcaps_js,  'TABS')

    # Replace ANIM map
    anim_s = html.find('const ANIM={')
    anim_e = html.find('};', anim_s) + 2
    if anim_s != -1 and anim_e > anim_s:
        html = html[:anim_s] + anim_js + html[anim_e:]

    # Update timestamps (reflects last CONTENT change — build skips when unchanged).
    now = datetime.now(timezone.utc)
    date_str  = f"{now.month}/{now.day}/{str(now.year)[2:]} {now.hour}:{now.minute:02d}"
    long_date = now.strftime('%B') + f' {now.day}, {now.year}'
    # Header stamp: match ONLY the id="upd" div, not the footer's "Last Updated".
    html = re.sub(r'(<div class="hdr-upd" id="upd">)Updated: [^<]+(</div>)',
                  r'\g<1>Updated: ' + date_str + r'\g<2>', html)
    # Footer version stamp. NOTE: this used to require the literal text
    # "Last Updated:" after the version, but the footer actually reads
    # "Prices current as of:" — so the pattern never matched and the site showed
    # a frozen "Catalog v3.4" no matter how many times version.txt incremented.
    # Matching the version token alone is both correct and layout-independent.
    html = re.sub(r'Catalog v[\d.]+', f'Catalog v{BUILD_VERSION}', html)
    # Pricing-error protection. Injected on every build so it can never be lost
    # by an index.html edit. Idempotent — only added when not already present.
    if 'Pricing Errors:' not in html:
        html = html.replace(
            '<strong>Age Restriction:</strong>',
            '<strong>Pricing Errors:</strong> All prices, quantities, tier breaks and product '
            'descriptions shown in this catalog are provided for reference only and are subject '
            'to change without notice. They are an invitation to enquire, not a binding offer. '
            'In the event of a typographical, clerical, data-entry or system error affecting any '
            'price, unit quantity, tier quantity, or description, EHF reserves the right to '
            'correct the error and to decline, cancel or amend any order or quotation placed at '
            'the erroneous figure, whether or not that order has been acknowledged. '
            '<strong>EHF does not honor pricing or quantity errors.</strong> No order is binding '
            'until confirmed in writing by EHF on a formal invoice.<br><br>'
            '<strong>Age Restriction:</strong>', 1)

    # Keep the "Prices current as of" stamp honest — it moves with every rebuild.
    html = re.sub(r'Prices current as of: [^&<]+',
                  f'Prices current as of: {long_date} ', html)

    # Write updated HTML back to repo file
    open(HTML_FILE, 'w', encoding='utf-8').write(html)
    print(f'  HTML updated ({len(html):,} chars)')

    # ── Deploy to Netlify ──
    print('Deploying to Netlify...')
    success = deploy_to_netlify(html)

    # ── Save hash (so next run knows we already deployed this data) ──
    if success:
        open(HASH_FILE, 'w').write(data_hash)
        print(f'  Hash saved: {data_hash[:8]}')

    print('Done.')

if __name__ == '__main__':
    main()
