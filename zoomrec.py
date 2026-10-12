import atexit
import logging
import logging.handlers
import os
import random
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta

import psutil
import pyautogui
import requests
import schedule

from zoomrec_core import (ERROR_SCREENSHOT_NAME, PREVIEW_FILE_NAME, STATUS_DIR_NAME,
                          RecordingSettings,
                          apply_zoom_options, build_join_url, build_record_command,
                          build_remux_command, find_meeting_window, load_meetings,
                          parse_wmctrl, write_status)

logging.basicConfig(
    format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)

# Turn DEBUG on:
#   - screenshots on errors and periodically during the meeting
#   - record joining
#   - do not exit container on error
DEBUG = os.getenv('DEBUG') == 'True'

# Disable failsafe
pyautogui.FAILSAFE = False

# Get vars
BASE_PATH = os.getenv('HOME')
CSV_PATH = os.getenv("MEETINGS_CSV", os.path.join(BASE_PATH, "meetings.csv"))
REC_PATH = os.path.join(BASE_PATH, "recordings")
AUDIO_PATH = os.path.join(BASE_PATH, "audio")
DEBUG_PATH = os.path.join(REC_PATH, "screenshots")
# Status and live preview for the web interface
STATUS_DIR = os.path.join(REC_PATH, STATUS_DIR_NAME)
ZOOM_CONF = os.path.join(BASE_PATH, ".config", "zoomus.conf")
PROFILE_ZOOM_CONF = os.path.join(BASE_PATH, ".zoom", "zoomus.conf")

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TELEGRAM_RETRIES = 5

# Name shown to the other participants
DISPLAY_NAME = os.getenv('DISPLAY_NAME') or 'zoomrec'

TIME_FORMAT = "%Y-%m-%d_%H-%M-%S"

# Play a random .wav from AUDIO_PATH into the meeting after joining (off by default)
PLAY_AUDIO = os.getenv('PLAY_AUDIO', 'false').lower() == 'true'

RECORDING_SETTINGS = RecordingSettings.from_env()

# Extra time after the planned end before recording is stopped
END_GRACE_SECONDS = 300
# The meeting counts as ended when its window is gone for this long
WINDOW_GONE_SECONDS = 15
# Interval of debug screenshots during a meeting
DEBUG_SCREENSHOT_INTERVAL = 300
# Interval of the live preview image for the web interface during a meeting
PREVIEW_INTERVAL = 15
PREVIEW_WIDTH = 640
ERROR_SCREENSHOT_WIDTH = 1280


STATUS = {'state': 'idle'}


def next_meeting_info():
    jobs = [job for job in schedule.get_jobs() if job.next_run is not None]
    if not jobs:
        return {'next_meeting': None, 'next_meeting_at': None}
    job = min(jobs, key=lambda j: j.next_run)
    # Jobs start one minute before the lecture
    return {'next_meeting': job.job_func.keywords.get('description'),
            'next_meeting_at': (job.next_run + timedelta(minutes=1)).isoformat(timespec='seconds')}


def update_status(**fields):
    """Update the status file read by the web interface. Never raises."""
    STATUS.update(fields)
    STATUS.update(next_meeting_info())
    STATUS['updated'] = datetime.now().isoformat(timespec='seconds')
    try:
        write_status(STATUS_DIR, STATUS)
    except OSError as ex:
        logging.error("Could not write status: " + str(ex))


def report_error(message, screenshot=False):
    """Show an error in the web interface, optionally with a screenshot of the virtual screen."""
    logging.error(message)
    has_screenshot = screenshot and save_screenshot(ERROR_SCREENSHOT_NAME, ERROR_SCREENSHOT_WIDTH)
    update_status(last_error=message, last_error_at=datetime.now().isoformat(timespec='seconds'),
                  last_error_screenshot=bool(has_screenshot))


def save_screenshot(file_name, width):
    """Scaled JPEG screenshot of the virtual screen in the status directory."""
    try:
        image = pyautogui.screenshot()
        height = round(image.height * width / image.width)
        image = image.resize((width, height)).convert('RGB')
        os.makedirs(STATUS_DIR, exist_ok=True)
        path = os.path.join(STATUS_DIR, file_name)
        image.save(path + ".tmp", format='JPEG', quality=75)
        os.replace(path + ".tmp", path)
        return True
    except Exception as ex:
        logging.error("Could not save screenshot: " + str(ex))
        return False


def save_preview():
    save_screenshot(PREVIEW_FILE_NAME, PREVIEW_WIDTH)


def remove_preview():
    try:
        os.remove(os.path.join(STATUS_DIR, PREVIEW_FILE_NAME))
    except OSError:
        pass


def debug_screenshot(description, label):
    if not DEBUG:
        return
    try:
        os.makedirs(DEBUG_PATH, exist_ok=True)
        pyautogui.screenshot(os.path.join(
            DEBUG_PATH, time.strftime(TIME_FORMAT) + "-" + description + "_" + label + ".png"))
    except Exception as ex:
        logging.error("Could not take screenshot: " + str(ex))


def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or len(TELEGRAM_TOKEN) < 3 or len(TELEGRAM_CHAT_ID) < 3:
        logging.debug("Telegram token or chat_id missing, no Telegram message sent.")
        return

    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    for tries in range(1, TELEGRAM_RETRIES + 1):
        try:
            result = requests.get(url, params={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=15).json()
            if result.get('ok'):
                return
        except (requests.RequestException, ValueError) as ex:
            logging.error("Sending Telegram message failed: " + str(ex))
        if tries < TELEGRAM_RETRIES:
            logging.error("Sending Telegram message failed, retrying in 5 seconds...")
            time.sleep(5)
    logging.error("Sending Telegram message failed {} times, please check your credentials!".format(TELEGRAM_RETRIES))


def find_process_id_by_name(process_name):
    list_of_process_objects = []
    for proc in psutil.process_iter():
        try:
            pinfo = proc.as_dict(attrs=['pid', 'name'])
            if process_name.lower() in pinfo['name'].lower():
                list_of_process_objects.append(pinfo)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return list_of_process_objects


def exit_process_by_name(name):
    list_of_process_ids = find_process_id_by_name(name)
    if len(list_of_process_ids) > 0:
        logging.info(name + " process exists | killing..")
        for elem in list_of_process_ids:
            process_id = elem['pid']
            try:
                os.kill(process_id, signal.SIGKILL)
            except Exception as ex:
                logging.error("Could not terminate " + name +
                              "[" + str(process_id) + "]: " + str(ex))


def list_windows():
    try:
        output = subprocess.run(['wmctrl', '-lx'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=10).stdout.decode(errors='replace')
    except subprocess.TimeoutExpired:
        return []
    return parse_wmctrl(output)


def window_is_fullscreen(window_id):
    result = subprocess.run(['xprop', '-id', window_id, '_NET_WM_STATE'], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=10)
    return b'_NET_WM_STATE_FULLSCREEN' in result.stdout


def show_meeting_fullscreen(meeting):
    """Make the meeting window fullscreen and minimize the other Zoom windows."""
    for window in list_windows():
        if window.wm_class == 'zoom.zoom' and window.id != meeting.id and window.title == 'Zoom Workplace':
            subprocess.run(['xdotool', 'windowminimize', window.id], stderr=subprocess.DEVNULL)
    if not window_is_fullscreen(meeting.id):
        logging.info("Switching meeting window to fullscreen")
        subprocess.run(['wmctrl', '-i', '-r', meeting.id, '-b', 'add,fullscreen'])
        subprocess.run(['wmctrl', '-i', '-a', meeting.id])
    # Keep the mouse out of the way so no toolbars are shown
    subprocess.run(['xdotool', 'mousemove', '0', '0'], stderr=subprocess.DEVNULL)


def start_recording(description, suffix=""):
    width, height = pyautogui.size()
    resolution = str(width) + 'x' + str(height)
    filename = os.path.join(REC_PATH, time.strftime(TIME_FORMAT) + "-" + description + suffix + ".mkv")
    command = build_record_command(os.getenv('DISPLAY'), resolution, filename, RECORDING_SETTINGS)
    ffmpeg = subprocess.Popen(command, preexec_fn=os.setsid)
    atexit.register(os.killpg, os.getpgid(ffmpeg.pid), signal.SIGQUIT)
    logging.info("Recording to " + filename)
    return ffmpeg, filename


def stop_recording(ffmpeg):
    try:
        os.killpg(os.getpgid(ffmpeg.pid), signal.SIGQUIT)
    except ProcessLookupError:
        pass
    atexit.unregister(os.killpg)
    try:
        ffmpeg.wait(timeout=60)
    except subprocess.TimeoutExpired:
        logging.error("FFmpeg did not stop in time, killing it")
        os.killpg(os.getpgid(ffmpeg.pid), signal.SIGKILL)
        ffmpeg.wait()


def remux_to_mp4(mkv_path):
    """Convert the finished .mkv recording to .mp4. Keeps the .mkv if that fails."""
    mp4_path = os.path.splitext(mkv_path)[0] + ".mp4"
    result = subprocess.run(build_remux_command(mkv_path, mp4_path),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0 and os.path.getsize(mp4_path) > 0:
        os.remove(mkv_path)
        logging.info("Saved recording " + mp4_path)
        return mp4_path
    logging.error("Remux to mp4 failed, keeping " + mkv_path + ": " +
                  result.stderr.decode(errors='replace')[-500:])
    if os.path.exists(mp4_path):
        os.remove(mp4_path)
    return mkv_path


def save_zoom_config():
    """Copy zoomus.conf into the persistent profile directory when it changed."""
    try:
        with open(ZOOM_CONF, 'rb') as f:
            current = f.read()
        if os.path.exists(PROFILE_ZOOM_CONF):
            with open(PROFILE_ZOOM_CONF, 'rb') as f:
                if f.read() == current:
                    return
        tmp = PROFILE_ZOOM_CONF + ".tmp"
        shutil.copyfile(ZOOM_CONF, tmp)
        os.replace(tmp, PROFILE_ZOOM_CONF)
        logging.info("Saved Zoom configuration to profile")
    except OSError as ex:
        logging.error("Could not save Zoom configuration: " + str(ex))


def start_zoom(meet_id, meet_pw):
    apply_zoom_options(ZOOM_CONF)
    url = build_join_url(meet_id, meet_pw, DISPLAY_NAME)
    if url is None:
        # Link without meeting number, let Zoom handle it
        url = meet_id
    logging.info("Starting Zoom with join URL")
    return subprocess.Popen(['zoom', '--url=' + url], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, preexec_fn=os.setsid)


def stop_zoom(zoom):
    # Ask Zoom to quit first, kill whatever is left after 10 seconds
    try:
        os.killpg(os.getpgid(zoom.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(10):
        if not find_process_id_by_name("zoom"):
            break
        time.sleep(1)
    exit_process_by_name("zoom")
    save_zoom_config()


def join(meet_id, meet_pw, duration, description):
    """Join and record a meeting. Errors are reported instead of stopping the scheduler."""
    try:
        record_meeting(meet_id, meet_pw, duration, description)
    except Exception as ex:
        logging.exception("Recording of %s failed" % description)
        report_error("Recording of '{}' failed: {}".format(description, ex), screenshot=True)
        send_telegram_message("Recording of '{}' failed: {}".format(description, ex))
        exit_process_by_name("zoom")
        exit_process_by_name("ffmpeg")
    finally:
        remove_preview()
        update_status(state='idle', meeting=None, since=None, planned_end=None, file=None)


def record_meeting(meet_id, meet_pw, duration, description):
    logging.info("Join meeting: " + description)
    end_date = datetime.now() + timedelta(seconds=duration + END_GRACE_SECONDS)
    update_status(state='joining', meeting=description,
                  since=datetime.now().isoformat(timespec='seconds'),
                  planned_end=end_date.isoformat(timespec='seconds'), file=None)

    ffmpeg_debug = None
    if DEBUG:
        ffmpeg_debug, _ = start_recording(description, "-JOIN")

    # Exit Zoom if running
    exit_process_by_name("zoom")
    zoom = start_zoom(meet_id, meet_pw)

    # Wait for the meeting window (we may sit in the waiting room until the host admits us)
    logging.info("Waiting for meeting window..")
    meeting = None
    last_preview = 0
    seen_titles = []
    while meeting is None and datetime.now() < end_date:
        time.sleep(2)
        windows = list_windows()
        meeting = find_meeting_window(windows)
        # Log every Zoom window title once, so unknown dialogs can be identified later
        for window in windows:
            if window.wm_class == 'zoom.zoom' and window.title not in seen_titles:
                seen_titles.append(window.title)
                logging.info("Zoom window: '%s'" % window.title)
        if time.time() - last_preview >= PREVIEW_INTERVAL:
            save_preview()
            update_status()
            last_preview = time.time()

    if meeting is None:
        report_error("Could not join '{}': meeting window did not appear until the planned end. "
                     "Zoom windows seen: {}".format(description, ", ".join(
                         "'%s'" % t for t in seen_titles) or "none"), screenshot=True)
        debug_screenshot(description, "join_error")
        if ffmpeg_debug is not None:
            stop_recording(ffmpeg_debug)
        stop_zoom(zoom)
        send_telegram_message("Failed to join meeting {}!".format(description))
        return

    logging.info("Joined meeting!")
    time.sleep(2)
    show_meeting_fullscreen(meeting)
    time.sleep(1)

    if ffmpeg_debug is not None:
        stop_recording(ffmpeg_debug)

    ffmpeg, filename = start_recording(description)
    update_status(state='recording', recording_since=datetime.now().isoformat(timespec='seconds'),
                  file=os.path.basename(filename))
    save_preview()
    send_telegram_message("Joined Meeting '{}' and started recording.".format(description))

    if PLAY_AUDIO:
        play_audio(description)

    # Record until the meeting window is gone or the planned end (+ grace time) is reached
    window_gone_since = None
    last_screenshot = time.time()
    ended_by_host = False
    while datetime.now() < end_date:
        time.sleep(5)
        meeting = find_meeting_window(list_windows())
        if meeting is None:
            window_gone_since = window_gone_since or time.time()
            if time.time() - window_gone_since >= WINDOW_GONE_SECONDS:
                ended_by_host = True
                logging.info("Meeting window closed, meeting has ended.")
                break
        else:
            window_gone_since = None
            show_meeting_fullscreen(meeting)
        if time.time() - last_screenshot >= DEBUG_SCREENSHOT_INTERVAL:
            debug_screenshot(description, "meeting")
            last_screenshot = time.time()
        if time.time() - last_preview >= PREVIEW_INTERVAL:
            save_preview()
            last_preview = time.time()
        update_status()
        print(f"Meeting ends at the latest in {end_date - datetime.now()}", end="\r", flush=True)

    if not ended_by_host:
        logging.info("Planned end of meeting reached.")
    logging.info("Meeting ends at %s" % datetime.now())

    update_status(state='finishing')
    stop_recording(ffmpeg)
    stop_zoom(zoom)
    final_path = remux_to_mp4(filename)
    update_status(last_recording=os.path.basename(final_path),
                  last_recording_at=datetime.now().isoformat(timespec='seconds'))

    send_telegram_message("Meeting '{}' ended.".format(description))


def play_audio(description):
    files = [f for f in os.listdir(AUDIO_PATH) if f.endswith(".wav")] if os.path.isdir(AUDIO_PATH) else []
    if not files:
        logging.error("No .wav files found!")
        return
    path = os.path.join(AUDIO_PATH, random.choice(files))
    # Alt+A toggles mute in the Zoom meeting window
    subprocess.run(['xdotool', 'key', 'alt+a'])
    time.sleep(2)
    play = subprocess.run(['/usr/bin/paplay', '--device=microphone', '-p', path],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if play.returncode != 0:
        logging.error("Failed playing file! - " + str(play.returncode) + " - " + str(play.stderr))
    subprocess.run(['xdotool', 'key', 'alt+a'])


def read_meetings():
    try:
        return load_meetings(CSV_PATH)
    except FileNotFoundError:
        logging.error("Meetings file %s not found, waiting for it to appear." % CSV_PATH)
        return []


def join_ongoing_meeting():
    curr_date = datetime.now()
    for meeting in read_meetings():
        if not meeting.record:
            continue
        start_time = datetime.strptime(meeting.time, '%H:%M').time()
        # Check today's and yesterday's occurrence (meetings can cross midnight)
        for days_back in (0, 1):
            day = curr_date - timedelta(days=days_back)
            if day.strftime('%A').lower() != meeting.weekday:
                continue
            start_date = datetime.combine(day.date(), start_time)
            end_date = start_date + timedelta(minutes=meeting.duration)
            if start_date <= curr_date <= end_date:
                logging.info("Join meeting that is currently running..")
                join(meet_id=meeting.id, meet_pw=meeting.password,
                     duration=(end_date - curr_date).total_seconds(),
                     description=meeting.description)
                return


def setup_schedule():
    schedule.clear()
    count = 0
    for meeting in read_meetings():
        if not meeting.record:
            continue
        # Start one minute early
        join_time = (datetime.strptime(meeting.time, '%H:%M') - timedelta(minutes=1)).strftime('%H:%M')
        getattr(schedule.every(), meeting.weekday).at(join_time).do(
            join, meet_id=meeting.id, meet_pw=meeting.password,
            duration=meeting.duration * 60 + 60, description=meeting.description)
        count += 1
    logging.info("Added %s meetings to schedule." % count)


def csv_mtime():
    try:
        return os.path.getmtime(CSV_PATH)
    except OSError:
        return None


def main():
    os.makedirs(REC_PATH, exist_ok=True)
    os.makedirs(STATUS_DIR, exist_ok=True)
    # Keep a log file next to the status, the terminal on the virtual desktop is not persistent
    log_file = logging.handlers.RotatingFileHandler(
        os.path.join(STATUS_DIR, 'zoomrec.log'), maxBytes=1_000_000, backupCount=3)
    log_file.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().addHandler(log_file)
    if DEBUG:
        os.makedirs(DEBUG_PATH, exist_ok=True)

    setup_schedule()
    remove_preview()
    update_status(state='idle', meeting=None)
    join_ongoing_meeting()

    last_csv_mtime = csv_mtime()
    last_config_save = time.time()
    last_heartbeat = 0
    while True:
        schedule.run_pending()
        time.sleep(1)
        if time.time() - last_heartbeat >= 10:
            update_status()
            last_heartbeat = time.time()
        # Reload the schedule when meetings.csv changes, no restart needed
        current_mtime = csv_mtime()
        if current_mtime != last_csv_mtime:
            last_csv_mtime = current_mtime
            logging.info("meetings.csv changed, reloading schedule")
            try:
                setup_schedule()
            except Exception as ex:
                logging.error("Failed to reload meetings.csv: " + str(ex))
        # Settings changed manually in Zoom (via VNC) must survive container updates
        if time.time() - last_config_save >= 60:
            save_zoom_config()
            last_config_save = time.time()
        time_of_next_run = schedule.next_run()
        if time_of_next_run is None:
            print("No meetings scheduled", end="\r", flush=True)
            continue
        remaining = time_of_next_run - datetime.now()
        print(f"Next meeting in {remaining}", end="\r", flush=True)


if __name__ == '__main__':
    main()
