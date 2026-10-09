# CLAUDE.md – zoomrec-Fork: Uni-Vorlesungen automatisch aufnehmen

## Ziel

Fork von https://github.com/kastldratza/zoomrec, modernisiert für einen einzelnen Nutzer (Student, Wien, Zeitzone `Europe/Vienna`).
Zweck: Zoom-Vorlesungen der Uni automatisch aufnehmen, während der Nutzer in anderen Vorlesungen sitzt, und die Aufnahmen später ansehen.
Die offizielle Aufzeichnung durch die Uni wurde angefragt und ist nicht möglich.

Sprache mit dem Nutzer: Deutsch. Code, Commits und Kommentare: Englisch.

## Zielumgebung

- Linux-VM (x86_64), 6 AMD-Kerne, 12 GB RAM, dedizierte GTX 1070. Auf der VM laufen andere Dienste, also Ressourcen schonend nutzen.
- Docker ist vorhanden, Betrieb headless (kein Monitor) mit `--restart unless-stopped`.
- Encoding auf der CPU ist ausreichend. NVENC über die 1070 ist optional und nur ein späteres Nice-to-have.

## Ist-Zustand des Upstream-Repos (Stand Oktober 2026)

- Letzte Code-Änderung im Januar 2023, danach nur noch README- und Workflow-Änderungen. Das Projekt ist faktisch unmaintained.
- `Dockerfile`: `FROM ubuntu:20.04` (EOL), Zoom-Client fest auf `5.13.4.711` gepinnt. Dieser Client ist zu alt und wird von Zoom sehr wahrscheinlich nicht mehr zugelassen.
- Ablauf: Xfce + TigerVNC (Port 5901, Passwort `zoomrec`) im Container. `zoomrec.py` (~940 Zeilen) steuert den Zoom-Desktop-Client per `pyautogui`.
- Die Steuerung basiert auf **Bilderkennung**: 27 Referenz-Screenshots in `res/img/` und ~42 Aufrufe von `locateOnScreen`/`locateCenterOnScreen`. Mit einem neuen Zoom-Client passen diese Bilder nicht mehr.
- Beitritt läuft nur als **Gast** (Meeting-ID + Passwort oder URL, Name aus `DISPLAY_NAME`). Ein Login gibt es nicht. Meetings mit „nur authentifizierte Nutzer" scheitern daran.
- `meetings.csv` (Trennzeichen `;`, Spalten `weekday;time;duration;id;password;description;record`) wird **nur beim Start** gelesen (`setup_schedule()`), also ist nach jeder Änderung ein Neustart nötig.
- **Sicherheitsproblem:** `setup_schedule()` baut aus CSV-Feldern einen String und führt ihn per `compile()` + `eval()` aus. Das ist Code-Injection über die CSV und muss raus, besonders bevor ein Web-GUI die CSV schreibt.
- **Speicherproblem:** FFmpeg nimmt verlustfrei auf (`-vcodec libx264rgb -preset ultrafast -crf 0`, Audio `pcm_s16le`, `.mkv`). Pro 90-Minuten-Vorlesung entstehen dadurch enorm große Dateien.
- Nach dem Beitritt spielt `paplay` eine zufällige `.wav` aus `/home/zoomrec/audio` ins Meeting. Das soll standardmäßig **aus** sein.
- Telegram-Benachrichtigungen (Start, Ende, Fehler) existieren und sollen erhalten bleiben.

## Warum Fork statt Meeting-SDK-Bot (Attendee, Vexa …)

Seit 23.02.2026 dürfen Zoom-Meeting-SDK-Apps Meetings außerhalb ihres eigenen Accounts nicht mehr anonym betreten. Sie brauchen dafür OAuth und einen ZAK- oder On-Behalf-Token sowie eine eigene Marketplace-App. Mit einem Uni-Account ist das oft nicht erlaubt, und für einen Einzelnutzer ist es ohnehin zu viel Infrastruktur.
Der Desktop-Client braucht nichts davon und erlaubt einen **persistenten Login mit dem Uni-Account (SSO)**. Das ist der Hauptvorteil dieses Forks.

## Plan (Reihenfolge einhalten, jede Phase einzeln testen)

### Phase 1 – Lauffähig machen
1. Basis-Image auf `ubuntu:24.04` umstellen und Pakete, Python-Dependencies und VNC/Xfce-Setup anpassen.
2. Aktuellen Zoom-Linux-Client installieren, die Version per Build-Arg konfigurierbar machen und nicht hart verdrahten.
3. Container bauen und starten, dann per VNC prüfen, dass Zoom startet.

### Phase 2 – Persistenter Login
1. Zoom-Konfiguration (`~/.zoom`, `~/.config/zoomus.conf` usw. – die tatsächlichen Pfade im neuen Client prüfen) in ein Docker-Volume legen, z.B. `./zoom-profile:/home/zoomrec/.zoom`.
2. Doku in der README: Einmal per VNC verbinden und im Zoom-Client mit SSO der Uni anmelden. Danach soll der Login Container-Neustarts überleben.
3. Beitritt bevorzugt angemeldet über die Join-URL (`zoommtg://`-Link oder `zoom --url=...`) statt über den Gast-Dialog. Wenn die URL-Methode zuverlässig funktioniert, entfällt viel Bilderkennung.
4. Den Gast-Modus als Fallback behalten.

### Phase 3 – Bot-Logik robuster machen
1. So wenig Pixel-Matching wie möglich. Bevorzugt in dieser Reihenfolge: Join per URL/CLI, Tastenkürzel, `xdotool` mit Fenstertiteln und -klassen, und erst zuletzt `locateOnScreen`.
2. Wo Referenzbilder nötig bleiben: neue Screenshots vom aktuellen Client machen, mit `confidence` arbeiten (OpenCV), und Bilder sowie Logik pro Dialog sauber zuordnen.
3. Meeting-Ende zuverlässig erkennen (Fenster weg, Prozessstatus, „Meeting has been ended by host") und die geplante Dauer plus 5 Minuten als harte Obergrenze behalten.
4. Mikrofon und Kamera immer aus. Das Abspielen von Audio standardmäßig deaktivieren (`PLAY_AUDIO=false`).

### Phase 4 – Aufräumen und Sicherheit
1. `eval()` in `setup_schedule()` ersetzen durch direkte Aufrufe wie `getattr(schedule.every(), weekday).at(t).do(join, ...)`. CSV-Felder validieren (Wochentag aus Whitelist, Zeit `HH:MM`, Dauer als Integer, Beschreibung auf einen sicheren Dateinamen bereinigen).
2. FFmpeg auf vernünftige Dateigrößen umstellen: z.B. `libx264 -preset veryfast -crf 28 -pix_fmt yuv420p`, Audio `aac 128k`, Container `.mp4` mit `-movflags +faststart`. Optional `-r 10–15`, weil Folien kaum Bewegung haben. Für Ausfallsicherheit evtl. erst in `.mkv` aufnehmen und am Ende nach `.mp4` remuxen.
3. Schedule-Datei neu einlesen, ohne den Container neu zu starten (Datei-Änderungszeit prüfen oder Signal `SIGHUP`). Das braucht das Web-GUI.
4. Zeitzone Standard `Europe/Vienna`.
5. VNC-Passwort per Umgebungsvariable setzen, kein Standardpasswort ausliefern. In der Doku empfehlen, Port 5901 nicht öffentlich freizugeben (SSH-Tunnel).
6. Tests: Unit-Tests für CSV-Parsing und Validierung sowie für den Aufbau des FFmpeg-Kommandos.

### Phase 5 – Web-GUI (separater Container)
Python mit FastAPI oder Flask, schlankes Frontend ohne Build-Schritt. Funktionen:
- **Stundenplan:** Vorlesungen anlegen, bearbeiten, aktivieren/deaktivieren und löschen, mit Validierung wie in Phase 4. Die Datei atomar schreiben (temporäre Datei, dann umbenennen).
- **Aufnahmen:** Liste mit Datum, Größe und Dauer, im Browser abspielen (HTTP Range Requests für Seeking), herunterladen und löschen.
- **Status:** nächste geplante Aufnahme, ob gerade aufgenommen wird, letzte Fehler und Debug-Screenshots, Log-Ausschnitt.
- **Live-Ansicht:** noVNC eingebettet.
- **Absicherung:** Pflicht-Login (Passwort per Umgebungsvariable, gehasht, Session-Cookie). Standardmäßig nur auf `127.0.0.1` oder im LAN binden und Zugriff von außen über VPN/Tailscale dokumentieren. Kein Docker-Socket nötig, wenn Phase 4.3 umgesetzt ist.
- Beide Container über eine `docker-compose.yml` mit gemeinsamen Volumes (`recordings`, `config/meetings.csv`).

## Regeln

- **Niemals echte Zoom-Links, Meeting-Passwörter, Telegram-Tokens oder Zoom-Profile committen.** In `.gitignore` gehören `meetings.csv` (nur `meetings.example.csv` ins Repo), `.env`, `zoom-profile/`, `recordings/` und `audio/`.
- Kleine, nachvollziehbare Commits pro Schritt.
- Nach jeder Phase dem Nutzer kurz sagen, wie er es testet (Befehle plus worauf er per VNC achten soll).
- Bei Unsicherheit zur Zoom-UI erst einen Debug-Screenshot machen (`DEBUG=True`) und nicht raten.

## Hinweis zum Kontext

Empfehlung an den Nutzer: die Lehrenden über die Aufnahme informieren. Das Tool ist ausschließlich für den privaten Nachgebrauch gedacht. Funktionen zum Verstecken des Bots oder zum Umgehen von Hinweisen sind nicht Teil des Projekts.
