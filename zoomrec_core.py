"""Display-independent helpers of zoomrec: schedule parsing, join URLs, window lists
and FFmpeg commands.

Kept free of pyautogui/X11 imports so it can be unit tested without a display.
"""
import csv
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode, urlparse

CSV_DELIMITER = ';'

WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')

TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
MAX_DURATION_MINUTES = 24 * 60
MAX_DESCRIPTION_LENGTH = 80


class MeetingError(ValueError):
    """Raised for an invalid row in the meetings CSV."""


@dataclass(frozen=True)
class Meeting:
    weekday: str
    time: str  # HH:MM
    duration: int  # minutes
    id: str  # meeting ID or join URL
    password: str
    description: str  # safe for use in file names
    record: bool


def sanitize_description(text):
    """Turn a free-text description into a safe file name component."""
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', text.strip())
    safe = safe.strip('._-')[:MAX_DESCRIPTION_LENGTH]
    return safe or 'meeting'


def parse_meeting(row):
    """Validate one CSV row (dict) and return a Meeting. Raises MeetingError."""
    def field(name):
        value = row.get(name)
        return '' if value is None else str(value).strip()

    weekday = field('weekday').lower()
    if weekday not in WEEKDAYS:
        raise MeetingError(f"invalid weekday '{field('weekday')}'")

    start = field('time')
    if not TIME_RE.match(start):
        raise MeetingError(f"invalid time '{start}', expected HH:MM")

    try:
        duration = int(field('duration'))
    except ValueError:
        raise MeetingError(f"invalid duration '{field('duration')}', expected minutes") from None
    if not 0 < duration <= MAX_DURATION_MINUTES:
        raise MeetingError(f"duration {duration} out of range")

    meet_id = field('id')
    if meet_id.startswith(('https://', 'http://')):
        if any(c.isspace() or c in '"\'`' for c in meet_id):
            raise MeetingError("invalid characters in meeting URL")
    else:
        meet_id = meet_id.replace(' ', '')
        if not meet_id.isdigit():
            raise MeetingError(f"invalid meeting ID '{meet_id}'")

    password = field('password')
    if any(c in password for c in '\r\n'):
        raise MeetingError("invalid characters in password")

    record = field('record').lower()
    if record not in ('true', 'false'):
        raise MeetingError(f"invalid record value '{field('record')}', expected true/false")

    return Meeting(weekday=weekday, time=start, duration=duration, id=meet_id,
                   password=password, description=sanitize_description(field('description')),
                   record=record == 'true')


def load_meetings(path):
    """Read the meetings CSV. Invalid rows are logged and skipped."""
    meetings = []
    with open(path, mode='r', newline='', encoding='utf-8') as csv_file:
        reader = csv.DictReader(csv_file, delimiter=CSV_DELIMITER)
        for row in reader:
            if not any((v or '').strip() for v in row.values() if isinstance(v, str)):
                continue  # line with only delimiters
            try:
                meetings.append(parse_meeting(row))
            except MeetingError as ex:
                logging.error("meetings.csv line %d skipped: %s", reader.line_num, ex)
    return meetings


@dataclass(frozen=True)
class RecordingSettings:
    fps: int = 30
    crf: int = 24
    preset: str = 'veryfast'
    audio_bitrate: str = '192k'
    threads: int = 2
    audio_source: str = 'speaker.monitor'

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        default = cls()
        return cls(
            fps=int(env.get('RECORD_FPS', default.fps)),
            crf=int(env.get('RECORD_CRF', default.crf)),
            preset=env.get('RECORD_PRESET', default.preset),
            audio_bitrate=env.get('RECORD_AUDIO_BITRATE', default.audio_bitrate),
            threads=int(env.get('RECORD_THREADS', default.threads)),
        )


def build_record_command(display, resolution, output, settings=RecordingSettings()):
    """FFmpeg command recording screen and meeting audio to a crash-safe .mkv."""
    return [
        'ffmpeg', '-nostats', '-loglevel', 'error', '-y',
        '-thread_queue_size', '1024',
        '-f', 'pulse', '-sample_rate', '48000', '-ac', '2', '-i', settings.audio_source,
        '-thread_queue_size', '1024',
        '-f', 'x11grab', '-draw_mouse', '0',
        '-framerate', str(settings.fps), '-video_size', resolution, '-i', display,
        '-map', '1:v', '-map', '0:a',
        '-c:v', 'libx264', '-preset', settings.preset, '-crf', str(settings.crf),
        '-pix_fmt', 'yuv420p', '-g', str(settings.fps * 10),
        '-threads', str(settings.threads),
        '-c:a', 'aac', '-b:a', settings.audio_bitrate, '-ar', '48000', '-af', 'aresample=async=1',
        output,
    ]


def build_remux_command(source, target):
    """FFmpeg command converting the finished .mkv into a seekable .mp4 without re-encoding."""
    return [
        'ffmpeg', '-nostats', '-loglevel', 'error', '-y',
        '-i', source, '-map', '0', '-c', 'copy', '-movflags', '+faststart', target,
    ]


def build_join_url(meet_id, password='', name=''):
    """Build a zoommtg:// URL that joins a meeting directly, as guest with the given name.

    meet_id is either a numeric meeting ID or a https join link like
    https://tuwien.zoom.us/j/123456789?pwd=abc. Returns None for links without a
    meeting number (e.g. personal /my/ links); those are passed to Zoom unchanged.
    """
    if meet_id.startswith(('https://', 'http://')):
        parsed = urlparse(meet_id)
        match = re.search(r'/(?:j|w|s|wc/join)/(\d+)', parsed.path)
        if not match:
            return None
        host = parsed.netloc
        confno = match.group(1)
        password = parse_qs(parsed.query).get('pwd', [password])[0]
    else:
        host = 'zoom.us'
        confno = meet_id

    params = {'action': 'join', 'confno': confno}
    if password:
        params['pwd'] = password
    if name:
        params['uname'] = name
    return f"zoommtg://{host}/join?{urlencode(params, quote_via=quote)}"


@dataclass(frozen=True)
class Window:
    id: str
    wm_class: str
    title: str


def parse_wmctrl(output):
    """Parse the output of `wmctrl -lx` into Window objects."""
    windows = []
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        title = parts[4] if len(parts) == 5 else ''
        windows.append(Window(id=parts[0], wm_class=parts[2], title=title))
    return windows


def find_meeting_window(windows):
    """The Zoom meeting window is titled "Meeting" (Zoom 7)."""
    for window in windows:
        if window.wm_class == 'zoom.zoom' and window.title == 'Meeting':
            return window
    return None


def set_ini_option(text, section, key, value):
    """Set key=value in a section of an INI text (as written by Qt), keeping everything else."""
    lines = text.splitlines()
    header = f'[{section}]'
    in_section = False
    insert_at = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            if in_section:
                break
            in_section = stripped == header
            if in_section:
                insert_at = i + 1
            continue
        if in_section:
            if stripped.split('=', 1)[0] == key:
                lines[i] = f'{key}={value}'
                return '\n'.join(lines) + '\n'
            if stripped:
                insert_at = i + 1
    if insert_at is None:
        lines = [header, f'{key}={value}', ''] + lines
    else:
        lines.insert(insert_at, f'{key}={value}')
    return '\n'.join(lines) + '\n'


# Zoom settings enforced before Zoom starts
ZOOM_OPTIONS = {
    # Zoom is stopped with a signal after each meeting, which it reports as crash on the next start
    'hideCrashReport': 'true',
}


def apply_zoom_options(conf_path):
    """Write ZOOM_OPTIONS into Zoom's zoomus.conf."""
    try:
        with open(conf_path) as f:
            text = f.read()
        for key, value in ZOOM_OPTIONS.items():
            text = set_ini_option(text, 'General', key, value)
        with open(conf_path, 'w') as f:
            f.write(text)
    except OSError as ex:
        logging.error("Could not update Zoom configuration: " + str(ex))
