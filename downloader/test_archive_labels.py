import unittest

from archive_labels import is_demo_archive


class ArchiveLabelTests(unittest.TestCase):
    def test_catalog_preview_descriptions_and_full_card_exclusions(self):
        for description in ['Discover: A preview', 'Discover: 3 tracks',
                            'DISCOVER: A PREVIEW', 'Story: The first chapter',
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
        self.assertTrue(is_demo_archive(title='Discover: 2 tracks'))
