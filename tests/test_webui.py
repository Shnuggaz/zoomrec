import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'webui'))

try:
    import flask  # noqa: F401
except ImportError:  # pragma: no cover
    flask = None

from zoomrec_core import read_meetings_file, write_status  # noqa: E402


def box(kind, body):
    return struct.pack('>I4s', 8 + len(body), kind) + body


def fake_mp4(seconds):
    mvhd = box(b'mvhd', bytes(4) + struct.pack('>IIII', 0, 0, 1000, seconds * 1000) + bytes(80))
    return box(b'ftyp', b'isom' + bytes(4)) + box(b'moov', mvhd) + box(b'mdat', bytes(5000))


@unittest.skipIf(flask is None, 'flask not installed')
class WebUiTest(unittest.TestCase):
    def setUp(self):
        from app import create_app
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.rec = os.path.join(self.tmp, 'recordings')
        self.cfg = os.path.join(self.tmp, 'config')
        os.makedirs(self.rec)
        os.makedirs(self.cfg)
        self.csv = os.path.join(self.cfg, 'meetings.csv')
        with open(self.csv, 'w') as f:
            f.write('weekday;time;duration;id;password;description;record\n'
                    'monday;09:55;90;https://tuwien.zoom.us/j/1?pwd=a;;VO_Mathe;true\n')
        self.video = '2026-10-12_09-54-00-VO_Mathe.mp4'
        with open(os.path.join(self.rec, self.video), 'wb') as f:
            f.write(fake_mp4(5400))
        self.app = create_app(recordings_dir=self.rec, meetings_csv=self.csv, password='geheim')
        self.app.testing = True
        self.client = self.app.test_client()

    def login(self):
        response = self.client.post('/login', data={'password': 'geheim'})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as s:
            self.csrf = s['csrf']

    def test_requires_login(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertEqual(self.client.get(f'/recordings/{self.video}').status_code, 302)

    def test_wrong_password(self):
        response = self.client.post('/login', data={'password': 'falsch'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Falsches Passwort', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_open_redirect_is_blocked(self):
        response = self.client.post('/login?next=//evil.example', data={'password': 'geheim'})
        self.assertEqual(response.headers['Location'], '/')

    def test_secret_key_is_persisted(self):
        self.assertTrue(os.path.exists(os.path.join(self.cfg, '.webui-secret')))

    def test_library_lists_recording(self):
        self.login()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('VO Mathe', html)
        self.assertIn('1:30:00', html)
        self.assertIn('Montag, 12.10.2026 09:54', html)

    def test_status_files_are_hidden_from_library(self):
        write_status(os.path.join(self.rec, '.zoomrec'), {'state': 'idle'})
        self.login()
        self.assertNotIn('status.json', self.client.get('/').get_data(as_text=True))

    def test_range_request_for_seeking(self):
        self.login()
        response = self.client.get(f'/recordings/{self.video}', headers={'Range': 'bytes=100-199'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.data), 100)
        self.assertEqual(response.headers['Content-Range'].split('/')[0], 'bytes 100-199')
        self.assertEqual(response.mimetype, 'video/mp4')

    def test_download_is_attachment(self):
        self.login()
        response = self.client.get(f'/recordings/{self.video}/download')
        self.assertIn('attachment', response.headers['Content-Disposition'])

    def test_path_traversal(self):
        self.login()
        for name in ['..%2Fconfig%2Fmeetings.csv', '.zoomrec', 'meetings.csv', '..']:
            self.assertEqual(self.client.get(f'/recordings/{name}').status_code, 404, name)

    def test_delete_requires_csrf(self):
        self.login()
        self.assertEqual(self.client.post(f'/recordings/{self.video}/delete').status_code, 400)
        self.assertTrue(os.path.exists(os.path.join(self.rec, self.video)))
        self.client.post(f'/recordings/{self.video}/delete', data={'csrf': self.csrf})
        self.assertFalse(os.path.exists(os.path.join(self.rec, self.video)))

    def test_active_recording_cannot_be_deleted(self):
        write_status(os.path.join(self.rec, '.zoomrec'), {'state': 'recording', 'file': self.video})
        self.login()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('wird aufgenommen', html)
        self.client.post(f'/recordings/{self.video}/delete', data={'csrf': self.csrf})
        self.assertTrue(os.path.exists(os.path.join(self.rec, self.video)))

    def test_status_api(self):
        write_status(os.path.join(self.rec, '.zoomrec'), {
            'state': 'recording', 'meeting': 'VO_Mathe', 'file': self.video,
            'updated': datetime.now().isoformat(timespec='seconds')})
        self.login()
        status = self.client.get('/api/status').get_json()
        self.assertEqual(status['state'], 'recording')
        self.assertFalse(status['stale'])
        self.assertIn('disk_free', status)

    def test_status_api_stale(self):
        write_status(os.path.join(self.rec, '.zoomrec'), {'state': 'idle', 'updated': '2020-01-01T00:00:00'})
        self.login()
        self.assertTrue(self.client.get('/api/status').get_json()['stale'])

    def test_add_meeting(self):
        self.login()
        response = self.client.post('/schedule/save', data={
            'csrf': self.csrf, 'description': 'UE Physik', 'weekday': 'wednesday', 'time': '14:00',
            'duration': '45', 'id': '123456789', 'password': 'abc', 'record': 'true'})
        self.assertEqual(response.status_code, 302)
        meetings, _ = read_meetings_file(self.csv)
        self.assertEqual([m.description for m in meetings], ['VO_Mathe', 'UE_Physik'])
        self.assertEqual(meetings[1].weekday, 'wednesday')

    def test_invalid_meeting_is_rejected(self):
        self.login()
        response = self.client.post('/schedule/save', data={
            'csrf': self.csrf, 'description': 'X', 'weekday': 'wednesday', 'time': '25:00',
            'duration': '45', 'id': '1', 'record': 'true'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('invalid time', response.get_data(as_text=True))
        self.assertEqual(len(read_meetings_file(self.csv)[0]), 1)

    def test_edit_toggle_delete_meeting(self):
        self.login()
        self.client.post('/schedule/save', data={
            'csrf': self.csrf, 'index': '0', 'description': 'VO Mathe', 'weekday': 'monday',
            'time': '10:00', 'duration': '90', 'id': '1', 'record': 'true'})
        self.assertEqual(read_meetings_file(self.csv)[0][0].time, '10:00')
        self.client.post('/schedule/0/toggle', data={'csrf': self.csrf})
        self.assertFalse(read_meetings_file(self.csv)[0][0].record)
        html = self.client.get('/schedule').get_data(as_text=True)
        self.assertIn('deaktiviert', html)
        self.client.post('/schedule/0/delete', data={'csrf': self.csrf})
        self.assertEqual(read_meetings_file(self.csv)[0], [])

    def test_schedule_page_shows_invalid_lines(self):
        with open(self.csv, 'a') as f:
            f.write('funday;09:00;60;1;;Bad;true\n')
        self.login()
        html = self.client.get('/schedule').get_data(as_text=True)
        self.assertIn('Zeile 3', html)
        self.assertTrue(re.search(r'Montag, 09:55', html))

    def test_watch_page(self):
        self.login()
        html = self.client.get(f'/watch/{self.video}').get_data(as_text=True)
        self.assertIn(f'src="/recordings/{self.video}"', html)

    def test_missing_password_refuses_start(self):
        from app import create_app
        os.environ.pop('WEB_PASSWORD', None)
        os.environ.pop('WEB_PASSWORD_HASH', None)
        with self.assertRaises(RuntimeError):
            create_app(recordings_dir=self.rec, meetings_csv=self.csv)


if __name__ == '__main__':
    unittest.main()
