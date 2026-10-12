import os
import struct
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from zoomrec_core import (Meeting, MeetingError, RecordingSettings, Window,  # noqa: E402
                          build_join_url, build_record_command, build_remux_command,
                          find_meeting_window, load_meetings, parse_meeting,
                          parse_wmctrl, sanitize_description, set_ini_option,
                          apply_zoom_options, mp4_duration, next_occurrence,
                          read_meetings_file, read_status, write_meetings_file,
                          write_status, find_color_blob, is_zoom_blue,
                          build_thumbnail_command, thumbnail_path, thumbnail_time,
                          move_into_library)

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
        for part in ['-ac 2 -i speaker.monitor', '-f x11grab',
                     '-framerate 15 -video_size 1024x576 -i :1',
                     '-c:v libx264 -preset veryfast -crf 24', '-pix_fmt yuv420p',
                     '-c:a aac -b:a 192k', '-f pulse -sample_rate 48000', '-threads 2']:
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


class JoinUrlTest(unittest.TestCase):
    def test_https_link(self):
        self.assertEqual(
            build_join_url('https://tuwien.zoom.us/j/2384973897?pwd=aBc.1', '', 'Lukas P'),
            'zoommtg://tuwien.zoom.us/join?action=join&confno=2384973897&pwd=aBc.1&uname=Lukas%20P')

    def test_password_in_link_wins(self):
        url = build_join_url('https://zoom.us/j/123?pwd=fromlink', 'fromcsv', 'x')
        self.assertIn('pwd=fromlink', url)

    def test_https_link_without_pwd_uses_csv_password(self):
        url = build_join_url('https://zoom.us/j/123', 'fromcsv', 'x')
        self.assertIn('pwd=fromcsv', url)

    def test_meeting_id(self):
        self.assertEqual(build_join_url('123456789', '741699', 'zoomrec'),
                         'zoommtg://zoom.us/join?action=join&confno=123456789&pwd=741699&uname=zoomrec')

    def test_no_password_no_name(self):
        self.assertEqual(build_join_url('123', '', ''), 'zoommtg://zoom.us/join?action=join&confno=123')

    def test_special_characters_are_encoded(self):
        url = build_join_url('123', 'a&b=c', 'Ä "x"')
        self.assertIn('pwd=a%26b%3Dc', url)
        self.assertIn('uname=%C3%84%20%22x%22', url)

    def test_personal_link_returns_none(self):
        self.assertIsNone(build_join_url('https://tuwien.zoom.us/my/someone', '', 'x'))


WMCTRL_OUTPUT = """0x01000003 -1 xfce4-panel.Xfce4-panel  9c13012e0b51 xfce4-panel
0x00e00028 -1 xfdesktop.Xfdesktop   9c13012e0b51 Desktop
0x02600003  0 xfce4-terminal.Xfce4-terminal  9c13012e0b51 zoomrec
0x01e0000d  0 zoom.zoom             9c13012e0b51 Zoom Workplace
0x01e00060  0 zoom.zoom             9c13012e0b51 Meeting
0x01e0006f  0 zoom.zoom             9c13012e0b51 Zoom Workplace
0x01e00070  0 zoom.zoom             9c13012e0b51
"""


class WindowTest(unittest.TestCase):
    def test_parse_wmctrl(self):
        windows = parse_wmctrl(WMCTRL_OUTPUT)
        self.assertEqual(len(windows), 7)
        self.assertEqual(windows[3], Window('0x01e0000d', 'zoom.zoom', 'Zoom Workplace'))
        self.assertEqual(windows[6].title, '')

    def test_find_meeting_window(self):
        self.assertEqual(find_meeting_window(parse_wmctrl(WMCTRL_OUTPUT)).id, '0x01e00060')

    def test_no_meeting_window(self):
        output = "\n".join(l for l in WMCTRL_OUTPUT.splitlines() if not l.endswith('Meeting'))
        self.assertIsNone(find_meeting_window(parse_wmctrl(output)))

    def test_other_app_named_meeting_is_ignored(self):
        self.assertIsNone(find_meeting_window([Window('0x1', 'xfce4-terminal.Xfce4-terminal', 'Meeting')]))


class IniOptionTest(unittest.TestCase):
    CONF = '[General]\nAudioAutoAdjust=false\nhideCrashReport=false\nxwayland=true\n\n[AS]\nshowframewindow=true\n'

    def test_replace_existing_key(self):
        result = set_ini_option(self.CONF, 'General', 'hideCrashReport', 'true')
        self.assertIn('hideCrashReport=true', result)
        self.assertNotIn('hideCrashReport=false', result)
        self.assertIn('[AS]\nshowframewindow=true', result)

    def test_add_missing_key_to_section(self):
        result = set_ini_option(self.CONF, 'General', 'newKey', '1')
        self.assertIn('xwayland=true\nnewKey=1\n\n[AS]', result)

    def test_key_in_other_section_is_not_touched(self):
        result = set_ini_option(self.CONF, 'General', 'showframewindow', 'false')
        self.assertIn('[AS]\nshowframewindow=true', result)
        self.assertIn('showframewindow=false\n\n[AS]', result)

    def test_missing_section(self):
        result = set_ini_option('[AS]\nx=1\n', 'General', 'k', 'v')
        self.assertTrue(result.startswith('[General]\nk=v\n'))

    def test_prefix_of_key_does_not_match(self):
        result = set_ini_option('[General]\nhideCrashReportX=1\n', 'General', 'hideCrashReport', 'true')
        self.assertIn('hideCrashReportX=1', result)
        self.assertIn('hideCrashReport=true', result)


class ApplyZoomOptionsTest(unittest.TestCase):
    def test_sets_hide_crash_report(self):
        f = tempfile.NamedTemporaryFile('w', suffix='.conf', delete=False)
        f.write('[General]\nhideCrashReport=false\ndeviceID=AA\n')
        f.close()
        self.addCleanup(os.remove, f.name)
        apply_zoom_options(f.name)
        with open(f.name) as conf:
            text = conf.read()
        self.assertIn('hideCrashReport=true', text)
        self.assertIn('deviceID=AA', text)

    def test_missing_file_is_logged(self):
        with self.assertLogs(level='ERROR'):
            apply_zoom_options('/nonexistent/zoomus.conf')


class MeetingsFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, 'meetings.csv')

    def test_round_trip(self):
        meetings = [
            parse_meeting(row()),
            parse_meeting(row(id='https://tuwien.zoom.us/j/1?pwd=a;b', password='x;y"z', record='false')),
        ]
        write_meetings_file(self.path, meetings)
        loaded, errors = read_meetings_file(self.path)
        self.assertEqual(loaded, meetings)
        self.assertEqual(errors, [])
        self.assertEqual(os.listdir(self.dir.name), ['meetings.csv'])

    def test_errors_are_returned(self):
        with open(self.path, 'w') as f:
            f.write('weekday;time;duration;id;password;description;record\n'
                    'monday;09:00;60;1;;A;true\nfunday;09:00;60;1;;B;true\n')
        meetings, errors = read_meetings_file(self.path)
        self.assertEqual(len(meetings), 1)
        self.assertEqual(errors[0][0], 3)
        self.assertIn('funday', errors[0][1])


class NextOccurrenceTest(unittest.TestCase):
    def meeting(self, weekday, time):
        return parse_meeting(row(weekday=weekday, time=time))

    def test_later_today(self):
        now = datetime(2026, 10, 12, 8, 0)  # Monday
        self.assertEqual(next_occurrence(self.meeting('monday', '09:55'), now), datetime(2026, 10, 12, 9, 55))

    def test_earlier_today_is_next_week(self):
        now = datetime(2026, 10, 12, 10, 0)
        self.assertEqual(next_occurrence(self.meeting('monday', '09:55'), now), datetime(2026, 10, 19, 9, 55))

    def test_other_weekday(self):
        now = datetime(2026, 10, 12, 10, 0)
        self.assertEqual(next_occurrence(self.meeting('sunday', '18:00'), now), datetime(2026, 10, 18, 18, 0))


class StatusTest(unittest.TestCase):
    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            status_dir = os.path.join(d, '.zoomrec')
            self.assertIsNone(read_status(status_dir))
            write_status(status_dir, {'state': 'recording', 'meeting': 'VO'})
            self.assertEqual(read_status(status_dir), {'state': 'recording', 'meeting': 'VO'})
            self.assertEqual(os.listdir(status_dir), ['status.json'])


def box(kind, body):
    return struct.pack('>I4s', 8 + len(body), kind) + body


class Mp4DurationTest(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_version_0(self):
        mvhd = box(b'mvhd', bytes([0, 0, 0, 0]) + struct.pack('>IIII', 0, 0, 1000, 5_400_000) + bytes(80))
        data = box(b'ftyp', b'isom' + bytes(4)) + box(b'moov', mvhd) + box(b'mdat', bytes(100))
        self.assertEqual(mp4_duration(self.write(data)), 5400.0)

    def test_version_1_after_mdat(self):
        mvhd = box(b'mvhd', bytes([1, 0, 0, 0]) + struct.pack('>QQIQ', 0, 0, 90000, 90000 * 61) + bytes(80))
        data = box(b'ftyp', b'isom' + bytes(4)) + box(b'mdat', bytes(1000)) + box(b'moov', mvhd)
        self.assertEqual(mp4_duration(self.write(data)), 61.0)

    def test_invalid_file(self):
        self.assertIsNone(mp4_duration(self.write(b'not an mp4 file at all')))
        self.assertIsNone(mp4_duration('/nonexistent.mp4'))


class ColorBlobTest(unittest.TestCase):
    def image(self, width, height, blue_box=None):
        pixels = {}
        for y in range(height):
            for x in range(width):
                pixels[(x, y)] = (255, 255, 255)
        if blue_box:
            x0, y0, x1, y1 = blue_box
            for y in range(y0, y1):
                for x in range(x0, x1):
                    pixels[(x, y)] = (14, 114, 237)
        return pixels.__getitem__

    def test_finds_center_of_button(self):
        getpixel = self.image(220, 90, (150, 40, 204, 72))
        self.assertEqual(find_color_blob(220, 90, getpixel, is_zoom_blue), (176, 55))

    def test_no_button(self):
        self.assertIsNone(find_color_blob(220, 90, self.image(220, 90), is_zoom_blue))

    def test_too_few_pixels(self):
        getpixel = self.image(50, 50, (10, 10, 15, 15))
        self.assertIsNone(find_color_blob(50, 50, getpixel, is_zoom_blue))

    def test_zoom_blue(self):
        self.assertTrue(is_zoom_blue((14, 114, 237)))
        self.assertTrue(is_zoom_blue((14, 114, 237, 255)))
        self.assertFalse(is_zoom_blue((255, 255, 255)))
        self.assertFalse(is_zoom_blue((34, 38, 44)))
        self.assertFalse(is_zoom_blue((220, 50, 50)))


class ThumbnailTest(unittest.TestCase):
    def test_time(self):
        self.assertEqual(thumbnail_time(None), 1.0)
        self.assertEqual(thumbnail_time(3), 1.0)
        self.assertEqual(thumbnail_time(100), 20.0)
        self.assertEqual(thumbnail_time(5400), 300.0)

    def test_path_and_command(self):
        self.assertEqual(thumbnail_path('/r/.zoomrec', 'a.mp4'), '/r/.zoomrec/thumbs/a.mp4.jpg')
        cmd = build_thumbnail_command('a.mp4', 't.jpg', 20.0)
        self.assertEqual(cmd[-1], 't.jpg')
        self.assertIn('-ss 20.0 -i a.mp4', ' '.join(cmd))


class MoveIntoLibraryTest(unittest.TestCase):
    def test_move(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as library:
            source = os.path.join(work, 'a.mp4')
            with open(source, 'wb') as f:
                f.write(b'video' * 1000)
            target = move_into_library(source, library)
            self.assertEqual(target, os.path.join(library, 'a.mp4'))
            self.assertFalse(os.path.exists(source))
            self.assertEqual(os.listdir(library), ['a.mp4'])
            with open(target, 'rb') as f:
                self.assertEqual(len(f.read()), 5000)

    def test_unavailable_library_keeps_source(self):
        with tempfile.TemporaryDirectory() as work:
            source = os.path.join(work, 'a.mp4')
            open(source, 'wb').close()
            with self.assertRaises(OSError):
                move_into_library(source, os.path.join(work, 'missing-share'))
            self.assertTrue(os.path.exists(source))


if __name__ == '__main__':
    unittest.main()
