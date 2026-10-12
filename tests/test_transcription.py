import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from transcription import (ScriberrClient, ScriberrError, TranscriptionSettings,  # noqa: E402
                           TranscriptionWorker, delete_transcript, format_timestamp,
                           normalize_transcript, read_state, read_transcript, search_segments,
                           to_vtt, transcript_path, vtt_path)

TRANSCRIPT = {'segments': [
    {'start': 0.5, 'end': 4.25, 'text': ' Willkommen zur Vorlesung. ', 'speaker': None},
    {'start': 4.25, 'end': 9.0, 'text': 'Heute geht es um Eigenwerte und Eigenvektoren.'},
    {'start': 9.0, 'end': 9.5, 'text': '  '},
    {'start': 3725.0, 'end': 3730.0, 'text': 'Der Eigenwert ist prüfungsrelevant.'},
], 'language': 'de'}


class FakeScriberr(BaseHTTPRequestHandler):
    """Minimal Scriberr v1 API: submit, status, transcript."""
    jobs = {}
    requests = []
    api_key = 'test-key'

    def log_message(self, *args):
        pass

    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if self.headers.get('X-API-Key') != self.api_key:
            self.reply(401, {'error': 'unauthorized'})
            return False
        return True

    def do_POST(self):
        if not self.authorized():
            return
        body = self.rfile.read(int(self.headers['Content-Length']))
        FakeScriberr.requests.append((self.path, self.headers.get('Content-Type'), body))
        if self.path != '/api/v1/transcription/submit' or b'name="audio"' not in body:
            return self.reply(400, {'error': 'Audio file is required'})
        job_id = f'job{len(self.jobs) + 1}'
        self.jobs[job_id] = 'pending'
        self.reply(200, {'id': job_id, 'status': 'pending', 'title': 'x'})

    def do_GET(self):
        if not self.authorized():
            return
        parts = self.path.strip('/').split('/')  # api v1 transcription <id> <what>
        job_id, what = parts[3], parts[4]
        status = self.jobs.get(job_id)
        if status is None:
            return self.reply(404, {'error': 'Job not found'})
        if what == 'status':
            return self.reply(200, {'id': job_id, 'status': status,
                                    'error_message': 'GPU out of memory' if status == 'failed' else None})
        if status != 'completed':
            return self.reply(200, {'job_id': job_id, 'transcript': None, 'available': False})
        self.reply(200, {'job_id': job_id, 'transcript': TRANSCRIPT, 'status': 'completed', 'available': True})


class ScriberrTestBase(unittest.TestCase):
    def setUp(self):
        FakeScriberr.jobs = {}
        FakeScriberr.requests = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeScriberr)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.rec = os.path.join(self.tmp, 'recordings')
        self.status = os.path.join(self.rec, '.zoomrec')
        os.makedirs(self.status)


class ClientTest(ScriberrTestBase):
    def test_submit_sends_multipart_with_api_key(self):
        audio = os.path.join(self.tmp, 'a.mp3')
        with open(audio, 'wb') as f:
            f.write(b'ID3fakeaudio')
        job = ScriberrClient(self.url, 'test-key').submit(audio, 'VO Mathe', 'small', 'de', 'cpu', 'int8')
        self.assertEqual(job['id'], 'job1')
        path, content_type, body = FakeScriberr.requests[0]
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='))
        self.assertIn(b'name="language"\r\n\r\nde', body)
        self.assertIn(b'name="model"\r\n\r\nsmall', body)
        self.assertIn(b'filename="a.mp3"', body)
        self.assertIn(b'ID3fakeaudio', body)

    def test_wrong_key(self):
        with self.assertRaises(ScriberrError) as ctx:
            ScriberrClient(self.url, 'wrong').job('job1')
        self.assertIn('401', str(ctx.exception))

    def test_unreachable(self):
        with self.assertRaises(ScriberrError):
            ScriberrClient('http://127.0.0.1:1', 'k', timeout=2).job('x')


class FormatTest(unittest.TestCase):
    def test_normalize(self):
        result = normalize_transcript({'transcript': TRANSCRIPT})
        self.assertEqual(result['language'], 'de')
        self.assertEqual([s['text'] for s in result['segments']][0], 'Willkommen zur Vorlesung.')
        self.assertEqual(len(result['segments']), 3)  # empty segment dropped

    def test_normalize_string_and_missing(self):
        self.assertEqual(len(normalize_transcript({'transcript': json.dumps(TRANSCRIPT)})['segments']), 3)
        with self.assertRaises(ValueError):
            normalize_transcript({'transcript': None})

    def test_vtt(self):
        vtt = to_vtt([{'start': 3725.5, 'end': 3730.0, 'text': 'a --> b'}])
        self.assertTrue(vtt.startswith('WEBVTT\n\n'))
        self.assertIn('01:02:05.500 --> 01:02:10.000\na -> b', vtt)

    def test_timestamps(self):
        self.assertEqual(format_timestamp(65), '1:05')
        self.assertEqual(format_timestamp(3725), '1:02:05')

    def test_search(self):
        segments = normalize_transcript({'transcript': TRANSCRIPT})['segments']
        self.assertEqual(len(search_segments(segments, 'eigenwert')), 2)
        self.assertEqual(len(search_segments(segments, 'Eigenwert prüfung')), 1)
        self.assertEqual(search_segments(segments, '   '), [])


class WorkerTest(ScriberrTestBase):
    def make_recording(self, name):
        with open(os.path.join(self.rec, name), 'wb') as f:
            f.write(b'video')

    def worker(self, max_running=2, extract_ok=True):
        def extract(source, target):
            self.assertTrue(os.path.exists(source))
            if extract_ok:
                with open(target, 'wb') as f:
                    f.write(b'ID3audio')
            return extract_ok
        settings = TranscriptionSettings(url=self.url, api_key='test-key', max_running=max_running)
        return TranscriptionWorker(self.rec, self.status, ScriberrClient(self.url, 'test-key'), settings, extract)

    def test_full_cycle(self):
        self.make_recording('2026-10-12_09-54-00-VO_A.mp4')
        worker = self.worker()
        worker.step(force=True)
        state = read_state(self.status)
        self.assertEqual(state['2026-10-12_09-54-00-VO_A.mp4']['job_id'], 'job1')
        self.assertEqual(state['2026-10-12_09-54-00-VO_A.mp4']['status'], 'pending')
        # The temporary MP3 is removed after the upload
        self.assertEqual([n for n in os.listdir(os.path.join(self.status, 'transcripts')) if n.endswith('.mp3')], [])

        FakeScriberr.jobs['job1'] = 'processing'
        worker.step(force=True)
        self.assertEqual(read_state(self.status)['2026-10-12_09-54-00-VO_A.mp4']['status'], 'processing')

        FakeScriberr.jobs['job1'] = 'completed'
        worker.step(force=True)
        self.assertEqual(read_state(self.status)['2026-10-12_09-54-00-VO_A.mp4']['status'], 'done')
        transcript = read_transcript(self.status, '2026-10-12_09-54-00-VO_A.mp4')
        self.assertEqual(len(transcript['segments']), 3)
        self.assertTrue(os.path.exists(vtt_path(self.status, '2026-10-12_09-54-00-VO_A.mp4')))

        # Nothing is submitted again
        worker.step(force=True)
        self.assertEqual(len(FakeScriberr.requests), 1)

    def test_newest_first_and_limit(self):
        for day in ('01', '02', '03'):
            self.make_recording(f'2026-10-{day}_09-00-00-X.mp4')
        worker = self.worker(max_running=2)
        for _ in range(4):
            worker.step(force=True)
        state = read_state(self.status)
        self.assertEqual(sorted(state), ['2026-10-02_09-00-00-X.mp4', '2026-10-03_09-00-00-X.mp4'])
        self.assertEqual(state['2026-10-03_09-00-00-X.mp4']['job_id'], 'job1')

    def test_failed_job_and_failed_extraction(self):
        self.make_recording('2026-10-01_09-00-00-X.mp4')
        worker = self.worker()
        worker.step(force=True)
        FakeScriberr.jobs['job1'] = 'failed'
        worker.step(force=True)
        entry = read_state(self.status)['2026-10-01_09-00-00-X.mp4']
        self.assertEqual(entry['status'], 'failed')
        self.assertEqual(entry['error'], 'GPU out of memory')

        self.make_recording('2026-10-02_09-00-00-Y.mp4')
        self.worker(extract_ok=False).step(force=True)
        self.assertEqual(read_state(self.status)['2026-10-02_09-00-00-Y.mp4']['error'], 'Could not extract audio')

    def test_unreachable_scriberr_marks_failed(self):
        self.make_recording('2026-10-01_09-00-00-X.mp4')
        settings = TranscriptionSettings(url='http://127.0.0.1:1', api_key='k')
        worker = TranscriptionWorker(self.rec, self.status, ScriberrClient('http://127.0.0.1:1', 'k', timeout=2),
                                     settings, lambda s, t: open(t, 'wb').close() or True)
        worker.step(force=True)
        self.assertEqual(read_state(self.status)['2026-10-01_09-00-00-X.mp4']['status'], 'failed')

    def test_deleted_recording_is_forgotten(self):
        self.make_recording('2026-10-01_09-00-00-X.mp4')
        worker = self.worker()
        worker.step(force=True)
        os.remove(os.path.join(self.rec, '2026-10-01_09-00-00-X.mp4'))
        worker.step(force=True)
        self.assertEqual(read_state(self.status), {})

    def test_poll_interval(self):
        self.make_recording('2026-10-01_09-00-00-X.mp4')
        clock = [1000.0]
        worker = self.worker()
        worker.now = lambda: clock[0]
        worker.step()
        worker.step()  # too early, does nothing
        self.assertEqual(len(FakeScriberr.requests), 1)

    def test_delete_transcript(self):
        name = '2026-10-01_09-00-00-X.mp4'
        self.make_recording(name)
        worker = self.worker()
        worker.step(force=True)
        FakeScriberr.jobs['job1'] = 'completed'
        worker.step(force=True)
        delete_transcript(self.status, name)
        self.assertFalse(os.path.exists(transcript_path(self.status, name)))
        self.assertFalse(os.path.exists(vtt_path(self.status, name)))
        self.assertEqual(read_state(self.status), {})

    def test_settings_from_env(self):
        self.assertFalse(TranscriptionSettings.from_env({}).enabled)
        settings = TranscriptionSettings.from_env({'SCRIBERR_URL': 'https://t.example', 'SCRIBERR_API_KEY': 'k',
                                                   'SCRIBERR_MODEL': 'large-v3', 'SCRIBERR_DEVICE': 'cuda'})
        self.assertTrue(settings.enabled)
        self.assertEqual((settings.model, settings.device, settings.language), ('large-v3', 'cuda', 'de'))


if __name__ == '__main__':
    unittest.main()
