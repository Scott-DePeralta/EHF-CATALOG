#!/usr/bin/env python3
"""
EHF Catalog Auto-Builder
Fetches Google Sheets data, rebuilds index.html, deploys to Netlify.
Runs via GitHub Actions every 15 minutes.
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

# ── HELPERS ──────────────────────────────────────────────
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

def is_valid_pic(url):
    if not url: return False
    if 'slack-files.com' in url: return False
    if 'drive.google.com' in url: return bool(get_drive_id(url))
    if 'leadconnectorhq.com' in url or 'filesafe.space' in url: return True
    return False

def get_pic(raw):
    raw = raw.strip()
    if not is_valid_pic(raw): return ''
    if 'drive.google.com' in raw: return thumb(raw)
    return raw

def is_valid_coa(url):
    if not url: return False
    if 'slack-files.com' in url: return False
    return 'drive.google.com' in url or 'shopify.com' in url or 'cdn.' in url

def esc(s):
    return str(s).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').strip()

def clean_price(s):
    return s.strip().replace('$', '').replace(',', '')

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
def parse_flower(rows):
    items = []
    skip_names = {'PRODUCT NAME','CALL (408) 444-HEMP',''}
    for row in rows:
        if len(row) < 4: continue
        name = row[0].strip()
        if not name or name in skip_names or name.startswith('Last Updated'): continue
        thca_s = row[1].strip().replace('%','') if len(row)>1 else ''
        qty    = row[2].strip() if len(row)>2 else ''
        lb     = clean_price(row[3]) if len(row)>3 else ''
        half   = clean_price(row[4]) if len(row)>4 else ''
        qtr    = clean_price(row[5]) if len(row)>5 else ''
        oz     = clean_price(row[6]) if len(row)>6 else ''
        pic_raw= row[7].strip() if len(row)>7 else ''
        vid    = row[8].strip() if len(row)>8 else ''
        coa    = row[9].strip() if len(row)>9 else ''
        pic    = get_pic(pic_raw)
        if not pic or not is_valid_coa(coa): continue
        try: thca = float(thca_s)
        except: thca = None
        try: lb_f = float(lb)
        except: lb_f = 0
        try: half_f = float(half)
        except: half_f = 0
        try: qtr_f = float(qtr)
        except: qtr_f = 0
        try: oz_f = float(oz)
        except: oz_f = 0
        special = qty.upper() == 'MADE TO ORDER'
        st, sl = ST_MAP.get(name, auto_st(name))
        isnew = 'true' if name not in KNOWN_PREV else 'false'
        items.append({
            'n':name,'thca':thca,'qty':qty,'lb':lb_f,'half':half_f,
            'qtr':qtr_f,'oz':oz_f,'pic':pic,'vid':vid,'coa':coa,
            'st':st,'sl':sl,'isnew':isnew,'special':special,
        })
    return items

def build_flower_js(items):
    lines = ['const FLOWER=[']
    for i,p in enumerate(items):
        thca_js = str(p['thca']) if p['thca'] is not None else 'undefined'
        obj = (f'{{n:"{esc(p["n"])}",thca:{thca_js},qty:"{esc(p["qty"])}",lb:{p["lb"]},'
               f'half:{p["half"]},qtr:{p["qtr"]},oz:{p["oz"]},'
               f'pic:"{esc(p["pic"])}",vid:"{esc(p["vid"])}",coa:"{esc(p["coa"])}",'
               f'st:"{p["st"]}",sl:"{p["sl"]}",isnew:{p["isnew"]}')
        if p['special']: obj += ',special:true'
        obj += '}'
        lines.append(obj + (',' if i < len(items)-1 else ''))
    lines.append('];')
    return '\n'.join(lines)

def build_anim_js(items):
    lines = ['const ANIM={']
    for i,p in enumerate(items):
        cls = auto_anim(p['n'])
        lines.append(f"  '{esc(p['n'])}':'{cls}'" + (',' if i < len(items)-1 else ''))
    lines.append('};')
    return '\n'.join(lines)

# ── PREROLL PARSER ───────────────────────────────────────
SECTION_NAMES = {'KING SIZE PRE ROLLS','DOOBIES','HOTTIES','SINGLE MINI PRE ROLL'}

def parse_preroll(rows):
    items = []
    for row in rows:
        if not row or not row[0].strip(): continue
        name = row[0].strip()
        if name in ('PRODUCT NAME',): continue
        cann  = row[1].strip() if len(row)>1 else ''
        qty   = row[2].strip() if len(row)>2 else ''
        price = row[3].strip() if len(row)>3 else ''
        pic_raw = row[4].strip() if len(row)>4 else ''
        coa   = row[5].strip() if len(row)>5 else ''
        if name in SECTION_NAMES:
            items.append({'sec':True,'n':name})
            continue
        if not cann: continue
        pic = get_pic(pic_raw)
        if not pic or not is_valid_coa(coa): continue
        items.append({'sec':False,'n':name,'cann':cann,'qty':qty,
                      'price':price,'pic':pic,'coa':coa,'note':''})
    return items

def build_preroll_js(items):
    lines = ['const PREROLL=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        if p['sec']:
            lines.append(f'{{sec:true,n:"{esc(p["n"])}"}}{comma}')
        else:
            note = p.get('note','')
            lines.append(f'{{n:"{esc(p["n"])}",cann:"{esc(p["cann"])}",size:"",price:"{esc(p["price"])}",pic:"{esc(p["pic"])}",coa:"{esc(p["coa"])}",note:"{esc(note)}"}}{comma}')
    lines.append('];')
    return '\n'.join(lines)

# ── VAPE PARSER ──────────────────────────────────────────
VAPE_SECTIONS = {'2G DISPOSABLE VAPE\nBLINKERS BLEND',
                 '2G DISPOSABLE VAPE\nLIVE RESIN DIAMONDS\nPACKS POD',
                 '3G DISPOSABLE ESCO BARS','1G VAPE CARTRIDGE EHF'}

def parse_vape(rows):
    items = []
    box_col = unit_col = -1
    for row in rows:
        if not row or not row[0].strip(): continue
        name = row[0].strip()
        if name in ('PRODUCT NAME',):
            box_col, unit_col = find_price_columns(row)
            continue
        cann = row[1].strip() if len(row)>1 else ''
        if not cann:
            items.append({'sec':True,'n':name})
            continue
        pic_idx, coa_idx = find_url_columns(row)
        pic_raw = row[pic_idx].strip() if pic_idx != -1 else ''
        coa     = row[coa_idx].strip() if coa_idx != -1 else ''
        price, unit_price = find_prices(row, pic_idx, coa_idx, box_col, unit_col)
        pic = get_pic(pic_raw)
        if not pic or not is_valid_coa(coa): continue
        items.append({'sec':False,'n':name,'cann':cann,'qty':'',
                      'price':price,'unit':unit_price,'pic':pic,'coa':coa})
    return items

def build_vape_js(items):
    lines = ['const VAPE=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        if p['sec']:
            lines.append(f'{{sec:true,n:"{esc(p["n"])}"}}{comma}')
        else:
            lines.append(f'{{n:"{esc(p["n"])}",cann:"{esc(p["cann"])}",size:"",price:"{esc(p["price"])}",unit:"{esc(p.get("unit",""))}",pic:"{esc(p["pic"])}",coa:"{esc(p["coa"])}",note:""}}{comma}')
    lines.append('];')
    return '\n'.join(lines)

# ── EDIBLES PARSER ───────────────────────────────────────
EDIBLES_SECTIONS = {'SWEET TOOTH','SWEETH TOOTH','CBD CANDY','EHF','PESO PESO'}

def parse_edibles(rows):
    items = []
    box_col = unit_col = -1
    pieces_col = cat_col = -1
    for row in rows:
        if not row or not row[0].strip(): continue
        name = row[0].strip()
        if name in ('PRODUCT NAME',):
            box_col, unit_col = find_price_columns(row)
            pieces_col, cat_col = find_pieces_columns(row)
            continue
        cann = row[1].strip() if len(row)>1 else ''
        if not cann:
            items.append({'sec':True,'n':name})
            continue
        pic_idx, coa_idx = find_url_columns(row)
        pic_raw = row[pic_idx].strip() if pic_idx != -1 else ''
        coa     = row[coa_idx].strip() if coa_idx != -1 else ''
        price, unit_price = find_prices(row, pic_idx, coa_idx, box_col, unit_col)
        pic = get_pic(pic_raw)
        if not pic or not is_valid_coa(coa): continue
        # Read pieces + category from the sheet columns (fallback to hardcoded map)
        raw_pieces = row[pieces_col].strip() if 0 <= pieces_col < len(row) else ''
        raw_cat    = row[cat_col].strip()    if 0 <= cat_col    < len(row) else ''
        if raw_pieces or raw_cat:
            pieces = format_pieces(raw_pieces, raw_cat)
        else:
            pieces = piece_label(name)  # fallback to built-in map
        items.append({'sec':False,'n':name,'cann':cann,'qty':'',
                      'price':price,'unit':unit_price,'pic':pic,'coa':coa,
                      'note':'','pieces':pieces})
    return items
def build_edibles_js(items):
    lines = ['const EDIBLES=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        if p['sec']:
            lines.append(f'{{sec:true,n:"{esc(p["n"])}"}}{comma}')
        else:
            lines.append(f'{{n:"{esc(p["n"])}",cann:"{esc(p["cann"])}",size:"",price:"{esc(p["price"])}",unit:"{esc(p.get("unit",""))}",pieces:"{esc(p.get("pieces",""))}",pic:"{esc(p["pic"])}",coa:"{esc(p["coa"])}",note:"{esc(p.get("note",""))}"}}{comma}')
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

def find_url_columns(row):
    """Find picture and COA columns by detecting URLs — robust to extra price columns."""
    pic_idx = coa_idx = -1
    for i, cell in enumerate(row):
        c = cell.strip()
        if 'drive.google.com' in c or 'leadconnectorhq.com' in c or 'storage.googleapis.com' in c or 'filesafe.space' in c:
            if pic_idx == -1:
                pic_idx = i
        elif '.pdf' in c.lower() or 'shopify.com' in c or 'cdn.' in c:
            if coa_idx == -1:
                coa_idx = i
    return pic_idx, coa_idx

def _price_val(s):
    """Extract the leading dollar amount from a price string like '$100/10 UNITS' -> 100.0"""
    import re
    m = re.search(r'\$?\s*([\d,]+\.?\d*)', s.replace(',', ''))
    return float(m.group(1)) if m else 0.0

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
    price columns by header label, so column order/count doesn't matter."""
    items = []
    box_col = unit_col = -1
    for row in rows:
        if not row or not row[0].strip(): continue
        name = row[0].strip()
        if name in ('PRODUCT NAME',):
            box_col, unit_col = find_price_columns(row)
            continue
        cann = row[1].strip() if len(row)>1 else ''
        if not cann:
            items.append({'sec':True,'n':name})
            continue
        pic_idx, coa_idx = find_url_columns(row)
        pic_raw = row[pic_idx].strip() if pic_idx != -1 else ''
        coa     = row[coa_idx].strip() if coa_idx != -1 else ''
        price, unit_price = find_prices(row, pic_idx, coa_idx, box_col, unit_col)
        pic = get_pic(pic_raw)
        if not pic or not is_valid_coa(coa): continue
        items.append({'sec':False,'n':name,'cann':cann,'size':'',
                      'price':price,'unit':unit_price,'pic':pic,'coa':coa})
    return items

def build_generic_js(items, const_name):
    lines = [f'const {const_name}=[']
    for i,p in enumerate(items):
        comma = ',' if i < len(items)-1 else ''
        if p['sec']:
            lines.append(f'{{sec:true,n:"{esc(p["n"])}"}}{comma}')
        else:
            lines.append(f'{{n:"{esc(p["n"])}",cann:"{esc(p["cann"])}",size:"{esc(p.get("size",""))}",price:"{esc(p["price"])}",unit:"{esc(p.get("unit",""))}",pic:"{esc(p["pic"])}",coa:"{esc(p["coa"])}",note:""}}{comma}')
    lines.append('];')
    return '\n'.join(lines)

# ── NETLIFY DEPLOY ───────────────────────────────────────
def deploy_to_netlify(html_content):
    if not NETLIFY_TOKEN or not NETLIFY_SITE_ID:
        print('  No Netlify credentials — skipping deploy')
        return False
    content_bytes = html_content.encode('utf-8')
    sha1 = hashlib.sha1(content_bytes).hexdigest()
    headers_json = {
        'Authorization': f'Bearer {NETLIFY_TOKEN}',
        'Content-Type': 'application/json'
    }
    # Create deploy
    body = json.dumps({'files': {'/index.html': sha1}}).encode()
    req = Request(
        f'https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}/deploys',
        data=body, headers=headers_json, method='POST'
    )
    resp = urlopen(req, timeout=60).read()
    deploy = json.loads(resp)
    deploy_id = deploy.get('id','')
    if not deploy_id:
        print('  ERROR: No deploy ID returned')
        return False
    # Upload file
    headers_bin = {
        'Authorization': f'Bearer {NETLIFY_TOKEN}',
        'Content-Type': 'application/octet-stream'
    }
    req2 = Request(
        f'https://api.netlify.com/api/v1/deploys/{deploy_id}/files/index.html',
        data=content_bytes, headers=headers_bin, method='PUT'
    )
    urlopen(req2, timeout=120)
    print(f'  Deployed to Netlify (deploy {deploy_id})')
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
def main():
    print(f'\n=== EHF Catalog Builder v7 (pieces-from-sheet) — {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} ===')

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
    # DEBUG: print first 3 edibles prices so we can verify from the log
    _ed_prods = [x for x in edibles_items if not x.get('sec')][:3]
    for _p in _ed_prods:
        print(f'    EDIBLE {_p["n"]}: box={_p.get("price","")!r} unit={_p.get("unit","")!r} pieces={_p.get("pieces","")!r}')

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
        print('ACTION REQUIRED: Upload your EHF_Catalog.html renamed as index.html to the GitHub repo root.')
        sys.exit(0)  # exit 0 so workflow shows yellow, not red
    html = open(HTML_FILE, encoding='utf-8').read()

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

    # Update timestamps
    now = datetime.now(timezone.utc)
    date_str  = f"{now.month}/{now.day}/{str(now.year)[2:]} {now.hour}:{now.minute:02d}"
    long_date = now.strftime('%B') + f' {now.day}, {now.year}'
    html = re.sub(r'Updated: [^"<\n]+', f'Updated: {date_str}', html)
    html = re.sub(r'Catalog v[\d.]+ &nbsp;·&nbsp; Last Updated: [^<"]+',
                  f'Catalog v3.4 &nbsp;·&nbsp; Last Updated: {long_date}', html)

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
