import tempfile
import unittest
from pathlib import Path

from PIL import Image

from reelpost.media import CoverAsset, inspect_video, limits_for, prepare_cover
from mp4util import write_movie
from reelpost.media import VideoInfo


class MediaTests(unittest.TestCase):
    def test_reads_duration_dimensions_and_faststart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_movie(Path(tmp) / "clip.mp4", timescale=1000, duration=27000)
            info = inspect_video(path)
        self.assertEqual(info.duration_seconds, 27)
        self.assertEqual((info.width, info.height), (1080, 1920))
        self.assertTrue(info.faststart)

    def test_reads_version_1_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_movie(Path(tmp) / "clip.mp4", timescale=1000, duration=5000, version=1)
            info = inspect_video(path)
        self.assertEqual(info.duration_seconds, 5)

    def test_flags_moov_at_the_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_movie(Path(tmp) / "clip.mp4", timescale=1000, duration=5000, faststart=False)
            info = inspect_video(path)
        errors, _warnings = limits_for(info, ["instagram"])
        self.assertTrue(any("moov atom" in error for error in errors))

    def test_facebook_duration_and_instagram_size(self):
        long = VideoInfo(Path("clip.mp4"), 1000, 100, 1080, 1920, True)
        errors, _warnings = limits_for(long, ["facebook", "instagram"])
        self.assertTrue(any("90 seconds" in error for error in errors))
        huge = VideoInfo(Path("clip.mp4"), 301 * 1024 * 1024, 20, 1080, 1920, True)
        errors, _warnings = limits_for(huge, ["instagram"])
        self.assertTrue(any("300 MB" in error for error in errors))

    def test_png_cover_becomes_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cover.png"
            Image.new("RGBA", (90, 160), (10, 20, 30, 128)).save(source)
            asset = prepare_cover(source, 8 * 1024 * 1024)
            self.addCleanup(asset.close)
            self.assertIsInstance(asset, CoverAsset)
            self.assertTrue(asset.reencoded)
            self.assertEqual(asset.path.suffix, ".jpg")
            self.assertEqual((asset.width, asset.height), (90, 160))
