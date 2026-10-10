import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from zoomrec_core import (Meeting, MeetingError, RecordingSettings,  # noqa: E402
                          build_record_command, build_remux_command,
                          load_meetings, parse_meeting, sanitize_description)

VALID = {
    'weekday': 'Monday', 'time': '09:55', 'duration': '90', 'id': '123 456 7890',
    'password': 'abc123', 'description': 'VO Mathe 1', 'record': 'true',
}


def row(**changes):
    r = dict(VALID)
    r.update(changes)
    return r


class ParseMeetingTest(unittest.TestCase):
    def test_valid_row(self):
        self.assertEqual(parse_meeting(row()), Meeting(
            weekday='monday', time='09:55', duration=90, id='1234567890',
            password='abc123', description='VO_Mathe_1', record=True))

    def test_url_id(self):
        url = 'https://tuwien.zoom.us/j/123456789?pwd=abcDEF'
        self.assertEqual(parse_meeting(row(id=url, password='')).id, url)

    def test_record_false(self):
        self.assertFalse(parse_meeting(row(record='false')).record)

    def test_invalid_values(self):
        cases = {
            'weekday': ['mon', 'every()', ''],
            'time': ['9:55', '24:00', '09:60', '09:55"); import os; ("', ''],
            'duration': ['abc', '0', '-5', '10000', '1.5'],
            'id': ['12a45', '"; os.system("x")', 'https://x/j/1 2', 'https://x/"a'],
            'record': ['yes', ''],
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(MeetingError):
                        parse_meeting(row(**{field: value}))

    def test_missing_column(self):
        r = row()
        del r['duration']
        with self.assertRaises(MeetingError):
            parse_meeting(r)


class SanitizeDescriptionTest(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(sanitize_description('VO Maschinenbau 307.494'), 'VO_Maschinenbau_307.494')
        self.assertEqual(sanitize_description('../../etc/passwd'), 'etc_passwd')
        self.assertEqual(sanitize_description('a"; rm -rf /'), 'a_rm_-rf')
        self.assertEqual(sanitize_description('   '), 'meeting')
        self.assertEqual(sanitize_description('Ü' * 5), 'meeting')
        self.assertEqual(len(sanitize_description('x' * 500)), 80)


class LoadMeetingsTest(unittest.TestCase):
    def write_csv(self, text):
        f = tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, encoding='utf-8')
        f.write(text)
        f.close()
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_skips_invalid_and_empty_rows(self):
        path = self.write_csv(
            'weekday;time;duration;id;password;description;record\n'
            'monday;09:55;60;111111111111;741699;Important;true\n'
            '\n'
            'funday;14:00;90;222;x;Bad;true\n'
            'tuesday;17:00;90;https://zoom.us/j/1?pwd=a;;Url;false\n')
        with self.assertLogs(level='ERROR') as logs:
            meetings = load_meetings(path)
        self.assertEqual([m.description for m in meetings], ['Important', 'Url'])
        self.assertIn('line 4', logs.output[0])

    def test_trailing_delimiter_of_upstream_format(self):
        path = self.write_csv(
            'weekday;time;duration;id;password;description;record;\n'
            'monday;09:55;60;111111111111;741699;Important;true;\n')
        self.assertEqual(len(load_meetings(path)), 1)

    def test_example_file_is_valid(self):
        example = os.path.join(os.path.dirname(__file__), '..', 'meetings.example.csv')
        self.assertEqual(len(load_meetings(example)), 3)


class FfmpegCommandTest(unittest.TestCase):
    def test_record_command(self):
        cmd = build_record_command(':1', '1024x576', '/rec/a.mkv')
        self.assertEqual(cmd[0], 'ffmpeg')
        self.assertEqual(cmd[-1], '/rec/a.mkv')
        joined = ' '.join(cmd)
        for part in ['-f pulse -ac 2 -i speaker.monitor', '-f x11grab',
                     '-framerate 15 -video_size 1024x576 -i :1',
                     '-c:v libx264 -preset veryfast -crf 28', '-pix_fmt yuv420p',
                     '-c:a aac -b:a 128k', '-threads 2']:
            self.assertIn(part, joined)
        self.assertNotIn('crf 0', joined)
        self.assertNotIn('pcm_s16le', joined)

    def test_settings_from_env(self):
        settings = RecordingSettings.from_env({'RECORD_FPS': '10', 'RECORD_CRF': '30',
                                               'RECORD_PRESET': 'faster', 'RECORD_THREADS': '4'})
        joined = ' '.join(build_record_command(':1', '800x600', 'x.mkv', settings))
        self.assertIn('-framerate 10', joined)
        self.assertIn('-crf 30', joined)
        self.assertIn('-preset faster', joined)
        self.assertIn('-threads 4', joined)

    def test_defaults_without_env(self):
        self.assertEqual(RecordingSettings.from_env({}), RecordingSettings())

    def test_filename_is_single_argument(self):
        cmd = build_record_command(':1', '1024x576', '/rec/a b; rm x.mkv')
        self.assertEqual(cmd[-1], '/rec/a b; rm x.mkv')

    def test_remux_command(self):
        cmd = build_remux_command('a.mkv', 'a.mp4')
        self.assertEqual(cmd[-1], 'a.mp4')
        joined = ' '.join(cmd)
        self.assertIn('-i a.mkv', joined)
        self.assertIn('-c copy', joined)
        self.assertIn('-movflags +faststart', joined)


if __name__ == '__main__':
    unittest.main()
