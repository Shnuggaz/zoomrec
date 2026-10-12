"""Transcription of recordings with a self-hosted Scriberr instance (API v1).

Used by the recorder (submits audio, fetches transcripts) and the web interface
(reads transcripts, search, subtitles). Only the standard library is used.
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime

TRANSCRIPTS_DIR_NAME = 'transcripts'
STATE_FILE_NAME = 'state.json'

# Job states as reported by Scriberr
SCRIBERR_RUNNING = ('uploaded', 'pending', 'processing')


def transcripts_dir(status_dir):
    return os.path.join(status_dir, TRANSCRIPTS_DIR_NAME)


def transcript_path(status_dir, recording_name):
    return os.path.join(transcripts_dir(status_dir), recording_name + '.json')


def vtt_path(status_dir, recording_name):
    return os.path.join(transcripts_dir(status_dir), recording_name + '.vtt')


def build_audio_extract_command(source, target):
    """Mono 16 kHz MP3 of the recording: small upload, enough for speech recognition."""
    return ['ffmpeg', '-nostats', '-loglevel', 'error', '-y', '-i', source,
            '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '48k', target]


# --- Transcript format -----------------------------------------------------------

def normalize_transcript(payload):
    """Scriberr transcript response -> {'language': str, 'segments': [{start, end, text}]}."""
    transcript = payload.get('transcript') if isinstance(payload, dict) else None
    if isinstance(transcript, str):
        transcript = json.loads(transcript)
    if not isinstance(transcript, dict):
        raise ValueError('response contains no transcript')
    segments = []
    for segment in transcript.get('segments') or []:
        text = (segment.get('text') or '').strip()
        if not text:
            continue
        segments.append({'start': round(float(segment['start']), 2),
                         'end': round(float(segment['end']), 2), 'text': text})
    return {'language': transcript.get('language') or '', 'segments': segments}


def format_timestamp(seconds, vtt=False):
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    if vtt:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def to_vtt(segments):
    lines = ['WEBVTT', '']
    for segment in segments:
        lines.append(f"{format_timestamp(segment['start'], True)} --> {format_timestamp(segment['end'], True)}")
        lines.append(segment['text'].replace('-->', '->'))
        lines.append('')
    return '\n'.join(lines)


def search_segments(segments, query):
    """Segments containing all words of the query (case-insensitive)."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    return [s for s in segments if all(w in s['text'].lower() for w in words)]


def read_transcript(status_dir, recording_name):
    try:
        with open(transcript_path(status_dir, recording_name), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def read_state(status_dir):
    try:
        with open(os.path.join(transcripts_dir(status_dir), STATE_FILE_NAME), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(status_dir, state):
    write_json_atomic(os.path.join(transcripts_dir(status_dir), STATE_FILE_NAME), state)


def delete_transcript(status_dir, recording_name):
    for path in (transcript_path(status_dir, recording_name), vtt_path(status_dir, recording_name)):
        try:
            os.remove(path)
        except OSError:
            pass
    state = read_state(status_dir)
    if state.pop(recording_name, None) is not None:
        write_state(status_dir, state)


# --- Scriberr API --------------------------------------------------------------------

class ScriberrError(Exception):
    pass


class ScriberrClient:
    def __init__(self, base_url, api_key, timeout=60):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method, path, body=None, content_type=None, timeout=None):
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        request.add_header('X-API-Key', self.api_key)
        request.add_header('Accept', 'application/json')
        if content_type:
            request.add_header('Content-Type', content_type)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode('utf-8') or 'null')
        except urllib.error.HTTPError as ex:
            detail = ex.read().decode('utf-8', errors='replace')[:300]
            raise ScriberrError(f"HTTP {ex.code} for {method} {path}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as ex:
            raise ScriberrError(f"{method} {path} failed: {ex}") from None

    def submit(self, audio_path, title, model, language, device, compute_type):
        boundary = uuid.uuid4().hex
        fields = {'title': title, 'model': model, 'language': language, 'device': device,
                  'compute_type': compute_type, 'diarization': 'false'}
        parts = []
        for name, value in fields.items():
            if value:
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        with open(audio_path, 'rb') as f:
            audio = f.read()
        filename = os.path.basename(audio_path)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="{filename}"\r\n'
                     f'Content-Type: audio/mpeg\r\n\r\n'.encode() + audio + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        job = self._request('POST', '/api/v1/transcription/submit', b''.join(parts),
                            f'multipart/form-data; boundary={boundary}', timeout=600)
        if not isinstance(job, dict) or not job.get('id'):
            raise ScriberrError(f"unexpected submit response: {str(job)[:200]}")
        return job

    def job(self, job_id):
        return self._request('GET', f'/api/v1/transcription/{job_id}/status')

    def transcript(self, job_id):
        return self._request('GET', f'/api/v1/transcription/{job_id}/transcript')


# --- Worker (runs in the recorder) ------------------------------------------------------

@dataclass(frozen=True)
class TranscriptionSettings:
    url: str = ''
    api_key: str = ''
    model: str = 'small'
    language: str = 'de'
    device: str = 'cpu'
    compute_type: str = 'int8'
    max_running: int = 2
    poll_interval: int = 60

    @property
    def enabled(self):
        return bool(self.url and self.api_key)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        default = cls()
        return cls(
            url=env.get('SCRIBERR_URL', '').strip(),
            api_key=env.get('SCRIBERR_API_KEY', '').strip(),
            model=env.get('SCRIBERR_MODEL') or default.model,
            language=env.get('SCRIBERR_LANGUAGE') or default.language,
            device=env.get('SCRIBERR_DEVICE') or default.device,
            compute_type=env.get('SCRIBERR_COMPUTE_TYPE') or default.compute_type,
        )


class TranscriptionWorker:
    """Submits recordings without transcript to Scriberr and stores finished transcripts.

    step() does a small amount of work and returns; call it regularly while no meeting is
    recorded. extract_audio(source, target) -> bool creates the MP3 for upload.
    """

    def __init__(self, recordings_dir, status_dir, client, settings, extract_audio, now=time.time):
        self.recordings_dir = recordings_dir
        self.status_dir = status_dir
        self.client = client
        self.settings = settings
        self.extract_audio = extract_audio
        self.now = now
        self.last_step = 0

    def recordings(self):
        try:
            names = [n for n in os.listdir(self.recordings_dir) if n.endswith('.mp4') and not n.startswith('.')]
        except OSError:
            return []
        return sorted(names, reverse=True)  # newest first (names start with the date)

    def step(self, force=False):
        if not force and self.now() - self.last_step < self.settings.poll_interval:
            return
        self.last_step = self.now()
        state = read_state(self.status_dir)
        changed = False
        existing = set(self.recordings())

        # Forget jobs of deleted recordings
        for name in [n for n in state if n not in existing]:
            del state[name]
            changed = True

        # Poll running jobs
        running = 0
        for name, entry in state.items():
            if entry.get('status') not in SCRIBERR_RUNNING:
                continue
            try:
                job = self.client.job(entry['job_id'])
                status = (job or {}).get('status', '')
                if status == 'completed':
                    self.store(name, self.client.transcript(entry['job_id']))
                    entry.update(status='done', error=None)
                    logging.info("Transcript of %s saved" % name)
                elif status == 'failed':
                    entry.update(status='failed', error=(job or {}).get('error_message') or 'Scriberr job failed')
                    logging.error("Transcription of %s failed: %s" % (name, entry['error']))
                elif status:
                    entry['status'] = status
                    running += 1
                else:
                    running += 1
            except (ScriberrError, ValueError, KeyError) as ex:
                logging.error("Could not check transcription of %s: %s" % (name, ex))
                running += 1
            entry['updated'] = datetime.now().isoformat(timespec='seconds')
            changed = True

        # Submit the newest recording without transcript
        if running < self.settings.max_running:
            for name in self.recordings():
                if name in state or os.path.exists(transcript_path(self.status_dir, name)):
                    continue
                state[name] = self.submit(name)
                changed = True
                break

        if changed:
            write_state(self.status_dir, state)

    def submit(self, name):
        entry = {'updated': datetime.now().isoformat(timespec='seconds')}
        audio = os.path.join(self.status_dir, TRANSCRIPTS_DIR_NAME, name + '.upload.mp3')
        os.makedirs(os.path.dirname(audio), exist_ok=True)
        try:
            if not self.extract_audio(os.path.join(self.recordings_dir, name), audio):
                return {**entry, 'status': 'failed', 'error': 'Could not extract audio'}
            title = name.rsplit('.', 1)[0]
            job = self.client.submit(audio, title, self.settings.model, self.settings.language,
                                     self.settings.device, self.settings.compute_type)
            logging.info("Submitted %s to Scriberr (job %s)" % (name, job['id']))
            return {**entry, 'status': job.get('status') or 'pending', 'job_id': job['id'], 'error': None}
        except ScriberrError as ex:
            logging.error("Could not submit %s to Scriberr: %s" % (name, ex))
            return {**entry, 'status': 'failed', 'error': str(ex)}
        finally:
            try:
                os.remove(audio)
            except OSError:
                pass

    def store(self, name, payload):
        transcript = normalize_transcript(payload)
        write_json_atomic(transcript_path(self.status_dir, name), transcript)
        tmp = vtt_path(self.status_dir, name) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(to_vtt(transcript['segments']))
        os.replace(tmp, vtt_path(self.status_dir, name))
