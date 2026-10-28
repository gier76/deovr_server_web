# PlayaVR Server

Streamt deine eigenen Videos und Videos von Webseiten an Playa und DeoVR (Meta Quest).
Die Bibliothek liefert Playa über die Playa API v2 und DeoVR über `/deovr`.

## Lokale Videos
Standardordner: `/media/deovr` (in HA unter *Medien* / Samba-Freigabe `media/deovr`).
Unterordner werden in DeoVR zu Kategorien:

```
media/deovr/Urlaub/strand_180_LR.mp4
media/deovr/Filme/film_SBS.mp4
media/deovr/normal.mp4
```

## Webquellen (`sources`)
In den Add-on-Optionen (YAML-Ansicht) lassen sich beliebig viele Quellen eintragen. Es gibt zwei Typen:

```yaml
video_dir: /media/deovr
update_ytdlp: true
sources:
  - name: Seite mit direkten Videodateien
    type: web
    url: https://example.org/videos/
    format: 180_LR
    proxy: false
    depth: 1
  - name: Über yt-dlp
    type: ytdlp
    url: https://example.org/tag/xyz/
    format: 180_LR
    limit: 50
    max_height: 2160
```

### `type: web`
Findet Videodateien (`.mp4`, `.webm`, `.mkv`, `.mov`, `.m4v`), die direkt im HTML stehen: `<video>`-Tags, `og:video`, Links.

| Option | Bedeutung |
|---|---|
| `proxy` | `false` (Standard): DeoVR lädt direkt von der Quelle. `true`: Der Strom läuft über das Add-on (nötig bei Hotlink-Schutz) |
| `depth` | `0` = nur diese Seite, `1` = zusätzlich Unterseiten derselben Domain |
| `max_pages` | Höchstzahl der Unterseiten bei `depth: 1` (Standard 30) |

### `type: ytdlp`
Für Seiten, die yt-dlp versteht. Das Add-on liest die Liste per yt-dlp ein und löst die Stream-Adresse erst beim Abspielen auf, weil sie sonst abläuft. Der Videostrom läuft immer über das Add-on.

| Option | Bedeutung |
|---|---|
| `limit` | Höchstzahl der Einträge aus der Liste (Standard 50) |
| `max_height` | Höchste Auflösung (Zeilen), z. B. `2160` (optional) |
| `link_pattern` | (optional) Regulärer Ausdruck für den Pfad von Links auf Videoseiten. Nur relevant, wenn yt-dlp die Listenseite nicht selbst kennt (siehe unten). Standard: `/(?:video\|videos\|watch\|view\|movie\|hd-porn)[-/_]` |
| `cookies` | (optional) Cookie-Zeile, z. B. `name=wert; name2=wert2`, die beim Lesen der Listenseite und beim Auflösen mitgeschickt wird (etwa für eine Altersbestätigung, die die Seite per Cookie merkt) |

**Listenseiten:** yt-dlp kennt oft nur einzelne Videoseiten, nicht die Listen (Tags, Kategorien). Kann yt-dlp die Liste nicht lesen, sammelt das Add-on die Links auf Videoseiten direkt aus der Seite (Titel und Vorschaubild stammen aus dem Link) und lässt yt-dlp nur das einzelne Video auflösen. Findet es keine Links, steht auf `/debug` und `/selftest`, was die Seite geliefert hat (z. B. Altersabfrage oder Bot-Schutz).

Nicht unterstützt: reine HLS/DASH-Streams, Videos hinter Anmeldung, DRM.

### Gemeinsame Optionen
| Option | Bedeutung |
|---|---|
| `name` | Kategoriename in DeoVR |
| `url` | Seite oder Liste, die eingelesen wird |
| `format` | Feste Vorgabe, falls sich das Format nicht aus Dateiname/Titel erkennen lässt, z. B. `180_LR`, `360_TB`, `SBS`. Angaben im Titel haben Vorrang, fehlende Teile (Projektion oder 3D-Modus) ergänzt die Vorgabe. Ohne `format` (oder mit `format: auto`) wird das Format aus der Bildgröße erkannt, siehe unten |
| `warmup` | (nur `ytdlp`, optional) `false` schaltet das langsame Auflösen der Videos im Hintergrund ab. Es ermittelt Bildgröße (für die automatische VR-Erkennung) und Länge, ein Video alle 3 Sekunden |

Die Quellen werden beim Start und danach alle 30 Minuten neu eingelesen. Über `/refresh` lässt sich das sofort anstoßen. Bitte nur Inhalte einbinden, die du nutzen darfst.

## Weitere Optionen
- `video_dir`: Ordner mit den lokalen Videos (Standard `/media/deovr`).
- `public_url`: Feste Basisadresse für alle Links in den JSON-Antworten, z. B. `http://192.168.1.50:8000`. Nötig, wenn du die Bibliothek unter einem Namen wie `homeassistant.local` öffnest und DeoVR Vorschaubilder oder Videos darüber nicht laden kann.
- `update_ytdlp`: yt-dlp beim Start aktualisieren (Standard `true`).
- `auto_format`: VR-Format automatisch aus der Bildgröße erkennen (Standard `true`).
- `thumb_inline`: (Test) Vorschaubilder klein direkt in die Bibliothek einbetten statt als Adresse. Hilft, wenn DeoVR die Bild-Adressen nicht abruft. Macht die Bibliothek größer.
- `hide_broken`: Lokale Dateien, die ffprobe nicht lesen kann (defekt, unvollständig), in DeoVR ausblenden (Standard `true`). Auf `/debug` stehen sie weiter.
- `default_format`: Format für lokale Videos, deren Name keinen Hinweis enthält, z. B. `180_LR`. Leer lassen, wenn die automatische Erkennung entscheiden soll.

## Format: Automatik und manuelles Umstellen
DeoVR blendet den Projektions-Knopf aus, sobald das JSON dafür eine Vorgabe enthält. Automatik und DeoVR-Knopf im selben Eintrag gehen deshalb nicht zusammen (getestet mit 15.9.3915). Das Add-on löst das mit zwei Kategorien: Die normalen Kategorien enthalten die Videos mit automatisch erkanntem Format. Die Kategorie **"Format selbst wählen"** (Option `manual_category`, Standard an) enthält dieselben Videos ohne Formatvorgabe, dort stellst du Projektion und 3D-Modus in DeoVR selbst ein. Liegt die Automatik daneben, öffnest du das Video dort.

### Weitere Übergabe-Varianten (Option `format_mode`)
Seit 1.5.1 gilt `format_mode: filename` als Standard. Das Add-on erkennt das Format weiter automatisch (Dateiname, Ordnername, Quelle, Bildgröße) und schreibt es als Kürzel in die Video-Adresse, z. B. `/video/<id>_180_LR.mp4`. DeoVR liest das Kürzel selbst und behält dabei seinen **eigenen Projektions-Knopf**. Stimmt die Erkennung nicht, stellst du das Format direkt in DeoVR um. Alternativ setzt du es dauerhaft pro Video auf `/debug`.

Welche Übergabe bei dir funktioniert, hängt von der DeoVR-Version ab. Auf `/debug` steht oben ein Auswahlfeld "Format-Übergabe an DeoVR", mit dem du die Varianten ohne Neustart durchprobieren kannst (danach die Bibliothek in DeoVR neu laden). Die Wahl gilt bis zum Neustart; dauerhaft setzt du sie mit `format_mode`. Varianten: `json`, `filename`, `title` (Kürzel zusätzlich im Titel), `screen` (nur screenType), `stereo` (nur stereoMode), `none`.

`format_mode: json` schickt das Format stattdessen fest im JSON (`screenType`/`stereoMode`). Dann ist die Erkennung verbindlich, aber DeoVR blendet seinen Projektions-Knopf aus.

## Vorschaubilder per Tunnel (Option `thumb_tunnel`)
Der einfachste Weg zu Vorschaubildern in DeoVR, ohne Domain und Zertifikat (mit DeoVR 15.9.3915 auf der Meta Quest getestet, die Bilder erscheinen): Das Add-on startet beim Start einen **Cloudflare-Quick-Tunnel** und liefert die Vorschaubilder darüber per HTTPS aus. Die Adresse (`https://<zufällige-wörter>.trycloudflare.com`) trägt das Add-on selbst in die Bibliothek ein. Auf `/debug` steht, ob der Tunnel läuft.

```yaml
thumb_tunnel: true
```

Wichtig zu wissen:
- Nur die Vorschaubilder sind über den Tunnel erreichbar, nie die Bibliothek, die Videos oder `/debug`. Dafür läuft intern eine eigene kleine Server-Instanz (Port 8001, nicht veröffentlicht).
- Die Bilder laufen über das Internet und über Cloudflare. Die Adresse ist zufällig und nicht erratbar, aber öffentlich erreichbar, und die Bild-Adressen enthalten Video-IDs. Wenn dir das bei deinen Inhalten nicht passt, lass die Option aus.
- Quick-Tunnel sind ein kostenloser Testdienst ohne Verfügbarkeitszusage. Fällt er aus, fehlen nur die Bilder. Die Adresse ändert sich bei jedem Neustart des Add-ons.
- Beim Bauen des Add-ons wird `cloudflared` von GitHub geladen (rund 40 MB). Nach dem Update muss das Add-on neu gebaut werden. Für `amd64` und `aarch64`.
- Setzt du zusätzlich `https_url`, hat diese Vorrang und der Tunnel startet nicht.

## HTTPS für Vorschaubilder (Option `https_url`)
DeoVR zeigt Vorschaubilder offenbar nur an, wenn sie per **HTTPS mit gültigem Zertifikat** geladen werden (Bilder von `http://`-Adressen erscheinen nicht, ein HTTPS-Bild aus dem Internet schon). Die Videos laufen auch über HTTP. Für die Bilder kann das Add-on deshalb zusätzlich einen HTTPS-Server auf Port 8443 starten.

Voraussetzungen:
1. Ein Name mit gültigem Zertifikat, z. B. von einem Let's-Encrypt-Add-on (DuckDNS) für Home Assistant. Das Zertifikat liegt dann unter `/ssl/fullchain.pem` und `/ssl/privkey.pem`. Das Add-on bindet `/ssl` schreibgeschützt ein.
2. Dieser Name muss im Heimnetz auf die IP von Home Assistant zeigen (z. B. ein Eintrag im lokalen DNS oder Router), sonst erreicht die Quest ihn nicht oder landet an der falschen Adresse.
3. Port 8443 muss im Heimnetz erreichbar sein.

Optionen:
```yaml
https_url: https://deinname.duckdns.org:8443
ssl_cert: /ssl/fullchain.pem   # optional, das ist der Standard
ssl_key: /ssl/privkey.pem      # optional, das ist der Standard
```
Mit gesetzter `https_url` verweisen alle `thumbnailUrl` in der Bibliothek und in der Video-JSON darauf. Auf `/debug` steht, ob der HTTPS-Server läuft oder woran es scheitert. Nach einer Erneuerung des Zertifikats das Add-on neu starten. Mit `thumb_test: true` zeigt die Testbibliothek einen zusätzlichen Eintrag „TEST 4“ mit Bild per HTTPS vom Add-on.

## Format über den Dateinamen
| Namensteil | Bedeutung |
|---|---|
| `180` / `VR180` | 180°-Dome |
| `360` | 360°-Sphäre |
| `FISHEYE`, `FISHEYE190`, `MKX200` | Fisheye-Varianten |
| `LR` / `SBS` | Side-by-Side (3D) |
| `TB` / `OU` | Top-Bottom (3D) |
| nichts davon | siehe unten: Vorgabe `format`/`default_format`, sonst automatische Erkennung aus der Bildgröße |

### Automatische Erkennung aus der Bildgröße
Steht im Namen nichts und gibt es keine feste Vorgabe, schaut das Add-on auf die Bildgröße (nur bei Videos ab 2880 Pixel Breite, damit normale Filme flach bleiben):

| Seitenverhältnis (Breite:Höhe) | Beispiel | Ergebnis |
|---|---|---|
| ca. 2:1 | 4320×2160 | 180° Side-by-Side (`dome` + `sbs`) |
| ca. 1:1 | 3840×3840 | 360° Top-Bottom (`sphere` + `tb`) |
| anderes | | flach |

Das ist eine Faustregel. Ein 2:1-Video kann auch ein 360°-Video in Mono sein. Dann den Namen mit `360` ergänzen oder `format` setzen. Bei Webquellen ist die Bildgröße erst nach dem Auflösen im Hintergrund bekannt, auf `/debug` steht pro Video das erkannte Format. Mit `auto_format: false` wird nichts geraten.

### Format aus dem Ordnernamen
Lokale Videos erben das Format von ihren Ordnern unterhalb des Videoordners, ohne dass du sie umbenennen musst:

```
media/deovr/180_LR/urlaub.mp4        -> 180° Side-by-Side
media/deovr/VR/360_TB/Sub/film.mp4   -> 360° Top-Bottom (der innerste Ordner mit Angabe gewinnt)
media/deovr/2D/film.mp4              -> flach, es wird nichts geraten
```

Erkannt werden dieselben Teile wie im Dateinamen (`180`, `360`, `LR`, `SBS`, `TB`, `OU`, `FISHEYE`, `MKX200`), dazu `2D`, `FLAT` oder `MONO` für ausdrücklich flach. Reihenfolge: **von Hand in `/debug` gewählt** > Dateiname > Ordnername > `default_format` > automatische Erkennung aus der Bildgröße. Die Auswahl in `/debug` bleibt also immer möglich und gewinnt.

### Format von Hand einstellen
Auf `/debug` hat jedes Video in der Tabelle „Videos“ ein Auswahlfeld unter dem erkannten Format (automatisch, 2D, 180°/360° als Side-by-Side, Top-Bottom oder Mono, Fisheye …). Die Wahl gilt sofort und hat Vorrang vor Dateiname, Quellen-Vorgabe und Auto-Erkennung. Sie wird gespeichert und übersteht Neustarts. „automatisch“ nimmt sie zurück. In DeoVR die Bibliothek danach neu öffnen.

DeoVR blendet den eigenen Projektions-Button aus, sobald die JSON ein Format vorgibt. Deshalb gibt es diese Einstellung auf der Server-Seite.

## In DeoVR öffnen
Im DeoVR-Browser auf der Quest: `http://<HA-IP>:8000/deovr` (am besten mit der IP, nicht mit einem Namen).

## Diagnose
Oben auf `/debug` steht die Tabelle **"Was DeoVR seit dem Start abgerufen hat"**: wie oft die Quest Bibliothek, Video-JSON, Vorschaubilder und Videodateien geholt hat. Fehlt die Zeile "Vorschaubild", fragt DeoVR die Bilder gar nicht ab.

- **`/debug`** (auch über "Web-UI öffnen"): Version, Basis-URL, Quellen mit Fehlertext und der Auswertung, was die Seite geliefert hat (HTTP-Status, Altersabfrage/Bot-Schutz), pro Video Codec, Warnungen und der genaue Fehler bei fehlendem Thumbnail. Unten die Tabelle **"Letzte Anfragen"** mit Dauer pro Anfrage: was die Quest wirklich abgerufen hat. Oben steht der Fortschritt beim Erzeugen der Vorschaubilder. Bei vielen großen Videos dauert das beim ersten Start eine Weile, danach bleiben die Bilder unter `/data` erhalten.
- **`/selftest`**: Textbericht, der Schritt für Schritt prüft, ob ffmpeg, Thumbnail, JSON und die Quellen funktionieren. Er lässt sich komplett kopieren.

So gehst du bei Problemen vor:
1. In DeoVR die Bibliothek öffnen (mit der IP-Adresse), danach `/debug` im PC-Browser neu laden.
2. Stehen unter "Letzte Anfragen" Zeilen `/thumb/…` und `/video/…`? Wenn nein, erreicht die Quest diese Adressen nicht (meist ein Namens-/Netzwerkproblem, siehe `public_url`). Wenn ja, zeigt der Status (200/206/404/502), was schiefgeht.
3. `/selftest` aufrufen und das Ergebnis aufheben.

## Sicherheit
Keine Anmeldung. Nur im Heimnetz nutzen, Port nicht ins Internet weiterleiten.
