"""Unit tests for the decisions the build actually gets wrong.

Every serious defect this pipeline has had lived in a handful of small pure
functions -- is this a settlement, which region contains it, what is it called
-- and each was found by building the whole world and looking, which takes
about six minutes and finds only what someone thought to check.

So these are not coverage for its own sake. Each case is a specific thing that
shipped broken, written so that undoing the fix fails here in under a second
instead of surviving to the next full build.

    python -m unittest discover -s tests -v
"""

import array
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dump_build import (  # noqa: E402
    DEPTH_SCALE,
    _usable,
    is_settlement,
    propagate_containment,
    resolve_name,
    subclass_closure,
)
from classify import Exclusions, is_not_a_place  # noqa: E402
from contain import (  # noqa: E402
    is_country_item,
    select_admin1s,
    select_admin1s_under_country,
    select_admin1s_with_dissolved,
)
from contracts import coord, slugify  # noqa: E402
from countryblock import extra_fields  # noqa: E402
from emit import resolve_collisions  # noqa: E402
from naming import resolve_name_full, romanise  # noqa: E402
from validate import (  # noqa: E402
    BASELINE_DEFAULT,
    capital_presence,
    resolve_baseline,
)


def edges(*pairs):
    """(child, parent) pairs -> the flat int32 array the graph code reads."""
    flat = array.array('i')
    for child, parent in pairs:
        flat.append(child)
        flat.append(parent)
    return flat


def entity(**fields):
    """A scan record. instance_of is given as bare ints for readability."""
    if 'instance_of' in fields:
        fields['instance_of'] = ['Q%d' % q for q in fields['instance_of']]
    return fields


class SubclassClosure(unittest.TestCase):

    def test_includes_the_root_and_everything_under_it(self):
        closure = subclass_closure(edges((2, 1), (3, 2), (4, 3)), [1])
        self.assertEqual(closure, {1, 2, 3, 4})

    def test_unrelated_branches_stay_out(self):
        closure = subclass_closure(edges((2, 1), (9, 8)), [1])
        self.assertEqual(closure, {1, 2})

    def test_a_blocked_class_is_included_but_not_traversed_through(self):
        """The China case. Wikidata files 'administrative territorial entity of
        China' beneath 'administrative territorial entity of a defunct state',
        so an unguarded closure over 'former' swallows every current Chinese
        division -- all 21,494 towns of China. Blocking the one bad edge has to
        keep the blocked class itself while cutting everything below it."""
        # 1 = former, 2 = defunct-state bridge, 3 = admin entity of China,
        # 4 = town of China, 5 = a genuinely former thing
        graph = edges((2, 1), (3, 2), (4, 3), (5, 1))
        unguarded = subclass_closure(graph, [1])
        self.assertIn(4, unguarded, 'without the block the artifact is present')

        guarded = subclass_closure(graph, [1], blocked=frozenset({2}))
        self.assertIn(2, guarded, 'the blocked class is still a member')
        self.assertNotIn(3, guarded)
        self.assertNotIn(4, guarded, 'town of China must survive')
        self.assertIn(5, guarded, 'the rest of the closure is untouched')


class IsSettlement(unittest.TestCase):

    # (hard, former, soft, rescue)
    SETTLEMENT = {10, 11, 12, 13, 14}
    HARD = {90}            # monastery
    FORMER = {11, 12}      # 11 = abolished municipality, 12 = former capital
    SOFT = {13}            # neighborhood-descended
    RESCUE = {10, 14}      # city / administrative division

    def decide(self, record):
        return is_settlement(record, self.SETTLEMENT,
                             (self.HARD, self.FORMER, self.SOFT, self.RESCUE))

    def test_the_exclusion_fields_stay_in_this_order(self):
        # is_settlement unpacks the exclusions positionally, because it runs
        # once per entity across ~20M of them. Reordering the namedtuple would
        # therefore swap two sets silently -- categorical for former, say --
        # and the only symptom would be different places shipping.
        self.assertEqual(('hard', 'former', 'soft', 'rescue'), Exclusions._fields)

    def test_a_plain_settlement_is_kept(self):
        self.assertTrue(self.decide(entity(instance_of=[10])))

    def test_something_with_no_settlement_class_is_not_a_settlement(self):
        self.assertFalse(self.decide(entity(instance_of=[99])))

    def test_categorical_exclusion_beats_every_other_signal(self):
        """A monastery is not a town however else it is tagged."""
        self.assertFalse(self.decide(entity(instance_of=[10, 14, 90])))

    def test_rome_is_not_deleted_for_having_been_something_else(self):
        """Rome is an instance of 'abolished municipality in Italy' and also of
        'comune of Italy'. Treating former as categorical removed Rome,
        Florence, Milan, Moscow and Saint Petersburg from the dataset."""
        self.assertTrue(self.decide(entity(instance_of=[11, 10])))

    def test_something_whose_only_class_is_former_is_dropped(self):
        """Gavazzana and Veruno are genuinely abolished comuni: they carry the
        former class and nothing else."""
        self.assertFalse(self.decide(entity(instance_of=[11])))

    def test_an_entity_level_dissolution_date_wins_over_a_current_class(self):
        """Japan's dissolved municipalities are subclasses of municipality, so
        class evidence alone would rescue all 12,370 of them. 16,238 of 16,708
        carry the date instead."""
        self.assertFalse(self.decide(entity(instance_of=[10], dissolved='1954-01-01')))
        self.assertFalse(self.decide(entity(instance_of=[10], end_time='1954-01-01')))

    def test_bern_survives_being_a_college_town(self):
        """'college town' is a subclass of 'academic enclave', which is a
        subclass of 'neighborhood'. A transitive soft exclusion deleted Bern,
        Basel and every other university city."""
        self.assertTrue(self.decide(entity(instance_of=[13, 10])))

    def test_a_plain_neighbourhood_is_still_dropped(self):
        self.assertFalse(self.decide(entity(instance_of=[13])))

    def test_a_barangay_survives_on_administrative_status(self):
        """A Philippine barangay and a Vietnamese ward are filed under
        'neighborhood' and are the unit their addresses are written in.
        Excluding them cost the Philippines 2,084 places."""
        self.assertTrue(self.decide(entity(instance_of=[13, 14])))


class Containment(unittest.TestCase):

    def test_the_nearest_container_wins_not_the_lowest_qid(self):
        """Puerto Rico. 'US-PR' is a valid ISO 3166-2 code, so a Puerto Rican
        settlement reaches both its own municipality (one hop) and
        Puerto-Rico-as-a-US-state (two hops). Breaking ties on QID handed every
        one of them to the United States and left PR with 99 regions and no
        cities."""
        settlement, municipality, state = 900, 5000, 99
        graph = edges((settlement, municipality), (municipality, state))
        assign, _ = propagate_containment(graph, {municipality: municipality,
                                                  state: state})
        self.assertEqual(assign[settlement], municipality,
                         'the nearer municipality wins despite the higher QID')

    def test_equal_depth_breaks_on_the_lowest_qid_for_determinism(self):
        settlement = 900
        graph = edges((settlement, 700), (settlement, 300))
        assign, _ = propagate_containment(graph, {700: 700, 300: 300})
        self.assertEqual(assign[settlement], 300)

    def test_a_coarse_seed_loses_to_a_leaf_at_the_same_distance(self):
        """The coarse fallback exists so a capital pointing at the top-level
        region attaches to something -- but it must never outrank a real leaf
        division that is equally reachable."""
        settlement, leaf, coarse = 900, 800, 100
        graph = edges((settlement, leaf), (settlement, coarse))
        assign, _ = propagate_containment(
            graph, {leaf: leaf, coarse: coarse + DEPTH_SCALE})
        self.assertEqual(assign[settlement], leaf)

    def test_a_coarse_seed_still_catches_what_would_otherwise_orphan(self):
        """Madrid points at the Community of Madrid while the selected leaf is
        Madrid province, which sits beside the city rather than above it."""
        settlement, coarse = 900, 100
        graph = edges((settlement, coarse))
        assign, _ = propagate_containment(graph, {coarse: coarse + DEPTH_SCALE})
        self.assertEqual(assign[settlement], coarse)

    def test_an_unreachable_settlement_is_left_unassigned(self):
        assign, _ = propagate_containment(edges((900, 800)), {123: 123})
        self.assertNotIn(900, assign)


class Usable(unittest.TestCase):

    def test_a_short_real_name_is_usable(self):
        """'Au' is a village in Austria and 'Y' is a commune in France, so a
        minimum length would delete real places."""
        self.assertTrue(_usable('Au'))
        self.assertTrue(_usable('Y'))

    def test_something_with_no_letters_is_not_a_name(self):
        for junk in ('--', '-', '  ', '1', "-'"):
            self.assertFalse(_usable(junk), junk)

    def test_a_label_that_slugs_to_nothing_is_not_usable(self):
        """A Cyrillic name with a parenthesised qualifier folds to ' ( )',
        which is non-empty but slugs to nothing, and an empty slug is an empty
        identity."""
        self.assertFalse(_usable('Іванівка (Полтавська область)'))


class ResolveName(unittest.TestCase):

    def test_english_is_preferred(self):
        name, lang = resolve_name({'labels': {'en': 'Munich', 'de': 'Munchen'}}, 'de')
        self.assertEqual((name, lang), ('Munich', 'en'))

    def test_mul_comes_next(self):
        name, lang = resolve_name({'labels': {'mul': 'Aarau', 'de': 'Aarau DE'}}, 'de')
        self.assertEqual((name, lang), ('Aarau', 'mul'))

    def test_then_the_official_language(self):
        name, lang = resolve_name({'labels': {'de': 'Zurich', 'ca': 'Zuric'}}, 'de')
        self.assertEqual((name, lang), ('Zurich', 'de'))

    def test_a_clean_label_beats_a_qualified_one_further_up_the_order(self):
        """A Russian village labelled in several languages resolved to a bot
        label reading 'Afonino (Solton rayon)' when a plain 'Afonino' was
        sitting right there."""
        record = {'labels': {'ce': 'Afonino (Solton rayon)', 'de': 'Afonino'}}
        name, lang = resolve_name(record, None)
        self.assertEqual((name, lang), ('Afonino', 'de'))

    def test_a_qualified_label_is_still_used_when_it_is_all_there_is(self):
        record = {'labels': {'ce': 'Afonino (Solton rayon)'}}
        name, _lang = resolve_name(record, None)
        self.assertEqual(name, 'Afonino (Solton rayon)')

    def test_an_unusable_label_means_unnamed_rather_than_named_dash(self):
        """Shipping a row named '-' gives it the slug '-', which is not an
        identity. 6,749 rows looked like this."""
        self.assertEqual(resolve_name({'labels': {'ru': '-'}}, None), (None, None))

    def test_no_labels_at_all(self):
        self.assertEqual(resolve_name({}, None), (None, None))

    def test_a_latin_label_is_found_when_the_official_language_is_not_latin(self):
        """Preferring the official language unconditionally picked the Cyrillic
        label, which folded away to nothing, while a usable Latin-script label
        went unused."""
        record = {'labels': {'ru': 'Ивановка', 'de': 'Iwanowka'}}
        name, lang = resolve_name(record, 'ru')
        self.assertEqual((name, lang), ('Iwanowka', 'de'))


class CapitalPresence(unittest.TestCase):
    """The gate that found what the count-based checks structurally cannot: a
    country can hold thousands of villages and be missing its own capital while
    every total stays healthy."""

    def build(self, tmp, code, capital, cities):
        os.makedirs(os.path.join(tmp, 'data'), exist_ok=True)
        os.makedirs(os.path.join(tmp, 'json'), exist_ok=True)
        with open(os.path.join(tmp, 'data', '%s.ndjson' % code), 'w', encoding='utf-8') as out:
            out.write(json.dumps({'type': 'country', 'code': code,
                                  'capital_name': capital}) + '\n')
        published = '%s.json' % code
        with open(os.path.join(tmp, 'json', published), 'w', encoding='utf-8') as out:
            json.dump({'regions': [{'settlements': [
                {'slug': slug} for slug in cities]}]}, out)
        return {'code': code, 'files': {'json': 'json/' + published}}

    def test_a_present_capital_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self.build(tmp, 'FR', 'Paris', ['paris', 'lyon'])
            present, missing = capital_presence(
                {'countries': [entry]}, os.path.join(tmp, 'data'), os.path.join(tmp, 'json'))
            self.assertEqual((present, missing), (1, []))

    def test_a_missing_capital_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self.build(tmp, 'ES', 'Madrid', ['madridejos', 'barcelona'])
            present, missing = capital_presence(
                {'countries': [entry]}, os.path.join(tmp, 'data'), os.path.join(tmp, 'json'))
            self.assertEqual(present, 0)
            self.assertEqual(missing, [('ES', 'Madrid')])

    def test_it_measures_the_published_file_not_the_canonical_one(self):
        """Presence is checked against what a consumer receives, not against
        the canonical stream, which carries only the country-level block."""
        with tempfile.TemporaryDirectory() as tmp:
            entry = self.build(tmp, 'VA', 'Vatican City', ['vatican-city'])
            present, missing = capital_presence(
                {'countries': [entry]}, os.path.join(tmp, 'data'), os.path.join(tmp, 'json'))
            self.assertEqual((present, missing), (1, []))

    def test_a_country_declaring_no_capital_is_not_counted_either_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self.build(tmp, 'AQ', None, [])
            present, missing = capital_presence(
                {'countries': [entry]}, os.path.join(tmp, 'data'), os.path.join(tmp, 'json'))
            self.assertEqual((present, missing), (0, []))


if __name__ == '__main__':
    unittest.main(verbosity=2)


class Romanise(unittest.TestCase):
    """Transliteration recovers 871,614 settlements that were classified,
    contained and coordinate-bearing but had no name that survives slugify().
    What it refuses to do matters as much as what it does."""

    def test_chinese_uses_pinyin_with_syllables_joined(self):
        # A Chinese toponym is written Beijing, not Bei Jing.
        self.assertEqual('Xiacunzhen', romanise('下村镇', 'zh'))

    def test_pinyin_reads_the_right_character_in_context(self):
        # The first character of Chongqing is read chong here and zhong
        # elsewhere. A per-character table gets this wrong; pypinyin's phrase
        # dictionary gets it right, which is why it is worth the dependency.
        self.assertEqual('Chongqing', romanise('重庆', 'zh'))

    def test_han_is_refused_when_the_label_is_not_chinese(self):
        # Same characters, different readings in Japanese and Korean. Giving a
        # Japanese town a Mandarin name would be a confident wrong answer.
        self.assertIsNone(romanise('下村镇', 'ja'))

    def test_cyrillic_transliterates_and_keeps_its_own_capitals(self):
        self.assertEqual('Kholstovo', romanise('Холстово', 'ru'))
        self.assertEqual('Nizhniy Novgorod',
                         romanise('Нижний Новгород', 'ru'))

    def test_arabic_is_refused_because_an_abjad_has_no_vowels(self):
        # Measured on the real data: Casablanca comes out "ldr lbyd'" and
        # Sanaa "sn`'". 60k rows of that would be worse than 60k absent rows,
        # so abjads wait for a source with real BGN romanisations.
        self.assertIsNone(romanise('الغابة', 'ar'))
        self.assertIsNone(romanise('صنعاء', 'ar'))

    def test_abugidas_are_refused_because_the_tables_drop_vowels(self):
        # Burmese is the clearest: a town called Kyainglat comes out
        # "Kyiunlpmriu". Devanagari is a near-miss rather than a miss --
        # "Lksmipur" for Lakshmipur -- which is still not a name.
        self.assertIsNone(romanise('ကျိုင်းလပ်မြို့', 'my'))
        self.assertIsNone(romanise('लक्ष्मीपुर', 'ne'))

    def test_korean_and_japanese_are_refused(self):
        # The tables reach for Mandarin readings of hanja and kanji and return
        # camel-cased syllable soup.
        self.assertIsNone(romanise('망상해수욕장', 'ko'))
        self.assertIsNone(romanise('ほると台', 'ja'))

    def test_the_allowed_alphabets_all_work(self):
        self.assertEqual('Kholstovo', romanise('Холстово', 'ru'))
        self.assertEqual('Didvela', romanise('დიდველა', 'ka'))
        self.assertEqual('Sari Omer', romanise('Σαρή Ομέρ', 'el'))
        self.assertTrue(romanise('Աղվես', 'hy'))

    def test_a_caseless_script_gets_a_capital(self):
        # Georgian has no case, so the raw result is lowercase.
        self.assertEqual('Didvela', romanise('დიდველა', 'ka'))

    def test_latin_is_left_alone(self):
        self.assertIsNone(romanise('Paris', 'fr'))

    def test_something_with_no_letters_is_not_romanised(self):
        self.assertIsNone(romanise('--', 'xx'))
        self.assertIsNone(romanise('', 'zh'))


class ResolveNameWithRomanisation(unittest.TestCase):

    def test_a_real_latin_label_always_beats_a_transliteration(self):
        name, lang, source = resolve_name_full(
            {'labels': {'en': 'Paris', 'zh': '巴黎'}}, 'zh')
        self.assertEqual(('Paris', 'en'), (name, lang))
        self.assertIsNone(source, 'nothing was transliterated, so nothing to keep')

    def test_a_chinese_only_label_is_romanised_and_the_original_reported(self):
        name, lang, source = resolve_name_full({'labels': {'zh': '下村镇'}}, 'zh')
        self.assertEqual(('Xiacunzhen', 'zh'), (name, lang))
        self.assertEqual('下村镇', source,
                         'the caller needs the original to keep it in alt_names')

    def test_a_label_that_cannot_be_romanised_stays_unnamed(self):
        self.assertEqual((None, None, None),
                         resolve_name_full({'labels': {'ar': 'الغابة'}}, 'ar'))

    def test_romanisation_is_the_last_resort_not_the_first(self):
        # A Russian village labelled in ru and de must take the German label,
        # because a real name beats a machine-made one.
        name, lang, source = resolve_name_full(
            {'labels': {'ru': 'Москва', 'de': 'Moskau'}}, 'ru')
        self.assertEqual(('Moskau', 'de'), (name, lang))
        self.assertIsNone(source)


class RomanisationOnlyFromTheLocalLanguage(unittest.TestCase):
    """Mexico. Wikidata carries bot-written Chechen and Serbian Cyrillic labels
    for Mexican places -- transliterations of names that are Latin to begin
    with. Romanising those reverses the transliteration and corrupts the name:
    "Avikola la Morena (Ermosiyo)" for Avicola la Morena (Hermosillo), and
    "Benito Khuarez" for Benito Juarez, across 5,623 rows."""

    def test_a_foreign_cyrillic_label_is_not_romanised(self):
        record = {'labels': {'ce': 'Авикола ла Морена'}}
        self.assertEqual((None, None, None), resolve_name_full(record, 'es'))

    def test_the_local_language_label_is_romanised(self):
        name, lang, source = resolve_name_full({'labels': {'ru': 'Апанасовка'}}, 'ru')
        self.assertEqual(('Apanasovka', 'ru'), (name, lang))
        self.assertEqual('Апанасовка', source)

    def test_a_regional_variant_of_the_local_language_counts(self):
        # Chinese settlements are often labelled zh-cn rather than plain zh.
        name, lang, _ = resolve_name_full({'labels': {'zh-cn': '万市镇'}}, 'zh')
        self.assertEqual(('Wanshizhen', 'zh-cn'), (name, lang))

    def test_mul_is_accepted_as_local(self):
        name, lang, _ = resolve_name_full({'labels': {'mul': 'Холстово'}}, 'ru')
        self.assertEqual(('Kholstovo', 'mul'), (name, lang))

    def test_nothing_is_romanised_when_the_country_has_no_language(self):
        # Without a local language there is no way to tell a name from a
        # rendering of one, so refuse rather than guess.
        self.assertEqual((None, None, None),
                         resolve_name_full({'labels': {'ru': 'Апанасовка'}}, None))


class NativeScriptIsKept(unittest.TestCase):
    """A romanised row must still carry the original. Otherwise transliteration
    is lossy: the only spelling anyone local would recognise is gone."""

    def test_the_original_goes_into_alt_names_under_its_language(self):
        record = {'labels': {'zh': '下村镇'}}
        fields = extra_fields(record, 'zh', None, ('zh', '下村镇'))
        self.assertEqual({'zh': ['下村镇']}, fields['alt_names'])

    def test_it_joins_existing_alternates_rather_than_replacing_them(self):
        record = {'alt_labels': {'ru': ['Новоалександровка (Чувашия)']}}
        fields = extra_fields(record, 'ru', None, ('ru', 'Новоалександровка'))
        self.assertEqual(
            {'ru': ['Новоалександровка', 'Новоалександровка (Чувашия)']},
            fields['alt_names'], 'sorted, so the output does not vary by run')

    def test_a_row_that_was_not_romanised_is_untouched(self):
        record = {'labels': {'en': 'Paris'}}
        self.assertEqual({}, extra_fields(record, 'fr', None, None)['alt_names'])


class ACountryIsNotADivisionOfAnother(unittest.TestCase):
    """32 country items carry an ISO 3166-2 code as well, which makes them look
    like an ordinary division of some other country. Selecting them seeds the
    territory at depth 0, one hop above everything inside it, so its settlements
    resolve to the parent state -- and emits the territory twice, once as a
    region of the parent and once as its own country."""

    SETTLEMENT = {10}

    def test_the_uk_is_not_a_region_of_the_uk(self):
        """Q145 carries GB-UKM, so the United Kingdom is a subdivision of
        itself. It sits one hop above Gibraltar, whose P131 points at it, which
        is how Catalan Bay and Gibraltar itself shipped under GB while GI
        shipped 117 gun batteries."""
        candidates = {
            145: {'iso_3166_2': ['GB-UKM'], 'iso_3166_1': ['GB']},
            9089: {'iso_3166_2': ['GB-ENG']},
        }
        selected = select_admin1s('GB', candidates, self.SETTLEMENT)
        self.assertNotIn(145, selected, 'the country itself must not be a region')
        self.assertIn(9089, selected, 'real divisions are untouched')

    def test_a_dependent_territory_is_not_a_region_of_its_parent(self):
        candidates = {
            21203: {'iso_3166_2': ['NL-AW'], 'iso_3166_1': ['AW']},   # Aruba
            701: {'iso_3166_2': ['NL-ZH']},                            # a province
        }
        self.assertEqual({701}, set(select_admin1s('NL', candidates, self.SETTLEMENT)))

    def test_the_dissolved_tier_applies_the_same_rule(self):
        candidates = {1183: {'iso_3166_2': ['US-PR'], 'iso_3166_1': ['PR']}}
        self.assertEqual({}, select_admin1s_with_dissolved('US', candidates))

    def test_tier_two_applies_it_too(self):
        # A country's P131 children can include another country.
        records = {785: {'instance_of': ['Q56061'], 'iso_3166_1': ['JE']},
                   99: {'instance_of': ['Q56061']}}
        selected = select_admin1s_under_country(145, {145: [785, 99]}, records, {56061})
        self.assertEqual({99}, set(selected))

    def test_an_ordinary_division_is_never_mistaken_for_a_country(self):
        self.assertFalse(is_country_item({'iso_3166_2': ['FR-971']}))
        self.assertTrue(is_country_item({'iso_3166_2': ['FR-971'],
                                         'iso_3166_1': ['GP']}))


class FormerPlacesAndMiscategorisedBranches(unittest.TestCase):
    """The two roots that shipped wrong, and the edge that made gun batteries
    into administrative divisions."""

    def test_both_former_roots_are_closed_over(self):
        from classify import FORMER_ROOTS
        self.assertEqual(2, len(FORMER_ROOTS),
                         'Wikidata splits "no longer here" along two branches: '
                         'the administrative one, and the place itself ceasing '
                         'to be a place')

    def test_a_place_whose_only_class_is_former_is_dropped(self):
        # 22674925 stands for "former settlement" here.
        settlement, former = {10, 22674925}, {22674925}
        excl = Exclusions(hard=set(), former=former, soft=set(), rescue=set())
        self.assertFalse(is_settlement(entity(instance_of=[22674925]), settlement, excl))

    def test_a_modern_city_that_is_also_ancient_survives(self):
        """Rome, Athens, Damascus and Istanbul are all instances of an ancient
        or former class as well as a current one. The rule is that *every*
        settlement class must be former, and this is why."""
        settlement, former = {10, 22674925}, {22674925}
        excl = Exclusions(hard=set(), former=former, soft=set(), rescue=set())
        self.assertTrue(is_settlement(entity(instance_of=[22674925, 10]), settlement, excl))

    def test_military_area_is_blocked_out_of_the_administrative_closure(self):
        """Wikidata files 'military area' under 'administrative territorial
        entity', so military installation, military base, fortification and
        barracks all became administrative divisions. Gibraltar's tier-2
        selection took every qualifying P131 child and shipped 117 gun
        batteries, gates and bastions as its regions."""
        from classify import CLOSURE_BLOCKED
        self.assertIn(97095925, CLOSURE_BLOCKED,
                      'the real closure has to block the real class')
        # 1 = admin entity, 2 = military area, 3 = military base, 4 = a battery
        graph = edges((2, 1), (3, 2), (4, 3), (9, 1))
        closure = subclass_closure(graph, [1], blocked=frozenset({2}))
        self.assertNotIn(4, closure, 'a battery is not an administrative division')
        self.assertIn(2, closure, 'the blocked class itself stays')
        self.assertIn(9, closure, 'the rest of the closure is untouched')


class BaselineResolution(unittest.TestCase):
    """Which "before" the regression gates compare against.

    This is here because the gates were dead and nothing noticed. The default
    used to be a git ref in a repository that no longer exists, so every run
    printed one line about skipping and passed -- the per-country gate exists
    to stop a country being gutted unnoticed, and it was itself gutted
    unnoticed."""

    def test_none_is_an_explicit_no_baseline(self):
        self.assertIsNone(resolve_baseline('none'))
        self.assertIsNone(resolve_baseline(''))
        self.assertIsNone(resolve_baseline(None))

    def test_a_path_is_read_as_a_manifest(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as handle:
            json.dump({'locations': [{'s_country_code': 'MT', 'i_cities': 7}]}, handle)
            path = handle.name
        try:
            self.assertEqual(7, resolve_baseline(path)['locations'][0]['i_cities'])
        finally:
            os.unlink(path)

    def test_the_default_is_the_published_release(self):
        # Not a git ref. A ref has to be remembered and updated by hand, and
        # when it stops resolving the gate disappears in silence.
        self.assertEqual('r2', BASELINE_DEFAULT)


class RegionsMustBePlaces(unittest.TestCase):
    """Tier 2 selects a country's P131 children by the administrative-entity
    closure, and Wikidata files a lot of non-geography there. Hong Kong shipped
    "Accountancy" and "Agriculture and Fisheries" -- functional constituencies
    -- plus four dioceses and "Hong Kong Time", 39 of its 63 regions."""

    CLASSES = frozenset({5508804, 3146899, 12143})   # constituency, diocese, time zone

    def test_a_constituency_is_not_a_region(self):
        self.assertTrue(is_not_a_place(entity(instance_of=[5508804]), self.CLASSES))

    def test_an_iso_code_overrides_everything(self):
        """PH-00 is Metro Manila, the National Capital Region, which Wikidata
        also calls a metropolitan area. Jarvis Island, Johnston Atoll and
        Kingman Reef are the US Minor Outlying Islands' actual subdivisions and
        also wildlife refuges. An official subdivision code settles it."""
        record = entity(instance_of=[5508804])
        record['iso_3166_2'] = ['PH-00']
        self.assertFalse(is_not_a_place(record, self.CLASSES))

    def test_an_ordinary_division_is_untouched(self):
        self.assertFalse(is_not_a_place(entity(instance_of=[15110]), self.CLASSES))

    def test_the_roots_are_only_the_two_that_close_safely(self):
        """Closing over "electoral unit" removes 680 regions including every
        province of Argentina and every French departement; over "religious
        administrative territorial entity" it takes Macau's civil freguesias;
        over "metropolitan area" it takes Italy's citta metropolitane. Those
        are matched exactly instead, and this pins the distinction."""
        from classify import NOT_A_PLACE_ROOTS, NOT_A_PLACE_CLASSES
        self.assertEqual((473972, 12143), NOT_A_PLACE_ROOTS)
        for unsafe in (192611, 20926517, 1907114):
            self.assertNotIn(unsafe, NOT_A_PLACE_ROOTS)
        self.assertIn(1907114, NOT_A_PLACE_CLASSES, 'exact, not closed over')


class SandboxEntities(unittest.TestCase):
    """Wikidata's scratch items are editable by anyone and pick up whatever a
    test needed. Q15397819 shipped as a city in Egypt while claiming to be a
    municipality of Germany, with P17 the Cook Islands and coordinates in
    Japan."""

    def test_a_sandbox_item_is_never_a_settlement(self):
        record = entity(instance_of=[10])
        record['id'] = 15397819
        self.assertFalse(is_settlement(record, {10}))
        self.assertFalse(is_settlement(record, {10}, Exclusions(
            hard=set(), former=set(), soft=set(), rescue=set())))

    def test_an_ordinary_entity_with_the_same_class_is_kept(self):
        record = entity(instance_of=[10])
        record['id'] = 12345
        self.assertTrue(is_settlement(record, {10}))


class HandMaintainedRegionOverrides(unittest.TestCase):
    """The root-most rule is right almost everywhere. These are the places
    where ISO itself carries something that is not a first-level division and
    no rule can tell it apart from one that is."""

    SETTLEMENT = {10}

    def test_iso_groupings_are_not_divisions(self):
        """GB-EAW and GB-GBN are legal jurisdictions spanning England,
        Scotland, Wales and Northern Ireland, not divisions beside them."""
        candidates = {1: {'iso_3166_2': ['GB-EAW']}, 2: {'iso_3166_2': ['GB-ENG']}}
        self.assertEqual({2}, set(select_admin1s('GB', candidates, self.SETTLEMENT)))

    def test_lithuanias_municipalities_lose_to_its_counties(self):
        """ISO lists all 60 beside the 10 counties, and Wikidata does not
        record them as contained in one, so root-most cannot separate them."""
        candidates = {1: {'iso_3166_2': ['LT-01']}, 2: {'iso_3166_2': ['LT-AL']}}
        self.assertEqual({2}, set(select_admin1s('LT', candidates, self.SETTLEMENT)))

    def test_one_code_yields_one_division(self):
        """Sevastopol and 'administrative and municipal division of Ukraine'
        both claim UA-40. Shipping both puts one place under two ids; the
        lower QID wins so a rebuild cannot flip which."""
        candidates = {7525: {'iso_3166_2': ['UA-40']},
                      4456205: {'iso_3166_2': ['UA-40']}}
        self.assertEqual({7525}, set(select_admin1s('UA', candidates, self.SETTLEMENT)))

    def test_an_excluded_qid_loses_even_at_the_lower_number(self):
        """Lulua District has the lower QID but Kasai-Central is the province
        that now holds CD-KC."""
        candidates = {209706: {'iso_3166_2': ['CD-KC']},
                      6702952: {'iso_3166_2': ['CD-KC']}}
        self.assertEqual({6702952}, set(select_admin1s('CD', candidates, self.SETTLEMENT)))


class OneNameOnePlaceInARegion(unittest.TestCase):
    """Wikidata holds the administrative unit and the place at its seat as
    separate items, and it also holds genuinely different places that share a
    name. Distance is what tells the two apart."""

    @staticmethod
    def row(qid, name, lat, lng, admin2=None, **extra):
        settlement = {'id': qid, 'source': 'wikidata', 'admin1_id': 1,
                      'country_code': 'XX', 'name': name, 'name_lang': 'en',
                      'slug': slugify(name), 'latitude': coord(lat),
                      'longitude': coord(lng), 'admin2_id': admin2,
                      'population': None, 'geonames_id': None,
                      'alt_names': {}, 'native_label': []}
        settlement.update(extra)
        return settlement

    @staticmethod
    def stats():
        return {'merged_duplicates': 0, 'ambiguous_names': 0}

    def test_the_same_place_twice_becomes_one_row(self):
        """A comune and the town at its seat sit on top of each other."""
        rows = [self.row(747074, 'Aalen', 48.837222, 10.093611),
                self.row(31868047, 'Aalen', 48.837840, 10.092990)]
        stats = self.stats()
        out = resolve_collisions(rows, lambda q: None, stats)
        self.assertEqual([747074], [s['id'] for s in out])
        self.assertEqual(1, stats['merged_duplicates'])

    def test_the_surviving_row_takes_the_fields_the_other_had(self):
        """The administrative item carries the population, the settlement item
        the GeoNames id. Dropping either without absorbing it loses data."""
        rows = [self.row(100, 'Abla', 37.142222, -2.777222, population=None,
                         geonames_id=2521886),
                self.row(200, 'Abla', 37.142567, -2.777509, population=1421,
                         geonames_id=None)]
        out = resolve_collisions(rows, lambda q: None, self.stats())
        self.assertEqual(1, len(out))
        self.assertEqual(1421, out[0]['population'])
        self.assertEqual(2521886, out[0]['geonames_id'])

    def test_two_real_places_are_both_kept_and_qualified(self):
        """Germany has two towns called Aach, 80 km apart. Neither is a
        duplicate of the other, and a list showing 'Aach' twice is useless."""
        rows = [self.row(1, 'Aach', 48.467500, 8.478611, admin2='Q7167'),
                self.row(2, 'Aach', 47.842500, 8.853889, admin2='Q7169')]
        stats = self.stats()
        out = resolve_collisions(rows, {7167: 'Zollernalbkreis',
                                        7169: 'Konstanz'}.get, stats)
        self.assertEqual(0, stats['merged_duplicates'])
        self.assertEqual(['Aach', 'Aach (Konstanz)'], [s['name'] for s in out])
        self.assertEqual('aach-konstanz', out[1]['slug'])

    def test_a_qualifier_that_does_not_qualify_is_not_used(self):
        """Two rows under the same parent would both read 'Sarna (Kutina)',
        which distinguishes nothing. The bare name is left and counted."""
        rows = [self.row(1, 'Sarna', 45.0, 16.0, admin2='Q900'),
                self.row(2, 'Sarna', 45.2, 16.3, admin2='Q900')]
        stats = self.stats()
        out = resolve_collisions(rows, {900: 'Kutina'}.get, stats)
        self.assertEqual(['Sarna', 'Sarna'], [s['name'] for s in out])
        self.assertEqual(1, stats['ambiguous_names'])

    def test_a_parent_named_after_the_place_is_not_a_qualifier(self):
        """'Aach (Aach)' says nothing."""
        rows = [self.row(1, 'Aach', 48.4675, 8.478611, admin2='Q1'),
                self.row(2, 'Aach', 47.8425, 8.853889, admin2='Q2')]
        stats = self.stats()
        out = resolve_collisions(rows, {1: 'Aach', 2: 'Konstanz'}.get, stats)
        self.assertEqual(['Aach', 'Aach (Konstanz)'], [s['name'] for s in out])
        self.assertEqual(0, stats['ambiguous_names'])

    def test_three_items_strung_out_still_collapse_to_one(self):
        """Single-link, not pairwise: the outer two are 3 km apart and would
        survive a pairwise test even though each is 1.5 km from the middle."""
        rows = [self.row(3, 'Ourem', 39.6500, -8.5800),
                self.row(1, 'Ourem', 39.6635, -8.5800),
                self.row(2, 'Ourem', 39.6770, -8.5800)]
        stats = self.stats()
        out = resolve_collisions(rows, lambda q: None, stats)
        self.assertEqual([1], [s['id'] for s in out])
        self.assertEqual(2, stats['merged_duplicates'])

    def test_the_biggest_place_keeps_the_plain_name(self):
        """Colombia has a village of 61,549 called Bogota in the same region as
        the capital. Qualifying both renames the capital."""
        rows = [self.row(1093182, 'Bogota', 5.05, -74.0, admin2='Q1', population=61549),
                self.row(2841, 'Bogota', 4.61, -74.08, admin2='Q2', population=8034649)]
        out = resolve_collisions(rows, {1: 'Central Savanna', 2: 'Cundinamarca'}.get,
                                 self.stats())
        self.assertEqual(['Bogota', 'Bogota (Central Savanna)'],
                         [s['name'] for s in out])

    def test_different_names_are_never_touched(self):
        rows = [self.row(1, 'Aach', 48.4675, 8.478611),
                self.row(2, 'Aalen', 48.4675, 8.478611)]
        stats = self.stats()
        out = resolve_collisions(rows, lambda q: None, stats)
        self.assertEqual(2, len(out))
        self.assertEqual(0, stats['merged_duplicates'])


class TierTwoHonoursTheExcludedClasses(unittest.TestCase):
    """A territory with no ISO code of its own takes every administrative P131
    child of itself, which reaches parallel tiers that are not divisions of it.
    Reunion took its 24 communes, its 4 arrondissements and its 25 cantons."""

    ADMIN = {484170, 194203, 18524218}

    def selected(self, records):
        return set(select_admin1s_under_country(17070, {17070: list(records)},
                                                records, self.ADMIN))

    def test_a_commune_is_the_division_that_survives(self):
        records = {1: {'instance_of': ['Q484170']},        # commune of France
                   2: {'instance_of': ['Q18524218']},      # canton -- electoral
                   3: {'instance_of': ['Q194203']}}        # arrondissement
        self.assertEqual({1}, self.selected(records))

    def test_an_ordinary_administrative_child_is_still_taken(self):
        records = {1: {'instance_of': ['Q484170']}, 2: {'instance_of': ['Q484170']}}
        self.assertEqual({1, 2}, self.selected(records))
