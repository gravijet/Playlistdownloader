# Playlist Downloader

Webapp, die YouTube-/YouTube-Music- und Spotify-Playlists als Audio-ZIP-Archiv
ausliefert — mit ID3-Tags und eingebettetem Cover. YouTube- und YouTube-Music-
Links können zusätzlich als Video-ZIP (bis 1080p) heruntergeladen werden.

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
| YouTube, YouTube Music | yt-dlp (Python-API, Track für Track) | Audio oder Video direkt |
| Spotify | spotDL (Subprozess) | Metadaten von der Spotify-API, Audio von YouTube Music |

Spotify gibt keine Audiodaten heraus; spotDL liest dort nur die Trackliste und
sucht die Titel anschließend auf YouTube Music. Deshalb kann das Ergebnis
gelegentlich ein anderes Master oder eine Liveversion sein.

Video ist daher bewusst nur für YouTube und YouTube Music verfügbar. Es wird
als bestmögliche Kombination aus Video und Audio bis zur gewählten Höhe geladen
und als ZIP gepackt.

yt-dlp wird pro Track einzeln aufgerufen. Ein gesperrtes oder gelöschtes Video
bricht damit nicht die ganze Playlist ab, sondern landet als „übersprungen" in
der Ergebnisliste.

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
ab und lässt bereits fertige ZIPs unerreichbar zurück. Die verwaisten
Verzeichnisse räumt der Reaper nach `YTDLWEB_JOB_TTL_HOURS` selbst weg. Für den
Einsatzzweck ist das gewollt — eine Datenbank für Downloads, die ohnehin nach
drei Stunden verfallen, wäre Aufwand ohne Gegenwert.

Das Passwort steht zusätzlich in `/root/.ytdlweb-password`.

### Konfiguration

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `YTDLWEB_PASSWORD` | — | Zugangspasswort. Leer = offen für alle. |
| `YTDLWEB_MAX_TRACKS` | 300 | Maximale Titel pro Playlist |
| `YTDLWEB_MAX_CONCURRENT_JOBS` | 2 | Parallele Downloads |
| `YTDLWEB_JOB_TTL_HOURS` | 3 | Nach dieser Zeit wird das ZIP gelöscht |
| `YTDLWEB_FORCE_IPV4` | true | Downloads über IPv4 erzwingen |
| `SPOTIFY_CLIENT_ID` / `_SECRET` | — | Eigene Spotify-App, falls spotDLs Standard-Keys limitiert werden |

### Warum IPv4 für ausgehende Downloads

YouTube blockt Rechenzentrums-**IPv6**-Bereiche deutlich aggressiver als IPv4
(„Sign in to confirm you're not a bot"). `YTDLWEB_FORCE_IPV4=true` bindet
yt-dlp deshalb an die IPv4-Adresse des Servers. Eingehend bleibt IPv6 davon
unberührt.

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
| `GET` | `/api/jobs/{id}` | Status und Fortschritt |
| `POST` | `/api/jobs/{id}/cancel` | Abbrechen |
| `DELETE` | `/api/jobs/{id}` | Job und Dateien löschen |
| `GET` | `/api/jobs/{id}/download` | Fertiges ZIP |
| `GET` | `/healthz` | Healthcheck, ohne Auth |

## Rechtliches

Das Werkzeug lädt herunter, was ihm gegeben wird. Ob du an den jeweiligen
Inhalten die nötigen Rechte hast, entscheidet sich beim Inhalt, nicht beim
Werkzeug — und liegt bei dir. Die Instanz ist deshalb passwortgeschützt
ausgeliefert und nicht als öffentlicher Dienst gedacht.
