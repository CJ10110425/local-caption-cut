import unittest

from captioncut.captions import ass_color, map_to_timeline, wrap
from captioncut.common import ass_time, srt_time
from captioncut.cut import frame_plan
from captioncut.render import loudness_filter


class FixedWidthFont:
    """Every glyph is 10 px wide, so wrapping is easy to reason about."""

    def getlength(self, text):
        return 10 * len(text)


class FramePlanTest(unittest.TestCase):
    def test_clips_snap_to_frames_and_butt_together(self):
        clips, total = frame_plan([{'start': 0.61, 'end': 11.3}, {'start': 12.85, 'end': 20.3}], 30)
        self.assertEqual(clips[0]['source_start'], 18 / 30)
        self.assertEqual(clips[0]['frames'], round((11.3 - 0.61) * 30))
        self.assertEqual(clips[1]['timeline_start'], clips[0]['timeline_end'])
        self.assertEqual(total, clips[0]['frames'] + clips[1]['frames'])

    def test_rejects_backwards_and_out_of_range_cuts(self):
        with self.assertRaises(Exception):
            frame_plan([{'start': 5, 'end': 4}], 30)
        with self.assertRaises(Exception):
            frame_plan([{'start': 0, 'end': 12}], 30, source_duration=10)


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.clips, _ = frame_plan([{'start': 0, 'end': 10}, {'start': 20, 'end': 30}], 30)

    def test_caption_inside_second_clip_moves_back(self):
        self.assertEqual(list(map_to_timeline(21, 23, self.clips)), [(11, 13, 1)])

    def test_caption_across_removed_section_is_split(self):
        parts = list(map_to_timeline(9, 21, self.clips))
        self.assertEqual([p[2] for p in parts], [0, 1])
        self.assertAlmostEqual(parts[0][0], 9)
        self.assertAlmostEqual(parts[1][1], 11)

    def test_slivers_are_dropped(self):
        self.assertEqual(list(map_to_timeline(9.9, 15, self.clips)), [])


class WrapTest(unittest.TestCase):
    def test_wraps_cjk_by_glyph(self):
        self.assertEqual(wrap('一二三四五六', FixedWidthFont(), 40), ['一二三四', '五六'])

    def test_keeps_latin_words_whole(self):
        self.assertEqual(wrap('我去 Networking 活動', FixedWidthFont(), 100), ['我去', 'Networking', '活動'])

    def test_punctuation_never_starts_a_line(self):
        self.assertEqual(wrap('一二三四，五', FixedWidthFont(), 40), ['一二三四，', '五'])

    def test_explicit_newlines(self):
        self.assertEqual(wrap('上面\n下面', FixedWidthFont(), 400), ['上面', '下面'])


class FormatTest(unittest.TestCase):
    def test_ass_color_is_bgr(self):
        self.assertEqual(ass_color('#FFD964'), '&H0064D9FF')
        self.assertEqual(ass_color('302a24', 0x80), '&H80242A30')

    def test_times(self):
        self.assertEqual(ass_time(3725.456), '1:02:05.46')
        self.assertEqual(srt_time(3725.456), '01:02:05,456')

    def test_second_loudness_pass_uses_measurements(self):
        measured = {'input_i': '-27.7', 'input_tp': '0.7', 'input_lra': '7.1', 'input_thresh': '-38.1',
                    'target_offset': '-0.3'}
        spec = loudness_filter({'I': -16, 'TP': -1.5, 'LRA': 11}, measured)
        self.assertIn('measured_I=-27.7', spec)
        self.assertIn('linear=false', spec)


if __name__ == '__main__':
    unittest.main()
