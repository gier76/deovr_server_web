# DeoVR Server

Streamt deine eigenen Videos und Videos von Webseiten an DeoVR (Meta Quest) im DeoVR-JSON-Format.

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
