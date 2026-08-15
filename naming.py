"""Choosing a name for an entity that has up to several hundred of them.

The rule is a preference order with a quality filter over it, and both halves
were bought with defects. The order exists because "require an English label"
silently deletes places that have one in every language but English; the
quality filter exists because relaxing that then reaches bot-generated labels
that are descriptions rather than names.

A name has to survive `slugify()`, not merely accent folding. The slug is the
identity a consumer matches on, so a label that folds to something non-empty
but slugs to nothing is not a name at all.
"""

import re
import unicodedata

from anyascii import anyascii
from pypinyin import Style, lazy_pinyin

from contracts import remove_accents, slugify

# --- romanisation -----------------------------------------------------------
#
# 871,614 settlements -- 31% of everything classified and contained -- used to
# be thrown away here, because a name has to survive slugify() to have an
# identity and a name in Chinese characters does not. China alone lost 632,342
# of its 698,299 eligible places.
#
# Transliterating recovers the names of 785,021 of them. Far fewer actually
# ship, because most then fail the next test instead: Wikidata has China's
# villages and their containment but not their coordinates, so the loss moves
# from "no name" to "no position" rather than disappearing. That is worth
# knowing before reading the gain below as small.
#
# What this does *not* do is transliterate everything it can:

# An allowlist, not a blocklist, because a name is published identity and the
# failure mode is silent: a wrong name still slugs, still ships, and nobody
# notices. Each of these was checked against real output before being added.
#
# What they have in common is that they are alphabets -- one letter, one sound,
# vowels written. Transliterating them is a mapping. Two other families are
# not, and both were tried and rejected:
#
#   abjads       omit short vowels, so a lookup returns a consonant skeleton.
#                The Arabic for Casablanca comes out "ldr lbyd'" and for Sanaa
#                "sn`'". 60k rows.
#   abugidas     carry inherent vowels that the tables drop unevenly. Bengali
#                and Devanagari are near-misses -- "Lksmipur" for Lakshmipur --
#                and Burmese is not close: "Kyiunlpmriu" for a town whose name
#                is Kyainglat. ~1,000 rows.
#
# CJK outside Chinese fails differently: the tables reach for Mandarin readings
# of kanji and hanja and return "MangSangHaeSuYogJang" for a Korean beach.
# Chinese itself is handled by pypinyin below, which actually knows the
# language. All of these wait for a source with real romanisations.
_ROMANISABLE = frozenset(('CYRILLIC', 'GREEK', 'GEORGIAN', 'ARMENIAN'))

# Han characters carry different readings in Chinese, Japanese and Korean, and
# pypinyin only knows the Chinese ones -- it would give a Japanese town a
# Mandarin name. Applied only where the label says it is Chinese.
_CHINESE = ('zh', 'cmn', 'wuu', 'yue', 'nan', 'hak', 'gan', 'lzh')


def _script_of(text):
    """The Unicode script of the first letter, which is enough: a label mixing
    scripts is vanishingly rare and the first letter decides how to read it."""
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            return unicodedata.name(ch).split()[0]
        except ValueError:
            return None
    return None


def romanise(text, lang):
    """A Latin-script rendering of `text`, or None where one cannot honestly be
    produced.

    Deterministic, and that is load-bearing: the result becomes a name and a
    slug, so pypinyin and anyascii are pinned to exact versions in
    requirements.txt. Upgrading either one rewrites names.
    """
    if not text:
        return None
    script = _script_of(text)
    if script is None or script == 'LATIN':
        return None
    if script == 'CJK':
        if not (lang and lang.split('-')[0] in _CHINESE):
            return None
        # Syllables joined rather than spaced, which is how a Chinese toponym
        # is written in pinyin: Beijing, not Bei Jing.
        out = ''.join(lazy_pinyin(text, style=Style.NORMAL))
        # lazy_pinyin passes through anything it has no reading for, so a label
        # that is still Han afterwards did not convert.
        return out.capitalize() if out and _script_of(out) == 'LATIN' else None
    if script not in _ROMANISABLE:
        return None
    out = anyascii(text)
    if not out:
        return None
    # Abugidas and most Indic scripts have no case, so anyascii returns
    # "ludhiana" where Cyrillic returns "Kholstovo". Capitalise only when
    # nothing in the result is already uppercase, so a name that carries its
    # own capitalisation -- "Nizhniy Novgorod" -- is left alone.
    return out if any(c.isupper() for c in out) else out.capitalize()

# A label that is really a description rather than a name. Wikidata carries
# bot-generated labels in some languages that pack a gloss and coordinates
# into the label field -- "Caicun (kapital sa baranggay sa Republikang Popular
# sa Tsina, Anhui Sheng, lat 30,72, long 118,55)" is one entity's Cebuano
# label. A parenthesised qualifier is the reliable marker.
_QUALIFIED = re.compile(r'\(')


# How much of a label has to survive folding for it to be a Latin-script name
# rather than a non-Latin one the fold left a residue of.
_MIN_LETTERS_KEPT = 0.6

# Past this, a label is a description of a place and not its name. The same
# judgement the parenthesis rule above makes, for the cases that carry no
# parenthesis:
#
#   Wohnsiedlung Gontardweg 52; 53; 54; 55; 56; 57; 58; ...        (249 chars)
#   Conjunto formado pela casa, capela, jardins e portais da ...    (97)
#   Uklad urbanistyczny z sylwetka miasta od pld. i wsch., ...     (240)
#
# Names are short: the median is 11 characters and the 99th percentile is 49,
# so this cannot reach one. The longest thing it removes that is arguably a
# name is a 249-character Honduran label consisting of a village name followed
# by a message to the author's friends.
_MAX_NAME = 100

# A label that is a postal address rather than a name. Wikidata's English
# labels for Russian villages are routinely the whole containment chain:
#
#   Pavlovskaya, Vozhegodsky Selsoviet, Vozhegodsky District, Vologda Oblast
#   Novoye, Sosnovskoye Rural Settlement, Vologodsky District, Vologda Oblast
#
# The name is the head; everything after it is where it is, which the row
# already records in admin1_id and admin2_id. Structurally this is the
# parenthesised gloss above with commas instead of brackets, and it reaches
# 2,715 rows.
#
# Two commas at least, and an administrative word after the first, so a real
# name keeps its comma: "Washington, D.C." has one comma and no such word, and
# a Russian village genuinely called "Frunze, 2" is untouched for both reasons.
_ADDRESS_TAIL = re.compile(
    r'\b(district|oblast|krai|okrug|raion|rayon|selsoviet|sel.soviet|'
    r'rural settlement|urban settlement|municipality|county|province|region|'
    r'prefecture|voivodeship|governorate|department|commune|canton|'
    # "state" was missing from this list, which is why Nigerian and Indian
    # addresses kept their whole chain: "Abiriba Post Office, Abiriba, Ohafia,
    # Abia State".
    r'state|emirate|subdistrict|sub-district|taluk|tehsil|mandal|upazila)\b',
    re.IGNORECASE)


def _strip_address(text):
    """The head of a comma-separated containment chain, or the text unchanged."""
    if text.count(',') < 2:
        return text
    head, _, tail = text.partition(',')
    if not _ADDRESS_TAIL.search(tail):
        return text
    head = head.strip()
    return head if head and _usable(head) else text


def strip_qualifier(text):
    """(bare name, what was in the brackets), or (text, None).

    A trailing bracket on a label is Wikidata disambiguating its own item:
    "Dushi (Baghlan Province)", "Encamp (Andorra)", "Floq (Klos)". This
    pipeline disambiguates too, by a different rule and in a different format,
    and two systems produced the collision each was meant to prevent -- this
    built "Floq (Klos)" while upstream shipped "Floq, Klos", the two slugged
    alike, and they met only in the final sweep. So the bracket comes off 17,345
    labels and resolve_collisions puts back whatever is actually needed, in one
    format. 2,077 pairs turn out to be the same place twice within 2 km, which
    the differing brackets had been hiding from both systems.

    What was inside is returned so the caller can put it back on a row that
    nothing else can tell apart, rather than dropping it. That saved 768 rows.

    Square brackets are the same thing and were missed at first: Mexico's
    statistical office tags its rows "[Nuevo Centro de Poblacion]" and
    "[Sociedad Productora Rural]", and German labels disambiguate with
    "Baumgarten [Sonnenberg]". 4,507 rows, most of them short enough that no
    length check would ever have found them.

    The opening bracket is found by counting depth from the end rather than by
    matching a flat "(...)$", because the contents can contain brackets of
    their own -- "Eichholz (Werder (Havel))", where Werder (Havel) is itself a
    place name, and "Leonidovka (Korneev auyldyq okrugi (Soltustik Qazaqstan
    oblysy))". A flat pattern silently leaves those whole; 93 rows shipped that
    way.
    """
    for opening, closing in (('(', ')'), ('[', ']')):
        if not text.endswith(closing):
            continue
        depth = 0
        for index in range(len(text) - 1, -1, -1):
            if text[index] == closing:
                depth += 1
            elif text[index] == opening:
                depth -= 1
                if depth == 0:
                    break
        else:
            continue
        if depth:
            continue
        head, inner = text[:index].strip(), text[index + 1:-1].strip()
        if head and inner and _usable(head):
            return head, inner
    return text, None


# A Russian administrative formation described rather than named:
#
#   Gorodskoe poselenie <<Gorod Zavitinsk>>          urban settlement "town of Zavitinsk"
#   Munitsipal'noe obrazovanie <<Rabochiy poselok (pgt) Arkhara>>
#
# The name is inside the guillemets and the wrapper is a description of what
# kind of administrative unit it is. 1,830 rows.
_GUILLEMETS = re.compile(r'^.*?<<\s*(.+?)\s*>>\s*$')

# What is left inside is still often prefixed by the kind of place it is, in
# Russian: "Gorod X" (town), "Selo X" (village), "Rabochiy poselok X".
_RUSSIAN_KIND = re.compile(
    r'^(gorod|selo|derevnya|posyolok|poselok|rabochiy poselok|'
    r'pgt|stanitsa|khutor|aul|slobod[ak])\s+', re.IGNORECASE)


def strip_description(text):
    """The name inside a description of it, or the text unchanged."""
    match = _GUILLEMETS.match(text)
    if not match:
        return text
    inner = match.group(1).strip()
    # "(pgt)" and the like sit between the kind and the name.
    inner = re.sub(r'\s*\([^()]*\)\s*', ' ', inner).strip()
    inner = _RUSSIAN_KIND.sub('', inner).strip()
    return inner if inner and _usable(inner) else text


def _usable(text):
    """A label that can be a name at all: Latin script, containing a letter,
    and surviving slugification, because the slug is the identity.

    Length is deliberately not a criterion. "Au" is a village in Austria and
    "Y" is a commune in France, so a minimum length would delete real places;
    what actually marks junk is having no letters, as in "--".

    The proportion is a criterion, and that is the part that was wrong.
    Surviving the fold was read as "already Latin", which holds for Cyrillic --
    "Киров" folds to nothing -- right up until a label uses a Cyrillic letter
    that carries a diacritic. Chuvash "ă" decomposes to "a" plus a combining
    breve, so "Уракăва" folded to "a": non-empty, so the label was taken for a
    Latin name, romanisation never ran, and 659 Russian settlements shipped
    called "a", "e" or "aae" -- their diacritics and nothing else. Requiring
    most of the letters to survive rejects the label instead, and the
    romanisation path then reaches the Russian label and returns "Urakovo".
    """
    if not text:
        return False
    if len(text) > _MAX_NAME:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    kept = [c for c in remove_accents(text) if c.isalpha()]
    if len(kept) < len(letters) * _MIN_LETTERS_KEPT:
        return False
    return bool(slugify(text))


def resolve_name(record, native_lang):
    """(name, language) -- the common case, for callers that do not care
    whether the name was transliterated."""
    name, lang, _source = resolve_name_full(record, native_lang)
    return name, lang


def resolve_name_full(record, native_lang):
    """en -> mul -> the country's official language -> any label at all,
    lowest language code first so an entity labelled only in, say, es and pt
    resolves the same way on every run.

    A plain "require an English label" rule silently drops entities whose only
    label is in another language, and Wikidata increasingly tags names 'mul'
    rather than duplicating them as 'en'.

    Two passes over that order. The first accepts only a clean label -- one
    that contains a letter, slugs to something, and carries no parenthesised
    qualifier. The second relaxes the qualifier rule, so an entity whose every
    label is qualified still gets a name. Preferring a clean label matters
    because the fallback tiers reach bot-generated labels: a Russian village
    labelled in ru, de and nl resolved to a Crimean Tatar label reading
    "Afonino (Solton rayon)" when a plain "Afonino" was available.

    Labels are tested with slugify(), not accent folding: a Cyrillic name with
    a parenthesised qualifier folds to " ( )", which is non-empty but slugs to
    nothing, and an empty slug is an empty identity.

    Returns (name, language, romanised_from). The third is the original-script
    label when the name had to be transliterated, and None when a real Latin
    label was found -- which is what lets the caller keep the native form.
    """
    labels = record.get('labels') or {}
    if not labels:
        return None, None, None
    order = ['en', 'mul']
    if native_lang:
        order.append(native_lang)
    order.extend(sorted(labels))

    seen = set()
    candidates = []
    for lang in order:
        if lang in seen:
            continue
        seen.add(lang)
        text = labels.get(lang)
        if text:
            candidates.append((lang, text))

    # Romanising the local label is *not* the last resort. It used to be, and
    # that let any language's Latin label win over the country's own name:
    # Bulgarian villages shipped as "Arda (obwod Chaskowo)" from a Polish
    # label, Belarusian ones as "Dabucyno valscius" from Lithuanian and
    # "Lyeninski Rayon" from Cebuano, and Chuvash ones as "Oerakovo" from
    # French where the Russian label gives "Urakovo". 12,204 rows took a name
    # from a language with no connection to the place.
    #
    # English and mul still come first, because a real English exonym is a
    # better name than a transliteration. Everything else comes after.
    #
    # Romanisation stays restricted to the local language. A label in an
    # unrelated language is frequently itself a transliteration of a Latin
    # original, and reversing one corrupts the name: Wikidata carries
    # bot-written Chechen and Serbian Cyrillic labels for Mexican places, and
    # romanising those produced "Avikola la Morena (Ermosiyo)" for Avicola la
    # Morena (Hermosillo) across 5,623 rows.
    base = native_lang.split('-')[0] if native_lang else None
    local = [(lang, text) for lang, text in candidates
             if lang == 'mul' or (base is not None and lang.split('-')[0] == base)]
    preferred = [(lang, text) for lang, text in candidates
                 if lang in ('en', 'mul')
                 or (base is not None and lang.split('-')[0] == base)]

    def _romanised(pairs, clean):
        for lang, text in pairs:
            out = romanise(text, lang)
            if out and _usable(out) and not (clean and _QUALIFIED.search(out)):
                return out, lang, text
        return None

    def _plain(pairs, clean):
        for lang, text in pairs:
            if _usable(text) and not (clean and _QUALIFIED.search(text)):
                return text, lang, None
        return None

    # Clean labels first, in three bands; then the same three with a
    # parenthesised qualifier allowed, for an entity whose every label is
    # qualified.
    for clean in (True, False):
        for found in (_plain(preferred, clean),
                      _romanised(local, clean),
                      _plain(candidates, clean)):
            if found:
                name, lang, source = found
                return _strip_address(strip_description(name)), lang, source

    # Nothing usable and nothing that can honestly be romanised -- an Arabic
    # label, or a label that is punctuation. Returning the first candidate
    # anyway would ship a row named "-" with "-" as its slug, which is not a
    # name and not an identity; the caller counts this as unnamed and drops it.
    return None, None, None


def alt_names_for(record, native_lang):
    alts = record.get('alt_labels') or {}
    out = {}
    if alts.get('en'):
        out['en'] = sorted(alts['en'])
    if native_lang and native_lang != 'en' and alts.get(native_lang):
        out[native_lang] = sorted(alts[native_lang])
    return out
