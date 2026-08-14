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

    for lang, text in candidates:
        if _usable(text) and not _QUALIFIED.search(text):
            return text, lang, None
    for lang, text in candidates:
        if _usable(text):
            return text, lang, None

    # No Latin-script label anywhere. Transliterate, in the same preference
    # order, and report what it was transliterated from so the caller can keep
    # the original script in the record rather than losing it.
    #
    # Only from the local language, though. A label in an unrelated language is
    # frequently itself a transliteration of a Latin original, and reversing
    # one corrupts the name: Wikidata carries bot-written Chechen and Serbian
    # Cyrillic labels for Mexican places, and romanising those produced
    # "Avikola la Morena (Ermosiyo)" for Avicola la Morena (Hermosillo), and
    # "Benito Khuarez" for Benito Juarez, across 5,623 rows. The local-language
    # label is the only one that is the name rather than a rendering of it.
    base = native_lang.split('-')[0] if native_lang else None
    for lang, text in candidates:
        if lang != 'mul' and (base is None or lang.split('-')[0] != base):
            continue
        romanised = romanise(text, lang)
        if romanised and _usable(romanised):
            return romanised, lang, text

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
