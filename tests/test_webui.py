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
        self.assertIn('Mo., 12.10.2026 · 09:54', html)

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
        self.assertIn('Wird aufgenommen', html)
        self.client.post(f'/recordings/{self.video}/delete', data={'csrf': self.csrf})
        self.assertTrue(os.path.exists(os.path.join(self.rec, self.video)))

    def test_running_recording_outside_library_is_listed(self):
        write_status(os.path.join(self.rec, '.zoomrec'),
                     {'state': 'recording', 'file': '2026-10-13_09-54-00-VO_Analysis.mkv'})
        self.login()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('VO Analysis', html)
        self.assertIn('Wird aufgenommen', html)
        self.assertEqual(self.client.get('/recordings/2026-10-13_09-54-00-VO_Analysis.mkv').status_code, 404)

    def test_status_api(self):
        write_status(os.path.join(self.rec, '.zoomrec'), {
            'state': 'recording', 'meeting': 'VO_Mathe', 'file': self.video,
            'updated': datetime.now().isoformat(timespec='seconds')})
        self.login()
        status = self.client.get('/api/status').get_json()
        self.assertEqual(status['state'], 'recording')
        self.assertFalse(status['stale'])
        self.assertIn('disk_free', status)

    def test_error_screenshot(self):
        status_dir = os.path.join(self.rec, '.zoomrec')
        write_status(status_dir, {'state': 'idle', 'last_error': 'x', 'last_error_screenshot': True})
        self.login()
        self.assertFalse(self.client.get('/api/status').get_json()['error_screenshot'])
        with open(os.path.join(status_dir, 'last_error.jpg'), 'wb') as f:
            f.write(b'\xff\xd8jpeg')
        self.assertTrue(self.client.get('/api/status').get_json()['error_screenshot'])
        response = self.client.get('/last-error.jpg')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/jpeg')

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
        self.assertIn('pausiert', html)
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

    def test_thumbnail(self):
        self.login()
        self.assertEqual(self.client.get(f'/thumbs/{self.video}').status_code, 404)
        thumbs = os.path.join(self.rec, '.zoomrec', 'thumbs')
        os.makedirs(thumbs)
        with open(os.path.join(thumbs, self.video + '.jpg'), 'wb') as f:
            f.write(b'\xff\xd8jpeg')
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn(f'/thumbs/{self.video}?v=', html)
        response = self.client.get(f'/thumbs/{self.video}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/jpeg')
        self.assertEqual(self.client.get('/thumbs/..%2F..%2Fconfig%2Fmeetings.csv').status_code, 404)
        # Deleting the recording removes its thumbnail
        self.client.post(f'/recordings/{self.video}/delete', data={'csrf': self.csrf})
        self.assertFalse(os.path.exists(os.path.join(thumbs, self.video + '.jpg')))

    def test_watch_series_navigation(self):
        for day in ('05', '19'):
            with open(os.path.join(self.rec, f'2026-10-{day}_09-54-00-VO_Mathe.mp4'), 'wb') as f:
                f.write(fake_mp4(60))
        self.login()
        html = self.client.get(f'/watch/{self.video}').get_data(as_text=True)
        self.assertIn('Einheit 2 von 3', html)
        self.assertIn('/watch/2026-10-05_09-54-00-VO_Mathe.mp4', html)
        self.assertIn('/watch/2026-10-19_09-54-00-VO_Mathe.mp4', html)

    def test_week_view(self):
        from app import week_view
        from zoomrec_core import parse_meeting
        base = {'id': '1', 'password': '', 'record': 'true'}
        meetings = [parse_meeting({**base, 'weekday': 'monday', 'time': '09:00', 'duration': '120', 'description': 'A'}),
                    parse_meeting({**base, 'weekday': 'saturday', 'time': '19:30', 'duration': '90', 'description': 'B'})]
        week = week_view(meetings, {})
        self.assertEqual(week['days'][-1], 'saturday')
        self.assertEqual(len(week['days']), 6)
        self.assertEqual(week['hours'][0], 8)
        self.assertEqual(week['hours'][-1], 21)
        first = week['blocks'][0]
        self.assertEqual(first['day'], 0)
        self.assertAlmostEqual(first['top'], 60 * 100 / (13 * 60))
        self.assertEqual(first['end'], '11:00')

    def add_transcript(self, name=None):
        from transcription import TRANSCRIPTS_DIR_NAME, to_vtt
        name = name or self.video
        directory = os.path.join(self.rec, '.zoomrec', TRANSCRIPTS_DIR_NAME)
        os.makedirs(directory, exist_ok=True)
        segments = [{'start': 12.0, 'end': 15.0, 'text': 'Heute: Eigenwerte <und> Eigenvektoren.'},
                    {'start': 3725.0, 'end': 3730.0, 'text': 'Der Eigenwert kommt zur Prüfung.'}]
        with open(os.path.join(directory, name + '.json'), 'w') as f:
            import json
            json.dump({'language': 'de', 'segments': segments}, f)
        with open(os.path.join(directory, name + '.vtt'), 'w') as f:
            f.write(to_vtt(segments))

    def test_search_transcripts(self):
        self.add_transcript()
        self.login()
        html = self.client.get('/search?q=eigenwert').get_data(as_text=True)
        self.assertIn('2 Treffer in 1 Aufnahme', html)
        self.assertIn('<mark>Eigenwert</mark>e &lt;und&gt;', html)
        self.assertIn(f'/watch/{self.video}?t=3725.0', html)
        self.assertIn('1:02:05', html)
        html = self.client.get('/search?q=quantenphysik').get_data(as_text=True)
        self.assertIn('Nichts gefunden', html)

    def test_search_without_transcripts(self):
        self.login()
        self.assertIn('Noch keine Transkripte', self.client.get('/search').get_data(as_text=True))

    def test_watch_with_transcript(self):
        self.add_transcript()
        self.login()
        html = self.client.get(f'/watch/{self.video}?t=3725').get_data(as_text=True)
        self.assertIn('data-start="3725.0"', html)
        self.assertIn(f'src="/transcripts/{self.video}.vtt"', html)
        self.assertIn('Heute: Eigenwerte &lt;und&gt; Eigenvektoren.', html)
        response = self.client.get(f'/transcripts/{self.video}.vtt')
        self.assertEqual(response.mimetype, 'text/vtt')
        self.assertTrue(response.get_data(as_text=True).startswith('WEBVTT'))
        self.assertEqual(self.client.get('/transcripts/..%2Fx.mp4.vtt').status_code, 404)

    def test_transcript_badges_and_retry(self):
        from transcription import write_state
        status_dir = os.path.join(self.rec, '.zoomrec')
        write_state(status_dir, {self.video: {'status': 'failed', 'error': 'GPU out of memory'}})
        self.login()
        self.assertIn('Transkription fehlgeschlagen', self.client.get('/').get_data(as_text=True))
        self.assertIn('GPU out of memory', self.client.get(f'/watch/{self.video}').get_data(as_text=True))
        self.client.post(f'/recordings/{self.video}/transcribe', data={'csrf': self.csrf})
        from transcription import read_state
        self.assertEqual(read_state(status_dir), {})
        write_state(status_dir, {self.video: {'status': 'processing', 'job_id': 'j'}})
        self.assertIn('Wird transkribiert', self.client.get('/').get_data(as_text=True))

    def test_delete_removes_transcript(self):
        self.add_transcript()
        self.login()
        self.client.post(f'/recordings/{self.video}/delete', data={'csrf': self.csrf})
        self.assertFalse(os.path.exists(os.path.join(self.rec, '.zoomrec', 'transcripts', self.video + '.json')))

    def test_login_lockout(self):
        for _ in range(5):
            self.assertEqual(self.client.post('/login', data={'password': 'falsch'}).status_code, 200)
        response = self.client.post('/login', data={'password': 'geheim'})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Zu viele Fehlversuche', response.get_data(as_text=True))
        # Another address is not affected
        response = self.client.post('/login', data={'password': 'geheim'}, environ_base={'REMOTE_ADDR': '10.0.0.2'})
        self.assertEqual(response.status_code, 302)

    def test_security_headers(self):
        response = self.client.get('/login')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertNotIn('Strict-Transport-Security', response.headers)

    def test_behind_proxy(self):
        from app import create_app
        app = create_app(recordings_dir=self.rec, meetings_csv=self.csv, password='geheim', trust_proxy=True)
        client = app.test_client()
        headers = {'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '203.0.113.7', 'X-Forwarded-Host': 'rec.example.com'}
        response = client.post('/login', data={'password': 'geheim'}, headers=headers)
        cookie = response.headers['Set-Cookie']
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertEqual(response.headers['Location'], '/')
        self.assertIn('max-age', response.headers['Strict-Transport-Security'])
        # Lockout uses the forwarded client address
        for _ in range(5):
            client.post('/login', data={'password': 'x'}, headers=headers)
        self.assertEqual(client.post('/login', data={'password': 'geheim'}, headers=headers).status_code, 429)
        other = dict(headers, **{'X-Forwarded-For': '203.0.113.8'})
        self.assertEqual(client.post('/login', data={'password': 'geheim'}, headers=other).status_code, 302)

    def test_missing_password_refuses_start(self):
        from app import create_app
        os.environ.pop('WEB_PASSWORD', None)
        os.environ.pop('WEB_PASSWORD_HASH', None)
        with self.assertRaises(RuntimeError):
            create_app(recordings_dir=self.rec, meetings_csv=self.csv)


if __name__ == '__main__':
    unittest.main()
