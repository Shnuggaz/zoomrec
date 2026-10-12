"""zoomrec web interface: recorder status, recordings library and schedule editor."""
import dataclasses
import hmac
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timedelta

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from zoomrec_core import (PREVIEW_FILE_NAME, STATUS_DIR_NAME, WEEKDAYS, MeetingError,
                          mp4_duration, next_occurrence, parse_meeting, read_meetings_file,
                          read_status, write_meetings_file)

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


def create_app(recordings_dir=None, meetings_csv=None, password=None, password_hash=None):
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

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=load_secret_key(os.path.dirname(meetings_csv)),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
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
        status['disk_free_percent'] = round(usage.free * 100 / usage.total)
        status['preview'] = (status.get('state') in ('joining', 'recording')
                             and not status['stale']
                             and os.path.exists(os.path.join(status_dir, PREVIEW_FILE_NAME)))
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
            items.append({
                'name': name, 'title': title, 'date': date, 'size': format_size(stat.st_size),
                'duration': format_duration(duration), 'in_progress': in_progress,
                'playable': name.endswith('.mp4') and not in_progress,
            })
        items.sort(key=lambda i: i['date'], reverse=True)
        return items

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

    @app.context_processor
    def template_globals():
        return {'csrf_token': session.get('csrf', ''), 'weekday_names': WEEKDAY_NAMES}

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            if check_password_hash(password_hash, request.form.get('password', '')):
                session.clear()
                session.permanent = True
                session['auth'] = True
                session['csrf'] = secrets.token_hex(16)
                target = request.args.get('next', '')
                if not target.startswith('/') or target.startswith('//'):
                    target = url_for('index')
                return redirect(target)
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
        courses = sorted({r['title'] for r in recordings})
        return render_template('index.html', status=recorder_status(), recordings=recordings,
                               courses=courses)

    @app.route('/api/status')
    def api_status():
        return jsonify(recorder_status())

    @app.route('/preview.jpg')
    def preview():
        path = os.path.join(status_dir, PREVIEW_FILE_NAME)
        if not os.path.isfile(path):
            abort(404)
        response = send_file(path, mimetype='image/jpeg', max_age=0)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/watch/<name>')
    def watch(name):
        recording_path(name)
        item = next((r for r in list_recordings() if r['name'] == name), None)
        if item is None or not item['playable']:
            abort(404)
        return render_template('watch.html', item=item)

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
            flash(f'„{name}“ wurde gelöscht.', 'ok')
        return redirect(url_for('index'))

    # --- schedule ------------------------------------------------------------

    @app.route('/schedule')
    def schedule_view():
        meetings, errors = load_schedule()
        now = datetime.now()
        rows = [{'index': i, 'meeting': m, 'next': next_occurrence(m, now) if m.record else None}
                for i, m in enumerate(meetings)]
        return render_template('schedule.html', rows=rows, errors=errors)

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

