import atexit
import logging
import os
import random
import signal
import subprocess
import time
from datetime import datetime, timedelta

import psutil
import pyautogui
import requests
import schedule

from zoomrec_core import (RecordingSettings, build_join_url, build_record_command,
                          build_remux_command, find_meeting_window, load_meetings,
                          parse_wmctrl)

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
    else:
        logging.error("Remux to mp4 failed, keeping " + mkv_path + ": " +
                      result.stderr.decode(errors='replace')[-500:])
        if os.path.exists(mp4_path):
            os.remove(mp4_path)


def start_zoom(meet_id, meet_pw):
    url = build_join_url(meet_id, meet_pw, DISPLAY_NAME)
    if url is None:
        # Link without meeting number, let Zoom handle it
        url = meet_id
    logging.info("Starting Zoom with join URL")
    return subprocess.Popen(['zoom', '--url=' + url], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, preexec_fn=os.setsid)


def stop_zoom(zoom):
    try:
        os.killpg(os.getpgid(zoom.pid), signal.SIGQUIT)
    except ProcessLookupError:
        pass
    time.sleep(2)
    exit_process_by_name("zoom")


def join(meet_id, meet_pw, duration, description):
    logging.info("Join meeting: " + description)
    end_date = datetime.now() + timedelta(seconds=duration + END_GRACE_SECONDS)

    ffmpeg_debug = None
    if DEBUG:
        ffmpeg_debug, _ = start_recording(description, "-JOIN")

    # Exit Zoom if running
    exit_process_by_name("zoom")
    zoom = start_zoom(meet_id, meet_pw)

    # Wait for the meeting window (we may sit in the waiting room until the host admits us)
    logging.info("Waiting for meeting window..")
    meeting = None
    while meeting is None and datetime.now() < end_date:
        time.sleep(2)
        meeting = find_meeting_window(list_windows())

    if meeting is None:
        logging.error("Meeting window did not appear until the planned end.")
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
        print(f"Meeting ends at the latest in {end_date - datetime.now()}", end="\r", flush=True)

    if not ended_by_host:
        logging.info("Planned end of meeting reached.")
    logging.info("Meeting ends at %s" % datetime.now())

    stop_recording(ffmpeg)
    stop_zoom(zoom)
    remux_to_mp4(filename)

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
    if DEBUG:
        os.makedirs(DEBUG_PATH, exist_ok=True)

    setup_schedule()
    join_ongoing_meeting()

    last_csv_mtime = csv_mtime()
    while True:
        schedule.run_pending()
        time.sleep(1)
        # Reload the schedule when meetings.csv changes, no restart needed
        current_mtime = csv_mtime()
        if current_mtime != last_csv_mtime:
            last_csv_mtime = current_mtime
            logging.info("meetings.csv changed, reloading schedule")
            try:
                setup_schedule()
            except Exception as ex:
                logging.error("Failed to reload meetings.csv: " + str(ex))
        time_of_next_run = schedule.next_run()
        if time_of_next_run is None:
            print("No meetings scheduled", end="\r", flush=True)
            continue
        remaining = time_of_next_run - datetime.now()
        print(f"Next meeting in {remaining}", end="\r", flush=True)


if __name__ == '__main__':
    main()
