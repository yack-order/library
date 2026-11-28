import unittest

from archive_labels import is_demo_archive


class ArchiveLabelTests(unittest.TestCase):
    def test_catalog_preview_descriptions_and_full_card_exclusions(self):
        for description in ['Check out this preview of the first book in the Diary of a Wimpy Kid series', 'Discover: A preview', 'Discover: 3 tracks',
                            'DISCOVER: A PREVIEW', 'Enjoy the first chapter',
                            'Introduction: Prologue', 'Chapter 1: The Beginning',
                            'Chapter one', '2 tracks Full Card: 25 tracks. Available: Until December']:
            with self.subTest(description=description):
                self.assertTrue(is_demo_archive(description=description))
        for description in ['Discover: The full card', 'Discover ten stories',
                            'Discover fascinating facts', 'Discover the science',
                            'A regular full playlist', None]:
            with self.subTest(description=description):
                self.assertFalse(is_demo_archive(description=description))

    def test_explicit_demo_labels_survive_full_descriptions(self):
        for filename in ['Example[DEMO][abc12].7z', 'Example[demo][abc12].zip']:
            self.assertTrue(is_demo_archive(filename, 'Discover: The full card'))

    def test_regular_discovery_titles_are_not_preview_evidence(self):
        self.assertFalse(is_demo_archive('Discover India[abc12].7z', title='Discover India'))
        self.assertFalse(is_demo_archive(title='Discover: 2 tracks'))

class ExplicitEvidenceParityTests(unittest.TestCase):
    def test_shared_preview_cases(self):
        import json
        from pathlib import Path
        for case in json.loads(Path(__file__).with_name('preview_cases.json').read_text()):
            with self.subTest(description=case['description']):
                self.assertEqual(is_demo_archive(description=case['description'], user_id='yoto'), case['preview'])
                self.assertFalse(is_demo_archive(description=case['description'], user_id='community'))

    def test_only_bound_archive_source_can_correct_a_historical_marker(self):
        self.assertTrue(is_demo_archive('Old [DEMO].7z', 'Discover: Listen to the full card for free!'))
        self.assertFalse(is_demo_archive('Old [DEMO].7z', 'Discover: Listen to the full card for free!', user_id='yoto', verified_source=True))
        self.assertFalse(is_demo_archive('Old [DEMO].7z', 'Discover the meaning of friendship.', user_id='yoto', verified_source=True))
        self.assertTrue(is_demo_archive('Old.7z', 'In this free preview, hear one song.', user_id='yoto', verified_source=True))

    def test_complete_track_inventory_overrides_stale_preview_text(self):
        self.assertFalse(is_demo_archive(description='Discover: 2 tracks. Full Card: 19 tracks.', user_id='yoto', track_count=19))
        self.assertTrue(is_demo_archive(description='Discover: 2 tracks. Full Card: 19 tracks.', user_id='yoto', track_count=2))

    def test_withdrawal_requires_explicit_notice_names(self):
        from archive_labels import is_withdrawal_notice
        card = {'content': {'chapters': [{'title': 'this title is no longer free ', 'duration': 13, 'tracks': [{'title': 'this title is no longer free ', 'duration': 13}]}]}}
        self.assertTrue(is_withdrawal_notice(card))
        card['content']['chapters'][0]['tracks'][0]['title'] = 'A short original poem'
        self.assertFalse(is_withdrawal_notice(card))
