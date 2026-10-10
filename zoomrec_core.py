"""Display-independent helpers of zoomrec: schedule parsing and FFmpeg commands.

Kept free of pyautogui/X11 imports so it can be unit tested without a display.
"""
import csv
import logging
import os
import re
from dataclasses import dataclass

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
    fps: int = 15
    crf: int = 28
    preset: str = 'veryfast'
    audio_bitrate: str = '128k'
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
        '-f', 'pulse', '-ac', '2', '-i', settings.audio_source,
        '-thread_queue_size', '1024',
        '-f', 'x11grab', '-draw_mouse', '0',
        '-framerate', str(settings.fps), '-video_size', resolution, '-i', display,
        '-map', '1:v', '-map', '0:a',
        '-c:v', 'libx264', '-preset', settings.preset, '-crf', str(settings.crf),
        '-pix_fmt', 'yuv420p', '-g', str(settings.fps * 10),
        '-threads', str(settings.threads),
        '-c:a', 'aac', '-b:a', settings.audio_bitrate, '-af', 'aresample=async=1',
        output,
    ]


def build_remux_command(source, target):
    """FFmpeg command converting the finished .mkv into a seekable .mp4 without re-encoding."""
    return [
        'ffmpeg', '-nostats', '-loglevel', 'error', '-y',
        '-i', source, '-map', '0', '-c', 'copy', '-movflags', '+faststart', target,
    ]
