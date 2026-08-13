"""Stage 1 of the dump pipeline: one streaming pass over the Wikidata truthy
dump, reducing ~982 GB of N-Triples to a few GB of exactly the facts the
location build needs.

Why a dump at all: live SPARQL cannot finish the largest countries. The
endpoint cancels an expensive query at its deadline and reports it as HTTP 429,
so "too expensive" and "slow down" are indistinguishable from the client side
and no amount of retrying or chunking fixes it.

Why two stages: nothing here can decide whether an entity is a settlement.
That needs the transitive P279 (subclass of) closure under Q486972, and
attaching a settlement to its region needs the transitive P131 (located in)
graph. Both are scattered across the whole dump, so the classification cannot
happen until the pass is over. This stage therefore harvests without judging --
it keeps anything that could plausibly matter and lets stage 2 decide.

    graph-p131.i32      child,parent int32 pairs -- the containment graph
    graph-p279.i32      child,parent int32 pairs -- the class graph
    entities/<qid>.jsonl  entity records, sharded by their P17 (country)
    entities/0.jsonl      records with no P17 at all
    scan-stats.json      counters, for checking a run against the last one

Memory is bounded by design and does not grow with the dump: the dump is
grouped by subject (verified -- no subject reappears after its block ends), so
only one entity is ever buffered, plus a fixed write buffer per shard. The two
graphs are the one thing accumulated across the whole file, and they are
int32 arrays rather than Python objects for that reason.

Usage:
    zcat latest-truthy.nt.gz | python dump_scan.py --out-dir dump-scan
    python dump_scan.py --input sample.nt --out-dir /tmp/scan-test
"""

import argparse
import array
import json
import os
import sys
import time

# --- what we harvest -------------------------------------------------------

# wdt: properties kept on any entity we keep. Anything not listed is dropped
# at the predicate test, which is what makes the pass affordable: schema:
# description alone is 37% of all triples in the dump, and rdfs:label,
# skos:prefLabel and schema:name are exact triplicates of each other, so two
# of the three are pure waste.
PROPS = {
    b'P31': 'instance_of',      # -> place_type, and the settlement test
    b'P279': 'subclass_of',     # -> the class graph
    b'P131': 'located_in',      # -> containment, and admin2_id
    b'P625': 'coord',
    b'P17': 'country',
    b'P300': 'iso_3166_2',      # -> the admin-1 tier
    b'P297': 'iso_3166_1',      # -> the country list
    b'P1082': 'population',
    b'P1566': 'geonames_id',
    b'P421': 'timezone',
    b'P281': 'postal_code',
    b'P2044': 'elevation',
    b'P1705': 'native_label',
    b'P2046': 'area',
    b'P402': 'osm_relation_id',
    b'P1376': 'capital_of',
    b'P576': 'dissolved',       # exclusion: a division that no longer exists
    b'P582': 'end_time',        # exclusion: same
    b'P37': 'official_language',
    b'P424': 'wm_lang_code',

    # Country-level facts. The upstream ODbL dataset carries these and we did
    # not, purely because they were never asked for -- they are in the same
    # CC0 source as everything else here, so closing that gap costs a rescan
    # and nothing else.
    b'P36': 'capital',
    b'P30': 'continent',
    b'P38': 'currency',
    b'P78': 'tld',
    b'P298': 'iso_3166_1_alpha3',
    b'P299': 'iso_3166_1_numeric',
    b'P474': 'calling_code',
    b'P1549': 'demonym',
    # These two sit on the currency item rather than the country, and are what
    # turn a bare currency QID into a name and a symbol.
    b'P498': 'currency_code',
    b'P5061': 'unit_symbol',
}

# Properties whose presence alone justifies keeping an entity. An entity with
# none of these cannot be a settlement, a region, a country, or a node on the
# path between them, so it is dropped and never written.
#
# P131 earns its place even without a coordinate: a settlement's containment
# chain can pass through an intermediate division that carries no P625 of its
# own, and dropping that node would break the walk from settlement to region.
#
# wm_lang_code and currency_code are here because a country's official
# language and its currency are referenced by QID, and the name behind that
# QID lives on the referenced item. Those items are not places and would
# otherwise be dropped. Language items happened to survive the previous scan
# only because most of them carry P279 -- an accident, not a guarantee.
KEEP_IF = ('located_in', 'coord', 'subclass_of', 'iso_3166_2', 'iso_3166_1',
           'wm_lang_code', 'currency_code')

# Properties used only as "does this entity have one at all", never for their
# value. Wikidata records "dissolved, date unknown" as an unknown-value snak,
# which RDF renders as a blank node -- so the object is unusable but its
# presence is the whole point. SPARQL's FILTER NOT EXISTS binds a blank node
# like any other term and excludes the entity; dropping the triple here
# because its object is not parseable would instead resurrect every division
# abolished on an unknown date. These keep a 'somevalue' marker instead.
PRESENCE_ONLY = frozenset(('dissolved', 'end_time'))

# Multi-valued fields. Everything else keeps its first value, which is
# deterministic because the dump is subject-grouped and we never reorder.
MULTI = frozenset((
    'instance_of', 'subclass_of', 'located_in', 'native_label',
    'official_language', 'iso_3166_2', 'iso_3166_1',
    'capital', 'continent', 'currency', 'tld', 'calling_code',
))

# Monolingual-text properties, kept per language like labels rather than
# collapsed to whichever language the dump happened to emit first.
LANG_KEYED = frozenset(('demonym',))

ENTITY_PREFIX = b'<http://www.wikidata.org/entity/Q'
ENTITY_PREFIX_LEN = len(ENTITY_PREFIX)
DIRECT_PREFIX = b'<http://www.wikidata.org/prop/direct/'
DIRECT_PREFIX_LEN = len(DIRECT_PREFIX)
LABEL_PRED = b'<http://www.w3.org/2000/01/rdf-schema#label> '
ALT_PRED = b'<http://www.w3.org/2004/02/skos/core#altLabel> '

SHARD_FLUSH_LINES = 2048
SHARD_MAX_OPEN = 256


# --- N-Triples value parsing ------------------------------------------------

_ESCAPES = {
    b'\\n': '\n', b'\\r': '\r', b'\\t': '\t', b'\\"': '"',
    b'\\\\': '\\', b"\\'": "'",
}


def unescape(raw):
    """N-Triples literal body (the bytes between the quotes) -> str.

    The common case has no backslash at all, so that is tested first and
    returns without touching the slow path.
    """
    if b'\\' not in raw:
        return raw.decode('utf-8', 'replace')
    out = []
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i:i + 1]
        if c != b'\\':
            out.append(c.decode('utf-8', 'replace'))
            i += 1
            continue
        pair = raw[i:i + 2]
        if pair in _ESCAPES:
            out.append(_ESCAPES[pair])
            i += 2
        elif pair == b'\\u':
            out.append(chr(int(raw[i + 2:i + 6], 16)))
            i += 6
        elif pair == b'\\U':
            out.append(chr(int(raw[i + 2:i + 10], 16)))
            i += 10
        else:
            out.append(pair.decode('utf-8', 'replace'))
            i += 2
    return ''.join(out)


def split_langtag(obj):
    """Language-tagged literal -> (body_bytes, lang_str), or (None, None).

    Scanned from the right. A language tag cannot contain '"@', so the last
    occurrence is always the real separator even when the text itself contains
    an escaped quote followed by an at-sign. Finding it from the right avoids
    the escape-aware forward scan in parse_object(), which matters because
    labels are a quarter of every triple in the dump.
    """
    i = obj.rfind(b'"@')
    if i < 1 or obj[0:1] != b'"':
        return None, None
    return obj[1:i], obj[i + 2:].decode('ascii', 'replace')


def parse_object(obj):
    """One N-Triples object term -> ('iri'|'lit', value, lang).

    Entity IRIs come back as the bare QID ('Q1234') rather than the full IRI:
    it is what every consumer here wants and it is a third of the bytes.
    """
    if obj.startswith(b'<'):
        end = obj.rfind(b'>')
        iri = obj[1:end]
        if iri.startswith(b'http://www.wikidata.org/entity/'):
            return 'iri', iri[31:].decode('ascii', 'replace'), None
        return 'iri', iri.decode('utf-8', 'replace'), None
    if not obj.startswith(b'"'):
        return None, None, None
    # Find the closing quote, skipping any that are backslash-escaped.
    i = 1
    n = len(obj)
    while i < n:
        c = obj[i]
        if c == 0x5C:      # backslash
            i += 2
            continue
        if c == 0x22:      # quote
            break
        i += 1
    if i >= n:
        return None, None, None
    body = obj[1:i]
    tail = obj[i + 1:]
    lang = None
    if tail.startswith(b'@'):
        lang = tail[1:].decode('ascii', 'replace')
    return 'lit', unescape(body), lang


# --- output -----------------------------------------------------------------

class ShardWriter:
    """Append JSON lines to per-country shard files, keeping at most
    SHARD_MAX_OPEN handles open. Buffers per shard so a country that appears
    in scattered runs across the dump still costs one write per few thousand
    records rather than one per record.
    """

    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        # Shards are opened for append, because a country reappears in
        # scattered runs across the dump. That makes a rerun into a directory
        # that already has output silently double every shard, so clear it.
        for stale in os.listdir(directory):
            if stale.endswith('.jsonl'):
                os.remove(os.path.join(directory, stale))
        self.buffers = {}
        self.handles = {}
        self.counts = {}

    def _handle(self, shard):
        handle = self.handles.get(shard)
        if handle is None:
            if len(self.handles) >= SHARD_MAX_OPEN:
                self.close_handles()
            handle = open(os.path.join(self.dir, '%s.jsonl' % shard), 'a',
                          encoding='utf-8')
            self.handles[shard] = handle
        return handle

    def write(self, shard, line):
        buf = self.buffers.get(shard)
        if buf is None:
            buf = self.buffers[shard] = []
        buf.append(line)
        self.counts[shard] = self.counts.get(shard, 0) + 1
        if len(buf) >= SHARD_FLUSH_LINES:
            self._handle(shard).write('\n'.join(buf) + '\n')
            del self.buffers[shard]

    def close_handles(self):
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()

    def close(self):
        for shard, buf in self.buffers.items():
            if buf:
                self._handle(shard).write('\n'.join(buf) + '\n')
        self.buffers.clear()
        self.close_handles()


# --- the pass ---------------------------------------------------------------

def scan(stream, out_dir, progress_every=20_000_000):
    os.makedirs(out_dir, exist_ok=True)
    shards = ShardWriter(os.path.join(out_dir, 'entities'))

    p131 = array.array('i')
    p279 = array.array('i')

    stats = {
        'lines': 0, 'triples_kept': 0, 'entities_seen': 0, 'entities_kept': 0,
        'p131_edges': 0, 'p279_edges': 0, 'labels': 0, 'alt_labels': 0, 'blank_nodes': 0,
        'bad_lines': 0,
    }

    cur_qid = None          # bytes, compared directly -- no int() per line
    cur = None              # the record being accumulated
    started = time.monotonic()

    def flush():
        if cur is None:
            return
        stats['entities_seen'] += 1
        qid_num = int(cur_qid)

        for parent in cur.get('located_in', ()):
            if parent.startswith('Q'):
                p131.append(qid_num)
                p131.append(int(parent[1:]))
                stats['p131_edges'] += 1
        for parent in cur.get('subclass_of', ()):
            if parent.startswith('Q'):
                p279.append(qid_num)
                p279.append(int(parent[1:]))
                stats['p279_edges'] += 1

        if not any(k in cur for k in KEEP_IF):
            return

        # Labels were buffered raw and are decoded only now, past the keep
        # check, so the great majority of the dump -- entities that are not
        # places and never get written -- costs nothing for its labels. The
        # decision cannot be made earlier: Wikidata emits an entity's labels
        # before its wdt: claims, so when they go past nothing is yet known
        # about the entity.
        raw_labels = cur.pop('_labels', None)
        if raw_labels:
            labels = {}
            for lang, body in raw_labels:
                if lang not in labels:
                    labels[lang] = unescape(body)
                    stats['labels'] += 1
            cur['labels'] = labels
        raw_alts = cur.pop('_alts', None)
        if raw_alts:
            alts = {}
            for lang, body in raw_alts:
                alts.setdefault(lang, []).append(unescape(body))
                stats['alt_labels'] += 1
            cur['alt_labels'] = alts

        cur['id'] = qid_num
        country = cur.get('country')
        shard = country[1:] if country and country.startswith('Q') else '0'
        shards.write(shard, json.dumps(cur, separators=(',', ':'),
                                       ensure_ascii=False, sort_keys=True))
        stats['entities_kept'] += 1

    # The line counter and the next report point are locals, not dict entries,
    # and the report fires on an equality test rather than a modulo. Both are
    # per-line costs paid ~7.7 billion times: counting through stats['lines']
    # and testing `% progress_every` measured 27% slower than this over the
    # whole pass. The check must stay at the top of the body -- most lines
    # leave early through one of the `continue`s below, so a check after them
    # fires only when a multiple happens to survive every filter.
    lines = 0
    next_report = progress_every if progress_every else -1

    for line in stream:
        lines += 1

        if lines == next_report:
            elapsed = time.monotonic() - started
            print('  %6.0fs  %5.2fB lines  %7.2fM entities kept  %6.1fM P131  %5.1fM P279  (%.2fM lines/s)'
                  % (elapsed, lines / 1e9, stats['entities_kept'] / 1e6,
                     stats['p131_edges'] / 1e6, stats['p279_edges'] / 1e6,
                     lines / elapsed / 1e6), flush=True)
            next_report += progress_every

        if not line.startswith(ENTITY_PREFIX):
            continue
        sep = line.find(b'> ', ENTITY_PREFIX_LEN)
        if sep < 0:
            stats['bad_lines'] += 1
            continue
        qid = line[ENTITY_PREFIX_LEN:sep]
        if not qid.isdigit():
            continue

        # Dispatch on the predicate by offset rather than slicing the rest of
        # the line out first: that slice allocated a bytes object for every
        # one of the dump's ~7.7 billion triples, the great majority of which
        # are then discarded a microsecond later.
        pos = sep + 2
        if line.startswith(DIRECT_PREFIX, pos):
            end = line.find(b'> ', pos + DIRECT_PREFIX_LEN)
            if end < 0:
                stats['bad_lines'] += 1
                continue
            field = PROPS.get(line[pos + DIRECT_PREFIX_LEN:end])
            if field is None:
                continue
            obj_at = end + 2
        elif line.startswith(LABEL_PRED, pos):
            field = 'label'
            obj_at = pos + len(LABEL_PRED)
        elif line.startswith(ALT_PRED, pos):
            field = 'alt_label'
            obj_at = pos + len(ALT_PRED)
        else:
            continue

        # Every N-Triples line ends ' .\n'. Trust it, and fall back to the
        # tolerant strip only when it does not hold.
        if line.endswith(b' .\n'):
            obj = line[obj_at:-3]
        else:
            obj = line[obj_at:].rstrip()
            if obj.endswith(b'.'):
                obj = obj[:-1].rstrip()

        if qid != cur_qid:
            flush()
            cur_qid = qid
            cur = {}

        stats['triples_kept'] += 1

        # Labels are buffered undecoded -- see flush().
        if field == 'label':
            body, lang = split_langtag(obj)
            if lang:
                cur.setdefault('_labels', []).append((lang, body))
            continue
        if field == 'alt_label':
            body, lang = split_langtag(obj)
            if lang:
                cur.setdefault('_alts', []).append((lang, body))
            continue
        if field in LANG_KEYED:
            body, lang = split_langtag(obj)
            if lang:
                cur.setdefault(field, {}).setdefault(lang, unescape(body))
            continue

        kind, value, _lang = parse_object(obj)
        if kind is None:
            if obj.startswith(b'_:'):
                stats['blank_nodes'] += 1
                if field not in PRESENCE_ONLY:
                    continue
                value = 'somevalue'
            else:
                stats['bad_lines'] += 1
                continue

        if field in MULTI:
            values = cur.setdefault(field, [])
            if value not in values:
                values.append(value)
        elif field not in cur:
            cur[field] = value

    stats['lines'] = lines
    flush()
    shards.close()

    with open(os.path.join(out_dir, 'graph-p131.i32'), 'wb') as out:
        p131.tofile(out)
    with open(os.path.join(out_dir, 'graph-p279.i32'), 'wb') as out:
        p279.tofile(out)

    stats['elapsed_seconds'] = round(time.monotonic() - started, 1)
    stats['shards'] = len(shards.counts)
    with open(os.path.join(out_dir, 'scan-stats.json'), 'w', encoding='utf-8') as out:
        out.write(json.dumps(stats, indent=4, sort_keys=True))
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--input', help='N-Triples file; default reads stdin')
    parser.add_argument('--out-dir', default='dump-scan')
    parser.add_argument('--progress-every', type=int, default=20_000_000,
                        help='lines between progress reports (0 to silence)')
    args = parser.parse_args()

    if args.input:
        stream = open(args.input, 'rb')
    else:
        stream = sys.stdin.buffer

    try:
        stats = scan(stream, args.out_dir, args.progress_every)
    finally:
        if args.input:
            stream.close()

    print('\nscanned %d lines in %.0fs (%.2fM lines/s)'
          % (stats['lines'], stats['elapsed_seconds'],
             stats['lines'] / max(1e-9, stats['elapsed_seconds']) / 1e6))
    print('entities: %d seen, %d kept (%.1f%%) across %d shards'
          % (stats['entities_seen'], stats['entities_kept'],
             100.0 * stats['entities_kept'] / max(1, stats['entities_seen']),
             stats['shards']))
    print('graph: %d P131 edges, %d P279 edges' % (stats['p131_edges'], stats['p279_edges']))
    print('labels: %d, alt labels: %d, bad lines: %d'
          % (stats['labels'], stats['alt_labels'], stats['bad_lines']))


if __name__ == '__main__':
    main()
