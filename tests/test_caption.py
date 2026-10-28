import unittest

from reelpost.caption import compose_caption, parse_tags
from reelpost.errors import ReelpostError


class CaptionTests(unittest.TestCase):
    def test_tags_are_normalized_and_deduped(self):
        tags = parse_tags(["#Berlin, travel", "berlin", "food"])
        self.assertEqual(tags, ["Berlin", "travel", "food"])

    def test_rejects_a_tag_with_a_hyphen(self):
        with self.assertRaises(ReelpostError):
            parse_tags(["hello-world"])

    def test_rejects_more_than_30_tags(self):
        with self.assertRaises(ReelpostError):
            parse_tags([f"tag{i}" for i in range(31)])

    def test_does_not_repeat_a_hashtag_already_in_the_description(self):
        caption = compose_caption("Morning light #Berlin", ["berlin", "travel"])
        self.assertEqual(caption, "Morning light #Berlin\n\n#travel")

    def test_rejects_a_caption_over_the_limit(self):
        with self.assertRaises(ReelpostError):
            compose_caption("a" * 2201, [])
