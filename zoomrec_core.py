"""Display-independent helpers of zoomrec: schedule parsing, join URLs, window lists
and FFmpeg commands.

Kept free of pyautogui/X11 imports so it can be unit tested without a display.
"""
import csv
import json
import logging
import os
import re
import struct
from dataclasses import dataclass
from datetime import timedelta
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


CSV_COLUMNS = ('weekday', 'time', 'duration', 'id', 'password', 'description', 'record')


def read_meetings_file(path):
    """Read the meetings CSV. Returns (meetings, errors) with errors as (line number, message)."""
    meetings = []
    errors = []
    with open(path, mode='r', newline='', encoding='utf-8') as csv_file:
        reader = csv.DictReader(csv_file, delimiter=CSV_DELIMITER)
        for row in reader:
            if not any((v or '').strip() for v in row.values() if isinstance(v, str)):
                continue  # line with only delimiters
            try:
                meetings.append(parse_meeting(row))
            except MeetingError as ex:
                errors.append((reader.line_num, str(ex)))
    return meetings, errors


def load_meetings(path):
    """Read the meetings CSV. Invalid rows are logged and skipped."""
    meetings, errors = read_meetings_file(path)
    for line_no, message in errors:
        logging.error("meetings.csv line %d skipped: %s", line_no, message)
    return meetings


def write_meetings_file(path, meetings):
    """Write meetings atomically (temporary file in the same directory, then rename)."""
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f, delimiter=CSV_DELIMITER, lineterminator='\n')
        writer.writerow(CSV_COLUMNS)
        for m in meetings:
            writer.writerow([m.weekday, m.time, m.duration, m.id, m.password, m.description,
                             'true' if m.record else 'false'])
    os.replace(tmp, path)


def next_occurrence(meeting, now):
    """Next start datetime of a weekly meeting at or after now (naive local time)."""
    hour, minute = map(int, meeting.time.split(':'))
    days_ahead = (WEEKDAYS.index(meeting.weekday) - now.weekday()) % 7
    start = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start < now:
        start += timedelta(days=7)
    return start


# Status shared between recorder and web interface, stored next to the recordings
STATUS_DIR_NAME = '.zoomrec'
STATUS_FILE_NAME = 'status.json'
PREVIEW_FILE_NAME = 'preview.jpg'


def write_status(status_dir, status):
    """Write the recorder status atomically as JSON."""
    os.makedirs(status_dir, exist_ok=True)
    path = os.path.join(status_dir, STATUS_FILE_NAME)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(status, f)
    os.replace(tmp, path)


def read_status(status_dir):
    try:
        with open(os.path.join(status_dir, STATUS_FILE_NAME), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def mp4_duration(path):
    """Duration in seconds from the mvhd box of an MP4 file, None if it cannot be read."""
    def boxes(f, start, end):
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            header = f.read(8)
            if len(header) < 8:
                return
            size, kind = struct.unpack('>I4s', header)
            header_size = 8
            if size == 1:
                size = struct.unpack('>Q', f.read(8))[0]
                header_size = 16
            elif size == 0:
                size = end - pos
            if size < header_size:
                return
            yield kind, pos + header_size, pos + size
            pos += size

    try:
        with open(path, 'rb') as f:
            file_end = os.fstat(f.fileno()).st_size
            for kind, body, box_end in boxes(f, 0, file_end):
                if kind != b'moov':
                    continue
                for inner_kind, inner_body, _ in boxes(f, body, box_end):
                    if inner_kind != b'mvhd':
                        continue
                    f.seek(inner_body)
                    version = f.read(1)[0]
                    f.read(3)  # flags
                    if version == 1:
                        _, _, timescale, duration = struct.unpack('>QQIQ', f.read(28))
                    else:
                        _, _, timescale, duration = struct.unpack('>IIII', f.read(16))
                    return duration / timescale if timescale else None
    except (OSError, struct.error, IndexError):
        return None
    return None


@dataclass(frozen=True)
class RecordingSettings:
    fps: int = 15
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
