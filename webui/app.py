"""zoomrec web interface: recorder status, recordings library and schedule editor."""
import dataclasses
import hmac
import math
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timedelta

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from transcription import (SCRIBERR_RUNNING, delete_transcript, format_timestamp, read_state,
                           read_transcript, search_segments, transcript_path, vtt_path, write_state)

from zoomrec_core import (ERROR_SCREENSHOT_NAME, PREVIEW_FILE_NAME, STATUS_DIR_NAME, WEEKDAYS,
                          MeetingError,
                          mp4_duration, next_occurrence, parse_meeting, read_meetings_file,
                          read_status, thumbnail_path, write_meetings_file)

# Login lockout: after LOGIN_MAX_FAILURES wrong passwords within LOGIN_WINDOW seconds an IP
# address is blocked for LOGIN_WINDOW seconds
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 15 * 60

# Recorder writes its status about every 10 seconds
STATUS_STALE_SECONDS = 60
RECORDING_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*\.(mp4|mkv)$')
FILE_DATE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(.*)\.(mp4|mkv)$')
WEEKDAY_NAMES = {
    'monday': 'Montag', 'tuesday': 'Dienstag', 'wednesday': 'Mittwoch', 'thursday': 'Donnerstag',
    'friday': 'Freitag', 'saturday': 'Samstag', 'sunday': 'Sonntag',
}


def load_secret_key(config_dir):
    """Secret for session cookies, kept in the config directory so logins survive restarts."""
    key = os.getenv('WEB_SECRET_KEY')
    if key:
        return key
    path = os.path.join(config_dir, '.webui-secret')
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(key)
    except OSError:
        pass  # sessions are then only valid until restart
    return key


def format_size(num_bytes):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num_bytes < 1024 or unit == 'GB':
            return f"{num_bytes:.0f} {unit}" if unit in ('B', 'KB') else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024


def format_duration(seconds):
    if seconds is None:
        return '–'
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def highlight(text, query):
    """HTML-escaped text with all words of the query wrapped in <mark>."""
    result = str(escape(text))
    for word in sorted({w for w in query.split() if w}, key=len, reverse=True):
        result = re.sub('(' + re.escape(str(escape(word))) + ')', r'<mark>\1</mark>', result, flags=re.IGNORECASE)
    return Markup(result)


def format_total(seconds):
    """Total length of all recordings, e.g. '45 min' or '12,5 Stunden'."""
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    return f"{math.floor(seconds / 360) / 10:g} Stunden".replace('.', ',')


def course_hues(titles):
    """Well separated color hues for lectures (golden angle), stable for a given set of names."""
    return {title: round(210 + i * 137.508) % 360 for i, title in enumerate(sorted(set(titles)))}


def week_view(meetings, hues):
    """Blocks for a Monday-to-Friday (+ weekend if used) calendar of the schedule."""
    days = list(WEEKDAYS[:5]) + [d for d in WEEKDAYS[5:] if any(m.weekday == d for m in meetings)]
    starts = [int(m.time[:2]) * 60 + int(m.time[3:]) for m in meetings]
    ends = [s + m.duration for s, m in zip(starts, meetings)]
    first = min([8 * 60] + starts) // 60
    last = min(24, max([18 * 60] + ends) // 60 + (1 if max([18 * 60] + ends) % 60 else 0))
    span = (last - first) * 60
    blocks = []
    for index, (m, start, end) in enumerate(zip(meetings, starts, ends)):
        if m.weekday not in days:
            continue
        end = min(end, last * 60)
        blocks.append({
            'index': index, 'meeting': m, 'day': days.index(m.weekday),
            'top': (start - first * 60) * 100 / span, 'height': max(4.0, (end - start) * 100 / span),
            'end': f"{(start + m.duration) // 60 % 24:02d}:{(start + m.duration) % 60:02d}",
            'hue': hues.get(m.description.replace('_', ' '), 210),
        })
    return {'days': days, 'hours': list(range(first, last + 1)), 'blocks': blocks,
            'first': first, 'span_hours': last - first}


def create_app(recordings_dir=None, meetings_csv=None, password=None, password_hash=None,
               trust_proxy=None):
    recordings_dir = recordings_dir or os.getenv('RECORDINGS_DIR', '/data/recordings')
    meetings_csv = meetings_csv or os.getenv('MEETINGS_CSV', '/data/config/meetings.csv')
    status_dir = os.path.join(recordings_dir, STATUS_DIR_NAME)

    password_hash = password_hash or os.getenv('WEB_PASSWORD_HASH')
    password = password or os.getenv('WEB_PASSWORD')
    if not password_hash:
        if not password:
            raise RuntimeError("Set WEB_PASSWORD (or WEB_PASSWORD_HASH) for the web interface.")
        password_hash = generate_password_hash(password)
    os.environ.pop('WEB_PASSWORD', None)

    if trust_proxy is None:
        trust_proxy = os.getenv('TRUST_PROXY', 'false').lower() == 'true'

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=load_secret_key(os.path.dirname(meetings_csv)),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        # Behind a HTTPS reverse proxy the session cookie is only sent over HTTPS
        SESSION_COOKIE_SECURE=trust_proxy,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if trust_proxy:
        # Client address and scheme from X-Forwarded-For / X-Forwarded-Proto of one proxy
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    login_failures = {}
    duration_cache = {}

    # --- helpers -------------------------------------------------------------

    def recorder_status():
        status = read_status(status_dir) or {'state': 'unknown'}
        try:
            updated = datetime.fromisoformat(status.get('updated', ''))
            status['stale'] = (datetime.now() - updated).total_seconds() > STATUS_STALE_SECONDS
        except ValueError:
            status['stale'] = True
        usage = shutil.disk_usage(recordings_dir)
        status['disk_free'] = format_size(usage.free)
        status['disk_total'] = format_size(usage.total)
        status['disk_free_percent'] = round(usage.free * 100 / usage.total)
        status['preview'] = (status.get('state') in ('joining', 'recording')
                             and not status['stale']
                             and os.path.exists(os.path.join(status_dir, PREVIEW_FILE_NAME)))
        status['error_screenshot'] = bool(status.get('last_error_screenshot')) and os.path.exists(
            os.path.join(status_dir, ERROR_SCREENSHOT_NAME))
        return status

    def recording_path(name):
        if not RECORDING_NAME_RE.match(name):
            abort(404)
        path = os.path.join(recordings_dir, name)
        if not os.path.isfile(path):
            abort(404)
        return path

    def active_file():
        status = read_status(status_dir) or {}
        if status.get('state') in ('recording', 'finishing'):
            return status.get('file')
        return None

    def all_hues(recording_titles=()):
        meetings, _ = load_schedule()
        return course_hues([m.description.replace('_', ' ') for m in meetings] + list(recording_titles))

    def list_recordings():
        active = active_file()
        items = []
        for name in os.listdir(recordings_dir):
            if not RECORDING_NAME_RE.match(name):
                continue
            path = os.path.join(recordings_dir, name)
            stat = os.stat(path)
            match = FILE_DATE_RE.match(name)
            if match:
                date = datetime.strptime(match.group(1), '%Y-%m-%d_%H-%M-%S')
                title = match.group(2).replace('_', ' ')
            else:
                date = datetime.fromtimestamp(stat.st_mtime)
                title = name.rsplit('.', 1)[0]
            in_progress = name == active
            duration = None
            if name.endswith('.mp4') and not in_progress:
                key = (name, stat.st_mtime, stat.st_size)
                if key not in duration_cache:
                    duration_cache[key] = mp4_duration(path)
                duration = duration_cache[key]
            thumb = thumbnail_path(status_dir, name)
            items.append({
                'name': name, 'title': title, 'date': date, 'size': format_size(stat.st_size),
                'size_bytes': stat.st_size, 'duration': format_duration(duration),
                'duration_seconds': duration, 'in_progress': in_progress,
                'playable': name.endswith('.mp4') and not in_progress,
                'thumb_version': int(os.path.getmtime(thumb)) if os.path.exists(thumb) else None,
            })
        # The running recording is written locally by the recorder and not in the library yet
        if active and RECORDING_NAME_RE.match(active) and active not in {i['name'] for i in items}:
            match = FILE_DATE_RE.match(active)
            if match:
                items.append({
                    'name': active, 'title': match.group(2).replace('_', ' '),
                    'date': datetime.strptime(match.group(1), '%Y-%m-%d_%H-%M-%S'), 'size': '–',
                    'size_bytes': 0, 'duration': '–', 'duration_seconds': None, 'in_progress': True,
                    'playable': False, 'thumb_version': None,
                })
        hues = all_hues(i['title'] for i in items)
        transcripts = read_state(status_dir)
        for item in items:
            item['hue'] = hues[item['title']]
            item['transcript'] = transcript_state(item['name'], transcripts)
        items.sort(key=lambda i: i['date'], reverse=True)
        return items

    def transcript_state(name, state):
        if os.path.exists(transcript_path(status_dir, name)):
            return 'done'
        status = (state.get(name) or {}).get('status')
        if status in SCRIBERR_RUNNING:
            return 'running'
        return 'failed' if status == 'failed' else None

    transcript_cache = {}

    def cached_transcript(name):
        try:
            mtime = os.path.getmtime(transcript_path(status_dir, name))
        except OSError:
            return None
        cached = transcript_cache.get(name)
        if cached is None or cached[0] != mtime:
            cached = (mtime, read_transcript(status_dir, name))
            transcript_cache[name] = cached
        return cached[1]

    def load_schedule():
        try:
            return read_meetings_file(meetings_csv)
        except FileNotFoundError:
            return [], []

    # --- auth ----------------------------------------------------------------

    @app.before_request
    def require_login():
        if request.endpoint in ('login', 'static'):
            return None
        if not session.get('auth'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'login required'}), 401
            return redirect(url_for('login', next=request.path))
        if request.method == 'POST':
            token = request.form.get('csrf', '')
            if not hmac.compare_digest(token, session.get('csrf', '')):
                abort(400)
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        if trust_proxy:
            response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
        return response

    @app.context_processor
    def template_globals():
        return {'csrf_token': session.get('csrf', ''), 'weekday_names': WEEKDAY_NAMES}

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            now = time.time()
            client = request.remote_addr or '?'
            recent = [t for t in login_failures.get(client, []) if now - t < LOGIN_WINDOW]
            login_failures[client] = recent
            if len(recent) >= LOGIN_MAX_FAILURES:
                minutes = math.ceil((LOGIN_WINDOW - (now - recent[0])) / 60)
                return render_template('login.html', error=f'Zu viele Fehlversuche. Bitte in {minutes} Minuten erneut versuchen.'), 429
            if check_password_hash(password_hash, request.form.get('password', '')):
                login_failures.pop(client, None)
                session.clear()
                session.permanent = True
                session['auth'] = True
                session['csrf'] = secrets.token_hex(16)
                target = request.args.get('next', '')
                if not target.startswith('/') or target.startswith('//'):
                    target = url_for('index')
                return redirect(target)
            recent.append(now)
            time.sleep(1)  # slow down guessing
            error = 'Falsches Passwort.'
        return render_template('login.html', error=error)

    @app.route('/logout', methods=['POST'])
    def logout():
        session.clear()
        return redirect(url_for('login'))

    # --- status and library --------------------------------------------------

    @app.route('/')
    def index():
        recordings = list_recordings()
        courses = {}
        for r in recordings:
            courses[r['title']] = courses.get(r['title'], 0) + 1
        stats = {
            'count': len(recordings),
            'size': format_size(sum(r['size_bytes'] for r in recordings)),
            'total': format_total(sum(r['duration_seconds'] or 0 for r in recordings)),
        }
        return render_template('index.html', status=recorder_status(), recordings=recordings,
                               courses=sorted(courses.items()), stats=stats)

    @app.route('/api/status')
    def api_status():
        return jsonify(recorder_status())

    def status_image(file_name):
        path = os.path.join(status_dir, file_name)
        if not os.path.isfile(path):
            abort(404)
        response = send_file(path, mimetype='image/jpeg', max_age=0)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/preview.jpg')
    def preview():
        return status_image(PREVIEW_FILE_NAME)

    @app.route('/last-error.jpg')
    def error_screenshot():
        return status_image(ERROR_SCREENSHOT_NAME)

    @app.route('/thumbs/<name>')
    def thumbnail(name):
        if not RECORDING_NAME_RE.match(name):
            abort(404)
        path = thumbnail_path(status_dir, name)
        if not os.path.isfile(path):
            abort(404)
        # URLs carry ?v=<mtime>, so the image can be cached
        return send_file(path, mimetype='image/jpeg', max_age=86400)

    @app.route('/watch/<name>')
    def watch(name):
        recording_path(name)
        recordings = list_recordings()
        item = next((r for r in recordings if r['name'] == name), None)
        if item is None or not item['playable']:
            abort(404)
        # Other recordings of the same lecture, oldest first
        series = sorted((r for r in recordings if r['title'] == item['title'] and r['playable']),
                        key=lambda r: r['date'])
        position = next(i for i, r in enumerate(series) if r['name'] == name)
        previous = series[position - 1] if position > 0 else None
        following = series[position + 1] if position + 1 < len(series) else None
        transcript = cached_transcript(name) if item['transcript'] == 'done' else None
        error = (read_state(status_dir).get(name) or {}).get('error') if item['transcript'] == 'failed' else None
        try:
            start = max(0.0, float(request.args.get('t', 0)))
        except ValueError:
            start = 0.0
        return render_template('watch.html', item=item, series=series, position=position + 1,
                               previous=previous, following=following, transcript=transcript,
                               transcript_error=error, start=start, timestamp=format_timestamp)

    @app.route('/transcripts/<name>.vtt')
    def subtitles(name):
        recording_path(name)
        path = vtt_path(status_dir, name)
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, mimetype='text/vtt', max_age=0)

    @app.route('/recordings/<name>/transcribe', methods=['POST'])
    def retranscribe(name):
        recording_path(name)
        delete_transcript(status_dir, name)
        state = read_state(status_dir)
        state.pop(name, None)
        write_state(status_dir, state)
        flash('Transkription wird neu gestartet.', 'ok')
        return redirect(request.form.get('next') or url_for('watch', name=name))

    @app.route('/search')
    def search():
        query = request.args.get('q', '').strip()
        results = []
        total = 0
        recordings = [r for r in list_recordings() if r['playable']]
        transcribed = sum(1 for r in recordings if r['transcript'] == 'done')
        if query:
            for r in recordings:
                transcript = cached_transcript(r['name']) if r['transcript'] == 'done' else None
                if not transcript:
                    continue
                matches = search_segments(transcript['segments'], query)
                if matches:
                    total += len(matches)
                    results.append({'recording': r, 'matches': [
                        {'start': m['start'], 'time': format_timestamp(m['start']),
                         'text': highlight(m['text'], query)} for m in matches[:50]],
                        'more': max(0, len(matches) - 50)})
        return render_template('search.html', query=query, results=results, total=total,
                               transcribed=transcribed, count=len(recordings))

    @app.route('/recordings/<name>')
    def stream(name):
        # conditional=True answers HTTP Range requests, needed for seeking in the browser
        return send_file(recording_path(name), conditional=True, max_age=0)

    @app.route('/recordings/<name>/download')
    def download(name):
        return send_file(recording_path(name), as_attachment=True, download_name=name)

    @app.route('/recordings/<name>/delete', methods=['POST'])
    def delete_recording(name):
        path = recording_path(name)
        if name == active_file():
            flash('Diese Aufnahme läuft gerade und kann nicht gelöscht werden.', 'error')
        else:
            os.remove(path)
            try:
                os.remove(thumbnail_path(status_dir, name))
            except OSError:
                pass
            delete_transcript(status_dir, name)
            flash('Aufnahme gelöscht.', 'ok')
        return redirect(url_for('index'))

    # --- schedule ------------------------------------------------------------

    @app.route('/schedule')
    def schedule_view():
        meetings, errors = load_schedule()
        now = datetime.now()
        recording_titles = [m.group(2).replace('_', ' ') for m in map(FILE_DATE_RE.match, os.listdir(recordings_dir)) if m]
        hues = all_hues(recording_titles)
        rows = [{'index': i, 'meeting': m, 'next': next_occurrence(m, now) if m.record else None,
                 'hue': hues[m.description.replace('_', ' ')]}
                for i, m in enumerate(meetings)]
        rows.sort(key=lambda r: (WEEKDAYS.index(r['meeting'].weekday), r['meeting'].time))
        today = WEEKDAYS[now.weekday()]
        return render_template('schedule.html', rows=rows, errors=errors, week=week_view(meetings, hues),
                               today=today)

    def meeting_form(index, values, error=None):
        return render_template('edit.html', index=index, values=values, error=error,
                               weekdays=WEEKDAYS)

    @app.route('/schedule/new')
    def schedule_new():
        return meeting_form(None, {'weekday': 'monday', 'time': '', 'duration': '90', 'id': '',
                                   'password': '', 'description': '', 'record': 'true'})

    @app.route('/schedule/<int:index>/edit')
    def schedule_edit(index):
        meetings, _ = load_schedule()
        if index >= len(meetings):
            abort(404)
        m = meetings[index]
        return meeting_form(index, {'weekday': m.weekday, 'time': m.time, 'duration': str(m.duration),
                                    'id': m.id, 'password': m.password, 'description': m.description,
                                    'record': 'true' if m.record else 'false'})

    @app.route('/schedule/save', methods=['POST'])
    def schedule_save():
        values = {key: request.form.get(key, '').strip() for key in
                  ('weekday', 'time', 'duration', 'id', 'password', 'description')}
        values['record'] = 'true' if request.form.get('record') else 'false'
        index = request.form.get('index', '')
        index = int(index) if index.isdigit() else None
        try:
            meeting = parse_meeting(values)
        except MeetingError as ex:
            return meeting_form(index, values, error=str(ex)), 400
        meetings, _ = load_schedule()
        if index is None:
            meetings.append(meeting)
        elif index < len(meetings):
            meetings[index] = meeting
        else:
            abort(404)
        write_meetings_file(meetings_csv, meetings)
        flash(f'„{meeting.description}“ gespeichert.', 'ok')
        return redirect(url_for('schedule_view'))

    @app.route('/schedule/<int:index>/toggle', methods=['POST'])
    def schedule_toggle(index):
        meetings, _ = load_schedule()
        if index >= len(meetings):
            abort(404)
        m = meetings[index]
        meetings[index] = dataclasses.replace(m, record=not m.record)
        write_meetings_file(meetings_csv, meetings)
        return redirect(url_for('schedule_view'))

    @app.route('/schedule/<int:index>/delete', methods=['POST'])
    def schedule_delete(index):
        meetings, _ = load_schedule()
        if index >= len(meetings):
            abort(404)
        removed = meetings.pop(index)
        write_meetings_file(meetings_csv, meetings)
        flash(f'„{removed.description}“ gelöscht.', 'ok')
        return redirect(url_for('schedule_view'))

    return app

