# Playlist Downloader

Webapp für YouTube, YouTube Music und Spotify. Einzelne Titel werden standardmäßig
direkt als MP3 ausgeliefert, Playlists und Alben als ZIP — mit Metadaten und Cover.
Andere Audioformate und YouTube-Videos (bis 1080p) sind optional auswählbar.
Auch eine Playlist mit nur einem verfügbaren Titel bleibt ein ZIP.

Läuft auf <https://yt.benjaminberger.at>.

## Architektur

```
Browser ──HTTPS──> Cloudflare ──HTTPS/IPv6──> nginx ──HTTP──> uvicorn (127.0.0.1:8090)
                                                                    │
                                                          yt-dlp / spotDL + ffmpeg
                                                                    │
                                                        /var/lib/ytdlweb/jobs/<id>/
```

Downloads laufen **asynchron**: `POST /api/jobs` legt nur einen Job an und
antwortet sofort. Der Browser pollt danach den Status. Das ist keine Kosmetik —
Cloudflare bricht Origin-Requests nach 100 Sekunden mit *Error 524* ab, und eine
Playlist braucht Minuten. Ein synchroner Download wäre über die Domain nie
fertig geworden.

### Quellen

| Quelle | Werkzeug | Weg |
| --- | --- | --- |
| YouTube, YouTube Music | yt-dlp (Python-API, bis zu 3 Titel parallel) | Audio oder Video direkt |
| Spotify | spotDL (Subprozess) | Metadaten von der Spotify-API, Audio von YouTube Music |

Spotify gibt keine Audiodaten heraus; spotDL liest dort nur die Trackliste und
sucht die Titel anschließend auf YouTube Music. Deshalb kann das Ergebnis
gelegentlich ein anderes Master oder eine Liveversion sein.

Video ist daher bewusst nur für YouTube und YouTube Music verfügbar. Es wird
als bestmögliche Kombination aus Video und Audio bis zur gewählten Höhe geladen
und als einzelne Mediendatei ausgeliefert. Video-Playlists werden als ZIP gepackt.

yt-dlp wird pro Track einzeln aufgerufen. Ein gesperrtes oder gelöschtes Video
bricht damit nicht die ganze Playlist ab. Bevor ein Titel als „übersprungen"
landet, wird er bis zu `YTDLWEB_TRACK_RETRIES`-mal (Standard 4) mit Pause
dazwischen erneut versucht — die meisten Abbrüche sind YouTube-seitiges
Throttling, kein echtes „gibt's nicht". spotDL bekommt denselben Gedanken über
`YTDLWEB_SPOTDL_MAX_RETRIES`.

Ist ein Titel trotzdem übersprungen worden und der Auftrag eine ZIP (Playlist,
Album, Kanal, Künstler), bietet die Oberfläche pro übersprungenem Titel einen
„Erneut versuchen"-Knopf. Das lädt nur diesen einen Titel nach und hängt ihn
in die bereits fertige ZIP-Datei an, statt den ganzen Auftrag zu wiederholen.
Das funktioniert nur, wenn der Titel eindeutig identifizierbar war (bei
YouTube immer, bei Spotify nur wenn spotDL einen klaren Suchbegriff gemeldet
hat) — sonst bleibt der Knopf weg.

Pro Auftrag werden standardmäßig bis zu drei Titel gleichzeitig verarbeitet;
die Nummerierung im ZIP erhält die Playlist-Reihenfolge. Nur erfolgreich
konvertierte Dateien werden übernommen. Bei Einzelvideos werden die bereits
gelesenen Metadaten für den Download wiederverwendet. Die fertige Einzeldatei
wird ohne ZIP-Erstellung und ohne zusätzliche Dateikopie bereitgestellt.

Ein YouTube-Kanal-Link (`/channel/…`, `/c/…`, `/@handle`) lädt alle Uploads,
nicht nur die auf der Kanal-Startseite vorausgewählten Videos: yt-dlp gibt für
einen nackten Kanal-Link zunächst nur die Tabs selbst zurück (Videos, Live,
Shorts, …), der „Videos"-Tab wird deshalb automatisch nachgeladen. Ein
Spotify-Künstler-Link lädt die komplette Diskografie (alle Alben, Singles,
Compilations) — das übernimmt spotDL bereits selbst.

## Dateien

```
app/config.py       Konfiguration aus der Umgebung
app/downloader.py   yt-dlp- und spotDL-Ansteuerung, ZIP-Bau
app/jobs.py         Job-Warteschlange, Fortschritt, Aufräumen
app/main.py         FastAPI-Endpunkte
app/static/         Oberfläche (kein Build-Schritt, kein Framework)
deploy/             systemd-Units, nginx-Vhost, Update-Skript
```

## Betrieb

```bash
systemctl status ytdlweb          # Dienst
journalctl -u ytdlweb -f          # Logs
systemctl restart ytdlweb         # Neustart
```

Konfiguration liegt in `/etc/ytdlweb.env` (Passwort, Limits). Nach Änderungen
`systemctl restart ytdlweb`.

Die Job-Liste liegt im Arbeitsspeicher: ein Neustart bricht laufende Downloads
ab und lässt bereits fertige Downloads unerreichbar zurück. Die verwaisten
Verzeichnisse räumt der Reaper nach `YTDLWEB_JOB_TTL_HOURS` selbst weg. Für den
Einsatzzweck ist das gewollt — eine Datenbank für Downloads, die ohnehin nach
24 Stunden verfallen, wäre Aufwand ohne Gegenwert.

Das Passwort steht zusätzlich in `/root/.ytdlweb-password`.

### Konfiguration

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `YTDLWEB_PASSWORD` | — | Zugangspasswort. Leer = offen für alle. |
| `YTDLWEB_MAX_TRACKS` | 5000 | Maximale Titel pro Playlist/Album/Kanal/Künstler |
| `YTDLWEB_MAX_CONCURRENT_JOBS` | 2 | Parallele Downloads |
| `YTDLWEB_TRACK_WORKERS` | 3 | Parallele YouTube-Titel pro Auftrag (1–6) |
| `YTDLWEB_SPOTIFY_THREADS` | 6 | Parallele Spotify-Titel pro Auftrag (1–12) |
| `YTDLWEB_TRACK_RETRIES` | 4 | Versuche pro Titel, bevor er als „übersprungen" gilt |
| `YTDLWEB_TRACK_RETRY_DELAY` | 4 | Pause in Sekunden zwischen zwei Versuchen |
| `YTDLWEB_SPOTDL_MAX_RETRIES` | 5 | Versuche von spotDL selbst pro Titel |
| `YTDLWEB_JOB_TTL_HOURS` | 24 | Nach dieser Zeit wird die Datei gelöscht |
| `YTDLWEB_HISTORY_LIMIT` | 200 | Einträge im Download-Verlauf pro Browser |
| `YTDLWEB_FORCE_IPV4` | true | Downloads über IPv4 erzwingen |
| `SPOTIFY_CLIENT_ID` / `_SECRET` | — | Eigene Spotify-App, falls spotDLs Standard-Keys limitiert werden |

### Warum IPv4 für ausgehende Downloads

YouTube blockt Rechenzentrums-**IPv6**-Bereiche deutlich aggressiver als IPv4
(„Sign in to confirm you're not a bot"). `YTDLWEB_FORCE_IPV4=true` bindet
yt-dlp deshalb an die IPv4-Adresse des Servers. Eingehend bleibt IPv6 davon
unberührt.

Dasselbe gilt für spotDLs YouTube-Music-Suche: yt-dlp hat dafür ein eigenes
Flag, spotDL/ytmusicapi nicht. Ohne Gegenmaßnahme liefen deren Anfragen über
IPv6 und kamen bei den meisten Titeln als kaputte, nicht als JSON lesbare
Antwort zurück — die Suche stürzte dann für den Titel komplett ab, statt ihn
einfach als „nicht gefunden" zu werten und die nächste Suchmethode zu
versuchen. `app/spotdl_runner.py` zwingt deshalb bei `YTDLWEB_FORCE_IPV4=true`
auch spotDL auf IPv4 (`_spotdl_base_cmd` startet spotDL darüber statt direkt).

### Wenn YouTube trotzdem blockt

Eine Netscape-Cookie-Datei nach `/var/lib/ytdlweb/cookies.txt` legen
(Besitzer `ytdlweb`, Rechte 600). yt-dlp und spotDL nutzen sie automatisch.
Danach `systemctl restart ytdlweb`.

### Updates

`ytdlweb-update.timer` aktualisiert yt-dlp und spotDL wöchentlich und startet
den Dienst neu, falls sich die Version geändert hat. Manuell:

```bash
systemctl start ytdlweb-update
```

Ein veraltetes yt-dlp ist mit Abstand die häufigste Ursache dafür, dass
plötzlich gar nichts mehr lädt.

## API

| Methode | Pfad | Zweck |
| --- | --- | --- |
| `POST` | `/api/login` | `{"password": "…"}` → Session-Cookie |
| `GET` | `/api/config` | Formate, Medienart und Limits |
| `POST` | `/api/jobs` | `{"url": "…", "format": "mp3-320"}` oder `{"url": "…", "format": "video-1080"}` → Job |
| `GET` | `/api/jobs` | Alle eigenen Jobs, die noch im Speicher sind (aktiv oder fertig innerhalb der TTL) |
| `GET` | `/api/jobs/{id}` | Status und Fortschritt |
| `POST` | `/api/jobs/{id}/cancel` | Abbrechen |
| `DELETE` | `/api/jobs/{id}` | Job und Dateien löschen |
| `GET` | `/api/jobs/{id}/download` | Einzeldatei oder Playlist-ZIP mit passendem Dateinamen und MIME-Typ |
| `POST` | `/api/jobs/{id}/retry/{index}` | Einen übersprungenen Titel nachladen und in die fertige ZIP einhängen |
| `GET` | `/api/history` | Eigener Download-Verlauf, auch nach Ablauf der Datei |
| `GET` | `/healthz` | Healthcheck, ohne Auth |

Der Job-Status enthält `is_playlist`, `download_name`, `download_size` und
`download_type`. `zip_name` und `zip_size` bleiben für ZIP-Ergebnisse kompatibel.
Übersprungene Titel stehen in `failed` (`{index, title, retryable, retrying}`).
`current_track` zeigt bei mehreren parallelen Titeln alle gerade aktiven Namen
(kommagetrennt), `log_tail` die letzten Zeilen des tatsächlichen yt-dlp-/
spotDL-Protokolls — die Oberfläche blendet das über „Details" pro Auftrag ein.

### Wessen Downloads sind das? („Verlauf" / eigene Jobs)

Ein zusätzliches, vom Login unabhängiges Cookie (`ytdlweb_uid`, ein Zufallswert,
ein Jahr gültig) markiert den Browser. `GET /api/jobs` und `GET /api/history`
liefern nur, was mit demselben Cookie erzeugt wurde — auch bei offener Instanz
ohne Passwort sieht so niemand die Downloads eines anderen Browsers. Der
Verlauf selbst liegt als kleine JSON-Datei pro Browser unter
`/var/lib/ytdlweb/history/<uid>.json` und bleibt bestehen, wenn der Job (und
damit die Datei) längst vom Reaper entfernt wurde — nur zum erneuten
Herunterladen reicht das dann natürlich nicht mehr.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check app/static/app.js
```

Die Tests verwenden temporäre Dateien und lokale Medien; YouTube- oder
Spotify-Zugriff ist dafür nicht nötig. Der Konvertierungstest benötigt ffmpeg.

## Rechtliches

Das Werkzeug lädt herunter, was ihm gegeben wird. Ob du an den jeweiligen
Inhalten die nötigen Rechte hast, entscheidet sich beim Inhalt, nicht beim
Werkzeug — und liegt bei dir. Die Instanz ist deshalb passwortgeschützt
ausgeliefert und nicht als öffentlicher Dienst gedacht.
