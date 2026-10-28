#!/usr/bin/env python3
"""
Selbstgehosteter Video-Server für Playa & DeoVR (v4).

Quellen:
  1. Lokale Videos:  videos/<Kategorie>/<video>.mp4  (Unterordner = Kategorien)
  2. Webseiten (type: web):   Videodateien, die direkt im HTML verlinkt sind
  3. yt-dlp (type: ytdlp):    Seiten, die yt-dlp versteht; die Stream-Adresse
                              wird erst beim Abspielen aufgelöst

Das Format (180/360/SBS/TB ...) wird aus Dateiname/Titel erkannt,
bei Webquellen optional per "format" vorgegeben.

Start:  python server.py
In DeoVR (Quest) im Browser öffnen:  http://<IP>:8000/deovr
Diagnose im Browser:                 http://<IP>:8000/debug
"""
import base64
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from flask import (Blueprint, Flask, Response, abort, jsonify, redirect, request,
                   send_file, stream_with_context)
from werkzeug.middleware.proxy_fix import ProxyFix

VERSION = "1.5.3"
VIDEO_DIR = Path(os.environ.get("VIDEO_DIR", "videos")).resolve()
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "cache")).resolve()
PORT = int(os.environ.get("PORT", 8000))
REFRESH_MIN = int(os.environ.get("REFRESH_MIN", 30))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
RESOLVE_TTL = 600  # Sekunden, so lange gelten per yt-dlp aufgelöste Adressen
EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
MIMETYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
}
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CACHE_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

# ffmpeg, ffprobe und yt-dlp getrennt prüfen
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
YTDLP = os.environ.get("YTDLP_BIN") or shutil.which("yt-dlp")


def _load_options():
    """Add-on-Optionen (Home Assistant legt sie unter /data/options.json ab)."""
    try:
        return json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


_OPTS = _load_options()
# Format für lokale Videos, deren Name keinen Hinweis enthält, z. B. "180_LR". Leer = automatisch.
DEFAULT_FORMAT = (os.environ.get("DEFAULT_FORMAT") or _OPTS.get("default_format") or "").strip()
AUTO_FORMAT = str(os.environ.get("AUTO_FORMAT", _OPTS.get("auto_format", True))).lower() not in ("0", "false", "no")
THUMB_INLINE = str(os.environ.get("THUMB_INLINE", _OPTS.get("thumb_inline", False))).lower() in ("1", "true", "yes")
THUMB_TEST = str(os.environ.get("THUMB_TEST", _OPTS.get("thumb_test", False))).lower() in ("1", "true", "yes")
THUMB_TUNNEL = str(os.environ.get("THUMB_TUNNEL", _OPTS.get("thumb_tunnel", False))).lower() in ("1", "true", "yes")
_tunnel = {"url": "", "status": "aus"}  # Adresse und Zustand des Quick-Tunnels
HTTPS_URL = (os.environ.get("HTTPS_URL") or _OPTS.get("https_url") or "").strip().rstrip("/")
SSL_CERT = (os.environ.get("SSL_CERT") or _OPTS.get("ssl_cert") or "/ssl/fullchain.pem").strip()
SSL_KEY = (os.environ.get("SSL_KEY") or _OPTS.get("ssl_key") or "/ssl/privkey.pem").strip()
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", 8443))
_https_state = {"status": "aus"}  # für /debug
HIDE_BROKEN = str(os.environ.get("HIDE_BROKEN", _OPTS.get("hide_broken", True))).lower() not in ("0", "false", "no")
# "json": Format steht in der JSON (DeoVR fixiert die Projektion). "filename": Format steht nur im
# Dateinamen der Video-Adresse; DeoVR erkennt es daran und lässt die Projektion im Menü änderbar.
MANUAL_CAT = bool(_OPTS.get("manual_category", True))
MANUAL_NAME = "Format selbst wählen"
FORMAT_MODE = (os.environ.get("FORMAT_MODE") or _OPTS.get("format_mode") or "json").strip().lower()

# Auflösung/Länge von Webvideos merken (übersteht Neustarts): Item-ID -> {"w", "h", "dur"}
_META_FILE = CACHE_DIR / "meta.json"
_meta_lock = threading.Lock()


def _load_meta():
    try:
        return json.loads(_META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_meta = _load_meta()


def meta_set(i, w, h, dur):
    with _meta_lock:
        _meta[i] = {"w": int(w or 0), "h": int(h or 0), "dur": int(dur or 0)}
        try:
            _META_FILE.write_text(json.dumps(_meta), encoding="utf-8")
        except OSError:
            pass


app = Flask(__name__)
app.json.sort_keys = False  # Felder in der Reihenfolge des Codes ausgeben, wie deovr.com
# Hinter einem Reverse Proxy die Original-Adresse/Schema übernehmen
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
_probe_cache = {}
_broken = {}   # (id, mtime) -> Fehlertext für lokale Dateien, die ffprobe nicht lesen kann
_thumb_errors = {}  # Item-ID -> letzter Fehlertext der Thumbnail-Erzeugung
_page_info = {}     # URL -> Kennzahlen der zuletzt gelesenen Seite
_page_links = {}    # URL -> Links der zuletzt gelesenen Seite
_last_detail = []   # zuletzt an DeoVR gelieferte Video-JSON (für /debug)
_local_cache = {"ts": 0.0, "items": []}  # Dateiliste kurz zwischenspeichern
_requests = []      # die letzten Anfragen (für /debug): Zeit, Client, Pfad, Status, User-Agent
_requests_lock = threading.Lock()
_stats = {}       # Art der Anfrage -> {Status: Anzahl}; wird nicht abgeschnitten wie die Liste oben
_thumb_log = []   # die letzten Vorschaubild-Anfragen
_stats_last = {}  # Art der Anfrage -> (Zeit, User-Agent) der letzten Anfrage
_req_thumb_slots = threading.Semaphore(6)   # Thumbnails, die DeoVR gerade anfragt, überholen die Hintergrundarbeit
_thumb_slots = threading.Semaphore(3)       # höchstens 3 ffmpeg-Thumbnails gleichzeitig
_page_thumb_slots = threading.Semaphore(4)  # Vorschaubilder von Webseiten (schnell, eigene Warteschlange)
_progress = {"done": 0, "total": 0}         # Fortschritt beim Erzeugen der lokalen Thumbnails
_resolve_cache = {}
_resolve_errors = {}
_resolve_locks = {}
_resolve_guard = threading.Lock()


# ---------------------------------------------------------------- Quellen-Konfiguration
def load_sources():
    raw = os.environ.get("SOURCES_JSON", "").strip()
    if not raw:
        f = Path(os.environ.get("SOURCES_FILE", "sources.json"))
        if f.exists():
            raw = f.read_text(encoding="utf-8")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"[sources] Konfiguration ist kein gültiges JSON: {e}")
        return []
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict) and s.get("url")]


SOURCES = load_sources()
_remote = {}  # Quellenname -> {"items": [...], "updated": ts, "error": str|None, "url": str}


# ---------------------------------------------------------------- Format-Erkennung
def detect_format(name: str):
    """Leitet Projektion und Stereo-Modus aus Dateiname/Titel ab."""
    n = name.upper().replace("-", "_").replace(" ", "_")
    tokens = set(n.replace(".", "_").split("_"))

    if "360" in tokens:
        screen = "sphere"
    elif "FISHEYE190" in tokens or "RF52" in tokens:
        screen = "rf52"
    elif "MKX200" in tokens:
        screen = "mkx200"
    elif "FISHEYE" in tokens:
        screen = "fisheye"
    elif "180" in tokens or "VR180" in tokens or "VR" in tokens:
        screen = "dome"
    else:
        screen = "flat"

    if tokens & {"LR", "SBS", "3DH"}:
        stereo = "sbs"
    elif tokens & {"TB", "OU", "3DV"}:
        stereo = "tb"
    else:
        stereo = None
    return screen, stereo


def guess_format_by_size(w, h):
    """VR-Videos erkennt man oft an der Bildgröße: 2:1 = 180° Side-by-Side (z. B. 4320x2160),
    1:1 = 360° Top-Bottom (z. B. 3840x3840). Nur bei großen Bildern, damit normale Filme flach bleiben."""
    if not w or not h or w < 2880:
        return None
    r = w / h
    if 1.95 <= r <= 2.1:
        return ("dome", "sbs")
    if 0.95 <= r <= 1.05:
        return ("sphere", "tb")
    return None


def item_dims(it):
    """(Breite, Höhe), falls schon bekannt, sonst None. Startet nichts, ist also schnell."""
    if it["kind"] in ("local", "remote"):
        info = cached_probe(it)
        return (info["width"], info["height"]) if info["ok"] else None
    m = _meta.get(it["id"])
    if m and m.get("w"):
        return m["w"], m["h"]
    r = _resolve_cache.get(it["id"])
    if r and r.get("width"):
        return r["width"], r["height"]
    return None


FORMAT_CHOICES = [
    ("", "automatisch"),
    ("flat", "2D (flach)"),
    ("180_LR", "180° Side-by-Side"),
    ("180_TB", "180° Top-Bottom"),
    ("180", "180° Mono"),
    ("360_TB", "360° Top-Bottom"),
    ("360_LR", "360° Side-by-Side"),
    ("360", "360° Mono"),
    ("FISHEYE_LR", "Fisheye 180° SBS"),
    ("FISHEYE190_LR", "Fisheye 190° SBS"),
    ("MKX200_LR", "MKX200 SBS"),
    ("SBS", "Flach + Side-by-Side (3D-Film)"),
    ("TB", "Flach + Top-Bottom (3D-Film)"),
]
_FORMATS_FILE = CACHE_DIR / "formats.json"
_formats = {}  # Video-ID -> von dir in /debug gewähltes Format (hat Vorrang vor allem anderen)
try:
    _formats = json.loads(_FORMATS_FILE.read_text(encoding="utf-8"))
except Exception:
    _formats = {}


def set_format(i, fmt):
    if fmt:
        _formats[i] = fmt
    else:
        _formats.pop(i, None)
    try:
        _FORMATS_FILE.write_text(json.dumps(_formats), encoding="utf-8")
    except OSError as e:
        print(f"[formats] konnte nicht speichern: {e}")


_VR_ALONE = re.compile(r"(?<![A-Z0-9])VR(?![A-Z0-9])")


def folder_format(it):
    """Format aus den Ordnernamen unterhalb des Videoordners; der innerste Ordner mit Angabe gewinnt.
    Liefert (Projektion, Stereo, ausdrücklich_2D). Ohne Angabe: ("flat", None, False)."""
    if it.get("kind") != "local":
        return "flat", None, False
    try:
        parts = it["path"].relative_to(VIDEO_DIR).parent.parts
    except ValueError:
        return "flat", None, False
    for name in reversed(parts):
        up = _VR_ALONE.sub("", name.upper())
        toks = set(up.replace("-", "_").replace(" ", "_").replace(".", "_").split("_"))
        screen, stereo = detect_format(up)
        if screen != "flat" or stereo is not None:
            return screen, stereo, False
        if toks & {"2D", "FLAT", "MONO"}:
            return "flat", None, True
    return "flat", None, False


def format_of(it):
    """Format: 1. Angaben im Namen, 2. Vorgabe 'format' der Quelle (bei lokalen Videos 'default_format'),
    3. automatisch aus der Bildgröße. 'format: auto' schaltet die Vorgabe ab."""
    ov = _formats.get(it["id"])
    if ov:
        return detect_format(ov)
    screen, stereo = detect_format(it["name"])
    # Ordnername (z. B. "180_LR", "360_TB", "2D"): ergänzt, was der Dateiname offen lässt
    f_screen, f_stereo, f_flat = folder_format(it)
    if screen == "flat":
        screen = f_screen
    if stereo is None:
        stereo = f_stereo
    if f_flat and screen == "flat" and stereo is None:
        return screen, stereo  # Ordner sagt ausdrücklich "2D": nichts erraten
    explicit = (it.get("fmt") or (DEFAULT_FORMAT if it["kind"] == "local" else "")).strip()
    if explicit.lower() == "auto":
        explicit = ""
    if explicit:
        d_screen, d_stereo = detect_format(explicit)
        if screen == "flat":
            screen = d_screen
        if stereo is None:
            stereo = d_stereo
    elif AUTO_FORMAT and screen == "flat" and stereo is None:
        dims = item_dims(it)
        g = guess_format_by_size(*dims) if dims else None
        if g:
            screen, stereo = g
    return screen, stereo


# ---------------------------------------------------------------- Items
def make_id(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _scan_local():
    items = []
    for p in sorted(VIDEO_DIR.rglob("*")):
        if p.suffix.lower() in EXTENSIONS and p.is_file():
            rel = p.relative_to(VIDEO_DIR)
            cat = rel.parent.name if rel.parent != Path(".") else "Alle Videos"
            items.append({
                "id": make_id(rel.as_posix()),
                "kind": "local",
                "cat": cat,
                "title": p.stem.replace("_", " "),
                "name": p.name,
                "ext": p.suffix.lower(),
                "path": p,
            })
    return items


def local_items():
    """Lokale Videos. Die Dateiliste wird 30 s gemerkt, weil DeoVR viele Anfragen auf einmal stellt
    und das Durchsuchen großer Ordner (Netzlaufwerk) sonst jedes Mal dauert."""
    now = time.time()
    if now - _local_cache["ts"] > 30 or not _local_cache["items"]:
        _local_cache["items"] = _scan_local()
        _local_cache["ts"] = now
    return _local_cache["items"]


def remote_items():
    out = []
    for state in list(_remote.values()):
        out.extend(state["items"])
    return out


def all_items():
    return local_items() + remote_items()


def find_item(i: str):
    for it in all_items():
        if it["id"] == i:
            return it
    return None


def base_url():
    if PUBLIC_URL:
        return PUBLIC_URL
    return request.host_url.rstrip("/")


def thumb_base():
    """Basisadresse für Vorschaubilder. DeoVR zeigt Bilder von http://-Adressen offenbar nicht an,
    deshalb kann hier eine https://-Adresse (Option https_url) stehen."""
    return HTTPS_URL or _tunnel["url"] or base_url()


def numeric_id(i: str) -> int:
    """DeoVR erwartet in der Video-JSON eine Zahl als id (Beispiel der Doku: 123)."""
    return int(i[:7], 16)


def thumb_path(i: str) -> str:
    """Vorschaubild-Adresse mit Versionsteil: DeoVR merkt sich Bilder (auch fehlgeschlagene) pro Adresse.
    Ändert sich die Version, holt es sie neu."""
    return f"/thumb/v{VERSION.replace('.', '')}/{i}.jpg"


_NAME_FLAGS = {"dome": "180", "sphere": "360", "fisheye": "FISHEYE", "mkx200": "MKX200", "rf52": "FISHEYE190"}


def filename_flags(screen, stereo) -> str:
    """Teile, an denen DeoVR das Format im Dateinamen erkennt, z. B. '_180_LR'."""
    parts = []
    if screen in _NAME_FLAGS:
        parts.append(_NAME_FLAGS[screen])
    if stereo == "sbs":
        parts.append("LR")
    elif stereo == "tb":
        parts.append("TB")
    return "".join("_" + p for p in parts)


def validate_detail(d: dict):
    """Prüft eine Video-JSON grob gegen das Beispiel der DeoVR-Doku."""
    p = []
    if not isinstance(d.get("id"), int):
        p.append("id ist keine Zahl")
    if not isinstance(d.get("title"), str) or not d.get("title"):
        p.append("title fehlt")
    if not isinstance(d.get("videoLength"), int):
        p.append("videoLength ist keine Zahl")
    if not isinstance(d.get("is3d"), bool):
        p.append("is3d ist kein true/false")
    if FORMAT_MODE in ("json", "screen") and d.get("screenType") not in ("flat", "dome", "sphere", "fisheye", "mkx200", "rf52"):
        p.append("screenType unbekannt")
    if FORMAT_MODE in ("json", "stereo") and d.get("is3d") and d.get("stereoMode") not in ("sbs", "tb", "cuv", "off"):
        p.append("stereoMode fehlt oder unbekannt")
    if not str(d.get("thumbnailUrl", "")).startswith("http"):
        p.append("thumbnailUrl fehlt")
    try:
        src = d["encodings"][0]["videoSources"][0]
        if not str(src["url"]).startswith("http"):
            p.append("videoSources.url ist keine Adresse")
        if not isinstance(src.get("resolution"), int):
            p.append("resolution ist keine Zahl")
    except Exception:
        p.append("encodings/videoSources fehlen")
    return p


def host_is_ip(host: str) -> bool:
    if host.strip("[]").lower() == "localhost":
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- yt-dlp
_ytdlp_version = []


def ytdlp_version():
    """Version von yt-dlp (einmal ermittelt und gemerkt)."""
    if not YTDLP:
        return "nicht gefunden"
    if not _ytdlp_version:
        try:
            out = subprocess.run([YTDLP, "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=20).stdout
            _ytdlp_version.append(out.decode(errors="ignore").strip() or "?")
        except Exception as e:
            return f"Fehler: {e}"
    return _ytdlp_version[0]


def run_ytdlp(args, timeout=120, cookies=None):
    if not YTDLP:
        raise RuntimeError("yt-dlp ist nicht installiert")
    extra = ["--add-headers", f"Cookie:{cookies}"] if cookies else []
    r = subprocess.run([YTDLP, "--no-warnings", "--ignore-config", *extra, *args],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    out = r.stdout.decode(errors="ignore").strip()
    err = r.stderr.decode(errors="ignore").strip()[-400:]
    if not out:
        raise RuntimeError(err or "keine Ausgabe von yt-dlp")
    try:
        data = json.loads(out)
    except ValueError:
        raise RuntimeError(f"yt-dlp lieferte kein JSON: {out[:120]} {err}".strip())
    if not isinstance(data, dict):  # z. B. "null"
        raise RuntimeError(f"yt-dlp lieferte keine Daten (Antwort: {out[:60]}). {err}".strip())
    return data


def _lock_for(key):
    with _resolve_guard:
        return _resolve_locks.setdefault(key, threading.Lock())


def resolve_ytdlp(it, force=False):
    """Löst die Seite per yt-dlp in eine abspielbare Datei-Adresse auf (gecacht)."""
    with _lock_for(it["id"]):
        c = _resolve_cache.get(it["id"])
        if c and not force and time.time() - c["ts"] < RESOLVE_TTL:
            return c
        try:
            hf = f"[height<={int(it['max_height'])}]" if it.get("max_height") else ""
            sel = f"b[ext=mp4][protocol^=http]{hf}/b[protocol^=http]{hf}/b{hf}/b"
            data = run_ytdlp(["-J", "--no-playlist", "-f", sel, it["page"]], timeout=90,
                             cookies=it.get("cookies"))
            if data.get("entries"):
                data = next((e for e in data["entries"] if e), data)
            url = data.get("url")
            if not url and data.get("requested_formats"):
                url = data["requested_formats"][0].get("url")
            if not url:
                raise RuntimeError("yt-dlp hat keine Datei-Adresse geliefert")
            proto = data.get("protocol") or ""
            if proto.startswith(("m3u8", "http_dash")) or urlparse(url).path.endswith(".m3u8"):
                raise RuntimeError("Es gibt nur einen HLS/DASH-Stream (wird nicht unterstützt)")
            headers = dict(data.get("http_headers") or {})
            headers.pop("Accept-Encoding", None)
            info = {
                "ts": time.time(), "url": url, "headers": headers,
                "height": int(data.get("height") or 1080),
                "width": int(data.get("width") or 0),
                "duration": int(data.get("duration") or 0),
                "thumbnail": data.get("thumbnail"),
                "ext": data.get("ext") or "mp4",
                "vcodec": data.get("vcodec") or "?",
            }
            _resolve_cache[it["id"]] = info
            _resolve_errors.pop(it["id"], None)
            meta_set(it["id"], info["width"], info["height"], info["duration"])
            return info
        except Exception as e:
            _resolve_errors[it["id"]] = str(e)
            print(f"[yt-dlp] Auflösen von {it['page']} fehlgeschlagen: {e}")
            raise


def media_source(it, force=False):
    """(URL, Header), über die das Video abrufbar ist."""
    if it["kind"] == "local":
        return str(it["path"]), {}
    if it["kind"] == "ytdlp":
        r = resolve_ytdlp(it, force=force)
        return r["url"], dict(r["headers"])
    h = {"User-Agent": UA}
    if it.get("referer"):
        h["Referer"] = it["referer"]
    return it["url"], h


def ff_input_args(headers):
    h = dict(headers)
    ua = h.pop("User-Agent", UA)
    args = ["-user_agent", ua]
    rest = "".join(f"{k}: {v}\r\n" for k, v in h.items())
    if rest:
        args += ["-headers", rest]
    return args


# ---------------------------------------------------------------- ffprobe / Warnungen
def _probe_key(it):
    return (it["id"], it["path"].stat().st_mtime if it["kind"] == "local" else 0)


def _probe_default() -> dict:
    return {"duration": 0, "width": 0, "height": 1080, "vcodec": "?",
            "pix_fmt": "?", "profile": "?", "acodec": "-", "ok": False}


def probe(it, network=True) -> dict:
    """Liest Länge, Auflösung und Codecs via ffprobe (gecacht).

    Bei Webvideos wird nur dann das Netzwerk benutzt, wenn network=True.
    """
    local = it["kind"] == "local"
    key = _probe_key(it)
    if key in _probe_cache:
        return _probe_cache[key]

    info = _probe_default()
    if local and key in _broken:
        return info  # schon als defekt bekannt, nicht jedes Mal neu versuchen
    if not local and not network:
        return info
    if not FFPROBE:
        return info
    try:
        target, headers = media_source(it)
        cmd = [FFPROBE, "-v", "error"]
        if not local:
            cmd += ff_input_args(headers)
        cmd += ["-show_entries",
                "stream=codec_type,codec_name,width,height,pix_fmt,profile:format=duration",
                "-of", "json", target]
        out = subprocess.check_output(cmd, timeout=60 if local else 40)
        data = json.loads(out)
        info["duration"] = int(float(data.get("format", {}).get("duration", 0) or 0))
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and info["vcodec"] == "?":
                info.update(vcodec=s.get("codec_name", "?"),
                            width=int(s.get("width", 0)),
                            height=int(s.get("height", 1080)),
                            pix_fmt=s.get("pix_fmt", "?"),
                            profile=s.get("profile", "?"))
            elif s.get("codec_type") == "audio" and info["acodec"] == "-":
                info["acodec"] = s.get("codec_name", "-")
        info["ok"] = True
        _probe_cache[key] = info
    except Exception as e:
        print(f"[ffprobe] Fehler bei {it['name']}: {e}")
        if local:
            _broken[key] = "Datei nicht lesbar (ffprobe schlägt fehl, z. B. fehlendes moov-Atom bei abgebrochenem Download)"
    return info


def is_broken(it) -> bool:
    """Lokale Datei, die ffprobe nicht lesen konnte (defekt, unvollständig)."""
    if it["kind"] != "local":
        return False
    try:
        return _probe_key(it) in _broken
    except OSError:
        return False


def cached_probe(it) -> dict:
    """Nur bereits ermittelte Werte, ohne ffprobe aufzurufen (schnell, für Listen)."""
    try:
        return _probe_cache.get(_probe_key(it)) or _probe_default()
    except OSError:
        return _probe_default()


def warnings_for(info: dict):
    w = []
    if info["vcodec"] not in ("h264", "hevc", "vp9", "av1", "?"):
        w.append(f"Videocodec {info['vcodec']} wird auf der Quest evtl. nicht unterstützt")
    if info["pix_fmt"] not in ("yuv420p", "yuv420p10le", "?"):
        w.append(f"Pixelformat {info['pix_fmt']} ist problematisch (besser yuv420p)")
    if "10" in info["profile"] and info["vcodec"] == "h264":
        w.append("H.264 10-Bit (High 10) wird nicht hardwarebeschleunigt abgespielt")
    if info["acodec"] in ("ac3", "eac3", "dts", "truehd"):
        w.append(f"Audio {info['acodec']} kann Probleme machen (besser AAC)")
    if info["vcodec"] == "av1":
        w.append("AV1 geht nur auf der Quest 3 zuverlässig")
    return w


# ---------------------------------------------------------------- Thumbnails
def http_fetch(url, referer=None, timeout=20, limit=8_000_000, headers=None):
    """Lädt eine Adresse. Gibt (Daten, Zeichensatz, Content-Type, Status, Endadresse) zurück."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit)
        charset = r.headers.get_content_charset() or "utf-8"
        ctype = r.headers.get("Content-Type", "")
        return data, charset, ctype, r.status, r.geturl()


def http_get(url, referer=None, timeout=20, limit=8_000_000, headers=None):
    data, charset, ctype, _, _ = http_fetch(url, referer, timeout, limit, headers)
    return data, charset, ctype


# Schlagwörter, die auf Altersabfrage, Bot-Schutz oder JavaScript-Zwang hindeuten
PAGE_HINTS = (
    ("verify your age", "Altersabfrage"), ("age verification", "Altersabfrage"),
    ("are you 18", "Altersabfrage"), ("volljährig", "Altersabfrage"),
    ("just a moment", "Bot-Schutz (Cloudflare)"), ("cf-chl", "Bot-Schutz (Cloudflare)"),
    ("captcha", "Captcha/Bot-Schutz"), ("access denied", "Zugriff verweigert"),
    ("enable javascript", "Seite verlangt JavaScript"),
)


def page_hints(text: str):
    low = text[:300_000].lower()
    out = []
    for word, label in PAGE_HINTS:
        if word in low and label not in out:
            out.append(label)
    return out


def stereo_crop(it):
    _, stereo = format_of(it)
    if stereo == "sbs":
        return ["crop=iw/2:ih:0:0"]
    if stereo == "tb":
        return ["crop=iw:ih/2:0:0"]
    return []


def _thumb_from_page(it, out):
    """Lädt das auf der Seite angegebene Vorschaubild. Gibt den Dateipfad oder None zurück."""
    with _page_thumb_slots:
        if out.exists() and out.stat().st_size > 0:
            return out
        thumb_url = it.get("thumb")
        if it["kind"] == "ytdlp" and not thumb_url:
            try:
                thumb_url = resolve_ytdlp(it).get("thumbnail")
            except Exception:
                thumb_url = None
        if not thumb_url:
            return None
        try:
            data, _, ctype = http_get(thumb_url, referer=it.get("referer"))
            if not data:
                return None
            raw = CACHE_DIR / f"{it['id']}.raw"
            raw.write_bytes(data)
            if FFMPEG:  # in ein normales JPEG umwandeln (Seiten liefern oft WebP)
                subprocess.run(
                    [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                     "-vf", "scale=640:-2", "-frames:v", "1", "-q:v", "3", str(out)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            if out.exists() and out.stat().st_size > 0:
                raw.unlink(missing_ok=True)
            else:
                raw.replace(out)
            _thumb_errors.pop(it["id"], None)
            return out
        except Exception as e:
            _thumb_errors[it["id"]] = f"Vorschaubild der Seite nicht ladbar ({thumb_url}): {e}"
            print(f"[thumb] Vorschaubild von {thumb_url} nicht ladbar: {e}")
            return None


def _vr_tile_crop(it):
    """Bei VR-Videos mit quadratischem Einzelbild (z. B. ein Auge von 4320x2160) die Mitte als 16:9 nehmen:
    so sieht das Vorschaubild aus wie ein Kachelbild und nicht wie ein verzerrter Halbkugel-Ausschnitt."""
    screen, _ = format_of(it)
    if screen == "flat":
        return []
    return ["crop=iw:if(lt(iw/ih\\,1.5)\\,trunc(iw*9/32)*2\\,ih)"]


def make_thumb(it, slots=None):
    """Erzeugt ein Vorschaubild (bei 3D-Videos nur das linke/obere Auge)."""
    slots = slots or _thumb_slots
    out = CACHE_DIR / f"{it['id']}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out

    # Webvideo mit Vorschaubild auf der Seite: schnell laden, in eigener Warteschlange. Sie soll nicht
    # hinter den langsamen ffmpeg-Aufträgen für große lokale Dateien warten.
    if it["kind"] != "local":
        got = _thumb_from_page(it, out)
        if got:
            return got

    if it["kind"] == "local":
        probe(it)
        if is_broken(it):
            _thumb_errors[it["id"]] = "Datei defekt, kein Vorschaubild möglich"
            return None
    with slots:
        if out.exists() and out.stat().st_size > 0:
            return out
        if not FFMPEG:
            _thumb_errors[it["id"]] = "ffmpeg nicht gefunden"
            return None
        if it["kind"] == "local":
            probe(it)  # Bildgröße bekannt machen, damit das richtige Auge ausgeschnitten wird
        vf = stereo_crop(it) + _vr_tile_crop(it) + ["scale=640:-2"]
        local = it["kind"] == "local"
        try:
            target, headers = media_source(it)
        except Exception as e:
            _thumb_errors[it["id"]] = f"Keine Quelle: {e}"
            print(f"[thumb] Keine Quelle für {it['name']}: {e}")
            return None
        src_args = [] if local else ff_input_args(headers)
        if local:
            dur = probe(it)["duration"]
            timestamps = (max(dur // 10, 1), 1, 0)
        else:
            timestamps = (5, 1, 0)

        for ts in timestamps:
            try:
                r = subprocess.run(
                    [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                     *src_args, "-noaccurate_seek", "-ss", str(ts), "-i", target,
                     "-an", "-sn", "-frames:v", "1", "-vf", ",".join(vf),
                     "-pix_fmt", "yuvj420p", "-q:v", "3", str(out)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=90 if not local else 60)
            except subprocess.TimeoutExpired:
                _thumb_errors[it["id"]] = f"ffmpeg: Zeitüberschreitung (bei {ts}s)"
                print(f"[ffmpeg] Zeitüberschreitung bei Thumbnail für {it['name']} (bei {ts}s)")
                continue
            if out.exists() and out.stat().st_size > 0:
                _thumb_errors.pop(it["id"], None)
                return out
            tail = r.stderr.decode(errors="ignore").strip()[-500:]
            _thumb_errors[it["id"]] = f"ffmpeg (bei {ts}s): {tail}"
            print(f"[ffmpeg] Thumbnail für {it['name']} bei {ts}s fehlgeschlagen:\n{tail}")
    return None


_inline_cache = {}  # id -> data-URI (kleine Vorschaubilder direkt in der Bibliothek)


def small_thumb_uri(it):
    """Kleines Vorschaubild als data:-URI, falls schon erzeugt. Startet nichts."""
    i = it["id"]
    if i in _inline_cache:
        return _inline_cache[i]
    small = CACHE_DIR / f"{i}.small.jpeg"
    if small.exists() and small.stat().st_size > 0:
        uri = "data:image/jpeg;base64," + base64.b64encode(small.read_bytes()).decode()
        _inline_cache[i] = uri
        return uri
    return None


def make_small_thumb(it):
    big = CACHE_DIR / f"{it['id']}.jpg"
    small = CACHE_DIR / f"{it['id']}.small.jpeg"
    if small.exists() or not big.exists() or not FFMPEG:
        return
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(big),
                    "-vf", "scale=256:-2", "-q:v", "7", str(small)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def inline_thumb_worker():
    """Option thumb_inline: erzeugt für alle Einträge (auch Webquellen) Vorschaubilder und deren
    kleine Fassung für die Bibliothek. Läuft alle 2 Minuten, bis alles da ist."""
    while True:
        missing = 0
        try:
            for it in all_items():
                big = CACHE_DIR / f"{it['id']}.jpg"
                if not big.exists() and it["kind"] != "local":
                    _thumb_from_page(it, big)
                make_small_thumb(it)
                if not (CACHE_DIR / f"{it['id']}.small.jpeg").exists():
                    missing += 1
        except Exception as e:
            print(f"[inline-thumbs] {e}")
        time.sleep(5 if missing else 120)


def pregenerate():
    """Probe-Daten und Vorschaubilder lokaler Videos im Hintergrund erzeugen (3 gleichzeitig).
    Fertige Vorschaubilder bleiben in /data/cache und werden beim nächsten Start übersprungen."""
    items = local_items()
    _progress["total"], _progress["done"] = len(items), 0

    def work(it):
        try:
            probe(it)
            make_thumb(it)
        except Exception as e:
            print(f"[pregenerate] {it['name']}: {e}")
        finally:
            _progress["done"] += 1

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(work, items))
    print("Vorschaubilder lokaler Videos fertig.")


# ---------------------------------------------------------------- Webseiten auslesen (type: web)
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}


def is_video_url(u: str) -> bool:
    return Path(urlparse(u).path).suffix.lower() in VIDEO_EXT


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.videos = []
        self.links = []
        self.meta = {}
        self.title = ""
        self._in_title = False
        self._cur_a = None
        self._cur_video = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            k = a.get("property") or a.get("name")
            if k and a.get("content"):
                self.meta[k.lower()] = a["content"]
        elif tag == "video":
            self._cur_video = {"srcs": [], "poster": a.get("poster"),
                               "title": a.get("title") or a.get("aria-label")}
            if a.get("src"):
                self._cur_video["srcs"].append(a["src"])
        elif tag == "source" and self._cur_video is not None:
            src = a.get("src") or a.get("data-src")
            if src:
                self._cur_video["srcs"].append(src)
        elif tag == "a" and a.get("href"):
            self._cur_a = {"href": a["href"], "text": "", "img": None, "alt": None,
                           "title": a.get("title")}
        elif tag == "img" and self._cur_a is not None and not self._cur_a["img"]:
            src = a.get("src") or ""
            lazy = a.get("data-src") or a.get("data-original") or a.get("data-lazy-src")
            # bei Lazy-Loading steht in src oft nur ein Platzhalter (data:...)
            self._cur_a["img"] = lazy if (lazy and (not src or src.startswith("data:"))) else (src or lazy)
            if a.get("alt"):
                self._cur_a["alt"] = a["alt"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "video" and self._cur_video is not None:
            if self._cur_video["srcs"]:
                self.videos.append(self._cur_video)
            self._cur_video = None
        elif tag == "a" and self._cur_a is not None:
            self.links.append(self._cur_a)
            self._cur_a = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._cur_a is not None:
            self._cur_a["text"] += data


def scrape_page(url, referer=None, headers=None):
    """Liest eine Seite aus. Gibt (Videos, Unterseiten) zurück."""
    data, charset, ctype, status, final_url = http_fetch(url, referer, headers=headers)
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    p = PageParser()
    p.feed(text)
    _page_links[url] = p.links

    found, seen = [], set()

    def add(u, title, thumb):
        if not u:
            return False
        u = urljoin(url, html.unescape(u.strip()))
        if not is_video_url(u) or u in seen:
            return False
        seen.add(u)
        found.append({"url": u, "title": (title or "").strip(),
                      "thumb": urljoin(url, thumb) if thumb else None,
                      "page": url})
        return True

    page_title = (p.meta.get("og:title") or p.title or "").strip()
    page_thumb = p.meta.get("og:image")
    _page_info[url] = {"bytes": len(data), "title": " ".join(page_title.split())[:120],
                       "video_tags": len(p.videos), "links": len(p.links),
                       "status": status, "ctype": ctype.split(";")[0].strip(),
                       "final_url": final_url, "hints": page_hints(text)}

    for v in p.videos:
        srcs = sorted(v["srcs"], key=lambda s: 0 if s.lower().split("?")[0].endswith(".mp4") else 1)
        for s in srcs:
            if add(s, v["title"] or page_title, v["poster"] or page_thumb):
                break

    for k in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        add(p.meta.get(k), page_title, page_thumb)

    for l in p.links:
        add(l["href"], l["text"] or l.get("title"), l["img"])

    subpages = []
    for l in p.links:
        href = urljoin(url, html.unescape(l["href"].strip())).split("#")[0]
        if href.startswith(("http://", "https://")) and not is_video_url(href):
            subpages.append(href)
    return found, subpages


def remote_title(raw_title: str, url: str) -> str:
    t = " ".join((raw_title or "").split())
    if not t:
        t = Path(unquote(urlparse(url).path)).stem
        t = t.replace("_", " ")
    return t[:150]


def source_name(src):
    return (src.get("name") or urlparse(src["url"]).netloc or "Webquelle").strip()


def page_info_text(url: str) -> str:
    """Kurzbeschreibung, was beim Lesen einer Seite herausgekommen ist."""
    pi = _page_info.get(url)
    if not pi:
        return "Seite noch nicht gelesen."
    txt = (f"Seite gelesen: HTTP {pi.get('status')}, {pi.get('ctype') or '?'}, "
           f"{pi.get('bytes', 0)} Bytes, Titel '{pi.get('title', '')}', "
           f"{pi.get('video_tags', 0)} video-Tags, {pi.get('links', 0)} Links.")
    if pi.get("final_url") and pi["final_url"] != url:
        txt += f" Weitergeleitet nach {pi['final_url']}."
    if pi.get("hints"):
        txt += " Hinweis im Seitentext: " + ", ".join(pi["hints"]) + "."
    return txt


def refresh_web(src, name):
    url = src["url"]
    depth = int(src.get("depth") or 0)
    max_pages = int(src.get("max_pages") or 30)
    fmt = (src.get("format") or "").strip()
    proxy = bool(src.get("proxy", False))

    found, subs = scrape_page(url)
    results = list(found)
    if depth >= 1:
        host = urlparse(url).netloc
        visited = {url}
        for sp in subs:
            if len(visited) > max_pages:
                break
            if sp in visited or urlparse(sp).netloc != host:
                continue
            visited.add(sp)
            time.sleep(0.3)  # höflich bleiben
            try:
                f2, _ = scrape_page(sp, referer=url)
                results.extend(f2)
            except Exception as e:
                print(f"[source:{name}] Unterseite {sp} fehlgeschlagen: {e}")
    if not results:
        raise RuntimeError(
            f"0 Videodateien gefunden. {page_info_text(url)} "
            f"Typische Ursachen: Altersabfrage oder Bot-Schutz, Inhalte werden per JavaScript "
            f"nachgeladen, oder die Videos liegen nur auf Detailseiten. Dann type: ytdlp verwenden.")
    items, seen = [], set()
    for f in results:
        if f["url"] in seen:
            continue
        seen.add(f["url"])
        basename = unquote(Path(urlparse(f["url"]).path).name)
        title = remote_title(f["title"], f["url"])
        items.append({
            "id": make_id("remote:" + f["url"]),
            "kind": "remote",
            "cat": name,
            "title": title,
            "name": f"{basename} {title}",
            "ext": Path(urlparse(f["url"]).path).suffix.lower(),
            "url": f["url"],
            "thumb": f["thumb"],
            "referer": f["page"],
            "fmt": fmt,
            "proxy": proxy,
        })
    return items


# Standardmuster für Links auf Videoseiten (nur der Pfadteil der Adresse wird geprüft)
LISTING_PATTERN = r"/(?:video|videos|watch|view|movie|hd-porn)[-/_]"


def scrape_listing(src, name, limit, fmt, max_height, cookies):
    """Rückfall: Die Listenseite selbst auslesen und die Links auf Videoseiten sammeln.
    Die eigentliche Videoadresse löst später yt-dlp pro Video auf."""
    url = src["url"]
    pattern = re.compile(src.get("link_pattern") or LISTING_PATTERN, re.I)
    scrape_page(url, headers={"Cookie": cookies} if cookies else None)  # füllt _page_info/_page_links
    host = urlparse(url).netloc
    items, seen = [], set()
    for l in _page_links.get(url, []):
        href = urljoin(url, html.unescape(l["href"].strip())).split("#")[0]
        if (not href.startswith(("http://", "https://")) or urlparse(href).netloc != host
                or href in seen or href.rstrip("/") == url.rstrip("/")):
            continue
        if not pattern.search(urlparse(href).path):
            continue
        seen.add(href)
        thumb = l.get("img")
        thumb = urljoin(url, thumb) if thumb and not thumb.startswith("data:") else None
        title = remote_title(l.get("title") or l.get("alt") or l.get("text") or "", href)
        items.append({
            "id": make_id("ytdlp:" + href),
            "kind": "ytdlp",
            "cat": name,
            "title": title,
            "name": f"{title} {unquote(Path(urlparse(href).path.rstrip('/')).name)}",
            "ext": ".mp4",
            "page": href,
            "thumb": thumb,
            "referer": href,
            "fmt": fmt,
            "max_height": max_height,
            "cookies": cookies,
            "warmup": src.get("warmup", True) is not False,
            "duration": 0,
        })
        if len(items) >= limit:
            break
    if not items:
        raise RuntimeError(f"keine Links auf Videoseiten gefunden (Muster '{pattern.pattern}'; "
                           f"mit link_pattern anpassbar). {page_info_text(url)}")
    return items


def refresh_ytdlp(src, name):
    url = src["url"]
    limit = int(src.get("limit") or 50)
    fmt = (src.get("format") or "").strip()
    max_height = src.get("max_height")
    cookies = (src.get("cookies") or "").strip() or None

    entries, ytdlp_err = [], None
    try:
        data = run_ytdlp(["--flat-playlist", "-J", "--playlist-end", str(limit), url],
                         timeout=180, cookies=cookies)
        entries = data.get("entries") or [data]
    except Exception as e:
        ytdlp_err = str(e)
    items, seen = [], set()
    for e in entries:
        if not e:
            continue
        page = e.get("webpage_url") or e.get("url") or (url if e is data else None)
        if not page or not str(page).startswith("http") or page in seen:
            continue
        seen.add(page)
        thumb = e.get("thumbnail")
        if not thumb and e.get("thumbnails"):
            thumb = (e["thumbnails"][-1] or {}).get("url")
        title = remote_title(e.get("title") or "", page)
        items.append({
            "id": make_id("ytdlp:" + page),
            "kind": "ytdlp",
            "cat": name,
            "title": title,
            "name": f"{title} {unquote(Path(urlparse(page).path).name)}",
            "ext": ".mp4",
            "page": page,
            "thumb": thumb,
            "referer": page,
            "fmt": fmt,
            "max_height": max_height,
            "cookies": cookies,
            "warmup": src.get("warmup", True) is not False,
            "duration": int(e.get("duration") or 0),
        })
    if not items:
        # yt-dlp kennt diese Listenseite nicht (oder lieferte nichts): Links aus der Seite sammeln
        try:
            items = scrape_listing(src, name, limit, fmt, max_height, cookies)
        except Exception as e:
            raise RuntimeError(f"yt-dlp-Liste: {ytdlp_err or 'leer'} | Seitenauswertung: {e}")
        print(f"[source:{name}] yt-dlp kannte die Liste nicht ({ytdlp_err or 'leer'}), "
              f"{len(items)} Links aus der Seite übernommen")
    return items


def refresh_source(src):
    name = source_name(src)
    stype = (src.get("type") or "web").lower()
    err = None
    items = []
    try:
        items = refresh_ytdlp(src, name) if stype == "ytdlp" else refresh_web(src, name)
        print(f"[source:{name}] {len(items)} Videos gefunden ({stype})")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[source:{name}] Fehler: {err}")

    old = _remote.get(name)
    if err and old:  # bei Fehlern die alte Liste behalten
        old["error"] = err
        return
    _remote[name] = {"items": items, "updated": time.time(), "error": err,
                     "url": src["url"], "type": stype}


def refresh_all():
    for src in SOURCES:
        try:
            refresh_source(src)
        except Exception as e:
            print(f"[refresh] {e}")


WARMUP_PAUSE = 3  # Sekunden Pause zwischen zwei yt-dlp-Auflösungen (schont die Seite)


def warmup_ytdlp():
    """Löst yt-dlp-Videos nacheinander im Hintergrund auf, damit Bildgröße (VR-Format) und Länge
    bekannt sind. Bewusst langsam, damit die Seite nicht überlastet wird."""
    todo = [i for i in remote_items()
            if i["kind"] == "ytdlp" and i.get("warmup", True) and i["id"] not in _meta]
    if todo:
        print(f"[warmup] {len(todo)} Webvideos werden nacheinander aufgelöst (Format/Länge)")
    for it in todo:
        try:
            resolve_ytdlp(it)
        except Exception:
            pass
        time.sleep(WARMUP_PAUSE)


def refresh_loop():
    while True:
        refresh_all()
        warmup_ytdlp()
        time.sleep(max(REFRESH_MIN, 1) * 60)


# ---------------------------------------------------------------- Streaming-Proxy
def open_upstream(it, force=False):
    url, headers = media_source(it, force=force)
    headers.setdefault("User-Agent", UA)
    headers.pop("Accept-Encoding", None)
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)


def proxy_stream(it):
    r = None
    for attempt in (0, 1):
        try:
            r = open_upstream(it, force=(attempt == 1))
            break
        except urllib.error.HTTPError as e:
            # abgelaufene yt-dlp-Adresse: einmal neu auflösen
            if it["kind"] == "ytdlp" and attempt == 0 and e.code in (401, 403, 404, 410):
                continue
            abort(e.code if 400 <= e.code < 600 else 502)
        except Exception as e:
            print(f"[proxy] {it['name']}: {e}")
            abort(502)
    if r is None:
        abort(502)

    out = {}
    for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
              "Last-Modified", "ETag"):
        if r.headers.get(h):
            out[h] = r.headers[h]
    out.setdefault("Content-Type", MIMETYPES.get(it["ext"], "video/mp4"))
    out.setdefault("Accept-Ranges", "bytes")

    def gen():
        try:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            r.close()

    return Response(stream_with_context(gen()), status=r.status, headers=out)


# ---------------------------------------------------------------- Routen
@app.before_request
def start_timer():
    request.environ["_t0"] = time.time()


@app.after_request
def log_request(resp):
    """Merkt sich die letzten Anfragen, damit /debug zeigt, was die Quest wirklich abruft."""
    if request.path in ("/debug", "/selftest", "/favicon.ico"):
        return resp
    # Bilder und Links, die die Diagnoseseite selbst lädt, zählen nicht (sonst sieht man nicht,
    # was die Quest abruft)
    if (request.headers.get("Referer") or "").split("?")[0].rstrip("/").endswith("/debug"):
        return resp
    entry = (time.strftime("%H:%M:%S"), request.remote_addr or "?", request.method,
             request.full_path.rstrip("?"), resp.status_code,
             (request.headers.get("User-Agent") or "")[:80],
             "Range" if request.headers.get("Range") else "",
             int((time.time() - request.environ.get("_t0", time.time())) * 1000))
    kind = ("Bibliothek" if request.path == "/deovr" else
            "Video-JSON" if request.path.startswith("/deovr/") else
            "Vorschaubild" if request.path.startswith("/thumb/") else
            "Videodatei" if request.path.startswith("/video/") else "sonstige")
    with _requests_lock:
        _requests.append(entry)
        del _requests[:-60]
        s = _stats.setdefault(kind, {})
        s[resp.status_code] = s.get(resp.status_code, 0) + 1
        if kind != "sonstige":
            _stats_last[kind] = (entry[0], entry[5])
        if kind == "Vorschaubild":
            _thumb_log.append(entry)
            del _thumb_log[:-15]
    return resp


@app.route("/")
def index():
    return (f"<h1>PlayaVR Server läuft</h1><p>Version {VERSION}</p>"
            f"<p>In DeoVR öffnen: <code>{base_url()}/deovr</code></p>"
            f"<p>In Playa verbinden: <code>{base_url()}</code> (Playa API v2)</p>"
            f"<p><a href='/debug'>Diagnose</a> · <a href='/selftest'>Selbsttest</a></p>")


@app.route("/selftest")
def selftest():
    """Prüft Schritt für Schritt, ob Thumbnail, JSON und Abruf der eigenen Adressen funktionieren."""
    lines = []

    def say(ok, text):
        lines.append(f"[{'OK ' if ok else 'FEHLER'}] {text}")

    say(True, f"Version {VERSION}")
    say(bool(FFMPEG), f"ffmpeg: {FFMPEG or 'nicht gefunden'}")
    say(bool(FFPROBE), f"ffprobe: {FFPROBE or 'nicht gefunden'}")
    say(True, f"Anfrage kam an mit Host='{request.host}', Client={request.remote_addr}, "
              f"Basis-URL für Links='{base_url()}'")
    host = request.host.rsplit(":", 1)[0] if ":" in request.host else request.host
    if not PUBLIC_URL and not host_is_ip(host):
        say(False, "Die Adresse enthält einen Namen statt einer IP (z. B. homeassistant.local). "
                   "Die Quest löst solche Namen oft nicht auf. Bibliothek in DeoVR mit der IP öffnen "
                   "oder public_url setzen.")

    items = all_items()
    local = [i for i in items if i["kind"] == "local"]
    say(bool(items), f"{len(local)} lokale Videos, {len(items) - len(local)} Videos aus Webquellen")
    it = (local or items or [None])[0]
    if it is None:
        say(False, f"Keine Videos gefunden. Videoordner: {VIDEO_DIR} (existiert: {VIDEO_DIR.exists()})")
    else:
        say(True, f"Testvideo: {it['title']} ({it['kind']})")
        if it["kind"] == "local":
            p = it["path"]
            say(os.access(p, os.R_OK), f"Datei lesbar: {p} ({p.stat().st_size} Bytes)")
        try:
            info = probe(it)
            say(info["ok"], f"ffprobe: {info['vcodec']} {info['profile']} {info['pix_fmt']} "
                            f"{info['width']}x{info['height']}, Audio {info['acodec']}, {info['duration']} s")
            for w in warnings_for(info):
                say(False, w)
        except Exception as e:
            say(False, f"ffprobe: {e}")

        # Thumbnail frisch erzeugen (ohne Cache)
        if FFMPEG and it["kind"] == "local":
            tmp = CACHE_DIR / "selftest.jpg"
            tmp.unlink(missing_ok=True)
            try:
                r = subprocess.run(
                    [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", "1",
                     "-i", str(it["path"]), "-an", "-sn", "-frames:v", "1",
                     "-vf", ",".join(stereo_crop(it) + ["scale=640:-2"]), "-q:v", "3", str(tmp)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
                ok = tmp.exists() and tmp.stat().st_size > 0
                say(ok, f"Thumbnail erzeugen: {'Datei ' + str(tmp.stat().st_size) + ' Bytes' if ok else 'fehlgeschlagen'}")
                if not ok:
                    lines.append(r.stderr.decode(errors="ignore").strip()[-800:])
            except Exception as e:
                say(False, f"Thumbnail erzeugen: {e}")
            tmp.unlink(missing_ok=True)

        # Eigene Adressen so abrufen, wie sie in den JSON-Antworten stehen
        for label, url in (("Thumbnail", f"{base_url()}/thumb/{it['id']}.jpg"),
                           ("Detail-JSON", f"{base_url()}/deovr/{it['id']}")):
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    body = r.read()
                say(True, f"{label} von der Server-Seite abrufbar: {url} -> HTTP {r.status}, {len(body)} Bytes, "
                          f"{r.headers.get('Content-Type')}")
                if label == "Detail-JSON":
                    probs = validate_detail(json.loads(body))
                    say(not probs, "Detail-JSON entspricht dem Beispiel der DeoVR-Doku" if not probs
                        else "Detail-JSON weicht von der DeoVR-Doku ab: " + "; ".join(probs))
            except Exception as e:
                say(False, f"{label} NICHT abrufbar: {url} -> {e}")
                if not PUBLIC_URL and not host_is_ip(host):
                    lines.append("       (Geprüft aus dem Add-on heraus. Die Quest kann den Namen "
                                 "womöglich ebenfalls nicht auflösen. Mit der IP-Adresse testen.)")

    for src in SOURCES:
        name = source_name(src)
        stype = (src.get("type") or "web").lower()
        st = _remote.get(name)
        if st is None:
            say(False, f"Quelle '{name}' ({stype}): noch nicht geladen")
        elif st["error"]:
            say(False, f"Quelle '{name}' ({stype}): {st['error']}")
        else:
            say(True, f"Quelle '{name}' ({stype}): {len(st['items'])} Videos")
        if stype == "web":
            lines.append("       " + page_info_text(src["url"]))
    if any((s.get("type") or "").lower() == "ytdlp" for s in SOURCES):
        say(bool(YTDLP), f"yt-dlp: {YTDLP or 'nicht gefunden'}, Version {ytdlp_version()}")

    lines.append("")
    lines.append("Hinweis: Diese Prüfung läuft im Add-on selbst. Ob die Quest die Adressen erreicht, "
                 "zeigt die Tabelle 'Letzte Anfragen' auf /debug, nachdem du in DeoVR die Bibliothek geöffnet hast.")
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


@app.route("/deovr", methods=["GET", "POST"])
def deovr_library():
    if THUMB_TEST:  # Test: /deovr liefert die kleine Testbibliothek, damit sie über die Domain geöffnet werden kann
        return deovr_test()
    cats = {}
    for it in all_items():
        if HIDE_BROKEN and is_broken(it):
            continue
        i = it["id"]
        cats.setdefault(it["cat"], []).append({
            "title": it["title"],
            "videoLength": (it.get("duration") or (_meta.get(it["id"]) or {}).get("dur")
                            or cached_probe(it)["duration"]),
            "video_url": f"{base_url()}/deovr/{i}",
            "thumbnailUrl": (small_thumb_uri(it) if THUMB_INLINE else None) or f"{thumb_base()}{thumb_path(i)}",
        })
    scenes = [{"name": c, "list": l} for c, l in cats.items()]
    if MANUAL_CAT:
        # Zweite Kategorie mit denselben Videos, aber ohne Formatvorgabe: dort bleibt DeoVRs eigener
        # Projektions-Knopf, falls die Automatik daneben liegt.
        allv = [dict(v, video_url=v["video_url"] + "_m") for l in cats.values() for v in l]
        scenes.append({"name": MANUAL_NAME, "list": allv})
    return jsonify({"scenes": scenes, "authorized": "1"})


@app.route("/deovr-test")
def deovr_test():
    """Kleine Testbibliothek: dasselbe Video mit drei verschiedenen Arten von Vorschaubild.
    Zeigt in DeoVR, welche davon angezeigt wird."""
    it = next((x for x in all_items()
               if (CACHE_DIR / f"{x['id']}.jpg").exists() and not is_broken(x)), None)
    if not it:
        return jsonify({"scenes": [{"name": "Test", "list": []}], "authorized": "1"})
    make_small_thumb(it)
    base = {"videoLength": (cached_probe(it)["duration"] or 60), "video_url": f"{base_url()}/deovr/{it['id']}"}
    items = [
        dict(base, title="TEST 1 - Bild per HTTP vom Add-on", thumbnailUrl=f"{base_url()}/thumb/{it['id']}.jpg"),
        *([dict(base, title="TEST 4 - Bild per HTTPS vom Add-on",
                thumbnailUrl=f"{HTTPS_URL}/thumb/{it['id']}.jpg")] if HTTPS_URL else []),
        dict(base, title="TEST 2 - Bild eingebettet (data-URI)",
             thumbnailUrl=small_thumb_uri(it) or f"{base_url()}/thumb/{it['id']}.jpg"),
        dict(base, title="TEST 5 - JPEG per HTTP aus dem Internet",
             thumbnailUrl="http://httpbin.org/image/jpeg"),
        dict(base, title="TEST 6 - JPEG per HTTPS aus dem Internet",
             thumbnailUrl="https://httpbin.org/image/jpeg"),
        dict(base, title="TEST 3 - Bild per HTTPS aus dem Internet",
             thumbnailUrl="https://www.google.com/images/branding/googlelogo/1x/googlelogo_color_272x92dp.png"),
    ]
    return jsonify({"scenes": [{"name": "Thumbnail-Test", "list": items}], "authorized": "1"})


@app.route("/deovr/<i>")
def deovr_video(i):
    manual = i.endswith("_m")
    if manual:
        i = i[:-2]
    it = find_item(i)
    if not it:
        abort(404)
    # DeoVR holt die Detail-JSON vieler Einträge auf einmal. Deshalb hier weder yt-dlp noch ffprobe
    # für Webvideos starten; aufgelöst wird erst beim Abspielen (/video/...).
    if it["kind"] == "local":
        info = probe(it)
    elif it["kind"] == "ytdlp":
        r = _resolve_cache.get(it["id"]) or {}
        m = _meta.get(it["id"]) or {}
        info = {"duration": r.get("duration") or m.get("dur") or it.get("duration") or 0,
                "height": r.get("height") or m.get("h") or int(it.get("max_height") or 1080)}
    else:
        info = cached_probe(it)
    screen, stereo = format_of(it)  # erst jetzt: die Bildgröße ist nun bekannt

    mode = "none" if manual else FORMAT_MODE
    flags = filename_flags(screen, stereo) if mode in ("filename", "title") else ""
    if it["kind"] == "remote" and not it["proxy"]:
        url = it["url"]  # direkter Link zur Originaldatei
    else:
        url = f"{base_url()}/video/{i}{flags}{it['ext']}"

    thumb_url = f"{thumb_base()}{thumb_path(i)}"
    video_source = {"resolution": info["height"], "url": url}
    data = {
        "id": numeric_id(i) + (100000000 if manual else 0),
        "title": it["title"] + (flags if mode == "title" else ""),
        "authorized": 1,
        "videoLength": int(info["duration"] or 0),
        "skipIntro": 0,
        "thumbnailUrl": thumb_url,
        # Laut DeoVR-Doku soll is3d immer true sein, sonst erzwingt DeoVR Mono und blendet die
        # Projektionen aus. Mono wird über stereoMode "off" ausgedrückt (so macht es auch XBVR).
        "is3d": True,
        # Laut DeoVR-Doku ist "h264" der dokumentierte Name; der echte Codec
        # (z. B. hevc) wird vom Player aus der Datei selbst erkannt.
        "encodings": [{
            "name": "h264",
            "videoSources": [video_source],
        }],
    }
    if mode in ("json", "screen"):
        data["screenType"] = screen
    if mode in ("json", "stereo"):
        data["stereoMode"] = stereo or "off"
    _last_detail[:] = [json.dumps(data, indent=1, ensure_ascii=False)]
    return jsonify(data)


@app.route("/video/<i>.<ext>")
def video(i, ext):
    i = i.split("_")[0]  # Format-Anhang im Dateinamen (z. B. abc123_180_LR) abschneiden
    it = find_item(i)
    if not it:
        abort(404)
    if it["kind"] == "local":
        return send_file(it["path"], conditional=True,
                         mimetype=MIMETYPES.get(it["ext"], "video/mp4"))
    if it["kind"] == "ytdlp" or it["proxy"]:
        return proxy_stream(it)
    return redirect(it["url"], 302)


@app.route("/thumb/<v>/<i>.jpg")
def thumb_versioned(v, i):
    return thumb(i)


@app.route("/thumb/<i>.jpg")
def thumb(i):
    it = find_item(i)
    if not it:
        abort(404)
    out = make_thumb(it, _req_thumb_slots)
    if not out:
        abort(404)
    return send_file(out, mimetype="image/jpeg", max_age=86400)


FORMAT_MODES = [
    ("json", "json: screenType + stereoMode im JSON (Erkennung verbindlich, DeoVR-Knopf meist weg)"),
    ("filename", "filename: Kürzel in der Video-Adresse"),
    ("title", "title: Kürzel im Titel + in der Video-Adresse"),
    ("screen", "screen: nur screenType im JSON"),
    ("stereo", "stereo: nur stereoMode im JSON"),
    ("none", "none: gar nichts senden (nur DeoVR-Menü)"),
]


@app.route("/setmode")
def setmode():
    global FORMAT_MODE
    m = request.args.get("m", "")
    if m not in {c for c, _ in FORMAT_MODES}:
        abort(400)
    FORMAT_MODE = m
    return redirect("/debug")


@app.route("/setformat")
def setformat():
    i = request.args.get("id", "")
    fmt = request.args.get("fmt", "")
    if not find_item(i) or fmt not in {c for c, _ in FORMAT_CHOICES}:
        abort(400)
    set_format(i, fmt)
    _inline_cache.pop(i, None)
    return redirect(f"/debug#v{i}")


@app.route("/refresh")
def refresh():
    threading.Thread(target=refresh_all, daemon=True).start()
    return redirect("/debug")


def format_select(i):
    cur = _formats.get(i, "")
    opts = "".join(f"<option value='{v}'{' selected' if v == cur else ''}>{html.escape(t)}</option>"
                   for v, t in FORMAT_CHOICES)
    return f"<select onchange=\"location='/setformat?id={i}&fmt='+encodeURIComponent(this.value)\">{opts}</select>"


@app.route("/debug")
def debug():
    rows = []
    for it in all_items():
        info = cached_probe(it)
        screen, stereo = format_of(it)
        w = "<br>".join(html.escape(x) for x in warnings_for(info)) or "–"
        if is_broken(it):
            w = html.escape(_broken[_probe_key(it)])
        if it["kind"] == "ytdlp" and it["id"] in _resolve_errors:
            w = html.escape("yt-dlp: " + _resolve_errors[it["id"]])
        thumb_ok = (CACHE_DIR / f"{it['id']}.jpg").exists()
        thumb_txt = f"<img class='t' src='/thumb/{it['id']}.jpg' loading='lazy'>" if thumb_ok else "nein"
        if not thumb_ok and it["id"] in _thumb_errors:
            thumb_txt += "<br><small>" + html.escape(_thumb_errors[it["id"]][-300:]) + "</small>"
        codec = (f"{info['vcodec']} {info['profile']} {info['pix_fmt']}<br>"
                 f"{info['width']}x{info['height']}, Audio: {info['acodec']}"
                 if info["ok"] else "(wird beim Abspielen ermittelt)")
        kind = {"local": "lokal", "ytdlp": "yt-dlp (Proxy)"}.get(
            it["kind"], "Web (Proxy)" if it.get("proxy") else "Web (direkt)")
        rows.append(
            f"<tr id='v{it['id']}'><td>{html.escape(it['cat'])}</td><td>{kind}</td>"
            f"<td>{html.escape(it['title'])}</td>"
            f"<td>{screen} / {stereo or 'mono'}{' (manuell)' if it['id'] in _formats else ''}<br>"
            f"{format_select(it['id'])}</td><td>{codec}</td>"
            f"<td>{thumb_txt}</td><td>{w}</td>"
            f"<td><a href='/deovr/{it['id']}'>JSON</a></td></tr>")

    src_rows = []
    for src in SOURCES:
        name = source_name(src)
        st = _remote.get(name)
        stype = (src.get("type") or "web").lower()
        if st:
            upd = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(st["updated"]))
            src_rows.append(
                f"<tr><td>{html.escape(name)}</td><td>{stype}</td><td>{html.escape(src['url'])}</td>"
                f"<td>{len(st['items'])}</td><td>{upd}</td>"
                f"<td>{html.escape(st['error'] or '–')}</td>"
                f"<td>{html.escape(page_info_text(src['url'])) if stype == 'web' else '–'}</td></tr>")
        else:
            src_rows.append(
                f"<tr><td>{html.escape(name)}</td><td>{stype}</td><td>{html.escape(src['url'])}</td>"
                f"<td>0</td><td>noch nicht geladen</td><td>–</td><td>–</td></tr>")
    src_table = (
        "<h3>Webquellen</h3><table><tr><th>Name</th><th>Typ</th><th>URL</th><th>Videos</th>"
        "<th>Aktualisiert</th><th>Fehler</th><th>Was die Seite geliefert hat</th></tr>" + "".join(src_rows) +
        "</table><p><a href='/refresh'>Webquellen jetzt neu einlesen</a></p>"
        if SOURCES else "<p>Keine Webquellen konfiguriert.</p>")

    with _requests_lock:
        recent = list(reversed(_requests))
    req_rows = "".join(
        f"<tr><td>{t}</td><td>{html.escape(ip)}</td><td>{m}</td><td>{html.escape(path)}</td>"
        f"<td>{code}</td><td>{ms} ms</td><td>{html.escape(ua)}</td><td>{rng}</td></tr>"
        for t, ip, m, path, code, ua, rng, ms in recent)
    req_table = (
        "<h3>Letzte Anfragen (neueste oben)</h3>"
        "<p>Hier siehst du, was die Quest wirklich abruft. Öffne in DeoVR die Bibliothek und lade diese Seite neu. "
        "Fehlen <code>/thumb/…</code>-Zeilen, erreicht die Quest die Bild-Adressen gar nicht. "
        "Stehen dort 404, gibt es das Bild nicht.</p>"
        "<table><tr><th>Zeit</th><th>Client</th><th>Methode</th><th>Pfad</th><th>Status</th>"
        "<th>Dauer</th><th>User-Agent</th><th></th></tr>"
        + (req_rows or "<tr><td colspan=8>noch keine Anfragen</td></tr>") +
        "</table>")

    with _requests_lock:
        stat_rows = "".join(
            f"<tr><td>{k}</td><td>{', '.join(f'{c}: {n}x' for c, n in sorted(_stats[k].items()))}</td>"
            f"<td>{html.escape(_stats_last[k][0])}</td><td>{html.escape(_stats_last[k][1])}</td></tr>"
            for k in ("Bibliothek", "Video-JSON", "Vorschaubild", "Videodatei") if k in _stats)
    stats_table = (
        "<h3>Was DeoVR seit dem Start abgerufen hat</h3>"
        "<table><tr><th>Art</th><th>Antworten (Status: Anzahl)</th><th>Zuletzt</th><th>User-Agent</th></tr>"
        + (stat_rows or "<tr><td colspan=4>noch nichts</td></tr>") + "</table>"
        "<p>Steht bei <b>Vorschaubild</b> gar nichts, fragt DeoVR die Bilder nicht ab (dann liegt es am "
        "Client, nicht an den Bildern). Stehen dort 200er, kommen die Bilder an.</p>")

    with _requests_lock:
        tl = list(reversed(_thumb_log))
    thumb_rows = "".join(
        f"<tr><td>{t}</td><td>{html.escape(ip)}</td><td>{html.escape(path)}</td><td>{code}</td>"
        f"<td>{ms} ms</td><td>{html.escape(ua)}</td></tr>"
        for t, ip, m, path, code, ua, rng, ms in tl)
    stats_table += (
        "<h3>Letzte Vorschaubild-Anfragen (ohne die Bilder dieser Diagnoseseite)</h3>"
        "<table><tr><th>Zeit</th><th>Client</th><th>Pfad</th><th>Status</th><th>Dauer</th><th>User-Agent</th></tr>"
        + (thumb_rows or "<tr><td colspan=6>noch keine</td></tr>") + "</table>")

    host = request.host.rsplit(":", 1)[0] if ":" in request.host else request.host
    hint = ""
    if not PUBLIC_URL and not host_is_ip(host):
        hint = ("<br><b>Hinweis:</b> Die Adresse enthält einen Namen statt einer IP. Der DeoVR-Player "
                "löst Namen wie <code>*.local</code> auf der Quest oft nicht auf, dann fehlen "
                "Vorschaubilder und Videos starten nicht. Öffne die Bibliothek in DeoVR mit der IP "
                "oder setze die Option <code>public_url</code>.")
    return (
        "<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>PlayaVR Server – Diagnose</title><style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px auto;max-width:1200px;"
        "padding:0 16px;color:#1b1f24;background:#f6f8fa;line-height:1.5}"
        "h2{margin-top:0}h3{margin:28px 0 8px}"
        "table{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;"
        "box-shadow:0 1px 3px #0002;font-size:14px}"
        "th{background:#24292f;color:#fff;text-align:left}"
        "td,th{padding:6px 10px;vertical-align:top;border-bottom:1px solid #e3e6ea}"
        "img.t{width:96px;border-radius:4px;display:block}"
        "pre{background:#fff;padding:12px;border-radius:8px;overflow:auto}"
        "code{background:#e8ecf0;padding:1px 5px;border-radius:4px}"
        "@media(prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}"
        "table,pre{background:#161b22}td,th{border-color:#30363d}th{background:#21262d}"
        "code{background:#30363d}a{color:#58a6ff}}</style>"
        f"<h2>Diagnose</h2><p><b>Version {VERSION}</b> · <a href='/selftest'>Selbsttest ausführen</a><br>"
        f"Vorschaubilder lokaler Videos: {_progress['done']} von {_progress['total']} bearbeitet, "
        f"{sum(1 for f in CACHE_DIR.glob('*.jpg'))} Bilder im Zwischenspeicher<br>"
        f"Vorschaubild-Tunnel (thumb_tunnel): {html.escape(_tunnel['status'])}"
        f"{' – ' + html.escape(_tunnel['url']) if _tunnel['url'] else ''}<br>"
        f"HTTPS für Vorschaubilder: {html.escape(HTTPS_URL) + ' – ' + html.escape(_https_state['status']) if HTTPS_URL else 'nicht eingerichtet (Option https_url)'}<br>"
        "Format-Übergabe an DeoVR (Test, gilt bis zum Neustart; danach Bibliothek in DeoVR neu laden): "
        "<select onchange=\"location='/setmode?m='+this.value\">"
        + "".join(f"<option value='{v}'{' selected' if v == FORMAT_MODE else ''}>{html.escape(t)}</option>"
                  for v, t in FORMAT_MODES)
        + "</select><br>"
        f"VR-Format automatisch erkennen: {'ja' if AUTO_FORMAT else 'nein'}"
        f"{', Standardformat lokal: ' + html.escape(DEFAULT_FORMAT) if DEFAULT_FORMAT else ''}<br>"
        f"ffmpeg: {FFMPEG or 'NICHT GEFUNDEN'}<br>"
        f"ffprobe: {FFPROBE or 'NICHT GEFUNDEN'}<br>"
        f"yt-dlp: {YTDLP or 'NICHT GEFUNDEN'}<br>Videoordner: {VIDEO_DIR}<br>"
        f"Basis-URL in den JSON-Antworten: <code>{html.escape(base_url())}</code>{hint}</p>"
        + stats_table + src_table +
        "<h3>Videos</h3><table><tr><th>Kategorie</th><th>Quelle</th><th>Titel</th>"
        "<th>Format</th><th>Codec</th><th>Thumb</th><th>Warnungen</th><th></th></tr>"
        + "".join(rows) + "</table>"
        + req_table
        + "<h3>Zuletzt an DeoVR gelieferte Video-JSON</h3><pre>"
        + html.escape(_last_detail[0] if _last_detail else "noch keine Anfrage") + "</pre>")


_TUNNEL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
THUMB_PORT = int(os.environ.get("THUMB_PORT", 8001))


def build_thumb_app():
    """Eigene kleine App, die NUR Vorschaubilder ausliefert. Nur sie wird durch den Tunnel erreichbar,
    nie die Bibliothek, Videos oder die Diagnoseseite."""
    t = Flask("thumbs")
    t.add_url_rule("/thumb/<v>/<i>.jpg", view_func=thumb_versioned)
    t.add_url_rule("/thumb/<i>.jpg", view_func=thumb)
    t.before_request(start_timer)
    t.after_request(log_request)
    return t


def run_tunnel():
    """Startet einen Cloudflare-Quick-Tunnel zur Vorschaubild-App und übernimmt dessen https-Adresse."""
    exe = shutil.which("cloudflared")
    if not exe:
        _tunnel["status"] = "FEHLER: cloudflared nicht installiert (Add-on neu bauen, siehe Doku)"
        print("[tunnel] " + _tunnel["status"])
        return
    from werkzeug.serving import make_server
    try:
        srv = make_server("127.0.0.1", THUMB_PORT, build_thumb_app(), threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except OSError as e:
        _tunnel["status"] = f"FEHLER: Port {THUMB_PORT} nicht frei ({e})"
        print("[tunnel] " + _tunnel["status"])
        return
    while True:
        _tunnel["url"], _tunnel["status"] = "", "startet ..."
        try:
            proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{THUMB_PORT}"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                m = _TUNNEL_RE.search(line)
                if m and not _tunnel["url"]:
                    _tunnel["url"], _tunnel["status"] = m.group(0), "läuft"
                    print(f"[tunnel] läuft: {_tunnel['url']}")
            proc.wait()
            _tunnel["status"] = f"beendet (Code {proc.returncode}), neuer Versuch in 30 s"
        except Exception as e:
            _tunnel["status"] = f"FEHLER: {e}, neuer Versuch in 30 s"
        print("[tunnel] " + _tunnel["status"])
        _tunnel["url"] = ""
        time.sleep(30)


def start_https():
    """Zweiter Server mit TLS, nur für Vorschaubilder (und alles andere, falls gewünscht).
    Braucht ein gültiges Zertifikat für den Namen in https_url, z. B. vom DuckDNS-Add-on in /ssl."""
    try:
        if not (Path(SSL_CERT).exists() and Path(SSL_KEY).exists()):
            _https_state["status"] = f"FEHLER: Zertifikat nicht gefunden ({SSL_CERT} / {SSL_KEY})"
            print("[https] " + _https_state["status"])
            return
        from werkzeug.serving import make_server
        srv = make_server("0.0.0.0", HTTPS_PORT, app, threaded=True, ssl_context=(SSL_CERT, SSL_KEY))
        _https_state["status"] = f"läuft auf Port {HTTPS_PORT}"
        print(f"[https] {_https_state['status']}, Adresse in den JSONs: {HTTPS_URL}")
        srv.serve_forever()
    except Exception as e:
        _https_state["status"] = f"FEHLER: {e}"
        print(f"[https] {_https_state['status']}")


# ---------------------------------------------------------------- Playa API (PlayaVR-App)
# Die Playa-App verbindet http://<server> und erwartet dort GET /api/playa/v2/... im Rsp-Format
# ({"status": {"code": 1}, "data": ...}). Authentifizierung ist abgeschaltet (config auth false),
# die App fragt dann kein Profil und keine Codes ab. Gespielt wird über die bestehenden
# /video/...-Adressen (Range-Anfragen werden bedient), Vorschaubilder über /thumb/...
PLAYA_API_VERSION = "1.14"
PLAYA_SITE_NAME = os.environ.get("PLAYA_SITE_NAME", "PlayaVR Server")

_PLAYA_SCREEN = {"flat": "FLT", "dome": "180", "sphere": "360",
                 "fisheye": "FSH", "mkx200": "FSH", "rf52": "FSH"}
_PLAYA_STEREO = {"sbs": "LR", "tb": "TB"}


def playa_rsp(data=None, code=1, message=""):
    """Antwort im Playa-Format Rsp<T>. code=1 ist Ok; Fehlercodes wie 404 stehen im JSON, HTTP bleibt 200."""
    rsp = {"status": {"code": code}}
    if message:
        rsp["status"]["message"] = message  # type: ignore[assignment]
    if data is not None:
        rsp["data"] = data
    return jsonify(rsp)


def playa_quality(h):
    """(Name, Ordnung) nach Videohöhe; Playa sortiert Listen nach Ordnung (höher = besser)."""
    for hh, name, order in ((4320, "8K+", 85), (3840, "7K", 75), (2880, "6K", 65),
                            (2560, "5K", 55), (2160, "4K", 45), (1920, "3K", 35),
                            (1440, "2K", 28), (1080, "1080p", 25)):
        if h >= hh:
            return name, order
    return "720p", 15


def playa_duration(it):
    """Länge in Sekunden, ohne neuen ffprobe-Lauf (Wert aus dem Probe-Cache oder der Liste)."""
    if it["kind"] == "ytdlp":
        r = _resolve_cache.get(it["id"]) or {}
        m = _meta.get(it["id"]) or {}
        d = r.get("duration") or m.get("dur") or it.get("duration")
    else:
        d = cached_probe(it).get("duration")
    return int(d or 0)


def playa_release(it):
    """Datum in Unix-Sekunden: Dateizeit bei lokalen Videos, sonst die jetzige Zeit."""
    if it["kind"] == "local":
        try:
            return int(it["path"].stat().st_mtime)
        except OSError:
            pass
    return int(time.time())


def playa_cat(it):
    return it.get("cat") or "Alle Videos"


def playa_link(it):
    """Ein VideoLinkView: Projektion/Stereo aus DeoVRs Format-Erkennung, Qualität aus der Höhe."""
    screen, stereo = format_of(it)
    dims = item_dims(it)
    qname, qord = playa_quality(dims[1] if dims else 0)
    return {"is_stream": True, "is_download": False,
            "url": f"{base_url()}/video/{it['id']}{it.get('ext', '.mp4')}",
            "projection": _PLAYA_SCREEN.get(screen, "FLT"),
            "stereo": _PLAYA_STEREO.get(stereo or "", "MN"),
            "quality_name": qname, "quality_order": qord}


def playa_video(it, full=False):
    """VideoListView (in /videos) oder VideoView (in /video/<id>) nach Playa-Schema."""
    cat = playa_cat(it)
    v = {"id": it["id"], "title": it["title"], "subtitle": cat,
         "release_date": playa_release(it),
         "preview_image": f"{thumb_base()}{thumb_path(it['id'])}",
         "details": [{"type": "full", "duration_seconds": playa_duration(it),
                      "transparency_mode": 0, "has_scripts": False}],
         "links": [playa_link(it)]}
    if full:
        v["description"] = ""
        v["categories"] = [{"id": cat, "title": cat}]
        v["has_scripts"] = False
    return v


def playa_categories():
    cats = {playa_cat(it) for it in all_items()
            if not (HIDE_BROKEN and is_broken(it))}
    return [{"id": c, "title": c} for c in sorted(cats)]


def playa_paged(items):
    """Seite nach Playa-Convention: page-index (0-based) und page-size, maximal 100 pro Seite."""
    try:
        page = max(int(request.args.get("page-index", 0)), 0)
        size = min(max(int(request.args.get("page-size", 20)), 1), 100)
    except ValueError:
        abort(400)
        return playa_rsp([], 400)  # unreachable (abort raises), beruhigt den Typ-Checker
    return playa_rsp(items[page * size:(page + 1) * size])


playa_bp = Blueprint("playa", __name__, url_prefix="/api/playa/v2")


@playa_bp.get("/version")
def playa_version():
    return playa_rsp(PLAYA_API_VERSION)


@playa_bp.get("/config")
def playa_config():
    return playa_rsp({"site_name": PLAYA_SITE_NAME, "actors": False, "categories": True,
                      "categories_groups": False, "studios": False, "analytics": False,
                      "scripts": False, "masks": False, "auth": False, "nsfw": False,
                      "ar": False, "auth_by_code": False})


@playa_bp.get("/categories")
def playa_categories_route():
    return playa_rsp(playa_categories())


@playa_bp.get("/categories-groups")
def playa_categories_groups():
    return playa_rsp([])


@playa_bp.get("/video-statuses")
def playa_video_statuses():
    return playa_rsp([])


@playa_bp.get("/videos")
def playa_videos():
    items = all_items()
    if HIDE_BROKEN:
        items = [it for it in items if not is_broken(it)]
    title = (request.args.get("title") or "").lower()
    if title:
        items = [it for it in items if title in it["title"].lower()]
    inc = {c for c in (request.args.get("included-categories") or "").split(",") if c}
    exc = {c for c in (request.args.get("excluded-categories") or "").split(",") if c}
    if inc:
        items = [it for it in items if playa_cat(it) in inc]
    if exc:
        items = [it for it in items if playa_cat(it) not in exc]
    order = request.args.get("order", "title")
    key = (lambda it: playa_release(it)) if order == "release_date" else \
          (lambda it: it["title"].lower())
    items.sort(key=key, reverse=request.args.get("direction", "asc") == "desc")
    return playa_paged([playa_video(it) for it in items])


@playa_bp.get("/video/<vid>")
def playa_video_detail(vid):
    it = find_item(vid)
    if not it:
        return playa_rsp(None, 404, "Video nicht gefunden")
    return playa_rsp(playa_video(it, full=True))


@playa_bp.get("/studios")
@playa_bp.get("/actors")
def playa_studios_actors():
    return playa_paged([])  # in config abgeschaltet; die App fragt diese Liste nicht ab


app.register_blueprint(playa_bp)


if __name__ == "__main__":
    print(f"PlayaVR Server Version {VERSION}")
    print(f"Videos aus: {VIDEO_DIR}")
    print(f"ffmpeg:  {FFMPEG or 'NICHT GEFUNDEN'}")
    print(f"ffprobe: {FFPROBE or 'NICHT GEFUNDEN'}")
    print(f"yt-dlp:  {YTDLP or 'NICHT GEFUNDEN'}")
    print(f"Webquellen: {len(SOURCES)}")
    if PUBLIC_URL:
        print(f"Öffentliche Basis-URL: {PUBLIC_URL}")
    print(f"DeoVR-URL:  http://<deine-IP>:{PORT}/deovr")
    print(f"Playa-URL:  http://<deine-IP>:{PORT}  (Playa API v2, ohne Pfad)")
    print(f"Diagnose:   http://localhost:{PORT}/debug")
    THUMB_REV = "2"  # erhöhen, wenn sich das Aussehen der Vorschaubilder ändert: alte werden neu erzeugt
    _rev_file = CACHE_DIR / "thumb_rev"
    if not _rev_file.exists() or _rev_file.read_text().strip() != THUMB_REV:
        for f in list(CACHE_DIR.glob("*.jpg")) + list(CACHE_DIR.glob("*.small.jpeg")):
            f.unlink(missing_ok=True)
        _rev_file.write_text(THUMB_REV)
        print("Vorschaubilder werden neu erzeugt (neues Format)")
    threading.Thread(target=pregenerate, daemon=True).start()
    if THUMB_INLINE:
        print("Vorschaubilder werden direkt in die Bibliothek eingebettet (thumb_inline)")
        threading.Thread(target=inline_thumb_worker, daemon=True).start()
    if SOURCES:
        threading.Thread(target=refresh_loop, daemon=True).start()
    if HTTPS_URL:
        threading.Thread(target=start_https, daemon=True).start()
    elif THUMB_TUNNEL:
        threading.Thread(target=run_tunnel, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
