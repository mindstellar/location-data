"""What counts as a settlement, and which classes rule one out.

The whole question is transitive. Wikidata does not say "this is a city"; it
says "this is an instance of a commune of France", and whether that makes it a
city depends on a subclass chain the streaming scan cannot see. So everything
here works from the P279 edge array: close a set of roots downward, then ask
whether an entity's P31 values landed inside it.

The exclusions are the interesting part, and they are three different kinds on
purpose. Collapsing them into one flat "not a city" set has been tried and it
deleted Bern, Rome and every town in China -- see the comments on each.

`load_edges` lives here rather than in a module of its own because the class
closure is its first consumer; contain.py imports it for the P131 array.
"""

import array
import collections
import os

Q_HUMAN_SETTLEMENT = 486972
# Wikidata files municipalities under the administrative branch, not under
# Q486972, so "instance of a settlement" alone misses them entirely: commune
# of France (Q484170), municipality of Germany (Q262166) and municipality
# (Q15284) are all outside the settlement closure. Measured on France, the
# settlement-only root matched 19,405 coordinate-bearing entities -- mostly
# hamlets, abbeys and lieux-dits -- while the real communes the upstream ODbL
# dataset carries were absent. Adding this root takes France to 63,012.
Q_MUNICIPALITY = 15284
SETTLEMENT_ROOTS = (Q_HUMAN_SETTLEMENT, Q_MUNICIPALITY)

Q_ADMIN_TERRITORIAL_ENTITY = 56061
# Items about an ISO code rather than about a place. A few ISO 3166-2 codes
# have their own Wikidata item carrying P300 -- "ISO 3166-2:IN-GA" sits
# alongside the real "Goa" -- and they must never be selected as divisions.
Q_ISO_STANDARD = 15087423
Q_COUNTRY_CODE_DATASET = 17305522

# Classes that reach a settlement root but must not ship as cities. Split in
# two, because the two kinds behave differently.
#
# HARD: categorically not a current city. Nothing rescues these -- a monastery
# is not a town however it is otherwise tagged, and a municipality abolished in
# 1954 is not a place anyone can select today.
# Categorical: describes what a thing *is*. A monastery is not a town however
# else it is tagged. Nothing rescues these.
HARD_EXCLUDE_ROOTS = {
    44613: 'monastery',
    130003: 'ski resort',
    79007: 'street',
    1348006: 'city block',
}

# "Former" is different in kind, and treating it as categorical deleted Rome,
# Florence, Milan, Moscow and Saint Petersburg from the dataset. These classes
# describe what a place *once was*, and a city can be a former national capital
# or a former city-state and still be a city: Moscow is an instance of "capital
# of Russia", Saint Petersburg of "former national capital", Milan and Florence
# of "Italian city-state", Rome of "abolished municipality in Italy" -- all of
# which descend from "former administrative territorial entity".
#
# A place is treated as former only when nothing says it is still current:
# either the entity itself carries a dissolution date or an end time, or every
# settlement class it has is a former one. Genuinely abolished comuni such as
# Gavazzana and Veruno carry the former class and nothing else, and Japan's
# dissolved municipalities overwhelmingly carry the entity-level date -- 16,238
# of 16,708 in the Japanese shard -- so both are still excluded.
# Two roots, because Wikidata splits "no longer here" along two branches. The
# administrative one covers abolished communes and dissolved districts. The
# other covers the place itself ceasing to be a place -- "former settlement",
# and beneath it abandoned village, ghost town, destroyed city, submerged
# settlement, and the whole of archaeology: hillfort (5,393 entities),
# settlement of protohistoric times (5,024), ancient city (3,196), Neolithic
# settlement (2,112), contour fort (1,843).
#
# It descends from settlement and from former entity, but not from former
# *administrative* entity, so the first root missed it entirely and a place
# whose only class was "former settlement" shipped as a current city.
#
# Note what the closure does not catch: "existing village of a former
# municipality in Finland" is a current village and stays out of it. That is
# the hierarchy earning its keep -- a keyword rule over labels would have
# deleted those.
FORMER_ROOTS = (19953632, 22674925)

# SOFT: granularity hints. These mean "finer than a city" on their own, but
# they co-occur with genuine cities often enough that excluding them outright
# is wrong. Verified case: "college town" is a subclass of "academic enclave",
# which is a subclass of "neighborhood" -- so a transitive neighborhood
# exclusion threw away Bern, Basel and every other university city. Positive
# evidence from CORE_CITY_ROOTS overrides them.
SOFT_EXCLUDE_ROOTS = {
    123705: 'neighborhood',
    5327369: 'chocho -- Japanese city district',
}

# Suburbs are deliberately NOT excluded. The word means two different things,
# and the countries where Wikidata actually uses the class are the ones where
# it is an addressing unit rather than a subdivision: of the 9,610 entities the
# class covers, Australia has 4,350, the UK 2,970, New Zealand 853 and South
# Africa 278, under classes like "gazetted locality of Victoria" and
# "suburb/locality of Tasmania". An Australian address names its suburb, so
# dropping them would leave a picker unable to express most Australian
# locations. Neighbourhoods -- 90,362 entities, informal and overlapping, with
# no postal identity -- are a different thing and stay excluded.

# Positive evidence that overrides a soft exclusion. Two kinds.
#
# A place that is a city, town, village or municipality in its own right. Not
# the settlement root itself: nearly everything reaches that, so it would
# rescue every neighbourhood too.
CORE_CITY_ROOTS = {
    515: 'city', 3957: 'town', 532: 'village', 15284: 'municipality',
}

# ...or an official administrative division, at any size. Administrative
# status, not size, is what separates a selectable location from an informal
# area: a Philippine barangay and a Vietnamese ward are the units their
# addresses are written in, and both are filed under "neighborhood" in
# Wikidata. Excluding them cost the Philippines 2,084 places and Vietnam
# 2,164, which the per-country validation gate caught. Informal areas --
# "neighborhood" and "residential area" themselves -- are not administrative
# entities and stay excluded; only 282 classes of the soft closure are.
ADMIN_RESCUE_ROOT = Q_ADMIN_TERRITORIAL_ENTITY

# Edges that are miscategorised upstream and drag a whole branch somewhere it
# does not belong. Included in a closure but never traversed through.
#
#   Q15623456  administrative territorial entity of a defunct state -- China's
#              current divisions hang off it, so an unguarded "former" closure
#              marks all 21,494 towns of China as former.
#   Q97095925  military area -- filed as a subclass of administrative
#              territorial entity, which makes military installation, military
#              base, fortification, barracks and gates into administrative
#              divisions. Gibraltar's tier-2 selection took every P131 child
#              that qualified and shipped 117 gun batteries, gates and bastions
#              as its regions.
CLOSURE_BLOCKED = frozenset({15623456, 97095925})

MAX_PROPAGATION_ROUNDS = 40


# --- graphs -----------------------------------------------------------------

def load_edges(path):
    """child,parent int32 pairs -> array('i'). Raises if the file is not a
    whole number of pairs, which would silently shift every edge."""
    size = os.path.getsize(path)
    if size % 8:
        raise ValueError('%s is %d bytes, not a whole number of int32 pairs' % (path, size))
    edges = array.array('i')
    with open(path, 'rb') as handle:
        edges.fromfile(handle, size // 4)
    return edges


def subclass_closure(p279, roots, blocked=frozenset()):
    """Every class that reaches one of `roots` through P279*, including the
    roots. Rescans the flat array until a pass adds nothing: the number of
    passes is the depth of the class tree, not its size.

    `blocked` classes are included but never traversed *through*, which is how
    a single miscategorised edge is cut without discarding the rest of the
    closure -- see CLOSURE_BLOCKED.
    """
    members = set(roots)
    for _round in range(MAX_PROPAGATION_ROUNDS):
        added = 0
        for i in range(0, len(p279), 2):
            child = p279[i]
            parent = p279[i + 1]
            if child not in members and parent in members and parent not in blocked:
                members.add(child)
                added += 1
        if not added:
            return members
    raise RuntimeError('P279 closure did not converge in %d rounds' % MAX_PROPAGATION_ROUNDS)


# --- the settlement test ----------------------------------------------------

# The four class sets is_settlement judges against, named rather than
# positional. They were a bare 4-tuple threaded through five functions, where
# swapping two entries would not fail anything -- it would quietly invert which
# places ship, which is the hardest kind of change to notice.
#
# is_settlement still unpacks it positionally, because it runs once per entity
# across ~20M of them and attribute lookups there are not free. Field order is
# therefore load-bearing, and pinned by a test rather than by hope.
Exclusions = collections.namedtuple('Exclusions', 'hard former soft rescue')


def exclusion_sets(p279):
    """Close every exclusion root over the subclass graph, in one place.

    Built by keyword so the construction site cannot get the order wrong, which
    is the half of the hazard a namedtuple actually removes.
    """
    return Exclusions(
        hard=subclass_closure(p279, list(HARD_EXCLUDE_ROOTS), CLOSURE_BLOCKED),
        former=subclass_closure(p279, list(FORMER_ROOTS), CLOSURE_BLOCKED),
        soft=subclass_closure(p279, list(SOFT_EXCLUDE_ROOTS), CLOSURE_BLOCKED),
        rescue=(subclass_closure(p279, list(CORE_CITY_ROOTS), CLOSURE_BLOCKED)
                | subclass_closure(p279, [ADMIN_RESCUE_ROOT], CLOSURE_BLOCKED)),
    )


def is_excluded_division(record, settlement_classes):
    """Dissolved, ended, or an item about an ISO code rather than a place."""
    if record.get('dissolved') or record.get('end_time'):
        return True
    for cls in record.get('instance_of', ()):
        if cls in ('Q%d' % Q_ISO_STANDARD, 'Q%d' % Q_COUNTRY_CODE_DATASET):
            return True
    return False


def is_settlement(record, settlement_classes, exclusions=None):
    """Whether this entity ships as a city.

    Three kinds of exclusion, and they behave differently on purpose:

      * categorical -- wins over everything, because it describes what the
        thing is rather than what it was or how big it is
      * former -- applies only when nothing contradicts it, i.e. the entity
        carries a dissolution date, or every settlement class it has is a
        former one
      * soft -- a granularity hint, lost to positive city or administrative
        evidence
    """
    if not exclusions:
        return any(cls.startswith('Q') and int(cls[1:]) in settlement_classes
                   for cls in record.get('instance_of', ()))
    hard, former, soft, rescue = exclusions

    hit = is_soft = is_core = False
    settlement_hits = []
    for cls in record.get('instance_of', ()):
        if not cls.startswith('Q'):
            continue
        qid = int(cls[1:])
        if qid in hard:
            return False
        if qid in soft:
            is_soft = True
        if qid in rescue:
            is_core = True
        if qid in settlement_classes:
            hit = True
            settlement_hits.append(qid)

    if not hit:
        return False
    if record.get('dissolved') or record.get('end_time'):
        return False
    if settlement_hits and all(q in former for q in settlement_hits):
        return False
    if is_soft and not is_core:
        return False
    return True
