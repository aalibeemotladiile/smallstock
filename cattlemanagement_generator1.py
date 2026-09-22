
# Generator script for the Cattle & Small Stock System.
# Running this writes out cattlemanagementapp.py, requirements.txt and a
# .streamlit/config.toml holding the theme colour, then launch with:
#     streamlit run cattlemanagementapp.py
import os

app_code = r'''
"""
Cattle Management System
========================

A friendly Streamlit app for keeping a herd register. One shared SQLite
database. You load cattle by their tag (add as many as you like), see the whole
herd as a searchable sheet, and look up any cow by its tag with a search button.

Run:
    pip install streamlit pandas
    streamlit run cattleapp.py
"""

import os
import io
import re
import sys
import base64
import json
import hmac
import inspect
import hashlib
import html as _html
import calendar as _cal
from datetime import datetime, date, timedelta, timezone

import sqlite3
import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────
# LOCAL TIME  (stamp records with local wall-clock time, stored naive)
# ──────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    _APP_TZ = ZoneInfo("Africa/Gaborone")
except Exception:
    _APP_TZ = timezone(timedelta(hours=2))


def now_local():
    return datetime.now(_APP_TZ).replace(tzinfo=None)


# ──────────────────────────────────────────────
# CONFIG / THEME
# ──────────────────────────────────────────────
def _default_data_dir():
    """A stable, writable folder for the database and backups.

    When the app is packaged as a Windows .exe or launched from a shortcut,
    the working directory is unpredictable (and may be read-only under
    Program Files). A relative 'cattle.db' would then land somewhere the user
    can't find or the app can't write. So we pick a per-user data folder:
      * Windows -> %LOCALAPPDATA%\\HDM Cattle Management
      * macOS   -> ~/Library/Application Support/HDM Cattle Management
      * Linux   -> $XDG_DATA_HOME or ~/.local/share/HDM Cattle Management
    Falls back to the current directory only if none of those can be created.
    """
    app_name = "HDM Cattle Management"
    try:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
                or os.path.expanduser("~")
        elif sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library",
                                "Application Support")
        else:
            base = os.environ.get("XDG_DATA_HOME") \
                or os.path.join(os.path.expanduser("~"), ".local", "share")
        target = os.path.join(base, app_name)
        os.makedirs(target, exist_ok=True)
        # Confirm we can actually write here; otherwise fall back.
        _probe = os.path.join(target, ".write_test")
        with open(_probe, "wb") as _f:
            _f.write(b"ok")
        os.remove(_probe)
        return target
    except Exception:
        return os.getcwd()


# ── Access key ────────────────────────────────
# One key opens this system, and nothing else does. The key itself is never
# written into the program or onto the disk — only fingerprints of it.
#
# What is stored where:
#   * here, in the program : SHA-256 of the key. Enough to recognise a
#                            correct key, not enough to work out what it is.
#   * activation.json      : a token tied to THIS computer, so the file
#                            cannot be carried to another machine to open
#                            the system there.
#
# What this does not do: the program is readable Python, so someone willing
# to edit it can cut the lock out altogether, and the herd databases are
# ordinary SQLite files. This keeps a copy from being opened by someone who
# was simply handed it; it is not encryption. Protect the computer itself
# and keep the backups somewhere private.
ACCESS_KEY_FINGERPRINT = \
    "ee4377831b0bbf0c9c7466875198157ef625751f075b96ab8375f168f05795dc"


def _key_fingerprint(text):
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def key_is_correct(text):
    """Digest against digest, in constant time — no early exit to time."""
    return hmac.compare_digest(_key_fingerprint(text), ACCESS_KEY_FINGERPRINT)


def _machine_id():
    """Something stable about this computer and this user account.

    Deliberately nothing that changes on its own — no MAC address, which
    python invents at random when it cannot read one, and no serial number
    that needs a privileged call. If the computer is renamed, the key is
    simply asked for once more.
    """
    import getpass, platform
    def safe(fn, default=""):
        try:
            value = fn()
        except Exception:
            return default
        return str(value or default)
    return "|".join([
        safe(platform.node),
        safe(getpass.getuser),
        safe(platform.system),
        safe(platform.machine),
        os.path.abspath(DATA_DIR),
    ])


def _activation_token():
    """What a valid activation.json holds — on this computer only.

    Keyed on the fingerprint rather than equal to it, so the value in the
    file is not the value sitting in the program, and a file lifted from an
    unlocked machine means nothing here.
    """
    return hmac.new(ACCESS_KEY_FINGERPRINT.encode("utf-8"),
                    _machine_id().encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _activation_file():
    return os.path.join(DATA_DIR, "activation.json")


def record_activation():
    """Remember the unlock, so it is asked for once and not every morning."""
    try:
        import getpass, platform
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_activation_file(), "w", encoding="utf-8") as f:
            json.dump({"token": _activation_token(),
                       "activated": now_local().isoformat(timespec="seconds"),
                       "computer": platform.node(),
                       "user": getpass.getuser()}, f, indent=2)
        return True, _activation_file()
    except Exception as exc:
        return False, str(exc)


def is_activated():
    """Has this computer been unlocked already?"""
    try:
        with open(_activation_file(), "r", encoding="utf-8") as f:
            stored = json.load(f).get("token", "")
        if isinstance(stored, str) and hmac.compare_digest(
                stored, _activation_token()):
            return True
    except Exception:
        pass
    # Handed the key by whoever deployed it: unlock, and write the record so
    # it stays unlocked when started normally afterwards.
    if key_is_correct(os.environ.get("CATTLE_ACCESS_KEY", "")):
        record_activation()
        return True
    return False


# ── Which herd is open ────────────────────────
# One system, two herds: cattle and goats. The opening screen sets this and
# everything below follows from it — the database, the words on screen, the
# category ladder, the pictures. Read before the first Streamlit command so
# the browser tab can carry the right name and icon.
try:
    SPECIES = st.session_state.get("section", "cattle")
except Exception:
    SPECIES = "cattle"
if SPECIES not in ("cattle", "goat", "sheep", "pig"):
    SPECIES = "cattle"

# Each herd keeps its own database file, side by side in the same folder. A
# goat is never counted among the cattle, and backing up or restoring one
# leaves the other alone. CATTLE_DB_PATH still points at the cattle file if
# it is set; the goat file is its neighbour.
_ENV_DB = os.environ.get("CATTLE_DB_PATH", "").strip()
if _ENV_DB:
    CATTLE_DB_FILE = _ENV_DB
else:
    CATTLE_DB_FILE = os.path.join(_default_data_dir(), "cattle.db")
DATA_DIR = os.path.dirname(os.path.abspath(CATTLE_DB_FILE))
GOAT_DB_FILE = os.environ.get("GOAT_DB_PATH", "").strip() \
    or os.path.join(DATA_DIR, "goats.db")
SHEEP_DB_FILE = os.environ.get("SHEEP_DB_PATH", "").strip() \
    or os.path.join(DATA_DIR, "sheep.db")
PIG_DB_FILE = os.environ.get("PIG_DB_PATH", "").strip() \
    or os.path.join(DATA_DIR, "pigs.db")
DB_PATH = {"goat": GOAT_DB_FILE, "sheep": SHEEP_DB_FILE,
           "pig": PIG_DB_FILE}.get(SPECIES, CATTLE_DB_FILE)

PRIMARY, TEAL2, INK = "#006868", "#02A6A6", "#06343A"
LIGHT_BG, GRID, MUTED = "#F0FAFA", "#CFE6E6", "#5E7373"
WARN, DANGER, OK_GREEN = "#F4A340", "#F85050", "#2E9E5B"

SEXES = ["Female", "Male"]
STATUSES = ["Active", "Sold", "Deceased", "Missing"]
# The two ladders have the same rungs — young, weaned, maiden female, adult
# female, entire male, castrate — under each herd's own words.
CATTLE_CATEGORIES = ["Calf", "Weaner", "Heifer", "Cow", "Bull", "Steer", "Ox",
                     "Other"]
GOAT_CATEGORIES = ["Kid", "Weaner", "Doeling", "Doe", "Buck", "Wether", "Other"]
SHEEP_CATEGORIES = ["Lamb", "Weaner", "Ewe Lamb", "Ewe", "Ram", "Wether",
                    "Other"]
PIG_CATEGORIES = ["Piglet", "Weaner", "Gilt", "Sow", "Boar", "Barrow", "Other"]

# ── Brand logo (embedded so the app stays self-contained) ──────
LOGO_B64 = "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"
LOGO_DATA_URI = "data:image/jpeg;base64," + LOGO_B64
# HDM Group logo (shown beside the "Powered by" credit).
# The cow mark rebuilt as a clean circular badge: a solid disc with the cow
# knocked out in white, cropped so the disc fills its frame exactly. The
# original is a small JPEG on a grey square, which leaves a grey ring and
# a broken edge when placed in a round button.
LOGO_DISC_B64 = "iVBORw0KGgoAAAANSUhEUgAAAOAAAADgBAMAAADoA3fSAAAAD1BMVEX9/f5dq8sAAAChzuAAAABniIA4AAAABHRSTlP+/gD+MHWGzwAADpJJREFUeNq1Xctu4zgWPaQa6AQNuKTJYh5AYrZWPZuAHv//JxRKyG4W46GTRacwSEnxAFNON0TOgnqQ4lNOWptKObKO7r3nPikxpMaKQwrK0h9FD7ICUD4yAEIaHzE6ff7hgJICOHh+UQMQ7KMB5SODFMYHBQc+Dz9Tli9lJqCkM1zBZ+S6b0bITCGzAOUjG5W5t8QEAHrbaMXmCZkDOInnog3ifc4XMgPwUGvxdiJy0rYBahzqDwAUTAoAxSZ+Gr1tQJmkqcsVVR7e7nviPPX603f1WknyTkCNV5Rd2joqCzGhUsEOHnXSrh8cxA4E9AWoE1qlGXg7G49uXzQe+s/tySSmvAEOVL5HwsOCnfTFc5J5QqtD3WUSyiUe3frw8KWdMSqOA8RlEkoqhYnnlW489ofZIymLRICYSlfgmZpoEcuQYbcQGwGQ8xSm/5Xwiufff9Q/XJ3V64astyETQFFO1vuc9MP+NCitRMQ1aJQwm0mdTUYi6FutSnITIU4IkEoAu0zzzXxloz8GaUMjhCHDXda5eDMil+sklBAABgNmmM9BfBoukQv4aChUNVhzPOg7LiHuVgDezQqlnjzB08whXD5mA0oIgJRBvuyOGTKGlEoDCi3CeERERRz8MaRUGnD5TdgfSkSj84hIuNf9PZ8JCQyR6dYXDDMiQAcAwu4KgoDyToweQX0Evc+gqmIDb0QG4OPsET6FEgEgWeF80SLiLgPwTqDQN7b1KbTMc8cTAAaPa7iAYtSa1+M3AKAyzHjCtgFkMh9K1aHQNahPb7sOANhzhhnPzwDw2pVxwGML8E5/w2PA86iE3ENtSVSlkmGwoC+kjeRdE1upiEp4/AbgCgC+BRUKacq+P1//FGsD2oWI1BFQi6FCHrEw7u4AyKrg2SLaEh6/AeQqwIvi03CF7wCKf/ztV2D/F321H1+vzv5S7u28EHFRJh6AKpgkxNwKwin2Wm9NuIHq6phKxyjqwRuiAWjjLS5vEAqDIgIoIhYcY+jLopuiuqGRZUgnMggoJwG7MGNgVI9DRum/nDAlmOVXGCBEAPBxFJAGXVDfuHXHzZgBS2/YbQB5FwBkY7K7jWRBpY25bdv2xCZ29cxJlHzjBTHNLzvt9PTfHgmvxrt6LkemqufzefT552uoX55N8V5nH3ot/RKKoR7rY6GqKfyZ+QQ8GfFnA6NmkF6VSjlS2Felc4utHp33bLzufldVB9PCFm1mlRLVAdchjb6NP/z+HUDpyU9fr/DT96K8vm4Njt/pEwnzSCjEwL/baPTfhPITBWS1HHc0hvYWgPJuvFATLdW28Pqc3/L63nccczVFzR9IsH6Y77A5hQKZGy3GIkUYvKBOWPM2djNneM/gq1U8dzrm8IZwV6VSDnqjiWbpSXdkvgTIvAoFKJ5mnlLjXxmmTGMq9yFAm8adaA7WlXy6G2pwtAx3g9TUbp8xhwXaxhBdTDylAXWEKfjk5/HSiGohOl3a0OL6rhqO/d6hoOTBbm0+idY2Z8lkg6HEEHJIl6rD3l6dUKzxTDCVt72o/GOPYgN1uqNL4wgAIFV1sIeg5HjD4djNbwLmH7P0DERaEkqI6FB72yx/3fond6W/AiMltg/6dqjtFIHjmElNNeAVVVVV3CK5GMSnk1NE+/YjR9b0pNNV1mb4jkHyYnAMOhUX8Q7lyFXO5ESZRc/xxjCuHKCKaujRphrCOupPXV21APB6vrZE8VfaeLvCL/8Z6w41tR1fr/DLP7tyJo2Ab246Wt+zJhOc91Wg30rnvGIDdZpJI7wJddu+2AM0f75yf2XWi5JPRtTfoSGn2pojUg9imYw2MJtXNqx4Tg5RIBDqg4jICKjmhaUGJ7XumUi5MNF+GJqOhtB2VF0VjW1wrjScR0psm3pk6adu6Aqn41vxv7adyi/1yzOer/W3367GgjgAWPxo//88fvrft3JQqRBLk7QLUhy5nrxMt922wTiwCEnd5PpSygEwXvhqRKBnoN3AMlOdex5L/NSI65wO1aRT923dHFsCX+qXIRWbxCCHY4w2vXkbQvjD9tajLoJxFb1zVHITKaaYcRtiEJkuNLhtfC1iGVC20qt4AZ1aJVIB5l54O9DByUYkZKWTv912wgCbM5Uw4BprAOlTXLO4yb4LD23tKCClBKhZhVtcZ4HoWS67CDVFzFSc4xSg+kINgMVqZOO3YuGGS2b1ooaV1eJ6T0KMbqEW4vloQ0Jp4mEhovAKqMbL6gu3L+n2q+IAuHsnvS0iCQ9/wdaNIo8cEOadFFVVcj0a4Y4DLdfIOujQJlaMW48glqLuAXLUojwt83frWSOTcp2EQFVao3eh/aVnjm3VS6DNLKrSV9Puz9fXgfHnPHKo9Cnq6vz1CpgGmNeh4dkVyt/8EhbVISjjrUsOMvfrowOH5/5UOjwvSOxJFl8eLPtlkA2EnichHAn31aaM4G29VQU6w/1CqVm7PF0g7g7xEqnxd8WmTmlghtIDALNH4MUu4SPMHxmEqdPoqM4mDd+kfLIJhWaG9PpJB0BS47TEUms0+TQGU0I1bAlYjk8EVh6nmZQq0WEON1IagLxMi7TgaH/CabvVy9rGtKgJuEWjW+6DPQ+I9t2hWlt1IOVUZRcbf3NFSqg//TDNGRIHfWmjxFPxueJ0HjWmoHG8yNTo1GmdEqPfDYc2o0eOHLfJOdWDZcTAaWSINEl+pmaMA6qYdMejEhbvE9CJA72bj6eYRJHhQGibvDhgGJHHmMNTCs2LA2yqZx4Qf0QpZcLMJ4a+TMbpw2bKufkVdY+0KrTQxYqPEXBRyJRhQPlRAirzp+Liq93ma7Szhmve9J0GXPMQlqIGgfiFEjKsOExzP10GSBtcdIQCOMVHUdTt4cpLAOmFeEHfpx9HUYex3nhKP46irlMuaSPSgAx4j4jrJWzeAai8tKHZpe5YLa/xDOIH5Gs0Ksr3eQaN6c3j9CTnUcF5RkXWqbSPpbw8K1Z8DaCfo3yNFc0FoSRgwAkzep4dt4c7HxG6YkchBhThZg1KMyuG5MDANvNxT6qqco3OIq9j0EShFD44QA+l3+gRCfvI9eKHAG47r2oUaFiULiR2TqfcrM934d+kjFggMouiIjXncPWc4/ocerlwy2zjR16MuI2SIlV+H9F/aU/btnnwKa5bYYWMgQwHgGrP0TcuLyiY/45jzwGTJEkBHPSas7S9RUvYrMj1TaRvWKq2dE+llF2S60mSpPq0G+yMS4l5qLDioEiJaFxRVmLhX4IGXE7F4w9Z6Umz5DQQ21LOXV4U8xRjdHWFr32IeC5eHDOCPl3zlLh5HIPG6yPKGsaX/XrWxHjD/CTiw9cZW1cfj3dHeKQ8DMSDE50fb8nMhcbJR742kWpRpM/LWU6qcxG5L5NSw5RiXHReE2dUqCDjM/s9Akv9uVaqWtVxzAIcbwxIIibGW8+KFJbmqDfZ9LktjpwfQypKIwCZiL1FWAow5tiM5XeN8qYAgEK/u8s9TO0mhZ8oMMy8m3JNW/hgTqzlxkiCwhVLOdaK5Qvide/wM27ETWCdkZyG3EHDLCmLNa49x59d6RFQsRms95O0CIjfd0EROVBU1aTabWeKzObC1q+k+5DoKqjUI98bFp4XV/gQVUkNQArbWOPzTqElFuSsrNh4qIBXhsQIWoY9sj+twytMc8l6wRo2qyGouoynMc3Fqn40ww8Ir6FCrB3U1PPbuPTFegbVfl5C+EN3tK6ZFm+37alWAKDq9vMY0pwX6TlwkoDxMKT52v3L5PVBztwLj94AYD/vQGIeI2cGlsKi6UjSKrioNr99n/nWdbEBXsEGG3ozVOzb9/Pa+20eoJxcl05xjuWWpoWxFp47oOZGmQjoDNUsvUJmuHzugFoXUDOg9GV57vUWC2+ba4TZF/Q7M+SooP7e2aOmN2B8djKA5327xntcQf25WlYvjV0MFekQml1Al/Oa/wDImMHTJmzCTc6oyq9Raqc+ipwB9y67Kw84hfGCBzLi5uLhiVwBDacwXvBgS57yVFOYHypmp7Be8FhwQCQUmr/EUJiXo+aQr4uW3cun+bMz1z2gGHOK+oVOC9/3LteoW6DJVBZfCpitUWIZaH6V7NHWkkwImK9RDqg75vZJk+/LLAGzh+WFyVHrhUdbTzwhYL/G6703SqOLNI6XZJuwtE9evCWrJuGEzVWCC01IYD91Txc/d5HodJFTlIu+YPGW7JILfYAyWBNlOhkAvGPaFRu3fr3HhSaUo8d5VTrQhjuB5mIBtUZZ0J00bRrHdve4kDO7BWWWgBTASaV9ItftC+G0ks77+IvuVvgDeabb3y8pswS8q4OWv4gzAlD1Y0ylwps8ywtNSACcZEylrogS6wbcLkUfo9Z3ReSrFn3dkmQhoEO3O5bB0TySFgLA6THxVWfIf7HL4x5A68ybaYqAxcWAAlB1hnIWO1hK7nn6os8NMukNcYANOgB0egPt1ZexzmmXOAPqr8cyLSF9ZFi5suBjTOnxwQDf2Ec8THAPQPmu5Lt4elfQ5AlE+C3oB7zL691SCvUKCP9n7J0K3QBQ/vFfYIM4Fg/PTUZMO63aII6+5zEUIgC07HEFIAV7h1+UAFQdsEtwG8PLzUjgFDIZgJK2FxK0KgG0TKwEpKi7iz0+rNBYZhPbSxD1xgwnGdFA0PTk09erwO/OsZANdWLhqiTCfkm3ua3NJF+p8SJZOwJIJd2u44seY59iG7RG9xEmIL9eZ3WnWuxzBwDtz9EHJxLb+tZt0LUd8fTKflvLhNtEY4b4+de3qxzWFJsOAFSX2rj4h0TiE7U8linjTZuTqFMKL7kXdCkqj3cU5h50/K1tR7z0dtfJ3a5LUX16XlJn3vSq+Mf5dfy0/S1je+3MDcSdpdkaQG89AKK6D9pAfNgiPaWJNnOL9Kw3SqSgdRuNrKqtmczad37NNvckRFfVffA295g28vcptv0jNvLH/KcKqgXaH/SnCkYpYf99hNr4/OMBP+TPTfwfuwUhNjnKsBEAAAAASUVORK5CYII="
LOGO_DISC_URI = "data:image/png;base64," + LOGO_DISC_B64

# The goat mark, built to match the cow badge exactly: the same solid
# disc, the goat knocked out in white with its detail lines in the disc
# colour. It opens the small stock side of the system.
GOAT_DISC_B64 = "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"
GOAT_DISC_URI = "data:image/png;base64," + GOAT_DISC_B64

# The halftone wash behind the opening screen: a log-polar field of pale
# dots, drawn large enough that it stays crisp stretched across a wide
# browser window.
WELCOME_BG_B64 = "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"
WELCOME_BG_URI = "data:image/png;base64," + WELCOME_BG_B64

# The HDM cross-and-bulb mark, shown above the word Cattle on the
# opening screen. Trimmed of its transparent margin so it centres
# properly, and reduced to a 64-colour PNG to keep the source small.
HDM_MARK_B64 = "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"
HDM_MARK_URI = "data:image/png;base64," + HDM_MARK_B64

# The system's own mark — the three herd badges packed into a triangle, cow at
# the apex with the goat and sheep below. This is the browser-tab icon; the
# company logo on the opening screen is untouched.
SYSTEM_MARK_B64 = "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAMAAABrrFhUAAAAwFBMVEVfrdFcrMzl8fZ3ub6gz+P0+Pv0+v3b6fRisNFdsNNe5+wAAP+j3PV6wN4Af3+22emxsfZVVaoAZa0wmcR//3+72+gVlL8AAABaqMn5+/xttdLS5u+y1uWSxdtircz+/v6CvdaizuDh7vTB3upVqqpVqv8A///7/P0/v79/f/9ks9L3+vxirc1ksdF///8/f79hrs6Xy+FjsdFps9Ngrc1GnsJ/f39hrs5bqsthrc1bqcpjsdBdq8tlstBYqdD7/P0x1w/QAAAAQHRSTlMbWZMF6mzaDq//BQEI/gKiAwMIEQJ8DAD9/v3+/v39Bf39/v4DAwEwBAIsTjFvAgTT/NISsf0Cj6twlozWVAuuJYhrKgAAHI1JREFUeNrtXQlD20qSFmAgBJK8mdlZSdWHLmODMWDjQA6O/P9/NVXVLal12iTP7JqR3hFi7Lb667ovef5/+eUNAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAPxtV9S4/osAwM3SP5Xrkl6K/hsA4F1G/vHxYTS9stc0Ojw+9if5b98zALTB6Hg6vdr/FdJ1FIqROBJh+Gv/ajr1oreHwHtzAphOD3HzYrRUoANzaQApRPrrcDp9cxJ4SwCiywhPfx9PXeVbdy9YyqP9aXSGb3uvAPgRHr5Ymt0DgJIixUtIBWBek+LX4bH/LgEgTTf9FR4pu9MsrF6EAnHDUvyaHr+hVnxLAPbDkLevlQhbL8MaMtyPxu8OgMi/Qt7XfPhhzyWRDLQ8unozNvDeivzt8eve7dNFVLBEIngjNngjAKb7YaYb21dB2soISCbh/vTdAIAbQeknifpFbf9BuzAQyAcoC9/EKPLegPuj6RFuVgeqsUvVKQrwN0fT6A0kwRsAEF2Z/SfVLZI+KDhAtJHC0VX0DgCw9K+DKvuTsKeXBNrAAkX/PVpFdSqQxAW7DwDKf8navUr+ml5CeXdvDaM4uFc1CaFJF+w6AJG/H4rG+WvaPxAwSpIdzFtX91C8IZVGQuxvnQS8LR9/dMXkHtf4nxCQuP+s+rIyRjGSA1EF/eVq2+aAt20BcETnDGFdAOBFtn/dLrBOEQkI/szR0bbFgLdlAkADKKire2H2qMgQyFqQ0VrnGhKO9rdMAt6WCYA3Vddx9pglIqAavEH/BrHIXwi3TALeVgkANWCCOxLk6+K5ag4BFCRATFAzhRQjIEsLOVhu2SbeLgCHJPCr1z16wwUCbcSB/kKmCqNA3svD4x0FgE2gOCikGgl2hZe+D6TKX2wYwxDkQQODgYZfWyWBbQKAKjB1Qn4yP22JEOguEhAGGJ3rQRmIq+OdBAA9GWMDk1hHfhd4+EAXHjoLBvodQMMJEFVFmAbLX9tUBNukALQBID/qBLm5IAbickHUXthHriOQ5G/LrFyUV8c7SAFsBBp6hoTEuoCS7s12k3tRSv/YNZSdN5EiODzYHglsEQAUgegFxLJO28XmwDEQpeMIGIlZfA5gm5pwmwDQptNqBKi4kAVkQQAZ/jAq7GUVVEOnWZDsJgCHNeEmoGIQqDxASDFA/LmQCAYe5wrUFk0Bb3siYN/NesQ1g0gXNC6MlpCisP+DWqw0gP3tpcu8bYoAN93RuETNM0jzGCmqx5p9fLIfne8cAJQIyWOfbRe4mQC0DpAgtGEC0QAgGG0vUbI9ACLWdO3bb8mNoUUkLQnENfMwC1S0gwBMw6Yn1GH+FkdtwoKiFidJA9hFAK6scxvUpV/cnRazZ69qEAWfrnYTgBb61/XwWFgT+O4fuw6AePX+UWOELUyg4b0AsG7/juB3mQDeDQCb7h/3rN8nBYQbXxreowyoCPdY9dNAGSt5NwAk9ehfV3JcGB9BpbluuNpJQ0j0CwAIOspl5D1Q8CzIM2poCB3vpincbwCqLpEYBxljB/YNYjdN4cOjMiZuU2FhvUSinQRQWUpJJRNWEMqddIZMVkj3aAARtCNgIskg8Yd4h93hvDCikwCMpSzbjKAa08AuBkQ4JCb6XUA+6gouKmHvV+d2ozSUAjsYEiMeOHLd4TaBx4Fyt3JUqSoJKJMnGO1oVHjfpeY2brcUAomlDpFLPXApAODLbgIQuaZQu8YTNmlqsqZQ2kraERvoCUQ7CECeGrNXHvKuVcmLSsSESkOSMDWF8zlvyEBNj7d3l2+SHC2qxKApClXTZQ4qmRHSATe7CACnx8OKDpD3qkUVMLFLSh3HSkqgELHKRMklavrXw24CQAUSNiwIlgBUvSRY90ZJ+TNfdrVAwi2RUW22r8ypvWf/IogPd7hGiBPEpT6rACB14FJHZ1RgfzrZVQBMckBR6k+yV+tWzEIu7nqjRKQC/B2uE7RMoFn7uSXjQhd+Ug/9UyTgcJcLJU2pLNVKC1sDKBv7742Lwclop0tliQSYCXjjUAKg16fJjI2w3HrHwPbL5Q9z9RcUXSNxGSfoIwB5vzz8526Xyxe6UFrPB2r7r5vGVQGIAuDY33kAfFspYRyjIuKzNlGC+99/Dy0zJjBQACDdKInu4QDe/9l76BozbmEOAFQDhbJn/1+mf73Bzb0JAMwFefRDb1QqYeg/eh8A2JIxvXGtiIDg07+j2/fTO2xMwmWzXgQ6yP9kdBi9Uf/427XPT/dho2y5UMT+/vEb3djbDVAYR4dLWA8Abn95GJ09+O8NAD86jZAIoLdegKpm4/3pex2igiBcfalSgajsHoIT9eXqbQfpvO0YnRvKFoyc7kg2hMwcmSA4gREVxt9cvl8KwOs4ml59GUEJgmbdcAK4+6vp9m3//2MAyLQ5i2iU1peRgk+fPlET0SdQoy80Sis6e/+jtPINRtFVdJwPUzuOroqX/99SwORhfHtxcHCA/13cjh9O/wyDyP9cfe3zn47TO8X7o9uje8T7m/y9ANxcNHIz49vL37/bm4OGn/PXwR+kfy4vxhvc8qsBmNyMxzcT/+ac/rKaeXs/vv3E69v1nrf6SK/hd9B7Liev2/0F/f9lNd+7ztebr17ser+DJq/3ceXl6/3A+1vRa+c3fwDAxPn0gXf99Ylan/NLB09fr2flvsfjjTnpFj91Psf1gvp683P769dcE6qd+Tyr3R/opw/Xc9rA2vW6AKAdnc32nmcf/fnjIoCGHY+vLB7n+J7n59mZ/cB6RiVimnWvN6Nje4V4sevd6bb19J1Zb/IbAEwu/bPn71SoFgRP0NH0Qr79E1ezPX1/Rkpcf3Z4u6vnuwDcXdOdFn8N7p5X/LbNLgTde/6ge+7v7tpbs57XsfDzEyxloopJf20X1beAypSUnwKCYI1UfIh8b28BJ+X90SiBLHWaZdEeWux5frSRK/Rw6R9cL3ruz9ABQhBdvg6AsX/2ndLzUqaZCKnjTbfvH2IR0higLBTL4MNZPwIoNp/viruF2KkV4PkK+U7g7pnfvAE5Oev1YEDrnb8GgLE/e1rixlUiEyHwpyW0IUDljDwHSUqlEIJP31c9CExu/NkHcIOB1eFRaRqmeV0lfJitZajJmNbTwSYXrdepqZoA/MOfBZInuoFWSuHp0KQn3bL/TChJTfF0eFqJ0dPHunlTKmrfvy6JlQpBWyNBBeFer2Eo5JFngGDDq2+9BgCfH7wnWdStgAKlkNAbCJAjJ+MEoGwJlfL72T8mHdLq4BF6uuZEVoQD7Pc+HvTplbHvPW68fUMEqw428Jprfx/FlcIlEDKpI6BpIILMqgJYjX76N+3sOne4FaQoCUCYtmJRTxrC3bybcc/9l7tX7d+s99cmAJz7e8tqjbNWmvigRgGQIPVDrcJJgdd2bsiu7v1WCkJS2n0lJlLc8ayLBsYVPDdmg1krojUAJqf/qu8KD1vpUVrpAdQkJbRsVHgtP5w2pcCtP3fJP813bs6/PkivRB/m+NF2eoJX79+sd7EWACKAurzTBIGsNgFCmCgVNM0v1SQB5FdolEynAuWACLurR3m1LnraUPo7RXc5AuN1AET+nWoDT2bucSMiUsi2U1g+1uns0l8t6jXjmUzIChDVYqBm7cCiRbE++N7m9I9qLK4icPZXrfDeq6/ehi7pAlmpfZcyVm1d8QD/60+qRvV44fI/5cmzOKGDBqoMTlPOBKk4DjOepCKcG4BFw9OcjA8Wm9M/6mh3cAEsVmso4MK//tQGJOpCMt2KK47pFV0jMX6nVz20c/9rRf4jy2c8PlCiBuUXpJRmtg7V0gjXHMAPfK0T1K3/bXP1j1YMVA705Hv05Us15+7VRECretWgdQVK/FvQ7oPAD1fSPLz4P8AZJEXkr8rZKfk8yTxHvETlgkShS+bF5V4mlRt83mz/GuJYJWhYuHSquRZ11APAg/+ha95B4ws63vfoiG4UOTM+CSlSgVIETEW4KES/MaLLuarAbIvvz4Sww8bnrqf9+XS10f5x9zHaGmhcQ1yO8OdRbnI56mGByFv8joJxv/p7aQuhRNm7U85oOGT/hCfotct/LguUmfPLVKrF3r/8hxLRr7AZ65OxKcE43DTIHs9eEQNCPPq3f9YFwK2/94f7xy97ztn2ElcbCWZyx/QX/YWh0hm8kkiinBHs5QhsxAB4zsCyBHL5jQIrIZLKaFSnHEWTLhaYTFb6zwFYvJjbvUGNQkQe2xuxhNA/XFyAeVtqWwaIj0MUrIaqHs7WEqjhMk1/WmpIWMHSfQCXpY/+GZ12AHBuJZYOAv0HCOwZpkWbSuYzdJyCaQh7EJBhwoZyYgfqcQttKPcMUY1zidp9xYqFnwnj0GkqERPJxbk2kOrTz4pmcQCY+F7OQ3Efd3X/zvz59BIZDvA+5QAAJ8D4TmT7FOlcDgqi3ph9MJadzBdWtUYfn9YzPzKNECZIoWg+EzeiFcowRjyClWuqeBUjoBj2mUHQOf6h3eBEqaMKKXBhJMrPwu/JzOMjkJyJCLp6hukXYCZO6WK+nAB7ZBf9EoB0HGiS/dbJFKRvpHS7UmhxDdduwMmlgAJgaA6+cvw40cIfMXe6FtYLS4GH8dcTFvmifIIG8iP+G3R4Afi7hPUWWOqXxA0nX8dmvT4VQLxP84udBvVES17BCbQI0gyw+NhKAWNHBVBArPicDmpdPI3pQFoIJcvv0XMiWesExbiPpUqkAQCZ617K+7bRAQnck7nNFieeP35gabjBOkWX/lz30D5yTFKNM9HmpTPKie+fVeOeY1p4jgj85vZ4oSrBT5o0Ngnj+uLKiHd6A4QZf6IA+hsZQ7fWqjTfS/3wNFlPkqEDbapAalJZNDqD36xk0UBojKs+I5i2b+0N4RoWohzQyLcheGQ33t95E4BT33sqWZrGOuk0JBNdSN3R9Ac8JhQtW8NtJTk+meRXwVFlCExpTVFEI0xErTlIJTFiXry5jI48ceLsY6cOVGAHlmYya31ghQngSKWND4s+0WkDgHGFwoA4GvmJy7tFaz2fNEPfFNhJAC59oti+8ecVCUWxRY610IAIAY1QiMA9ZCxhOQ5ZdWLm/k0PByRMW8JQWUPCGg7QvGBiuFc7gQGvtAIrSpbnXoNIyXDX7RWtkoWK5m+QlVkB6MLc3jaVNvBcOVnwp3CtYCP1pemha3pYuJ7VUfVoEE2bEGxsJORrprGss1Ypqi1V0XoNAC4rMpaYACxzdXZ1Eb0aiwsqJeCkB8ZNsx3yWZFs6uCWXQMoJMpPhUmVSfeua+vFJCiq4k/mI5gESaoUQifo5kSaebIrO4e43mUdgIn/outVrBCwu6r6Wjr4LcAT1J3rCZd9aY6QM4Y2A6BCIStTU5jXqJUGeKx0zdxCIeAtjHmImqR0cTUH7S2GIkbVIcI4tg+oSEkMqjKkQaaK+at+KWyhHID/Yce1qtsoYGsOLW4mMkRiaFFxjKO+2ZXfWA8pG6w5ntH9ukuiucJcvDT3KhoAzHx/xbtG1aMySIqWayIXYQSKjCWKAMiU6UuhcVwSnEgMmpW2KznQM9xwFYDzFkdQGntIGdOy/gAQwfDEZK3KhlWGy86huZp2RkzLxggxFGOBRkZWYXGnZTDP97SZRIr0FtPBc1Aq134CrWYgIwdN6IRDQSG7FUYClhEYVfgr5zUALto8YTSsZfFpUfPdbWu7TFtsRvyCxnogrKUAzJ5VmpJmzdgRt7ptPU0+ncgooEgxSbCahM4HTxgBBJmZkIKwvCBV+XinAlYnblUC0DQzNAvkrM99TTg50KwCv/YLx8KuRRuWTJLANFuVLJlxhawJXJ9CXK5H9t79KNSxRmNRjVD/y+qhkC9k4u4ZGjCx+VHU9k+m1UENgE47CzKirBY9gP5SLGXHh376tfWc5B/kerBJUVDaTBUmQNvNrAfkzqA6AaFVJlCsyhJJSbmGYmEOtuZjC4hhpVKurXrRAOA7dBQBoPjksc8VI5NOSBIV6i4AfkKjLJxCUqD7AHBVjmhZj4aMqPskDISkDHsmoFwJeUCTuVdY61JZ/y2mQg/teDDwvTAE1gGAclfGpvNRViiWXgPZFRrFG66sB/Y2JT1ngq30OgCiMV0rqUQaGQAgL4oeQJRASqebhu59mYYs5YQXJPFMXuXi5PxeBQDuk1W9C4Bk1+c+lsGGAOSilCMzwii+aihEGJkq6jZsBQDF5dW4AhrOsbK8nbr8qZSbayVrFYxVjTZIGed5HQBowaLpUQNAkD0ioAeAn9CcopTxvZA8UFlNq9qJQcINoEJ9PbT0l6RMhKB0pZ2/WY8xVxDg09dGfASlEGwDoMfZDNlSVa7aZlbrLs/6VpcBwpYBxHa9uG2W6JIjJrJRMZILVW0eSUWmMqoVAwDzUleULaUYHDnslIcj8ukWghc9AIDVUeXtxqy3ws4ARUMNEgBkp3OqSrMpHGbucHkwczSrT1zLdHU9DRReoW3hz6Tn6L5UU6RWaYtSbzHHGgox0KIGL/oirkazlvwpoHZCaw0hYSnVKLclA5DHvdiZViYtUjvJpLYeOj6JMt+s0GRMqUYLwqZIqSCAJgFqsoQSpTkToCF0sIEpXGTFtDn2wkaR9ukfnXUKZApX1pM2NGqT7EKQN3hPHkpsPGQqF8ADStrLpggAMoWBsolS0iNKOM+M+7aZRtkdag5NxRsCgEpLdZnCN03npdQCsQorwVwTvI/j7hB5wxlK82OSNjrLZqQTqyPZBrVtlEI2d4aIko1PqWQikWXgXhsh2IpATnjkJycqRuRMmgadoZumO9zJAFQrRypcll4wGQKoirqY4ImKWBsyUJW7suwERf+0JK9Lpu1lcxRi8j8u8tCS1Wr3ipIHIyUM47RwARdjpYb2BBrOCZs07e5wT9CZHvhVnXUs7/nLSBu1M81Xf3zjrldx/9h6UsVAPZm/qFpmaxbr3ZQBlpyPkX3MJ3PJIWoKgEoccy6gojYEwEQS8sB9LSTW5QzQTmuDftnLoGhYBwDobLlSVZuIZZpb+aVhVDxtrXXCqqysl6sVpSm6mNlMqkAbt4340zDNlJFVYpni9hGA3L8wYetGUHSvEwBSv6k7FJfsIn61QxHOa0FRcCw2exd24yo/a2jO1i1FQCUoinKAQyCxiZwr1QIdP8qU0KUyPMpxoBKQcVxLX9bD4p1WgNQVEUAACEMX7QA88aqrp0riZlnMU9bWx6BdGAdOQT30Ltw5A09c2lPULnAgBAWASrncPGt5ejNVWCSkbejCO0fil6UljErqtC0z1M4DlM7VGe45c2lTmjyv7rADb6uJFrc6Mq814tIgzba6TDVUbeM0cXKQJpFxW5bvABu4kIDsMAHR+MkkFTOitSQoxYOeoUx0ud54TWqsqgXQ546rc6DRFCcOlKLdG+Z8/tgNikHpRucUwGlSuhJ28NzIhslpFpFhU9934yYGuAqCpzEQCFUSSLkkA2j/iEOm+E+VlZ5rR2rMn3hPrQEBMvtBVB8MgW4beuJJ2yMkAniaTGyy9aRiCKVsvCzzO1FucDkWjhWIvlZSuhonTyaZOYkqqSEwuTlUhWjlZEWihXfPcjaRksLE9wqpIXEfdbM4WJcer1cckVMVSNfxVmSJNR4eVABs0+PlekU2hMSzspkPyxYk6+gpixXzJSO8CkfgtjU9bqtBiJPIIohNZRDSe4I3RjqPI1aKAHBNtu70eJcYJGFKAqU4sjjkWGmH3fDkRY31QBRBO3IqCv2mrE7IClOOtpCENkmSc9QkL+FqnA09ypPliFPTKo2URPufQodQL/XWXkeBBDLGT+jyhhTZQ2xyUHZJpAGnw3VPiUy1pIVZ3dTFyyBs1GMLG7grskSZa1S0rOfWcFJ4GOIym8jVKAhBzAZjLGSlZR9d63FnkZTXStPkrimb/TFQpzQdFdFtkxmLg7wI69Q9MjAlsRQTV0LWnC3XCxQZi8DinhdeUdQUrRathbwm1up0IZriTku9WTXHoFeTSXeZXItPrIMwu1cQh2XTBPuyiixh3UYA5211nRS4B0pjckqlWsUt3bCWWLI4k42yu+4yudzEM72TtaXFCOqJ1u5CyZvWQkkmyKSotWGLKDYlAbrNDXCYalw4BEj6VGjATnUsyzJjMJ5h4WpzrUlpBsNXtyu1u1CyxTE1haG1Vg9YeDc9laKX/qwVAFT8SS6wVflIpKyll2ruNk49+DPt1OfQo5Yp41iyqpki7zoybOWWOhAd1we3x3zWGbVQUK+ZYoVYz0/NahX4jYaJBhMYCZVXOOcma6BqY9EKAjuvdjcU6wGH8hPSoXb/mmJ1ykRBUydFJsrCrlICdt5fkwhYJaJSTKBBF/X7a+sZ+t6WJEUPOHGfjqq430G1G8Ed5fJLero8EmVo6BO0Kbzj1kO77UyZuIGIf6dcnqN+FFZSlCbW7X76+ZqmqYeo2ZChWDUZXZ7Y2iVOSECzTLbe8Di59BbWHiT3TIpMpswPCW+fVb4Ml9Kkn5fSBneWxgZceI2GiZuX7nJZrjSv9kjU7m/V6Mr1mi2OTV3IfX0xSxX0XGLNwQvZUAJ64fkPzfVyl0CSFsSd0wMVTViorBvnxEiamryWlDaLBfP2lhndUyqroLdt6nJ95+h5PbNPdErPSKUj5Lpz3Dm7Kw0BMPP/0dbkZQUX2g3hEiTn2QJbeZNWBSBZyaVkaW90G3cLwqC/yBnaWhFbeocbpQLUJRm7T8lFAhBK1HopAddv7Xku2+bofKkRGb1LYUrauTzYSgApls607e62uY+N2ouNq7jXt83lNFAtm6c+cbzfvFSWJFdQTwz3tHoSDZzYilISVJqdiSKvZfg+JUfZCYSedDQ6NhpRN90+zDdpnDTXWf0bdF41xQ6Yyp+QXtm/14ZvTlN2PVPlgg4LJ8KZ/DOT1aGSptBpciM816/3iv3jemebT5A497168xBrLCsTwwavUXNyb7Pz6kMuCAJJTxyOiQ5w78RKgv2DSkJ84/U23f8Hb9Pm6TxP4ra7F5ER00vRiIOsb3d311Pynh3ihGLVIT1aQ6RqGZLXrp31eifAXDbvr5f8u9frGqLy8Lna8ezSfEP6bzLw4KEYoKApEkZFu6RTuG5Do2ZxSiNpvXWTkOoDGfrJf+Z/fnj1HKFz/3QjkNeMqHBHaFwbSDmiWU2tysRpG7/+e0do0PG/aoSGQ7Yv39Z0UeEZ7h30DilxyHbir/Z0PkSFGiN0/gQSXcp+vbeiITabrBf5B9d6zRmBflz1spO3buxNy5yWcsqBXly/vGLsDb7xpbZepUEL4HXrjen+7nrG/OB6q98Zo+PMacJveX5c/MGgosZ6fzj46FXrPY9/c5CS8xWkjD/Of3x1Y070dV9/zF42GVXVtp43v26udz2ne7n4nfVeZrieO0kLXUFcj/JJF+sm1G0wTW5sh6nt/fj27Tte3x739njz/u3Y/43LrPcy29t7fKT1Hov1zv9ovecfj9/Mej/2ZqtN19tonN5kfHHZMq7ut2cKtq13eTGe/O56py2z8zZdb/OBipfji+J67QS9N1qP5inSRMWL88u/eaDiO74GAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgDe4/UfKnkXxnUxGdwAAAAASUVORK5CYII="
SYSTEM_MARK_URI = "data:image/png;base64," + SYSTEM_MARK_B64

HDM_LOGO_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAQDAwMDAgQDAwMEBAQFBgoGBgUFBgwICQcKDgwPDg4MDQ0PERYTDxAVEQ0NExoTFRcYGRkZDxIbHRsYHRYYGRj/2wBDAQQEBAYFBgsGBgsYEA0QGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBj/wAARCAAwACoDASIAAhEBAxEB/8QAGgAAAwEBAQEAAAAAAAAAAAAABQYIBwQJAP/EAC8QAAEEAQIFAgUDBQAAAAAAAAECAwQFEQAGBxIhMUETcQgVIlFhFDKBI0NikZL/xAAYAQADAQEAAAAAAAAAAAAAAAAEBQYDB//EACcRAAEDAwMEAgMBAAAAAAAAAAECAxEABCEFEjETQVFhFHEVgZHx/9oADAMBAAIRAxEAPwC/tLPETczuzOFV/uiOyl5+vhreaQrsV4wnP4yRnRifdVVYtKJ89hhShkJWrqR7azfjTe1FvwB3VW1s9qTLfhKQ00jOVHmHQa2bZWYUUnb5jFBXN02EqbQsdSMCRM9gBzM8CoQnb/3vY7nVuKXuu3VZlfqCSmUtBQf8QDhIH2HTV98B98WXEHghWXtyQuxQtyJJdAwHVNqxz4HYkYJ/OdeeMqit4TRclV7zaR3OM498arf4a+KHD7afAxqo3Ju2trZwnPuGPIc5VBKiMHH50dc7HGwWoP1Uro3ybK8U3ehSJEwuR4zmKqHX2lTb3E3h9uuy+Xbd3hU2MzGRHZkD1FDzhJ6n+NNelhBGDVohxLglBkeqwi3ekv38x2YVeuXlBXN4wcY/jQO8juStuTWGhlamjygeT3xrdrbaFJcTDKksrQ8f3LZVylXv4Ol2/wBkUtZtuZPjKleqyjmTzuAjOfbVSjVLe4Z+OoEbht/uK59+BvtPuxqDZCumreJPO07s49VKra4AicrrDiiBhxHqEJUr+n3TnucO/wDQ7dMY3uuvard0PMMgJQpKXAkdgSOoH851UljsPbVneJuHoa25g/uMrKc9/HbyfHnR2p+Grh5vOuF3byLxMlSi0RHlJSnCe3QoOl71vc2q0KdjYlO3Hc/XiB+s09eu9M1i0fatCo3DjnUAXwlMGRu7klWTGQEnBFRVBmTa6zj2Fa+6xNjuJdYdaJC0LBykjHnOvVWpdlv0EF+e36ctyO2t9GMcqykFQ/3nWVbT+GbhZtHcjF5Ggz7GVHUHGPmUgOobWOygkJAJHjOca2HQF2+l0jb2rfQtLesQsunmMD1S9M31tGvSDLvYyMrW2EjKlFSXC2oAAEkhYKfca5r29pLWosaOHaxlzyURv0/N9YdW2XUII8FSEkjP20Mn8HtkWD7shcJ1mQ68uQ48yoBTi1PF4lWQQrClHGc4HTQR3Y9EzcWUyNU7kYsYq1S25rM08090EkEc2UgguLSOgACjrNspSoKScijX/kLQptaRtIjvwRmkRN3UrLwE9oFkqDgXlJQUkBWcjwVJB9xrVtnbjoKmpcpLC3jR7COHJD8d0lJaSEhw5JGMhBSogdQDk6BW+19q2LcqbL2rImq9JMtta1nnK5byC4R9H0KQWknI7AnsCdELPZ9Debiku2UG1cbmz1h1sS1oYSoMobU4EpGcrbTyYz+3n6jOmV7qRumw2oQOaQ6VoatPeLyFBRiBPg/5TKeIGzQtSTfxgEwvmJXhXII/LzerzYxy485/HfTDHfZlRGpUZ1LrLqA424g5CkkZBB+xGkOo2BtW02u2lUCwjxFpLAiuSnM+kiUp5KMnCuXn69+2BkjXUzwwpo0duNDudxRo7SQhphqxWEtpAwEpz4AwBpUQjzVMhdxyUgj0a//Z"
HDM_LOGO_DATA_URI = "data:image/jpeg;base64," + HDM_LOGO_B64
BOT_AVATAR_B64 = "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"
BOT_AVATAR_URI = "data:image/png;base64," + BOT_AVATAR_B64
PIG_DISC_B64 = "iVBORw0KGgoAAAANSUhEUgAAAOAAAADgCAMAAAAt85rTAAAAwFBMVEVgsNHn8/lhr89gstP1+fuo2e3///////9/n79gq8x+wNyq1NRcqsoAAAD7/P1ttdPR5vCx1uWRxdthrcyCvtahzuDB3utdrMydzOBVqatdq8vg7vUA//9Zp8h///9cq8pcqstVqvxEnsN9vr7///9bqchdq8x/f38/v79V//9esNF/f/9mmcxitNA/f79grs1hstGq//8AAP9esdJfsNBfsNH8/f5VmbsAf38Af/9OnMResM5fsNFfsM9mmZlgrtB8TlTWAAAAQHRSTlOc3MQ1sP8tWAhf/gb+AP7+//7+/////8/+BI//ARICS3ED/wQDLroCBAPyAgUSBDdJAwEridZvBwICDV5luAViGK1l5wAAHstJREFUeNrNXQd7m8jW1pb73a8AZxoICdmyndhOT+4m2Vu2/f9/9Z0yAwMChCQki+fZJGurzMtp7ykzLNKzXM/3r1byr48ft9u7b+slXkl90f+tv91ttx8/yqtWr+6fz7OSxfwf+fr+E/99//XL3foJYYG/GoDNT5ZP67svX+/5DZ/uX189wF9Wb96z3LbrJ48jGb38S57WW5bl+zerX64Y4OoN/XnzZb1M9iLbwZks119u6P1vVtcJ8D2he/vT3dOh4GKQT3c/vSWM768N4OZ+g+g+r5dHgotALtef3/oPvBqAt+Qkvn47FV2D8dtXclK31wHwF/J9N7+T2SUzXWSQv9+QP/7lxQFuSHif1zOiqzGuP5MYNy8KkODd/PY0N7qA8em3m5MhngSQ4N19Pw88gfj97oa/5kUAErz5dXNXU0+DeDRAjMaru3dnRecxvrtb8dddFODzbfrnX8sLwGOIy7/+TG+fLwkQScuXJVwIHynq8gt/6YUAoro8Li4HTyAuHo/T08UxoeEyxtdjikeEjMUR4tsuLw5PTHF7hBAPBfgqXV1YO1t6usIFnBMgZjHb7y8EjyF+3/IizgXwTfppDS+Ij4S4/nSYO10chO/xxxeFxxB/fDwI4XSAGGi38OL4SIhbXszcAFeknslVXKSmq7kBvklvlleCjwLGzWQ1nQjwQ/oIV4OP1PQRlzQjwFV6d034COHdRC2dAnBzi/iSK7sQ4e1mHoDPq3R9dfjI1aSr5zkA4odcIz5GOCFc7AV4m978cZX4EOEfN+ntqQBvryk89IWL29MAXjW+SQgXe+zvqvEJwufjAT7fXjk+RjhejhoDuFmlT1eODxE+pavNcQAxvr9ofLDTmlUYLcYi/mKsOvGi+IzSWulpCF8dA/DDy/Izk9G3T0N4N8K8F8P50cvyz8wl4PAvmIbwzaEAb9PHdy9ofolWic1UkugpIkzePQ6GwwGA/9rcJPCC+AAlpzMEB2qSK01uNv+iER2+JgDc3N8vXxJfUmkUIKmnnSRBDIf395sA7fV+gC/sQAHxoQBJeBMBkiv9P4T2K17/1ULYC/Btun3J6i7rZs4CnAwwgS3Cy/5Wljr7NUa4uDIHA6VSGkh+mSFdnWaD9Mrk1x8UiwWyWIaLPob24YW6K0YjOpM8YJAXfDbJJ0rQGqUMSR///NvP4wBfxgBJdCoHROdQeoSPLzVpKU6rwjA7IMWGH6KgseipoF3eAIFFZ2WpBK+AwNcmyh34xRk7Jvh5DODm9uOFFZS0SzsUnTUMDuEZ55egzX5wRQ7yKfhOjfdqjwTfXlhBSXgsu4AuwyVrZz1A2AvO4q3xao36bFG3yzEbvLCCIjzBUMMjMTRi65egdeKOBBwilffRaxXqqPr7MMDN6qI5fJ4RPAOgInQ2ekFpegVH4BIPLry1JKdLOcj//hDTtQ7Af1wuh0DCqdhHal3W8IrOt+dm15+UIjgLpqjvC2s5IiSK8MPfhwP98+bxkhlfznlDVjT4JGS3ANpWkMQ40hYc+yTrX6Uz/O/nEap2mSKM5XqExoyP84bost2XsopaVxI48SdtbKKrYAwvmzwMZO18YtHG9wiXCgzkXGzSFkaedBHi+muTE59UQ/M/BETn4SUl3iH160g2cfvPS3gYINoBXdvKJEh3LpVRiPRib1wRUR4KnPhGHX0UhcEffvzv15sBgBdJIqCQwGBtt4aW93w52VxiDDsQXTta+qF1ueZAERu1Q+767n/St/0ANxco81qMezaYYfRjO+yKdMa+1tTSY6PTLdEFcaOGmmR5E5cRF7EAzx4iTMbwjONR1wQcrhPGnJFh+sX+MXK0gR10dAN1HBA+3MUibABuNjfn9p1F5Zi0GDC5cbryHp5299ggRZuEIWIrRmce8P9r60O0TuW2t06FTkrI+c1m0wPwH+cloRQQSBYU3Uv0gLx2dIFK4nTsg0wtb+ZfaJyRn00MkYE+hPRaTB8tlS/+sQtws1mdWz0lY1PIW7TAIxkiOABn0GPonKxK1xmg4Hswqo0v74dHL4fAXVeNCBeXcqGl1MhEYMw5mcBoJ39jOEAORvINETDnXz+4zLX00w2kvHQXwOfHsG2ssAb4+rwxUBM+UASQQ55kAErinzZok+gWja5joYBCr5GVTfRLeoUnkRQZUZVYZaWI+M/XXYCvzilAEMFAbsjLE0ZxihWraB5zr5DoBuoWXor+aKyKr/lT6xIVivBVB+Dt+zOxUOL8SnooABTElNfRsG4Vk1EfuU1I8FSWB/XMzLD/MtqwhI0vUcHT+9s2wDOxUBfy0sBLHlrceudSQF7QQlDJMlOVx4c6MMgGQBkx2DLU4OAxDEItzhQjrK8kQWclSJJHAOYaogwdJV7L2gwqqKUmm9yEhzrCNJFica4gXxcjrERwQaxzPSZBo6xJtAk+s7kXGDmHBai8EUcAm2C/8C5mZpYW2FSzKI4GphzXUHQ4BpRPgJO8+YXJ7KCiqCBmHVVRka+9igDepvPGCMO1FutTIcroMMxlEy6M6sGnGoh/rPuje5RCkU9qLAKWvnS48M2I+Wsttk5Q1ajdtY0wsyp4mOjHMGCB0LprplUH9z3RxfzFekOMw1c6KYZrunw0qPYAVFmQWyyaMlf9Aagj/kTZuJ/2qga4Wc2oocxZONOhrKEjO/Qy49JUIQTGsgFl+loZ0SsqLSoavWAp4zOLuYOgUlziMqim0Gt2hR6DWMl7yqIVHbsuBtOtUutAGfAtvu0St6J8KFzMrKHEyayIUQ/r4cjFyFRLlfOdFkwo4pA+KLqX/GXQaiZ6HSWAm1ezaajgo8zP9ACrJriZHvTQmZaRXhQlWYqyEv6tAGxJcPlqIwBX6ed3s+JzZA3tNdJ6OFcvJzvUIR9qjXHWO2iyTSnN0MfaFsDk3WfS0cV8UZ6HW6Q/AB3HYdt07bBLQ9b6Gs8CVd2CYpNA/ai5dhzrFxQFZ0kkCJnvf7iIm2Q+NlF91gjvNocBNHqHZ3ercJq/KinLFsAnioSLdJPOxEMhc2x/ztWSq+kMrqBSbINMpw/UUDPUAIjwYb6x02y7QXCL2TqCFN8tVZddU7+0Sej9GT4Zx8l0wUEIFeybVzM+oeo29DHtXSHAeYKEU8qxMSgXaIXheZfEMTwf/JQuuLQCh2noHtcm4TWPiUwdKBbp6+e+IFG3NCZZn1Wea5gsMBFJsVk59Q6fOcAOFWRmn2nwFxbQnamB5fPrdPHvPhMEYv+ahDKdfYrnrnxqphocnnXF1EonyYAvLbo/h0IlUzQU0e1OJt6k/17c95igy0xUypzQiHYdZs+rrMT6Q/Ezekmuyh4l5byuK9my3DsxasShFcmOPNAI7xcfekxQmeD9spFEusWuORK1Waa1dX2pZJ13yYMPgtRCyLsASyKVHdx5PnSPQ6Hfig81lCnvKOI6/bDoaeoajjvAbe99MrTcNua0upMnQBLlMV54QTUVlLtKaugG2XaMV73fH7rakQn2aihN5C/S3clXXfrGjgnBbcx9EtOHTn7DWGJ8JiSDXDak7jzsKCn6IpMlPj1oemW2vxjJ2lIpSaiz0uieqS9AI1z0JPPhlYp0j8pZdrjTbuQuKN+eLEJm224okCtFMkoJlLUEhT/U7JBq8IUmjpeZ6m+tySAJV0H4olsLyvZOfT0iwG2PhoogMYKxCtoBhIZmkoxwUCUy0I2+hQxX8WyrrTMBlHkGRtftsZh0ulDThlCM6+t/6xxDGDM/dvVsgfpB9SgzepnFLtMOrywzxzONPQg93zXSiw79u0hLqTLG1UpAKRgZkdOsb9b/v+8DtpJiFquqp5Z67quuPKROrKz6x/aQby92nGjjjEi54yJEG16jPz4Fir9Y+VBv6G7Jyl2mG7+kfHHKR03TjJGAL7+pPr3RWW+65fBb+gRIbnSx40RNHRlMaIb4QF7vtUGDo1Y0Jy4mL53py1JdUFB6N43EQBJlNyxN4eC6aeAqnhxRzejZDqHozae0eujnc+hGF//pEjWi5BaCAxa0zGzI4mi+zzTSszSlojxxaft88AV3S76qSdR8ThqoK10PUNWZPJM7aPiO3SWduwQBA9UAIYflfxY7RK2gu+3qD/TzEC6XXC5MplAcEWtQnWq8khM12YtTCqNoeiU2cGOk1UR/2NoKtW8cUitMNx9lapB2gKErsiCVDwSzm8W2L0jwfRY/3p6GC/vBQtlzVzlJw5QC4U+Ka/V0U6SOT/13egdY45mb1Gl8y1CHkdYmxJgC6i/sF6DOzMNwwrFdbPt8jHc0EIY4mqRdxaHX4T12u0UjlLH1/45yexMmEQgNsriqakmBO9Vo23grXAlxScAXPEAN1OP0w1DnguLEohslnGZrtrWOAqsTayoRPhrWwBexlpIi7tSXsrjDmUeLUhS4EAHB0Z0gz61fFgd5LwPtX+uciYvuc2Yk8mE+DneLbpSgHqIxsmkh0V5HvVNB4qIMu3vJZUHzJg4z0i5qWtAepCflZregzc5F2izQ/cyi4A5+z0V5iRlkk7DeAUjURGuvmezqH/ALKyIZ+AuUlzINQqscxTQzApDCmg4dMZEuAewulozVBISt0aaRKriM0YzgI4DdKEGzULieXNwJ6wBXImTEBtEgmZCsgH9At16N5ecmJ4ZqWlmDUbvWRB7HUFLi8nISQrpNCh7MGD6ME12AwJEZQxcmH1ZyrZyR0pYSmhnOrKoU3zj+ZI69+iEfBkjYXGRUeEt6/b0lLcXbhx7YRZl6N6YL0SarZL+lh3KBIYBa+zxcQjxNnxTA7EpXUrWW5XE3gH6hVKfb3Bfxy0gg6Ef7a7/kkJUtVS0Rt/NZVibROVninTxKjeJjgLtRUIWxPh8KtYguM8IjtQ9XNKZEWZ0EtsG6dOLHXzzFpBSl1AP3gqaVoPIcZyfbIH/us2pVClHP95UblovdVMk0vNCbgpbCgPKxkZfHX2XqHQGDZiiZVEH7rDhf5Jx34MWassocGhm2DNH4CrYOE8x6ygbfRWdiD5IkawEMAgD5y4ZaikQm5UzeTeC7mueHBi1nWMSyS2PGXoy5MpcUEgdxJ0Nct4PWeMWhAHXR2HYAWHgRKj807j1EUYrHDw4SzKiOiq9jEg9m2GKzEpk3IpRvMqbu82ccnrs1+2p/TawF0PLmnxCEIaqrSo1GxXNyvkYRqLYaFqEOGmZ930SPFnq5aONlmIs6SmpsdlJgOxojdgGSBVodymHh7tQ6KhN1tY02tXjBMRQqKq9mOgmjFx16001fKSKGlZtcG1+dML1DtnCIBJVPZKAlQuXBIkDkajy4mNXUpKEkCvYNFlQ+hVDG7OlGyOydDf34ngl0TLRAClcHAAQlOlR6Nt/xo0pXVP4nyqgbNDoqiu1vNBgqrcE+koJMpyq4Xjk4jo/U0ZrQdJ0K0EjowwUYHYlQezevTYXhDqjYZppEMNo3NmmWSd6gdbUHISaKvJvANo6lBRZFKmxSjTuaRYdnY55utStMByDVunOnLAHEb9elNoOaOLEvPT6Vx62KpsThlbVVHWUR7i2+twDSdoayUMhD80hFgw2SfoIHCDHAUJHR+4dE4ttR7J0HYoLBX239YHZbhMYJexxzNDFVo+pAITy9UPWMtPPdE663GZo6IoCxuIJBFgFmlZ18iQk41EIunXtKFu/+AR0qK2Nm2CLbRc1DlQpiC70NBuiIHVELV0GTXes60oUmUaZOB+hvms2FkPkYIQrb7O/1gAdLTp1sQkNU2KlYgmFHVHDN5F8cbdRsAYwnHzMMjDqbAaEnNibhWqJqlkjmR/6FFMrjHlTSNkAVAyR9q5lgcFSWAjtXF1oAW9kS/l7PIMKQJZI3clwXbERI7QAGHSQ6uN0eAa7bEtRxKzl0/BTUkzBCNDTPVasQHHSLfMFh/nSft1XAY4uZBIXCIZdy+oHWiv8OFeihWNGuyWjTUO1WAasuhirPOQ1ldJXmoh4Phug21YJsPhGyU8sTvz2b9NIliot/Wj94HR1UUgQYlQ2poGb7aEktQKoic8dCSb3eiDNoTxbSXMR8IjTUFlUFricvUXiWpjRJFMi6HOmolZJyqYbKhlHhFxTvgyWmqWxTzqxrv5YLaspHKXQ2qJ6gd6dCcDHJfCIEjvo5WTbFvgLVhZqdispCCBbyZITQwLZVuudU2gk7KhpO0TghLQU0diKguLwStajj2aT5ROh4tkE7JG8GyodSPSDzzrh4pGgMXVmZ4OhX0m2r+dI4I8ulNQ549RvRsygJi0qyPDS2UMlwnSit5hOhlqaZDBYVpGa4jAzKiqVGlAO4DdU7153ctNtnsZy5TWRVXZbDRKCZ3yHgaGjW+b5sx6u4GUWoBCB7dEAFpWFNS6EXLR+lCbpIVKsz3WmftRqgNuq1QFZhtl5B076yjE/54FeCTIqYns4PtexOioVVi3hXwh+k1Yk4UG0LGv9gqyyzsGtwtwVDDdB2C9vWXUfEk8sxO1HnuqhjY+XI4qvm5J52D1up3J5C2XT05mgSjOdM9EOWU0JqHvA2GnAUSYSTFr0t7HZ7ycoBGj0eiWopupL2a2F82VLH9T3dGuZCJVX6eICV7iQryreM0YwoHUDfQ+m8ccDjbOBLSj1DCN0GIaq8cXq3f0xVH1XQeEdIs51pd5fbdldR+8IcqZ95pA6qBZCmK6l9YxqATgJIzwFQPEayMwhkqQbSbKZt8IExPWKtO4kFzRa0Sr7ZcRGfy2q6C9BLFu8yqiYCLAigdQ4q/BJmpUr1DgINHWKou+OlEdTWCBJwzKW5D9XWSVLngxFynxdJBPRIUPnBDY1/oBAIIHp66jkiAehJC3mUa/iEFV3B0PhdBr5hb3kDkZY93knWmUcgDTXukPSI9dNQWG91sSN3gywr8UNcNKIEyGoMjRgZpWzvMN6H4YnmoWIHcIkmwlLIWBvsFNY41ZjEvFVTxKrY0Hq6OUpoJAKTQTXJhXNtcuoH7OKTccr7wZn0/iSL7JB6AqbdIsu5dAB9657kaXREfVHztIEew0Tw1C1AU0CYCJA3ZpMslbF9fUIeiP334K6CbinZ+glzqOvdUQbouerA6vcGRJU3d4QYoOoyIQqMlj6Ipi64B0oDlAEgSrzs49o00jwwlB4DtKEA6w+e8HbYJP9+x8uwMu4FaMJr8HNtlSe6078g72X8xDu6FWTFCCqhPADZm9KudzJZhtKHtxVoLmc1m0abIoWcjKHiGXMKs4k+gVArlh6G94LYpom32yupZKFl5NKKxJe7BxrGoUSNFLQ/EPhtBQMbQ3xFkuZAqCGuGyegefzV5FFnEjDt1GCOpmbSVeV5jIxzFmkASWTwsyhkZlLMJI00rMWU1peDRyT4jSGDW3ug6hl2KGiTB52X0jQnHOom5p0G1LEAMZAZ2WFPo6RSOctkzEv5uE8/l+9E0yOlZIUxbPiDbVDZ2jO8OUs2RUHONu3HeqS9rFsNXcKn27XgAwtLHE8MhxWX+FGAWqgV/xYSbwLoY4hpUVKYwdjxlWFz1sj2uorHmQyfJMiDd9KkQ1LaVDSQeGN40AVVvI+TIJXNIPMY/UYz6x1nQdpZ8YAJxic/DUBElA4kIxNUI1NOYXvd8AZJPsPHQsn9OWYlQPOUprU/UGWFI401Wh+Lr7Ic3jPjywcsQC/UUtwZak3iGyZI02gXCNCWClDDnaV6g+TIFteQPPGmY6nV0PB8yeOPgSPmRAyzo+WHVI7Jn4q2KdF4DoFTlH4y4Qt+h/aSK8e7QLKsrgf313zDFtfR/Wcyzdx6cIAFXkwg1uIGji1mY4Rn2wJd8wXJTeg7DImxAj/7W0q1nUgnunEaCnwYO0A22qQ8us0cs8mys+tSZq5cPHB2dIBQUhYnAwAZgZdJFRrOkxI5KI+bhzOJvhinkgoqZNpjjbNom/nYQQFWBrZbbStSVmvRtSh1eheCh58xa82ouFI3CCgiuILmaY2p53OMkiNY0LMQa9LJ2M6q+KCAvXsk/QGKzRwIVWyMJGDCP48NgTmbG60a/b6tR7Mpl8ipPCE5pbTxyNWSOpeooTRskqgi2auhkw/roBPQVHv7RBgY5+ODVXm4MCvD7TnJJn2vWiqQCSPjM6yUP7OyEDMlAaJPLwwXX8eu+LCOicet+N0SPXuYuIDvDuNqqPb8NpBdFPWuCsVelWrzFBWLLMzHE7cQ9qKIU6jx8Yr2cStTt9KDVkNHEfIYkKZ9yHTY6cD8eNjpwvN0IngQlxVtluXBPgrkhpHqEDeSRoAyNDq+1PaBOZOPPIKdk36avQYg/X2h5qoNqUl7MAF3xoisc3YoTS2M8SnGZxifCdgVF5k5A6R+GpXR9oxwtY88OuDQqvpc7NaGZqh/2Z07zonp7VYnWIisop2tH5rxAcWNZruTH+6nU50feERi3whe99Cqw05cMaorRuuaPdtECqKdb5xvxNZJVNmYUnY4amgoBOPLHwzvxWMpBmJDFBxkFIB6lKqCfSN4O8eOHXhwHItxpywMuxVx4PHjShlwRbzXgLZrE6c1eTSERs70QbRSZvldgw+9jRJFxQ/aO2K4e3DcwUf/WbN7aJqJMbY2OIKJtwqGvZvKn/UL4BsfPPNair9OdBjHZnyG2Zx+KHQy4XFTPUf/HXF4I23GdG1NRcV0Q1uK64jJzaIqHODvcmm+E3UhY3M85WuSQtcNLZk3NqK1k1bWc3jjUcdvkhg7p7VCz9m7nbtC2NBInXOajj00Xky57AVm88Ow7+oarHTniNJMGA8NAnw71wGqzh+6DDXLsWYfRsqfaWtWVZ+fbfxjUIxXzyS4Vl35/J54Go912Smr6jtA9ejjDb3D0To68qscxtg3KSDHnTNIUFGLyCmhTpIummkPCksGjsA95XxDft6KaX095CNytK1/WN4mKec26qwJixJeLe2WpkQCI6CauJ7+Q4xPOobakS2prDU2PoqxgxhKOb+5tVvR1r1VxldMxDd0DPWJB4kDb/tUO5FjEkjZbqB3j5e2DT49Fd/gQeKnnmNMiqoyvSscAulah7YTFevGE+t2j5eW3T38qVPl13Khcx/mb/gMyp7tDWB0OJeBfKwg3i9X2azBhYrp+Jb/vE3P9jgGy/xz4FlJmEo5h3/A5OeGW+3bLslk/xKx0PM8UINMUfcI8Uid53TLSAV62vc/dZ5yPvMjUWwoU8EM8GivmtSZ9PS3PW6ex54/OMtDbaznzCcI0RpqvJAyUCfLTVeguyjGn/WxRKcIkbNHOlsCnMpBKTv5VmGI6D6U90wPlrKeWdpDoRk+cafSjuipViafdm5W3BHc8wzQ+R4NtneD7e5dAS3EgGuU1KY+TA+QhL7d+5DT2R7uFoR40CUPVaJcmspbhXKHKAEsP+4+U/m8j+eDapoBWTro3+R0ri1pqJNaujrw9vQo6JkfsDhFiDaEfS5VccWR524PhjfwyOizPyLT9Mwt+t80J9Rok5eMrvDc3BhzsLYsP/Q91vzcDzm1URO1Xoo/LoefZkaqSRsU5HzVE27swNOUL/GY2q4Q6aQuxxOL1mSFIdmZ0819u+tBL/SgYdsEDAtNZUrLOVFzxaShZ7Zf4lHRlg6wlMd70fME6bQdKDLqcjiYhZX7R0W/6MO+A+PKae6KBloVzPj54WHfL/e49iZgAB1NMyu65JjHtafpm9mfhOb4gBNqqTg77ydjDvEmPRRg+mFmhCxEZ2ZzKy18H9LDAZ7jkdFm8rHBczjQfQA3ty/7VPrp+G43RwFEznaRZ56eiO8pXY3gGwWYPt/eLK8cIZV5n9NjAabP6ZUjRHzpKL49ADG6XDVCwnebngLwuhFOwLcXICH840oRwh/78e0HSCp+ndEC48Me+5sGMH1eXSVCxLfaj28KQIr4d1eHEPnZWHw/CCBV2u7gqiAC4ltNWvo0gEhmH68JIcDjCL8+BiCmI1cULig8vEnnBYgK8elaXA2sP03Uz0MAkkfeXoOaAmynhIfDAZKaPv744gjhx8fJ6nkoQPxgVFN4WfGheh6C7zCA6XtU0+8viBC+b3kRZwNIdYzV4qWECLBYjVUnZgFI7mv7MgEDltt0uvc8GmC6uU9Xd+8uDhHe3a3S+016foB8Fx8vrKeonY9HiO9IgORO0y/Ly0EEWH5JD3OeJwJMn2/TP/+6lCnC8q8/09vn9JIAWV0uY4psfEdp52kA0/Q+TW/WyXkVFT99fcNf9QIABeLd9/NBBPh+dxq8EwFSyEhvfns6D0SAp98I3iZ9OYACMf08v6aSbn5OT4Z3OsA0/eX+NYrx9+WMGPGTlr+j8F7f/3Ly8k4HSLVTEuPXb0uYAyN+yPLbVxLe7RxrmwUgaSqq0tvP61MxErr157f+A68IIKVSxDTe/nT3lBwJkt72dPcTTbu8eT/bsuYDSMGf2dTNl/XyUJD08uX6yw3zwNWca5oVIHqcldz8j9v1Ey97H07/kqf19qOoweqXeVc0M0DeiXj/SWjA1y9366elB9GC2vxk+bS++/JVQvmn+9fzr+YMAJmM37/yivbx43Z79229xKsBSP+3/na33X786JX71f3zeVby/97BewMk0eqbAAAAAElFTkSuQmCC"
PIG_DISC_URI = "data:image/png;base64," + PIG_DISC_B64
PIG_AVATAR_B64 = "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"
PIG_AVATAR_URI = "data:image/png;base64," + PIG_AVATAR_B64
SHEEP_DISC_B64 = "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"
SHEEP_DISC_URI = "data:image/png;base64," + SHEEP_DISC_B64
SHEEP_AVATAR_B64 = "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"
SHEEP_AVATAR_URI = "data:image/png;base64," + SHEEP_AVATAR_B64
GOAT_AVATAR_B64 = "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"
GOAT_AVATAR_URI = "data:image/png;base64," + GOAT_AVATAR_B64

# ── The words and pictures for this herd ──────
# One table saying what the cattle screens call a thing and what the goat
# screens call the same thing. Categories are stored in the database exactly
# as they appear here, and each herd has its own database, so the two sets
# never meet.
SPECIES_CFG = {
    "cattle": {
        "key": "cattle",
        "title": "Cattle Management & Inventory System",
        "short": "Cattle",
        "one": "cow", "One": "Cow", "many": "cattle", "Many": "Cattle",
        "young": "calf", "Young": "Calf", "youngs": "calves",
        "birth": "calving", "Birth": "Calving", "births": "calvings",
        "sire": "bull", "Sire": "Bull",
        "categories": CATTLE_CATEGORIES,
        "female_chain": ["Calf", "Weaner", "Heifer", "Cow"],
        "male_chain": ["Calf", "Weaner", "Steer", "Bull", "Ox"],
        "male_options": ["Steer", "Bull"],
        "young_cat": "Calf", "weaner_cat": "Weaner",
        "maiden_cat": "Heifer", "adult_f_cat": "Cow",
        "logo": LOGO_DATA_URI, "disc": LOGO_DISC_URI,
        "avatar": BOT_AVATAR_URI, "icon_b64": LOGO_B64, "emoji": "\U0001F404",
        "db_name": "cattle.db",
        "heading": "Cattle",
        "branded": True,
        "breeds": ["Brahman", "Tswana", "Tuli"],
        "example_weights": [310, 180, 445, 520, 300, 410],
        "example_prices": [12000, 7500, 9200],
        "example_place": "Serowe cattle post",
        "max_weight": 1500.0, "life_years": 30,
        # 283 days' gestation, a 21-day cycle, first service at 15 months.
        "gestation_days": 283, "oestrus_days": 21,
        "first_service_months": 15,
        "wean_months": 7, "maiden_months": 12,
    },
    "goat": {
        "key": "goat",
        "title": "Goat Management & Inventory System",
        "short": "Goats",
        "one": "goat", "One": "Goat", "many": "goats", "Many": "Goats",
        "young": "kid", "Young": "Kid", "youngs": "kids",
        "birth": "kidding", "Birth": "Kidding", "births": "kiddings",
        "sire": "buck", "Sire": "Buck",
        "categories": GOAT_CATEGORIES,
        "female_chain": ["Kid", "Weaner", "Doeling", "Doe"],
        "male_chain": ["Kid", "Weaner", "Wether", "Buck"],
        "male_options": ["Wether", "Buck"],
        "young_cat": "Kid", "weaner_cat": "Weaner",
        "maiden_cat": "Doeling", "adult_f_cat": "Doe",
        "logo": GOAT_DISC_URI, "disc": GOAT_DISC_URI,
        "avatar": GOAT_AVATAR_URI, "icon_b64": GOAT_DISC_B64,
        "emoji": "\U0001F410",
        "db_name": "goats.db",
        "heading": "Goat",
        "swap": {
            "cattle": "goats", "cow": "goat", "cows": "goats",
            "cow's": "goat's", "calf": "kid", "calves": "kids",
            "calving": "kidding", "calvings": "kiddings",
            "calved": "kidded", "calve": "kid",
            "heifer": "doeling", "heifers": "doelings",
            "bull": "buck", "bulls": "bucks",
            "steer": "wether", "steers": "wethers",
            "cattleman": "goatherd",
        },
        "asked": {
            "goats": "cattle", "goat": "cow", "does": "cows", "doe": "cow",
            "doeling": "heifer", "doelings": "heifers", "nanny": "cow",
            "kid": "calf", "kids": "calves", "kidding": "calving",
            "kiddings": "calvings", "kidded": "calved",
            "buck": "bull", "bucks": "bulls", "billy": "bull",
            "wether": "steer", "wethers": "steers",
        },
        "asked_phrases": [("due to kid", "due to calve"),
                          ("about to kid", "about to calve"),
                          ("to kid", "to calve")],
        "singulars": {"goats": "goat", "kids": "kid", "does": "doe",
                      "bucks": "buck", "doelings": "doeling",
                      "wethers": "wether"},
        "branded": False,
        "breeds": ["Boer", "Kalahari Red", "Savanna"],
        "example_weights": [34, 18, 52, 70, 30, 45],
        "example_prices": [1800, 1100, 1400],
        "example_place": "Serowe cattle post",
        "max_weight": 200.0, "life_years": 20,
        # 150 days' gestation, a 21-day cycle, first service at 8 months.
        "gestation_days": 150, "oestrus_days": 21,
        "first_service_months": 8,
        "wean_months": 3, "maiden_months": 7,
    },
    "sheep": {
        "key": "sheep",
        "title": "Sheep Management & Inventory System",
        "short": "Sheep",
        "one": "sheep", "One": "Sheep", "many": "sheep", "Many": "Sheep",
        "young": "lamb", "Young": "Lamb", "youngs": "lambs",
        "birth": "lambing", "Birth": "Lambing", "births": "lambings",
        "sire": "ram", "Sire": "Ram",
        "categories": SHEEP_CATEGORIES,
        "female_chain": ["Lamb", "Weaner", "Ewe Lamb", "Ewe"],
        "male_chain": ["Lamb", "Weaner", "Wether", "Ram"],
        "male_options": ["Wether", "Ram"],
        "young_cat": "Lamb", "weaner_cat": "Weaner",
        "maiden_cat": "Ewe Lamb", "adult_f_cat": "Ewe",
        "logo": SHEEP_DISC_URI, "disc": SHEEP_DISC_URI,
        "avatar": SHEEP_AVATAR_URI, "icon_b64": SHEEP_DISC_B64,
        "emoji": "\U0001F411",
        "db_name": "sheep.db",
        "heading": "Sheep",
        "swap": {
            "cattle": "sheep", "cow": "sheep", "cows": "sheep",
            "cow's": "sheep's", "calf": "lamb", "calves": "lambs",
            "calving": "lambing", "calvings": "lambings",
            "calved": "lambed", "calve": "lamb",
            "heifer": "ewe lamb", "heifers": "ewe lambs",
            "bull": "ram", "bulls": "rams",
            "steer": "wether", "steers": "wethers",
            "cattleman": "shepherd",
            "herd": "flock", "herds": "flocks",
        },
        "asked": {
            "sheep": "cattle", "ewe": "cow", "ewes": "cows",
            "hogget": "heifer", "hoggets": "heifers",
            "lamb": "calf", "lambs": "calves", "lambing": "calving",
            "lambings": "calvings", "lambed": "calved",
            "ram": "bull", "rams": "bulls",
            "wether": "steer", "wethers": "steers",
            "flock": "herd", "flocks": "herds",
        },
        "asked_phrases": [("due to lamb", "due to calve"),
                          ("about to lamb", "about to calve"),
                          ("to lamb", "to calve")],
        "singulars": {"lambs": "lamb", "ewes": "ewe", "rams": "ram",
                      "wethers": "wether"},
        "branded": False,
        "breeds": ["Dorper", "Damara", "Merino"],
        "example_weights": [45, 22, 60, 85, 40, 55],
        "example_prices": [2200, 1400, 1700],
        "example_place": "Serowe cattle post",
        "max_weight": 200.0, "life_years": 20,
        # 147 days' gestation, a 17-day cycle, first service at 8 months.
        "gestation_days": 147, "oestrus_days": 17,
        "first_service_months": 8,
        "wean_months": 3, "maiden_months": 7,
    },
    "pig": {
        "key": "pig",
        "title": "Piggery Management & Inventory System",
        "short": "Piggery", "heading": "Piggery",
        "one": "pig", "One": "Pig", "many": "pigs", "Many": "Pigs",
        "young": "piglet", "Young": "Piglet", "youngs": "piglets",
        "birth": "farrowing", "Birth": "Farrowing", "births": "farrowings",
        "sire": "boar", "Sire": "Boar",
        "categories": PIG_CATEGORIES,
        "female_chain": ["Piglet", "Weaner", "Gilt", "Sow"],
        "male_chain": ["Piglet", "Weaner", "Barrow", "Boar"],
        "male_options": ["Barrow", "Boar"],
        "young_cat": "Piglet", "weaner_cat": "Weaner",
        "maiden_cat": "Gilt", "adult_f_cat": "Sow",
        "logo": PIG_DISC_URI, "disc": PIG_DISC_URI,
        "avatar": PIG_AVATAR_URI, "icon_b64": PIG_DISC_B64,
        "emoji": "\U0001F416",
        "db_name": "pigs.db",
        "swap": {
            "cattle": "pigs", "cow": "pig", "cows": "pigs",
            "cow's": "pig's", "calf": "piglet", "calves": "piglets",
            "calving": "farrowing", "calvings": "farrowings",
            "calved": "farrowed", "calve": "farrow",
            "heifer": "gilt", "heifers": "gilts",
            "bull": "boar", "bulls": "boars",
            "steer": "barrow", "steers": "barrows",
            "cattleman": "pig farmer",
            "herd": "drift", "herds": "drifts",
        },
        "asked": {
            "pigs": "cattle", "pig": "cow", "hogs": "cattle", "hog": "cow",
            "sow": "cow", "sows": "cows", "gilt": "heifer", "gilts": "heifers",
            "piglet": "calf", "piglets": "calves",
            "farrowing": "calving", "farrowings": "calvings",
            "farrowed": "calved",
            "boar": "bull", "boars": "bulls",
            "barrow": "steer", "barrows": "steers",
            "drift": "herd", "drifts": "herds",
        },
        "asked_phrases": [("due to farrow", "due to calve"),
                          ("about to farrow", "about to calve"),
                          ("to farrow", "to calve")],
        "singulars": {"pigs": "pig", "piglets": "piglet", "sows": "sow",
                      "gilts": "gilt", "boars": "boar", "barrows": "barrow"},
        "branded": False,
        "breeds": ["Large White", "Landrace", "Duroc"],
        "example_weights": [80, 25, 140, 180, 70, 110],
        "example_prices": [3500, 2200, 2800],
        "example_place": "Serowe cattle post",
        "max_weight": 400.0, "life_years": 15,
        # 114 days' gestation, a 21-day cycle, first service at 8 months.
        "gestation_days": 114, "oestrus_days": 21,
        "first_service_months": 8,
        "wean_months": 2, "maiden_months": 6,
    },
}
SP = SPECIES_CFG[SPECIES]
CATEGORIES = SP["categories"]

# What the whole system is called, and the wordmark on the opening screen.
SYSTEM_NAME = "Cattle & Small Stock System"
SYSTEM_WORD = "Cattle &amp; Small Stock"


# ── Saying it in this herd's words ────────────
# The screens were written for cattle. Rather than keep a copy of every label
# per herd, the goat and sheep screens run the same text through the swap in
# the species table on the way out. It only ever touches what is shown: what
# goes into the database is the raw value, so records stay comparable.
_HERD_WORDS = SP.get("swap", {})
_HERD_RE = re.compile(
    r"\b(" + "|".join(sorted(_HERD_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE) if _HERD_WORDS else None


def _match_case(sample, word):
    """Give the replacement the same shape as the word it stands in for."""
    if sample.isupper() and len(sample) > 1:
        return word.upper()
    if sample[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


_DATA_URI_RE = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]+")

# "1 cattle" reads fine; "1 goats" does not. Put single counts back in the
# singular after the swap.
_ONE_OF = SP.get("singulars", {})
_ONE_RE = re.compile(r"\b1 (" + "|".join(_ONE_OF) + r")\b") if _ONE_OF else None


# A herd word that is already both singular and plural does not want the
# "(s)" the cattle screens add.
_TIDY = [(re.compile(re.escape(SP["one"]) + r"\(s\)", re.IGNORECASE),
          lambda m: m.group(0)[:-3])] if SP["one"] == SP["many"] else []


def _one(text):
    for rx, to in _TIDY:
        text = rx.sub(to, text)
    if _ONE_RE is None:
        return text
    return _ONE_RE.sub(lambda m: "1 " + _ONE_OF[m.group(1)], text)


# "Cattle post" is a place in Botswana, not a herd, and "Cattle & Small
# Stock System" is the name of this program — neither gets translated.
_HERD_KEEP = re.compile(
    r"\bcattle posts?\b|\bcattle\s*(?:&amp;|&)\s*small stock(?:\s+system)?\b",
    re.IGNORECASE)

# A few phrases need more than a word-for-word swap: "cow" is the animal in
# most sentences but the adult female where it sits beside "heifer".
_HERD_PHRASES = [
    (re.compile(r"\bcattle farm\b", re.IGNORECASE),
     lambda m: SP["One"] + " Farm"),
    (re.compile(r"\bcattle (purchases|sales|records|list)\b", re.IGNORECASE),
     lambda m: (SP["One"] if m.group(0)[:1].isupper() else SP["one"])
     + " " + m.group(1)),
    (re.compile(r"\bheifer becomes a cow\b", re.IGNORECASE),
     lambda m: SP["maiden_cat"].lower() + " becomes a "
     + SP["adult_f_cat"].lower()),
    (re.compile(r"\bcows and heifers\b", re.IGNORECASE),
     lambda m: SP["adult_f_cat"].lower() + "s and "
     + SP["maiden_cat"].lower() + "s"),
    (re.compile(r"\bcows? or heifers?\b", re.IGNORECASE),
     lambda m: SP["adult_f_cat"].lower() + " or " + SP["maiden_cat"].lower()),
]


def _swap(text):
    kept = []

    def keep(m):
        kept.append(m.group(0))
        return "\x02%d\x02" % (len(kept) - 1)

    text = _HERD_KEEP.sub(keep, text)
    for rx, to in _HERD_PHRASES:
        text = rx.sub(to, text)
    text = _HERD_RE.sub(
        lambda m: _match_case(m.group(0), _HERD_WORDS[m.group(0).lower()]), text)
    if kept:
        text = re.sub(r"\x02(\d+)\x02",
                      lambda m: kept[int(m.group(1))], text)
    return text


def T(text):
    """The herd's own word for something. Unchanged for cattle."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if _HERD_RE is None or not text:
        return text
    if "base64," not in text:
        return _one(_swap(text))
    # An embedded picture is a long run of letters; a word inside it is not a
    # word. Lift the pictures out, swap the prose, put the pictures back.
    kept = []

    def stash(m):
        kept.append(m.group(0))
        return "\x00%d\x00" % (len(kept) - 1)

    masked = _DATA_URI_RE.sub(stash, text)
    return _one(re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))],
                       _swap(masked)))


# The other direction: a question typed in this herd's words, understood by
# screens that were written about cattle.
_ASKED_WORDS = SP.get("asked", {})
_ASKED_RE = re.compile(
    r"\b(" + "|".join(sorted(_ASKED_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE) if _ASKED_WORDS else None
_ASKED_PHRASES = SP.get("asked_phrases", [])


def U(text):
    """Read a question in this herd's words, in the words the answers use."""
    if _ASKED_RE is None or not isinstance(text, str):
        return text
    low = text
    for a, b in _ASKED_PHRASES:
        low = re.sub(re.escape(a), b, low, flags=re.IGNORECASE)
    return _ASKED_RE.sub(lambda m: _ASKED_WORDS[m.group(0).lower()], low)
try:
    from PIL import Image
    _page_icon = Image.open(io.BytesIO(base64.b64decode(SYSTEM_MARK_B64)))
except Exception:
    _page_icon = SP["emoji"]  # falls back to emoji if Pillow is unavailable

st.set_page_config(page_title=f'{SP["heading"]} · {SYSTEM_NAME}',
                   layout="wide",
                   page_icon=_page_icon,
                   menu_items={"Get help": None, "Report a bug": None,
                               "About": SYSTEM_NAME})

st.markdown(T(f"""
<style>
    /* --- Hide Streamlit's own chrome so this looks like standalone software.
       Multiple selectors are used deliberately: Streamlit renames these DOM
       hooks between versions, so we target old and new names together. --- */
    #MainMenu {{ visibility:hidden; display:none; }}
    header {{ visibility:hidden; height:0 !important; }}
    footer {{ visibility:hidden; display:none; }}
    [data-testid="stToolbar"] {{ display:none !important; }}
    [data-testid="stToolbarActions"] {{ display:none !important; }}
    [data-testid="stDecoration"] {{ display:none !important; }}
    [data-testid="stStatusWidget"] {{ display:none !important; }}
    [data-testid="stHeader"] {{ display:none !important; }}
    .stDeployButton {{ display:none !important; }}
    .stAppDeployButton {{ display:none !important; }}
    a[href*="streamlit.io"] {{ display:none !important; }}
    /* Reclaim the space the hidden header used to occupy. */
    .stApp > header {{ display:none !important; }}
    .block-container {{ padding-top:1.2rem !important; }}

    /* One sans-serif face across every screen. No !important and no icon
       selectors here on purpose: Streamlit's own icon rules are more
       specific, so the arrows and glyphs keep their icon font. */
    html, body, .stApp, [data-testid="stAppViewContainer"],
    [data-testid="stSidebar"] {{
        font-family:"Segoe UI", system-ui, -apple-system, "Helvetica Neue",
                    Helvetica, Arial, "Noto Sans", sans-serif; }}
    /* Form controls do not inherit the page font on their own. */
    button, input, textarea, select, optgroup {{ font-family:inherit; }}
    .stApp {{ background:#FFFFFF; color:{INK}; }}
    .big-title {{ font-size:clamp(1.6rem,4.5vw,2.4rem); font-weight:800;
        background:linear-gradient(90deg,{PRIMARY},{TEAL2});
        -webkit-background-clip:text; -webkit-text-fill-color:transparent;
        background-clip:text; margin-bottom:.1rem; }}
    .sub {{ color:{MUTED}; font-size:.95rem; margin-bottom:1rem; }}
    /* Section headings carry a drawn mark rather than an emoji: a short
       rounded bar in the brand gradient, the same device the metric cards
       use down their left edge. One shape, every heading, at any size. */
    .section {{ position:relative; font-size:1.15rem; font-weight:500;
        color:{PRIMARY}; border-bottom:2px solid {LIGHT_BG};
        padding:0 0 .3rem 15px; margin:1.4rem 0 .8rem 0; }}
    .section::before {{ content:""; position:absolute; left:0; top:.18em;
        width:5px; height:1.05em; border-radius:3px;
        background:linear-gradient(180deg,{PRIMARY},{TEAL2}); }}
    [data-testid="stMetric"] {{ position:relative; overflow:hidden;
        background:#FFFFFF; border:1px solid {GRID}; border-radius:16px;
        padding:18px 20px 16px 22px; box-shadow:0 2px 10px rgba(6,52,58,.06);
        transition:transform .15s ease, box-shadow .15s ease, border-color .15s ease; }}
    [data-testid="stMetric"]::before {{ content:""; position:absolute; left:0; top:0;
        bottom:0; width:5px; background:linear-gradient(180deg,{PRIMARY},{TEAL2}); }}
    [data-testid="stMetric"]:hover {{ transform:translateY(-3px);
        box-shadow:0 8px 22px rgba(6,52,58,.12); border-color:{TEAL2}; }}
    [data-testid="stMetricLabel"] {{ color:{MUTED} !important; }}
    [data-testid="stMetricLabel"] p {{ font-size:.72rem !important; font-weight:500 !important;
        letter-spacing:.06em; text-transform:uppercase; color:{MUTED} !important; }}
    [data-testid="stMetricValue"] {{ color:{INK}; font-size:2rem !important;
        font-weight:800 !important; line-height:1.1; margin-top:2px; }}
    [data-testid="stMetricValue"] > div {{ color:{PRIMARY}; }}
    [data-testid="stMetricDelta"] {{ font-weight:700; }}

    /* ── "Things to do" list card (red, action-focused) ────────── */
    /* The things-to-do card is a full-height panel by design: it runs from
       the top of the counts down level with the Search and Clear buttons
       beside it, whether or not there is anything on it. The row has to
       agree to stretch first, then every wrapper down to the card. These
       rules go through the row's own children rather than a testid, so a
       Streamlit rename cannot quietly undo them. */
    [data-testid="stHorizontalBlock"]:has(.todo-block) {{
        align-items:stretch !important; }}
    [data-testid="stHorizontalBlock"] > div:has(.todo-block) {{
        display:flex; flex-direction:column; align-self:stretch; }}
    [data-testid="stHorizontalBlock"] > div:has(.todo-block) div:has(.todo-block) {{
        display:flex; flex-direction:column; flex:1 1 auto; min-height:0; }}
    /* Title above, card below. The block owns the height — it is meant to
       reach the Search and Clear buttons beside it. Stretching does that
       whenever the browser lets it; the floor guarantees it when it cannot,
       and matches the counts, the heading and the button row stacked. */
    .todo-block {{ display:flex; flex-direction:column; height:100%;
        min-height:185px; box-sizing:border-box; }}
    .todo-title {{ font-size:.72rem; font-weight:500; letter-spacing:.06em;
        text-transform:uppercase; color:{DANGER}; margin:0 0 6px 2px;
        display:flex; justify-content:space-between; align-items:center; }}
    .todo-card {{ position:relative; overflow:hidden; background:#FFF7F7;
        border:1px solid #F6C9C9; border-radius:16px; padding:14px 16px 12px 20px;
        box-shadow:0 2px 10px rgba(248,80,80,.10);
        flex:1 1 auto; min-height:0;
        display:flex; flex-direction:column; box-sizing:border-box;
        transition:transform .15s ease, box-shadow .15s ease, border-color .15s ease; }}
    .todo-card::before {{ content:""; position:absolute; left:0; top:0; bottom:0;
        width:5px; background:linear-gradient(180deg,{DANGER},#C62828); }}
    [data-testid="stHorizontalBlock"] > div:has(.todo-block) .todo-block {{
        flex:1 1 auto; }}
    .todo-card:hover {{ transform:translateY(-3px);
        box-shadow:0 8px 22px rgba(248,80,80,.22); border-color:{DANGER}; }}
    .todo-count {{ color:{DANGER}; font-weight:800; font-size:1rem; }}
    .todo-item {{ font-size:.82rem; line-height:1.35; color:{INK};
        margin-bottom:4px; white-space:nowrap; overflow:hidden;
        text-overflow:ellipsis; }}
    .todo-when {{ display:inline-block; min-width:62px; font-weight:700;
        color:#C62828; }}
    .todo-when.over {{ color:{DANGER}; text-decoration:underline; }}
    .todo-more {{ font-size:.72rem; color:{MUTED}; margin-top:4px; }}
    .todo-empty {{ font-size:.82rem; color:{MUTED}; }}

    /* ── Countdown badges ────────────────────────────────────────
       One per task inside the window, reddening as the days run down:
       5 days out is a pale hint, the last day is solid red, and once the
       day has passed it turns dark so a missed job stands apart. */
    .todo-cd {{ float:right; margin-left:8px; font-size:.68rem; font-weight:700;
        padding:1px 7px; border-radius:999px; white-space:nowrap;
        border:1px solid transparent; }}
    .todo-cd.cd-5 {{ background:#FFF1F1; color:#B36A6A; border-color:#F6D9D9; }}
    .todo-cd.cd-4 {{ background:#FFEAEA; color:#A85454; border-color:#F3CACA; }}
    .todo-cd.cd-3 {{ background:#FFE0E0; color:#B23B3B; border-color:#F0B6B6; }}
    .todo-cd.cd-2 {{ background:#FFD2D2; color:#A32222; border-color:#EC9E9E; }}
    .todo-cd.cd-1 {{ background:#FFBDBD; color:#8E1212; border-color:#E58585; }}
    .todo-cd.cd-0 {{ background:{DANGER}; color:#FFFFFF; border-color:#C62828;
        animation:todo-lamp-blink 1.8s ease-in-out infinite; }}
    .todo-cd.cd-late {{ background:#7F1414; color:#FFFFFF;
        border-color:#5C0E0E; }}
    /* A bar that empties as the last few days run out. */
    .todo-cdbar {{ height:4px; border-radius:999px; background:#F6D9D9;
        margin:8px 0 4px; overflow:hidden; }}
    .todo-cdbar span {{ display:block; height:4px; border-radius:999px;
        background:linear-gradient(90deg,{DANGER},#C62828);
        transition:width .4s ease; }}

    /* ── Urgent: a red warning light on the card ─────────────────
       Added only when something falls due within the next few days, and it
       keeps pulsing until the job is done or the day passes. The glow sits
       outside the card as a shadow rather than a border, so nothing shifts
       position when it turns on. */
    .todo-card.urgent {{ border-color:{DANGER};
        animation:todo-pulse 1.8s ease-in-out infinite; }}
    @keyframes todo-pulse {{
        0%   {{ box-shadow:0 0 0 0 rgba(248,80,80,.55),
                          0 2px 10px rgba(248,80,80,.20);
               border-color:#F6C9C9; }}
        50%  {{ box-shadow:0 0 26px 6px rgba(248,80,80,.75),
                          0 4px 18px rgba(248,80,80,.45);
               border-color:{DANGER}; }}
        100% {{ box-shadow:0 0 0 0 rgba(248,80,80,.55),
                          0 2px 10px rgba(248,80,80,.20);
               border-color:#F6C9C9; }} }}
    /* The little lamp beside the heading blinks with the same rhythm. */
    .todo-lamp {{ display:inline-block; width:9px; height:9px; margin-right:6px;
        border-radius:50%; background:{DANGER}; vertical-align:middle;
        animation:todo-lamp-blink 1.8s ease-in-out infinite; }}
    @keyframes todo-lamp-blink {{
        0%, 100% {{ opacity:.25; box-shadow:0 0 0 0 rgba(248,80,80,0); }}
        50% {{ opacity:1; box-shadow:0 0 9px 3px rgba(248,80,80,.85); }} }}
    /* Respect the system setting for people who find motion uncomfortable:
       the light stays on rather than blinking. */
    @media (prefers-reduced-motion: reduce) {{
        .todo-card.urgent {{ animation:none; border:2px solid {DANGER};
            box-shadow:0 0 20px 4px rgba(248,80,80,.55); }}
        .todo-lamp {{ animation:none; opacity:1; }}
    }}

    /* ── Styled data tables (Inventory breakdowns) ─────────────── */
    .dtable {{ width:100%; border-collapse:collapse; font-size:.9rem;
        border:1px solid {GRID}; border-radius:12px; overflow:hidden;
        box-shadow:0 1px 6px rgba(6,52,58,.05); margin-bottom:.4rem; }}
    .dtable thead th {{ background:linear-gradient(135deg,{PRIMARY},{TEAL2});
        color:#FFFFFF; text-align:left; padding:10px 12px; font-size:.7rem;
        font-weight:500; letter-spacing:.07em; text-transform:uppercase;
        white-space:nowrap; }}
    .dtable thead th.num, .dtable td.num {{ text-align:right; }}
    .dtable tbody td {{ padding:9px 12px; border-top:1px solid {GRID};
        color:{INK}; }}
    .dtable tbody tr:nth-child(even) {{ background:{LIGHT_BG}; }}
    .dtable tbody tr:hover {{ background:#E2F4F4; }}
    .dtable td.num {{ font-weight:700; font-variant-numeric:tabular-nums;
        white-space:nowrap; }}
    .dtable td.lbl {{ font-weight:600; }}
    .dtable tfoot td {{ padding:10px 12px; background:{LIGHT_BG};
        border-top:2px solid {PRIMARY}; color:{PRIMARY}; font-weight:800; }}
    .share-wrap {{ background:{GRID}; border-radius:999px; height:8px;
        width:100%; min-width:60px; overflow:hidden; }}
    .share-bar {{ background:linear-gradient(90deg,{PRIMARY},{TEAL2}); height:8px;
        border-radius:999px; }}
    .pct {{ font-size:.72rem; color:{MUTED}; font-weight:700; }}

    /* ── AI assistant (Question & Answer) ──────────────────────── */
    .bot-shell {{ border:1px solid {GRID}; border-radius:18px; overflow:hidden;
        background:#FFFFFF; box-shadow:0 4px 18px rgba(6,52,58,.08);
        margin-bottom:.6rem; }}
    .bot-bar {{ display:flex; align-items:center; gap:12px; padding:12px 16px;
        background:linear-gradient(135deg,{PRIMARY},{TEAL2}); }}
    .bot-face {{ width:38px; height:38px; border-radius:50%; flex:0 0 38px;
        background:rgba(255,255,255,.18); border:1px solid rgba(255,255,255,.45);
        display:flex; align-items:center; justify-content:center; font-size:19px; }}
    .bot-face-img {{ width:38px; height:38px; border-radius:50%; flex:0 0 38px;
        object-fit:cover; border:1px solid rgba(255,255,255,.55); }}
    .msg-av-img {{ width:28px; height:28px; border-radius:50%; flex:0 0 28px;
        object-fit:cover; }}
    .bot-id {{ color:#FFFFFF; font-weight:800; font-size:1rem; line-height:1.15; }}
    .bot-sub {{ color:rgba(255,255,255,.85); font-size:.72rem; display:flex;
        align-items:center; gap:6px; }}
    .bot-dot {{ width:7px; height:7px; border-radius:50%; background:#8BF5C4;
        box-shadow:0 0 0 0 rgba(139,245,196,.9); animation:botpulse 2s infinite; }}
    @keyframes botpulse {{
        0% {{ box-shadow:0 0 0 0 rgba(139,245,196,.7); }}
        70% {{ box-shadow:0 0 0 7px rgba(139,245,196,0); }}
        100% {{ box-shadow:0 0 0 0 rgba(139,245,196,0); }} }}
    .bot-body {{ padding:14px 16px; background:
        linear-gradient(180deg,#FBFEFE 0%,#FFFFFF 100%); }}
    .msg-row {{ display:flex; gap:9px; margin-bottom:12px; align-items:flex-start; }}
    .msg-row.me {{ flex-direction:row-reverse; }}
    .msg-av {{ width:28px; height:28px; border-radius:50%; flex:0 0 28px;
        display:flex; align-items:center; justify-content:center; font-size:14px;
        background:linear-gradient(135deg,{PRIMARY},{TEAL2}); color:#FFFFFF; }}
    .msg-av.me {{ background:{LIGHT_BG}; color:{PRIMARY};
        border:1px solid {GRID}; }}
    .bubble {{ max-width:82%; padding:10px 14px; border-radius:14px;
        font-size:.9rem; line-height:1.5; color:{INK};
        background:{LIGHT_BG}; border:1px solid {GRID};
        border-top-left-radius:4px; }}
    .bubble.me {{ background:linear-gradient(135deg,{PRIMARY},{TEAL2});
        color:#FFFFFF; border:none; border-top-left-radius:14px;
        border-top-right-radius:4px; }}
    .bubble b {{ color:{PRIMARY}; }}
    .bubble.me b {{ color:#FFFFFF; }}
    .bubble ul {{ margin:.35rem 0 .1rem 0; padding-left:1.1rem; }}
    .bubble li {{ margin-bottom:.15rem; }}
    .bot-empty {{ text-align:center; color:{MUTED}; font-size:.85rem;
        padding:14px 8px; }}

    /* ── Floating HerdIQ launcher (bottom-right, like modern chat widgets) ── */
    [data-testid="stPopover"] {{ position:fixed; right:26px; bottom:26px;
        z-index:999; }}
    [data-testid="stPopover"] button {{ width:60px; height:60px; padding:0;
        border-radius:50%; border:2px solid #FFFFFF; font-size:0; color:transparent;
        background-image:url("{SP["logo"]}"); background-size:cover;
        background-position:center; background-color:#FFFFFF;
        box-shadow:0 8px 24px rgba(0,104,104,.38); cursor:pointer;
        transition:transform .18s ease, box-shadow .18s ease; }}
    [data-testid="stPopover"] button:hover {{ transform:scale(1.08);
        box-shadow:0 12px 30px rgba(0,104,104,.5); }}
    [data-testid="stPopover"] button::after {{ content:""; position:absolute;
        right:2px; top:2px; width:13px; height:13px; border-radius:50%;
        background:#2ED47A; border:2px solid #FFFFFF; }}
    [data-testid="stPopoverBody"] {{ min-width:380px; max-width:460px;
        max-height:min(72vh, 660px); overflow-y:auto; }}
    /* The launcher is pinned to the bottom-right corner, but the panel it
       opens is drawn in a layer of its own that the browser positions from
       where the button used to sit in the page. That reading can put the
       panel over on the left. Pin the layer to the same corner as the
       button so the panel always opens above it. The :has() keeps this
       clear of the popovers Streamlit uses for its own dropdowns. */
    div[data-baseweb="popover"]:has([data-testid="stPopoverBody"]) {{
        position:fixed !important;
        right:26px !important; left:auto !important;
        bottom:100px !important; top:auto !important;
        transform:none !important; z-index:1000000 !important; }}
    div[data-baseweb="popover"]:has([data-testid="stPopoverBody"]) > div {{
        transform:none !important; }}
    @media (max-width: 640px) {{
        [data-testid="stPopover"] {{ right:14px; bottom:14px; }}
        [data-testid="stPopover"] button {{ width:52px; height:52px; }}
        [data-testid="stPopoverBody"] {{ min-width:280px;
            max-width:calc(100vw - 28px); }}
        div[data-baseweb="popover"]:has([data-testid="stPopoverBody"]) {{
            right:14px !important; bottom:80px !important; }}
    }}
    .stButton>button, .stDownloadButton>button, .stFormSubmitButton>button {{
        background:{PRIMARY}; border:1px solid {PRIMARY}; color:#FFF;
        font-weight:600; border-radius:8px; }}

    /* The way back to the opening screen: an outlined pill that fills in
       under the pointer, so it sits beside the heading without competing
       with the buttons that actually change records. */
    .st-key-back_to_welcome button {{
        background:#FFFFFF !important; color:{PRIMARY} !important;
        border:1.5px solid {GRID} !important; border-radius:999px !important;
        font-weight:600 !important; font-size:.82rem !important;
        letter-spacing:.02em; padding:7px 14px !important; min-height:0;
        box-shadow:0 1px 3px rgba(6,52,58,.06) !important;
        transition:background .15s ease, border-color .15s ease,
                   color .15s ease, transform .12s ease; }}
    .st-key-back_to_welcome button:hover {{
        background:{PRIMARY} !important; border-color:{PRIMARY} !important;
        color:#FFFFFF !important; transform:translateY(-1px);
        box-shadow:0 5px 14px rgba(0,104,104,.24) !important; }}
    .st-key-back_to_welcome button:active {{ transform:translateY(0); }}
    .st-key-back_to_welcome button p {{ font-weight:600 !important;
        margin:0; }}
    .stButton>button:hover, .stDownloadButton>button:hover,
    .stFormSubmitButton>button:hover {{
        background:{TEAL2}; border-color:{TEAL2}; color:#FFF; }}

    /* ── Export / download actions: compact, outlined, secondary ── */
    .stDownloadButton>button {{
        background:#FFFFFF; color:{PRIMARY}; border:1px solid {GRID};
        font-weight:600; font-size:.78rem; letter-spacing:.01em;
        padding:5px 14px; min-height:0; height:auto; border-radius:7px;
        max-width:100%; box-shadow:none; transition:all .15s ease; }}
    .stDownloadButton>button:hover {{
        background:{LIGHT_BG}; color:{PRIMARY}; border-color:{TEAL2};
        box-shadow:0 2px 8px rgba(0,104,104,.14); }}
    .stDownloadButton>button:active, .stDownloadButton>button:focus {{
        background:{LIGHT_BG}; color:{PRIMARY}; border-color:{PRIMARY};
        box-shadow:none; }}
    .stDownloadButton>button p {{ font-size:.78rem; font-weight:600; margin:0; }}
    /* ── Data entry: inputs, selects, tags, uploaders ──────────── */
    [data-testid="stWidgetLabel"] p {{
        font-size:.76rem !important; font-weight:700 !important; color:{MUTED};
        letter-spacing:.02em; margin-bottom:5px; }}

    div[data-baseweb="input"], div[data-baseweb="textarea"],
    div[data-baseweb="select"] > div:first-child {{
        border-radius:10px !important; border:1px solid {GRID} !important;
        background:#FFFFFF !important; box-shadow:0 1px 2px rgba(6,52,58,.04);
        transition:border-color .15s ease, box-shadow .15s ease; }}
    div[data-baseweb="input"]:hover, div[data-baseweb="textarea"]:hover,
    div[data-baseweb="select"] > div:first-child:hover {{
        border-color:{TEAL2} !important; }}
    div[data-baseweb="input"]:focus-within, div[data-baseweb="textarea"]:focus-within,
    div[data-baseweb="select"] > div:first-child:focus-within {{
        border-color:{PRIMARY} !important;
        box-shadow:0 0 0 3px rgba(0,104,104,.13) !important; }}

    [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
    [data-testid="stDateInput"] input, [data-testid="stTextArea"] textarea {{
        font-size:.9rem !important; color:{INK} !important;
        padding-top:9px !important; padding-bottom:9px !important; }}
    input::placeholder, textarea::placeholder {{
        color:#9FB6B6 !important; font-style:italic; }}

    /* Number input +/- steppers */
    [data-testid="stNumberInput"] button {{
        background:{LIGHT_BG} !important; border:none !important;
        color:{PRIMARY} !important; }}
    [data-testid="stNumberInput"] button:hover {{ background:{GRID} !important; }}

    /* Multi-select chips */
    span[data-baseweb="tag"] {{
        background:linear-gradient(135deg,{PRIMARY},{TEAL2}) !important;
        border-radius:7px !important; color:#FFFFFF !important;
        font-weight:600 !important; font-size:.78rem !important; }}
    span[data-baseweb="tag"] svg {{ fill:#FFFFFF !important; }}

    /* Dropdown menus */
    ul[data-baseweb="menu"] {{ border-radius:10px !important;
        border:1px solid {GRID} !important;
        box-shadow:0 8px 24px rgba(6,52,58,.14) !important; }}
    li[data-baseweb="menu-item"]:hover {{ background:{LIGHT_BG} !important; }}

    /* Radio groups read as options, not raw form controls */
    [data-testid="stRadio"] label {{ font-size:.88rem; }}

    /* File uploader */
    [data-testid="stFileUploaderDropzone"] {{
        background:{LIGHT_BG} !important; border:1.5px dashed {GRID} !important;
        border-radius:12px !important; }}
    [data-testid="stFileUploaderDropzone"]:hover {{
        border-color:{TEAL2} !important; background:#EAF7F7 !important; }}

    /* Expanders used for panels and suggestions */
    [data-testid="stExpander"] details {{ border:1px solid {GRID} !important;
        border-radius:12px !important; background:#FFFFFF; }}
    [data-testid="stExpander"] summary:hover {{ color:{PRIMARY} !important; }}

    .chip {{ display:inline-block; padding:3px 12px; border-radius:999px; color:#FFF;
        font-weight:700; font-size:.8rem; }}

    /* ── Backup status card ────────────────────────────────────── */
    .bk-card {{ display:flex; align-items:center; gap:14px; background:#FFFFFF;
        border:1px solid {GRID}; border-left:5px solid {OK_GREEN};
        border-radius:14px; padding:13px 18px; margin-bottom:.7rem;
        box-shadow:0 2px 10px rgba(6,52,58,.06); }}
    .bk-card.warn {{ border-left-color:{WARN}; background:#FFFDF7; }}
    .bk-card.bad {{ border-left-color:{DANGER}; background:#FFF7F7; }}
    .bk-icon {{ width:42px; height:42px; flex:0 0 42px; border-radius:12px;
        background:{LIGHT_BG}; display:flex; align-items:center;
        justify-content:center; font-size:20px; }}
    .bk-card.warn .bk-icon {{ background:#FDF3E2; }}
    .bk-card.bad .bk-icon {{ background:#FDEAEA; }}
    .bk-title {{ font-weight:800; color:{INK}; font-size:.95rem; line-height:1.25; }}
    .bk-sub {{ color:{MUTED}; font-size:.79rem; margin-top:2px;
        word-break:break-all; }}
    .bk-note {{ font-size:.78rem; color:{MUTED}; margin:-2px 0 10px 2px; }}

    /* ══ RESPONSIVE — resized desktop windows ═════════════════════ */
    [data-testid="stHorizontalBlock"] {{ flex-wrap:wrap; }}
    [data-testid="stHorizontalBlock"] > div {{ min-width:0; }}
    [data-testid="stVerticalBlock"] {{ min-width:0; }}
    .stButton>button, .stDownloadButton>button, .stFormSubmitButton>button {{
        white-space:normal; overflow-wrap:anywhere; }}
    [data-testid="stFileUploaderDropzone"] {{ flex-wrap:wrap; gap:.5rem; }}
    [data-testid="stFileUploaderDropzone"] > div {{ min-width:0; }}
    [data-testid="stDataFrame"], [data-testid="stTable"] {{ max-width:100%; }}

    @media (max-width: 1200px) {{
        .block-container {{ padding-left:1rem !important;
            padding-right:1rem !important; }}
        [data-testid="stMetricValue"] {{ font-size:1.5rem !important; }}
    }}

    /* Three- and four-column rows stop being readable below this, so drop
       them to two across before the phone rules take over. */
    @media (max-width: 1000px) {{
        [data-testid="stHorizontalBlock"] > div {{
            flex:1 1 48% !important; min-width:48% !important; }}
        [data-testid="stFileUploaderDropzone"] {{ flex-direction:column;
            align-items:stretch; text-align:center; }}
    }}

    /* ══ RESPONSIVE — tablets ═════════════════════════════════════ */
    @media (max-width: 900px) {{
        .big-title {{ font-size:1.45rem !important; line-height:1.25; }}
        /* Let the tab bar scroll sideways rather than squashing every tab */
        [data-testid="stTabs"] [data-baseweb="tab-list"] {{
            overflow-x:auto; flex-wrap:nowrap; scrollbar-width:thin;
            -webkit-overflow-scrolling:touch; }}
        [data-testid="stTabs"] [data-baseweb="tab"] {{ white-space:nowrap; }}
        .dtable {{ display:block; overflow-x:auto; }}
    }}

    /* ══ RESPONSIVE — phones ══════════════════════════════════════ */
    @media (max-width: 640px) {{
        .block-container {{ padding:0.7rem 0.75rem 5.5rem 0.75rem !important;
            max-width:100% !important; }}
        .big-title {{ font-size:1.12rem !important; line-height:1.2; }}
        .sub {{ font-size:.78rem; }}
        .section {{ font-size:.95rem; }}

        /* Stacked on a phone, so the card sizes to its own contents. */
        .todo-block {{ min-height:0; }}

        /* Stack side-by-side rows instead of crushing them */
        [data-testid="stHorizontalBlock"] {{ flex-direction:column !important;
            gap:.4rem !important; }}
        [data-testid="stHorizontalBlock"] > div {{ width:100% !important;
            flex:1 1 100% !important; min-width:100% !important; }}

        /* Figures stay readable */
        [data-testid="stMetricValue"] {{ font-size:1.3rem !important; }}
        [data-testid="stMetricLabel"] {{ font-size:.72rem !important; }}

        /* Buttons become full-width and easy to tap */
        .stButton>button, .stDownloadButton>button,
        .stFormSubmitButton>button {{ width:100% !important;
            min-height:42px !important; }}
        [data-testid="stPopover"] button {{ width:52px !important;
            height:52px !important; min-height:52px !important; }}

        /* 16px stops phones zooming in when a field is tapped */
        [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
        [data-testid="stDateInput"] input, [data-testid="stTextArea"] textarea {{
            font-size:16px !important; }}

        /* Wide tables scroll rather than overflow the screen */
        .dtable {{ display:block; overflow-x:auto; white-space:nowrap;
            font-size:.82rem; }}
        [data-testid="stDataFrame"] {{ font-size:.8rem; }}

        .bubble {{ max-width:90%; font-size:.86rem; }}
        .todo-item {{ white-space:normal; }}
        .bk-card {{ padding:11px 13px; }}
        .bk-sub {{ font-size:.73rem; }}
    }}

    /* ── Streamlit data tables (herd list, logs) ───────────────── */
    [data-testid="stDataFrame"] {{ border:1px solid {GRID}; border-radius:12px;
        overflow:hidden; box-shadow:0 1px 6px rgba(6,52,58,.05); }}
    [data-testid="stDataFrame"] thead tr th {{
        background:{LIGHT_BG} !important; color:{PRIMARY} !important;
        font-weight:500 !important; font-size:.74rem !important;
        letter-spacing:.05em; text-transform:uppercase;
        border-bottom:2px solid {GRID} !important; }}
    [data-testid="stDataFrame"] tbody tr td {{ font-size:.86rem; color:{INK}; }}
    [data-testid="stDataFrame"] tbody tr:hover td {{ background:{LIGHT_BG}; }}

    /* ── One accent everywhere ─────────────────────────────────────
       Streamlit paints its own controls with the accent colour it was
       given, and its default is red. The theme file the generator writes
       sets that properly; these rules make the same thing true even when
       the app is run from another folder, so checkboxes, radios, sliders,
       toggles, progress bars and links all carry the teal of the
       LIVESTOCK wordmark. */
    input[type="checkbox"], input[type="radio"], input[type="range"] {{
        accent-color:{PRIMARY} !important; }}
    [data-baseweb="checkbox"] span[aria-checked="true"],
    [data-baseweb="checkbox"] span[data-checked="true"] {{
        background-color:{PRIMARY} !important;
        border-color:{PRIMARY} !important; }}
    [data-baseweb="radio"] div[aria-checked="true"] > div,
    [data-baseweb="radio"] div[data-checked="true"] > div {{
        background-color:{PRIMARY} !important;
        border-color:{PRIMARY} !important; }}
    [data-testid="stSlider"] [role="slider"] {{
        background-color:{PRIMARY} !important; }}
    [data-testid="stSlider"] [data-baseweb="slider"] div[style*="rgb(255, 75, 75)"] {{
        background:{PRIMARY} !important; }}
    [data-testid="stProgress"] div[role="progressbar"] > div,
    [data-testid="stProgress"] > div > div > div > div {{
        background-color:{PRIMARY} !important;
        background-image:linear-gradient(90deg,{PRIMARY},{TEAL2}) !important; }}
    .stSpinner > div {{ border-top-color:{PRIMARY} !important; }}
    a, a:visited {{ color:{PRIMARY}; }}
    a:hover {{ color:{TEAL2}; }}
    ::selection {{ background:rgba(0,104,104,.16); }}

    /* ── Professional tab navigation ───────────────────────────── */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {{
        position:relative;
        gap:3px; background:{LIGHT_BG}; border:1px solid {GRID};
        padding:5px; border-radius:14px; margin-bottom:1.5rem;
        box-shadow:0 1px 2px rgba(6,52,58,.05);
        flex-wrap:wrap; }}
    /* A rule straight across, separating the tabs from the screen below. */
    [data-testid="stTabs"] [data-baseweb="tab-list"]::after {{
        content:""; position:absolute; left:0; right:0; bottom:-.75rem;
        height:2px; background:#000000; border-radius:1px; }}
    /* Only the main strip gets it — the tabs nested inside a screen do not. */
    [data-testid="stTabs"] [data-testid="stTabs"] [data-baseweb="tab-list"] {{
        margin-bottom:1rem; }}
    [data-testid="stTabs"] [data-testid="stTabs"] [data-baseweb="tab-list"]::after {{
        display:none; }}
    /* remove the default red underline / border */
    [data-testid="stTabs"] [data-baseweb="tab-highlight"],
    [data-testid="stTabs"] [data-baseweb="tab-border"] {{ display:none !important; }}
    [data-testid="stTabs"] [data-baseweb="tab"] {{
        height:auto; padding:7px 10px; border-radius:9px; white-space:nowrap;
        background:transparent; border:none; transition:all .15s ease; }}
    [data-testid="stTabs"] [data-baseweb="tab"] p {{
        font-size:.82rem; font-weight:600; color:{MUTED}; margin:0;
        letter-spacing:-.01em; }}
    [data-testid="stTabs"] [data-baseweb="tab"]:hover {{ background:#FFFFFF; }}
    [data-testid="stTabs"] [data-baseweb="tab"]:hover p {{ color:{PRIMARY}; }}
    [data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"] {{
        background:linear-gradient(135deg,{PRIMARY},{TEAL2});
        box-shadow:0 3px 8px rgba(0,104,104,.28); }}
    [data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"] p {{
        color:#FFFFFF !important; }}
    @media (max-width: 1100px) {{
        [data-testid="stTabs"] [data-baseweb="tab"] {{ padding:6px 9px; }}
        [data-testid="stTabs"] [data-baseweb="tab"] p {{ font-size:.76rem; }}
    }}
    @media (max-width: 640px) {{
        [data-testid="stTabs"] [data-baseweb="tab"] {{ padding:6px 10px; }}
        [data-testid="stTabs"] [data-baseweb="tab"] p {{ font-size:.8rem; }}
    }}
</style>
"""), unsafe_allow_html=True)


# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────
@st.cache_resource
def _open_conn(db_path):
    """Cached per file, so the cattle and goat herds each keep their own
    connection and switching between them never reaches the wrong database."""
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    # Production hardening: WAL lets readers and a writer work concurrently,
    # and busy_timeout waits instead of raising "database is locked".
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass

    # On Windows, a WAL/-shm sidecar left locked after an unclean exit can make
    # the *next* launch fail with "database is locked". Checkpoint and close the
    # connection when the process ends so the next run starts clean.
    import atexit

    def _close_clean(c=conn):
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass

    atexit.register(_close_clean)
    return conn


def get_conn():
    """The connection for the herd currently open."""
    return _open_conn(DB_PATH)


def init_db():
    c = get_conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS cattle (
            tag TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            breed TEXT DEFAULT '',
            sex TEXT DEFAULT '',
            dob TEXT DEFAULT '',
            colour TEXT DEFAULT '',
            weight REAL,
            category TEXT DEFAULT '',
            status TEXT DEFAULT 'Active',
            date_acquired TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            brand_number TEXT DEFAULT '',
            brand_type TEXT DEFAULT '',
            mother_tag TEXT DEFAULT '',
            origin_location TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    # Migrate older databases that predate the brand / parentage columns.
    _cols = {r[1] for r in c.execute("PRAGMA table_info(cattle)").fetchall()}
    if "brand_number" not in _cols:
        c.execute("ALTER TABLE cattle ADD COLUMN brand_number TEXT DEFAULT ''")
    if "brand_type" not in _cols:
        c.execute("ALTER TABLE cattle ADD COLUMN brand_type TEXT DEFAULT ''")
    if "mother_tag" not in _cols:
        c.execute("ALTER TABLE cattle ADD COLUMN mother_tag TEXT DEFAULT ''")
    if "origin_location" not in _cols:
        c.execute("ALTER TABLE cattle ADD COLUMN origin_location TEXT DEFAULT ''")
    c.execute("""
        CREATE TABLE IF NOT EXISTS cattle_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT,
            filename TEXT,
            image BLOB,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            type TEXT,
            tag TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT,
            date TEXT,
            tag TEXT,
            cow_name TEXT,
            buyer TEXT,
            buyer_contact TEXT,
            weight REAL,
            price REAL,
            payment_method TEXT,
            notes TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_no TEXT,
            date TEXT,
            tag TEXT,
            cow_name TEXT,
            breed TEXT,
            sex TEXT,
            seller TEXT,
            seller_contact TEXT,
            weight REAL,
            price REAL,
            payment_method TEXT,
            notes TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS losses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT,
            cow_name TEXT,
            type TEXT,
            date TEXT,
            cause TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            category TEXT,
            description TEXT,
            amount REAL,
            payment_method TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            activity TEXT,
            details TEXT,
            scope TEXT,
            tags TEXT,
            tag_count INTEGER,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            task TEXT,
            details TEXT,
            priority TEXT DEFAULT 'Normal',
            done INTEGER DEFAULT 0,
            tag TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS farm_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            farm_name TEXT DEFAULT '',
            farmer_name TEXT DEFAULT '',
            location TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            updated_at TEXT
        )
    """)
    # A reminder can belong to one cow. Older databases have no such column,
    # so their reminders survived the cow being deleted.
    _rem_cols = {r[1] for r in c.execute("PRAGMA table_info(reminders)").fetchall()}
    if _rem_cols and "tag" not in _rem_cols:
        c.execute("ALTER TABLE reminders ADD COLUMN tag TEXT DEFAULT ''")

    c.execute("INSERT OR IGNORE INTO farm_profile(id, farm_name) VALUES(1, '')")
    _fp_cols = {r[1] for r in c.execute("PRAGMA table_info(farm_profile)").fetchall()}
    if "logo" not in _fp_cols:
        c.execute("ALTER TABLE farm_profile ADD COLUMN logo BLOB")
    if "logo_name" not in _fp_cols:
        c.execute("ALTER TABLE farm_profile ADD COLUMN logo_name TEXT DEFAULT ''")
    c.execute("""
        CREATE TABLE IF NOT EXISTS breedings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cow_tag TEXT,
            bull_tag TEXT,
            service_date TEXT,
            method TEXT,
            status TEXT DEFAULT 'Served',
            check_date TEXT,
            due_date TEXT,
            calving_date TEXT,
            calf_tag TEXT,
            notes TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT,
            date TEXT,
            weight REAL,
            note TEXT,
            created_at TEXT
        )
    """)
    # Indexes keep counts, groupings and lookups fast on very large herds.
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_cattle_status ON cattle(status)",
        "CREATE INDEX IF NOT EXISTS idx_cattle_sex ON cattle(sex)",
        "CREATE INDEX IF NOT EXISTS idx_cattle_status_tag ON cattle(status, tag)",
        "CREATE INDEX IF NOT EXISTS idx_cattle_mother ON cattle(mother_tag)",
        "CREATE INDEX IF NOT EXISTS idx_images_tag ON cattle_images(tag)",
        "CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)",
        "CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date)",
        "CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date)",
        "CREATE INDEX IF NOT EXISTS idx_reminders_date ON reminders(date, done)",
        "CREATE INDEX IF NOT EXISTS idx_breed_cow ON breedings(cow_tag)",
        "CREATE INDEX IF NOT EXISTS idx_weights_tag ON weights(tag, date)",
        "CREATE INDEX IF NOT EXISTS idx_breed_status ON breedings(status, due_date)",
    ):
        c.execute(stmt)
    c.commit()


MAX_IMAGES = 1  # one passport-style photo per cow

# Billing settings — override with environment variables if you like.
# ── Currency ──────────────────────────────────
# Botswana first, then the neighbours a farmer here actually trades with,
# then the majors. Each entry is (code, symbol, name).
CURRENCIES = [
    ("BWP", "P",   "Botswana Pula"),
    ("ZAR", "R",   "South African Rand"),
    ("USD", "$",   "US Dollar"),
    ("GBP", "\u00a3",  "British Pound"),
    ("EUR", "\u20ac",  "Euro"),
    ("ZMW", "K",   "Zambian Kwacha"),
    ("NAD", "N$",  "Namibian Dollar"),
    ("SZL", "E",   "Swazi Lilangeni"),
    ("LSL", "L",   "Lesotho Loti"),
    ("ZWL", "Z$",  "Zimbabwean Dollar"),
    ("MZN", "MT",  "Mozambican Metical"),
    ("MWK", "MK",  "Malawian Kwacha"),
    ("AOA", "Kz",  "Angolan Kwanza"),
    ("KES", "KSh", "Kenyan Shilling"),
    ("TZS", "TSh", "Tanzanian Shilling"),
    ("UGX", "USh", "Ugandan Shilling"),
    ("NGN", "\u20a6",  "Nigerian Naira"),
    ("INR", "\u20b9",  "Indian Rupee"),
    ("CNY", "\u00a5",  "Chinese Yuan"),
    ("JPY", "\u00a5",  "Japanese Yen"),
    ("AUD", "A$",  "Australian Dollar"),
    ("CAD", "C$",  "Canadian Dollar"),
]
CURRENCY_BY_CODE = {code: (sym, name) for code, sym, name in CURRENCIES}
DEFAULT_CURRENCY = "BWP"


def _pdf_safe(text):
    """Can reportlab's standard fonts draw this?

    They use WinAnsi encoding, which has no naira or rupee sign. Printing one
    would come out as a black box on an invoice, so those currencies show
    their three-letter code instead.
    """
    try:
        text.encode("cp1252")
        return True
    except (UnicodeEncodeError, AttributeError):
        return False


# money() is called on nearly every screen, often inside loops, so the chosen
# currency is read from the database once and then held for the rest of the
# run. Streamlit re-executes this module on every rerun, which clears it.
_CURRENCY_CACHE = {}


def currency_code():
    """The code chosen in Billing, falling back to the Pula."""
    if "code" in _CURRENCY_CACHE:
        return _CURRENCY_CACHE["code"]
    try:
        code = get_meta("currency", "")
    except Exception:
        code = ""                      # asked before the database exists
        return DEFAULT_CURRENCY        # and do not cache a guess
    if code not in CURRENCY_BY_CODE:
        code = os.environ.get("CATTLE_CURRENCY_CODE", DEFAULT_CURRENCY)
    if code not in CURRENCY_BY_CODE:
        code = DEFAULT_CURRENCY
    _CURRENCY_CACHE["code"] = code
    return code


def set_currency(code):
    """Store the chosen currency and drop the cached value."""
    if code in CURRENCY_BY_CODE:
        set_meta("currency", code)
        _CURRENCY_CACHE.pop("code", None)


def currency_symbol(code=None):
    """What to put in front of an amount. Falls back to the code when the
    symbol cannot be drawn in a PDF."""
    code = code or currency_code()
    sym = CURRENCY_BY_CODE.get(code, ("P",))[0]
    return sym if _pdf_safe(sym) else code


def currency_name(code=None):
    code = code or currency_code()
    return CURRENCY_BY_CODE.get(code, ("", "Botswana Pula"))[1]


# Kept for anything that still reads the old constant.
CURRENCY = os.environ.get("CATTLE_CURRENCY", "P")




FARM_NAME = os.environ.get("CATTLE_FARM_NAME", "My Cattle Farm")
PAYMENT_METHODS = ["Cash", "Bank transfer", "Mobile money", "Cheque", "Other"]
EXPENSE_CATEGORIES = ["Feed", "Veterinary / Medicine", "Labour", "Transport",
                      "Equipment", "Water / Utilities", "Other"]

# Calendar activity types the user can log by hand.
EVENT_TYPES = ["Note", "Calf born", "Cow sold", "Death", "Missing", "Purchase",
               "Expense", "Activity", "Other"]
EVENT_ICON = {"Calf born": "🐄", "Cow sold": "💰", "Death": "🕊️", "Missing": "❓",
              "Purchase": "📥", "Expense": "💸", "Activity": "🧰", "Note": "📝",
              "Other": "•", "Acquired": "📥"}

# Common farm activities offered as quick picks (free text is also allowed).
# Branding is a cattle job, so it is only offered to the cattle herd.
ACTIVITY_TYPES = ["Vaccination", "Deworming", "Dipping / Spraying"] \
                 + (["Branding"] if SP["branded"] else []) \
                 + ["Ear tagging", "Weighing", "Pregnancy check", "Dehorning",
                  "Castration", "Feeding supplement", "Treatment", "Movement / Grazing",
                  "Inspection", "Other"]

# ── Breeding ──────────────────────────────────
# A goat carries for about five months, a cow for a little over nine.
GESTATION_DAYS = SP["gestation_days"]
BREEDING_METHODS = ["Natural service", "Artificial insemination (AI)",
                    "Embryo transfer"]
BREEDING_STATUSES = ["Served", "Pregnant", "Not pregnant", "Calved", "Lost"]


def add_cow(rec):
    """Insert one cow. Returns True on success, False if the tag already exists."""
    c = get_conn()
    exists = c.execute("SELECT 1 FROM cattle WHERE tag=?", (rec["tag"],)).fetchone()
    if exists:
        return False
    c.execute(
        "INSERT INTO cattle(tag, name, breed, sex, dob, colour, weight, category, "
        "status, date_acquired, notes, brand_number, brand_type, mother_tag, "
        "origin_location, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["tag"], rec["name"], rec["breed"], rec["sex"], rec["dob"], rec["colour"],
         rec["weight"], rec["category"], rec["status"], rec["date_acquired"],
         rec["notes"], rec.get("brand_number", ""), rec.get("brand_type", ""),
         rec.get("mother_tag", ""), rec.get("origin_location", ""),
         now_local().isoformat(timespec="seconds")))
    c.commit()
    return True


def update_cow(tag, rec):
    c = get_conn()
    # Keep existing brand / parentage / origin details if not passed in.
    cur = c.execute("SELECT brand_number, brand_type, mother_tag, origin_location "
                    "FROM cattle WHERE tag=?", (tag,)).fetchone()
    bn = rec.get("brand_number", cur["brand_number"] if cur else "")
    bt = rec.get("brand_type", cur["brand_type"] if cur else "")
    mt = rec.get("mother_tag", cur["mother_tag"] if cur else "")
    ol = rec.get("origin_location", cur["origin_location"] if cur else "")
    c.execute(
        "UPDATE cattle SET name=?, breed=?, sex=?, dob=?, colour=?, weight=?, "
        "category=?, status=?, date_acquired=?, notes=?, brand_number=?, "
        "brand_type=?, mother_tag=?, origin_location=? WHERE tag=?",
        (rec["name"], rec["breed"], rec["sex"], rec["dob"], rec["colour"],
         rec["weight"], rec["category"], rec["status"], rec["date_acquired"],
         rec["notes"], bn, bt, mt, ol, tag))
    c.commit()


def delete_cow(tag):
    """Remove a cow and the records that belong only to it.

    Sales and purchases are deliberately kept — they are financial history that
    happened, and the invoice should still show what was traded.
    """
    c = get_conn()
    c.execute("DELETE FROM cattle WHERE tag=?", (tag,))
    c.execute("DELETE FROM cattle_images WHERE tag=?", (tag,))
    c.execute("DELETE FROM weights WHERE tag=?", (tag,))
    c.execute("DELETE FROM breedings WHERE cow_tag=?", (tag,))
    c.execute("DELETE FROM events WHERE tag=?", (tag,))
    c.execute("DELETE FROM losses WHERE tag=?", (tag,))
    c.execute("DELETE FROM reminders WHERE tag=?", (tag,))
    # Reminders written before reminders had a tag column name the cow only in
    # their text, so they would otherwise sit in "Things to do" for an animal
    # that no longer exists. Match the tag as a whole word: deleting BW-01
    # must not take BW-011 with it.
    for row in c.execute("SELECT id, task, details FROM reminders "
                         "WHERE (tag IS NULL OR tag='') "
                         "AND (task LIKE ? OR details LIKE ?)",
                         ("%" + tag + "%", "%" + tag + "%")).fetchall():
        haystack = (row[1] or "") + " " + (row[2] or "")
        if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(tag)
                     + r"(?![A-Za-z0-9_-])", haystack):
            c.execute("DELETE FROM reminders WHERE id=?", (row[0],))
    # Any calf that pointed at this cow as its mother is now unlinked.
    c.execute("UPDATE cattle SET mother_tag='' WHERE mother_tag=?", (tag,))
    c.commit()


# ── removing several cattle at once ───────────
# Deleting is permanent and there is no undo, so everything here is built to
# show the farmer exactly what disappears before anything is touched.
def tags_for_removal(term="", status="", category=""):
    """Tags matching the filters, oldest tag first. Used to fill the picker."""
    sql = "SELECT tag, name, category, status, sex FROM cattle WHERE 1=1"
    params = []
    if term:
        sql += " AND (tag LIKE ? OR name LIKE ?)"
        params += ["%" + term + "%", "%" + term + "%"]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY tag"
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def removal_impact(tags):
    """What would be lost if these tags were removed.

    Sales and purchases are counted but never deleted — they are financial
    history that actually happened. The farmer is told they will stay.
    """
    impact = {"cattle": 0, "weights": 0, "breedings": 0, "events": 0,
              "losses": 0, "images": 0, "reminders": 0, "calves_unlinked": 0,
              "sales_kept": 0, "purchases_kept": 0}
    tags = [t for t in dict.fromkeys(tags) if t]
    if not tags:
        return impact
    c = get_conn()
    counted = [
        ("cattle", "SELECT COUNT(*) FROM cattle WHERE tag IN (%s)"),
        ("weights", "SELECT COUNT(*) FROM weights WHERE tag IN (%s)"),
        ("breedings", "SELECT COUNT(*) FROM breedings WHERE cow_tag IN (%s)"),
        ("events", "SELECT COUNT(*) FROM events WHERE tag IN (%s)"),
        ("losses", "SELECT COUNT(*) FROM losses WHERE tag IN (%s)"),
        ("images", "SELECT COUNT(*) FROM cattle_images WHERE tag IN (%s)"),
        ("reminders", "SELECT COUNT(*) FROM reminders WHERE tag IN (%s)"),
        ("calves_unlinked",
         "SELECT COUNT(*) FROM cattle WHERE mother_tag IN (%s) "
         "AND tag NOT IN (%s)"),
        ("sales_kept", "SELECT COUNT(*) FROM sales WHERE tag IN (%s)"),
        ("purchases_kept", "SELECT COUNT(*) FROM purchases WHERE tag IN (%s)"),
    ]
    for i in range(0, len(tags), 400):          # stay under SQLite's limit
        chunk = tags[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for key, template in counted:
            if key == "calves_unlinked":
                sql = template % (marks, marks)
                args = chunk + chunk
            else:
                sql = template % marks
                args = chunk
            try:
                impact[key] += c.execute(sql, args).fetchone()[0]
            except sqlite3.Error:
                pass                            # a table this build lacks
    return impact


def delete_cattle(tags):
    """Remove several cattle in one transaction. Returns how many went."""
    tags = [t for t in dict.fromkeys(tags) if t]
    removed = 0
    for tag in tags:
        if get_cow(tag):
            delete_cow(tag)
            removed += 1
    return removed


# ── bulk import (Excel / CSV) helpers ─────────
# Spreadsheet headings are matched loosely: everything except letters and
# digits is stripped, so "Weight (kg)", "weight_kg" and "WEIGHT KG" all land
# on the same field.
IMPORT_HEADER_ALIASES = {
    "tag": "tag", "tagno": "tag", "tagnumber": "tag", "tagid": "tag",
    "eartag": "tag", "eartagno": "tag", "eartagnumber": "tag",
    "animaltag": "tag", "cowtag": "tag", "identifier": "tag",
    "name": "name", "cowname": "name", "animalname": "name",
    "breed": "breed", "breedtype": "breed",
    "sex": "sex", "gender": "sex",
    "dob": "dob", "dateofbirth": "dob", "birthdate": "dob",
    "datedborn": "dob", "born": "dob", "birthday": "dob", "birth": "dob",
    "age": "age", "ageyears": "age", "approximateage": "age",
    "approxage": "age", "ageinyears": "age", "estimatedage": "age",
    "colour": "colour", "color": "colour", "coat": "colour",
    "coatcolour": "colour", "coatcolor": "colour",
    "weight": "weight", "weightkg": "weight", "liveweight": "weight",
    "liveweightkg": "weight", "mass": "weight", "masskg": "weight",
    "category": "category", "class": "category", "classification": "category",
    "group": "category", "type": "category",
    "status": "status", "state": "status", "herdstatus": "status",
    "dateacquired": "date_acquired", "acquired": "date_acquired",
    "acquisitiondate": "date_acquired", "dateacquiredon": "date_acquired",
    "datejoined": "date_acquired", "joined": "date_acquired",
    "arrivaldate": "date_acquired", "datein": "date_acquired",
    "brandnumber": "brand_number", "brand": "brand_number",
    "brandno": "brand_number", "brandmark": "brand_number",
    "mothertag": "mother_tag", "mother": "mother_tag", "dam": "mother_tag",
    "damtag": "mother_tag", "motherstag": "mother_tag",
    "originlocation": "origin_location", "origin": "origin_location",
    "location": "origin_location", "comingfrom": "origin_location",
    "source": "origin_location", "sourcelocation": "origin_location",
    "cattlepost": "origin_location", "kraal": "origin_location",
    "notes": "notes", "note": "notes", "comment": "notes", "comments": "notes",
    "remark": "notes", "remarks": "notes", "description": "notes",
}

# Category words that give the sex away, for both herds — a sheet written
# for one herd still imports cleanly into the other.
SEX_SHORTHAND = {"f": "Female", "m": "Male", "fem": "Female",
                 "cow": "Female", "heifer": "Female",
                 "bull": "Male", "steer": "Male", "ox": "Male",
                 "doe": "Female", "doeling": "Female", "nanny": "Female",
                 "buck": "Male", "wether": "Male", "billy": "Male"}

# Date formats we try, day-first ahead of month-first (Botswana convention).
IMPORT_DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d",
    "%d/%m/%y", "%d-%m-%y", "%m/%d/%Y", "%Y%m%d",
    "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d-%b-%y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S",
]

MAX_IMPORT_ROWS = 5000


def _norm_header(value):
    """Squash a spreadsheet heading down to a comparable key."""
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _cell_text(value):
    """A trimmed string for any cell; blanks and NaN come back empty."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in ("nan", "nat", "none", "null", "n/a", "na", "-", "--"):
        return ""
    return text


def _sniff_delimiter(text):
    """Pick the separator from the heading row.

    We do not let pandas sniff freely: on a single-column file it happily
    decides some letter is the delimiter and splits "Amount" into "Amo" and
    "nt". Counting known separators in the heading is duller and correct.
    """
    head = ""
    for line in text.splitlines():
        if line.strip():
            head = line
            break
    best, best_count = ",", 0
    for candidate in (",", ";", "\t", "|"):
        count = head.count(candidate)
        if count > best_count:
            best, best_count = candidate, count
    return best


def read_tabular_upload(upload):
    """Read an uploaded CSV or Excel file into a DataFrame of raw cell values."""
    fname = (getattr(upload, "name", "") or "").lower()
    try:
        upload.seek(0)
    except Exception:
        pass
    if fname.endswith((".xlsx", ".xlsm", ".xltx")):
        frame = pd.read_excel(upload, dtype=object)
    else:
        raw = upload.read()
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
        else:
            text = raw
        frame = pd.read_csv(io.StringIO(text), dtype=object,
                            sep=_sniff_delimiter(text),
                            engine="python", skip_blank_lines=True)
    frame.columns = [str(c) for c in frame.columns]
    return frame


def map_import_columns(frame):
    """Match sheet headings onto our fields. Returns (mapping, ignored columns)."""
    mapping, ignored = {}, []
    for col in frame.columns:
        field = IMPORT_HEADER_ALIASES.get(_norm_header(col))
        if field and field not in mapping:
            mapping[field] = col
        else:
            ignored.append(col)
    return mapping, ignored


def existing_tags(tags):
    """Which of these tags are already in the herd? Queried in chunks."""
    wanted = [t for t in dict.fromkeys(tags) if t]
    found = set()
    if not wanted:
        return found
    c = get_conn()
    for i in range(0, len(wanted), 400):
        chunk = wanted[i:i + 400]
        marks = ",".join("?" * len(chunk))
        rows = c.execute(f"SELECT tag FROM cattle WHERE tag IN ({marks})",
                         chunk).fetchall()
        found.update(r[0] for r in rows)
    return found


def _match_choice(value, choices, shorthand=None):
    """Fit a cell onto one of our allowed values. Returns (value, recognised)."""
    text = _cell_text(value)
    if not text:
        return "", True
    low = text.lower()
    for choice in choices:
        if low == choice.lower():
            return choice, True
    if shorthand and low in shorthand:
        return shorthand[low], True
    if len(low) >= 3:
        for choice in choices:
            if choice.lower().startswith(low):
                return choice, True
    return text, False


def _coerce_date_cell(value):
    """Turn a date cell into an ISO string. Returns (value, readable, note)."""
    if value is None:
        return "", True, ""
    if isinstance(value, datetime):
        return value.date().isoformat(), True, ""
    if isinstance(value, date):
        return value.isoformat(), True, ""
    text = _cell_text(value)
    if not text:
        return "", True, ""
    # A bare number is either a year on its own or an Excel serial day number.
    try:
        serial = float(text)
    except ValueError:
        serial = None
    if serial is not None:
        whole = int(serial)
        if 1900 <= whole <= 2100 and whole == serial:
            return (date(whole, 1, 1).isoformat(), True,
                    "only a year was given, read as 1 January " + str(whole))
        if 1 <= serial <= 80000:
            return (date(1899, 12, 30) + timedelta(days=whole)).isoformat(), True, ""
        return text, False, ""
    for fmt in IMPORT_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat(), True, ""
        except ValueError:
            continue
    return text, False, ""


def _coerce_weight_cell(value):
    """Read a weight cell, tolerating a kg suffix. Returns (value, note)."""
    text = _cell_text(value)
    if not text:
        return None, ""
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    try:
        number = float(cleaned)
    except ValueError:
        return None, "weight is not a number, left blank"
    if number <= 0:
        return None, ""
    if number > 2000:
        return None, "weight over 2,000 kg looks wrong, left blank"
    return number, ""


def build_import_rows(frame, mapping, defaults):
    """Turn an uploaded sheet into candidate records, each with a verdict.

    Verdicts are: new (ready to load), duplicate (tag already in the herd),
    skip (cannot be loaded at all) and blank (an empty spreadsheet row).
    """
    tag_col = mapping.get("tag")
    raw_tags = ([_cell_text(v) for v in frame[tag_col].tolist()]
                if tag_col is not None else [])
    already = existing_tags(raw_tags)

    seen_tags, rows = set(), []
    for offset, (_, raw) in enumerate(frame.iterrows()):
        vals = {field: raw[col] for field, col in mapping.items()}
        if all(not _cell_text(v) for v in vals.values()):
            rows.append({"line": offset + 2, "verdict": "blank",
                         "issues": [], "rec": None})
            continue

        issues = []
        tag = _cell_text(vals.get("tag"))

        sex, sex_ok = _match_choice(vals.get("sex"), SEXES, SEX_SHORTHAND)
        if not sex_ok:
            issues.append("sex not recognised, left blank")
            sex = ""

        category, cat_ok = _match_choice(vals.get("category"), CATEGORIES)
        if not cat_ok:
            issues.append("category not recognised, left blank")
            category = ""

        status, status_ok = _match_choice(vals.get("status"), STATUSES)
        if not status_ok:
            issues.append("status not recognised, set to " + defaults["status"])
            status = ""
        if not status:
            status = defaults["status"]

        dob, dob_ok, dob_note = _coerce_date_cell(vals.get("dob"))
        if not dob_ok:
            issues.append("date of birth unreadable, left blank")
            dob = ""
        elif dob_note:
            issues.append("date of birth: " + dob_note)

        # An age in years stands in for an unknown birth date.
        if not dob:
            age_text = _cell_text(vals.get("age"))
            if age_text:
                try:
                    years = int(float(age_text))
                except ValueError:
                    years = 0
                    issues.append("age is not a number, ignored")
                if years > 0:
                    dob = est_dob_from_age(years)
                    if dob:
                        issues.append("no birth date, estimated from an age of "
                                      + str(years) + " year(s)")

        acquired, acq_ok, acq_note = _coerce_date_cell(vals.get("date_acquired"))
        if not acq_ok:
            issues.append("date acquired unreadable, default used")
            acquired = ""
        elif acq_note:
            issues.append("date acquired: " + acq_note)
        if not acquired:
            acquired = defaults["date_acquired"]

        weight, weight_note = _coerce_weight_cell(vals.get("weight"))
        if weight_note:
            issues.append(weight_note)

        brand = _cell_text(vals.get("brand_number"))
        if brand and not brand.isalnum():
            issues.append("brand number must be letters and numbers only, "
                          "left blank")
            brand = ""

        rec = {
            "tag": tag,
            "name": _cell_text(vals.get("name")),
            "breed": _cell_text(vals.get("breed")),
            "sex": sex,
            "dob": dob,
            "colour": _cell_text(vals.get("colour")),
            "weight": weight,
            "category": category,
            "status": status,
            "date_acquired": acquired,
            "notes": _cell_text(vals.get("notes")),
            "brand_number": brand,
            "brand_type": "",
            "mother_tag": _cell_text(vals.get("mother_tag")),
            "origin_location": _cell_text(vals.get("origin_location")),
        }

        if not tag:
            verdict = "skip"
            issues.append("no tag, and a tag is required")
        elif tag in seen_tags:
            verdict = "skip"
            issues.append("this tag appears more than once in the file")
        elif tag in already:
            verdict = "duplicate"
            issues.append(T("already in the herd"))
        else:
            verdict = "new"
        if tag:
            seen_tags.add(tag)

        if rec["mother_tag"] and rec["mother_tag"] == tag:
            issues.append(T("a cow cannot be its own mother, mother tag cleared"))
            rec["mother_tag"] = ""

        rows.append({"line": offset + 2, "verdict": verdict,
                     "issues": issues, "rec": rec})

    # A mother tag that is neither in the herd nor anywhere in this file is
    # worth pointing out, but it does not stop the row loading.
    file_tags = {r["rec"]["tag"] for r in rows if r["rec"] and r["rec"]["tag"]}
    known_mothers = existing_tags([r["rec"]["mother_tag"] for r in rows
                                   if r["rec"] and r["rec"]["mother_tag"]])
    for r in rows:
        mother = r["rec"]["mother_tag"] if r["rec"] else ""
        if mother and mother not in known_mothers and mother not in file_tags:
            r["issues"].append("mother tag " + mother + " is not in the herd yet")
    return rows


def import_template_frame():
    """A short worked example users can fill in and upload."""
    today = date.today().isoformat()
    _b = SP["breeds"]
    _w = SP["example_weights"]
    frame = pd.DataFrame([
        {"Tag": "BW-0421", "Name": "Daisy", "Breed": _b[0], "Sex": "Female",
         "Date of birth": "2022-03-14", "Age (years)": "",
         "Colour": "Brown & white", "Weight (kg)": _w[0],
         "Category": SP["maiden_cat"],
         "Status": "Active", "Date acquired": today,
         "Mother tag": "", "Origin location": "Own farm",
         "Notes": "Dam BW-0107"},
        {"Tag": "BW-0422", "Name": "", "Breed": _b[1], "Sex": "Male",
         "Date of birth": "2023-11-02", "Age (years)": "", "Colour": "Black",
         "Weight (kg)": _w[1], "Category": SP["young_cat"], "Status": "Active",
         "Date acquired": today, "Mother tag": "BW-0421",
         "Origin location": "Own farm", "Notes": ""},
        {"Tag": "BW-0423", "Name": "Mmele", "Breed": _b[2], "Sex": "Female",
         "Date of birth": "", "Age (years)": 6, "Colour": "Red",
         "Weight (kg)": _w[2], "Category": SP["adult_f_cat"],
         "Status": "Active",
         "Date acquired": today, "Mother tag": "",
         "Origin location": SP["example_place"], "Notes": "Age estimated"},
    ])
    if SP["branded"]:
        # Cattle carry a brand; the goat and sheep sheets have no such column.
        frame.insert(frame.columns.get_loc("Mother tag"), "Brand number",
                     ["A042", "", "4B21"])
    return frame


def import_template_xlsx_bytes():
    """The template as a real .xlsx file, or None if openpyxl is unavailable."""
    buffer = io.BytesIO()
    try:
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            import_template_frame().to_excel(writer, index=False,
                                             sheet_name="Cattle")
    except Exception:
        return None
    return buffer.getvalue()


# ── bulk import of purchases (Excel / CSV) ────
# Same loose heading matching as the herd importer, but the fields that matter
# for a purchase: who sold it, what was paid, and how.
PURCHASE_HEADER_ALIASES = {
    "tag": "tag", "tagno": "tag", "tagnumber": "tag", "tagid": "tag",
    "eartag": "tag", "eartagno": "tag", "newtag": "tag", "animaltag": "tag",
    "cowtag": "tag", "identifier": "tag",
    "name": "name", "cowname": "name", "animalname": "name",
    "breed": "breed", "breedtype": "breed",
    "sex": "sex", "gender": "sex",
    "category": "category", "class": "category", "type": "category",
    "weight": "weight", "weightkg": "weight", "liveweight": "weight",
    "liveweightkg": "weight", "mass": "weight", "masskg": "weight",
    "price": "price", "purchaseprice": "price", "cost": "price",
    "amount": "amount_alias", "amountpaid": "price", "pricepaid": "price",
    "paid": "price", "total": "price", "value": "price", "pula": "price",
    "seller": "seller", "sellername": "seller", "soldby": "seller",
    "vendor": "seller", "supplier": "seller", "from": "seller",
    "sellercontact": "seller_contact", "contact": "seller_contact",
    "sellerphone": "seller_contact", "phone": "seller_contact",
    "sellernumber": "seller_contact", "telephone": "seller_contact",
    "date": "date", "purchasedate": "date", "datepurchased": "date",
    "datebought": "date", "boughton": "date", "transactiondate": "date",
    "dateofpurchase": "date",
    "paymentmethod": "payment_method", "payment": "payment_method",
    "paidby": "payment_method", "method": "payment_method",
    "notes": "notes", "note": "notes", "comment": "notes", "comments": "notes",
    "remark": "notes", "remarks": "notes", "description": "notes",
}
# "Amount" is ambiguous on a purchase sheet — it usually means money, but it
# can mean head count. Treat it as price only when there is no price column.
PURCHASE_HEADER_ALIASES["amount"] = "price"


def _coerce_price_cell(value):
    """Read a money cell, tolerating currency symbols, spaces and commas.

    Returns (value, note). A blank cell is not an error here; the caller
    decides whether a missing price is fatal.
    """
    text = _cell_text(value)
    if not text:
        return None, ""
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    # A trailing minus or a stray dash should not become a negative price.
    cleaned = cleaned.lstrip("-")
    if cleaned.count(".") > 1:                    # 1.234.56 → 1234.56
        head, _, tail = cleaned.rpartition(".")
        cleaned = head.replace(".", "") + "." + tail
    try:
        number = float(cleaned)
    except ValueError:
        return None, "price is not a number"
    if number <= 0:
        return None, "price must be greater than zero"
    return number, ""


def build_purchase_import_rows(frame, mapping, defaults):
    """Turn an uploaded sheet into candidate purchases, each with a verdict.

    Verdicts: new (ready to record), duplicate (that tag is already in the
    herd, so it cannot be bought again), skip (cannot be recorded at all) and
    blank (an empty spreadsheet row).
    """
    tag_col = mapping.get("tag")
    raw_tags = ([_cell_text(v) for v in frame[tag_col].tolist()]
                if tag_col is not None else [])
    already = existing_tags(raw_tags)

    seen_tags, rows = set(), []
    for offset, (_, raw) in enumerate(frame.iterrows()):
        vals = {field: raw[col] for field, col in mapping.items()}
        if all(not _cell_text(v) for v in vals.values()):
            rows.append({"line": offset + 2, "verdict": "blank",
                         "issues": [], "rec": None})
            continue

        issues = []
        tag = _cell_text(vals.get("tag"))

        sex, sex_ok = _match_choice(vals.get("sex"), SEXES, SEX_SHORTHAND)
        if not sex_ok:
            issues.append("sex not recognised, left blank")
            sex = ""

        category, cat_ok = _match_choice(vals.get("category"), CATEGORIES)
        if not cat_ok:
            issues.append("category not recognised, left blank")
            category = ""

        method, method_ok = _match_choice(vals.get("payment_method"),
                                          PAYMENT_METHODS)
        if not method_ok:
            issues.append("payment method not recognised, set to "
                          + defaults["payment_method"])
            method = ""
        if not method:
            method = defaults["payment_method"]

        bought, date_ok, date_note = _coerce_date_cell(vals.get("date"))
        if not date_ok:
            issues.append("purchase date unreadable, default used")
            bought = ""
        elif date_note:
            issues.append("purchase date: " + date_note)
        if not bought:
            bought = defaults["date"]

        weight, weight_note = _coerce_weight_cell(vals.get("weight"))
        if weight_note:
            issues.append(weight_note)

        price, price_note = _coerce_price_cell(vals.get("price"))

        seller = _cell_text(vals.get("seller")) or defaults["seller"]

        rec = {
            "tag": tag,
            "name": _cell_text(vals.get("name")),
            "breed": _cell_text(vals.get("breed")),
            "sex": sex,
            "category": category,
            "weight": weight,
            "price": price,
            "seller": seller,
            "seller_contact": (_cell_text(vals.get("seller_contact"))
                               or defaults["seller_contact"]),
            "date": bought,
            "payment_method": method,
            "notes": _cell_text(vals.get("notes")),
        }

        # A purchase needs a tag, a price and a seller to be a record of
        # anything. Everything else can be filled in later.
        if not tag:
            verdict = "skip"
            issues.append("no tag, and a tag is required")
        elif tag in seen_tags:
            verdict = "skip"
            issues.append("this tag appears more than once in the file")
        elif price is None:
            verdict = "skip"
            issues.append(price_note or "no price, and a price is required")
        elif not seller:
            verdict = "skip"
            issues.append("no seller, and a seller is required "
                          "(set one below to cover blank rows)")
        elif tag in already:
            verdict = "duplicate"
            issues.append(T("that tag is already in the herd, so it cannot be "
                          "bought again"))
        else:
            verdict = "new"
        if tag:
            seen_tags.add(tag)

        rows.append({"line": offset + 2, "verdict": verdict,
                     "issues": issues, "rec": rec})
    return rows


def purchase_template_frame():
    """A short worked example of a purchase sheet."""
    today = date.today().isoformat()
    _b = SP["breeds"]
    _w = SP["example_weights"]
    _p = SP["example_prices"]
    return pd.DataFrame([
        {"Tag": "BW-0801", "Name": "Duke", "Breed": _b[2], "Sex": "Male",
         "Category": SP["male_options"][1], "Weight (kg)": _w[3],
         "Price": _p[0],
         "Seller": "K. Rampa", "Seller contact": "71234567",
         "Purchase date": today, "Payment method": "Cash",
         "Notes": "Bought at Lobatse auction"},
        {"Tag": "BW-0802", "Name": "", "Breed": _b[0], "Sex": "Female",
         "Category": SP["maiden_cat"], "Weight (kg)": _w[4], "Price": _p[1],
         "Seller": "K. Rampa", "Seller contact": "71234567",
         "Purchase date": today, "Payment method": "Cash", "Notes": ""},
        {"Tag": "BW-0803", "Name": "", "Breed": _b[1], "Sex": "Female",
         "Category": SP["adult_f_cat"], "Weight (kg)": _w[5], "Price": _p[2],
         "Seller": "M. Dube", "Seller contact": "", "Purchase date": today,
         "Payment method": "Bank transfer", "Notes": ""},
    ])


def purchase_template_xlsx_bytes():
    """The purchase template as a real .xlsx, or None without openpyxl."""
    buffer = io.BytesIO()
    try:
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            purchase_template_frame().to_excel(writer, index=False,
                                               sheet_name="Purchases")
    except Exception:
        return None
    return buffer.getvalue()


# ── date pickers that reach back far enough ───
# Streamlit's date_input defaults its earliest selectable day to ten years
# ago, and the calendar's year list only covers the range it is given. For a
# birth date that is useless: a nine-year-old cow cannot be entered at all,
# and the year menu has nothing to choose from. Every historical date field
# below is given an explicit floor so the month and year menus are usable.
DOB_YEARS_BACK = SP["life_years"]   # comfortably beyond a lifespan
RECORD_YEARS_BACK = 30       # sales, purchases, expenses, activities


def earliest_selectable(*existing, years_back=RECORD_YEARS_BACK):
    """A floor for a date picker, widened to include any date already saved.

    A record older than the floor would otherwise make Streamlit raise, so an
    existing value always wins over the default.
    """
    floor = date(date.today().year - years_back, 1, 1)
    for value in existing:
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date) and value < floor:
            floor = date(value.year, 1, 1)
    return floor


# ── downloads that actually work in the desktop window ──
# A browser download inside an embedded WebView2 goes through the host's
# download plumbing, and when any part of that is unavailable the button does
# nothing at all — no file, no dialog, no error. There is nothing the page can
# do about it from the inside.
#
# In the desktop build the server and the window are the same machine, so we
# stop asking the webview to download anything and write the file straight to
# the user's Downloads folder instead. That cannot silently fail: either the
# file is written and we show its full path, or we show the error.
#
# Running in an ordinary browser (streamlit run), we keep the normal download
# button, because there the server may be somewhere else entirely.
DESKTOP_MODE = os.environ.get("CATTLE_DESKTOP") == "1"


def _windows_downloads_folder():
    """Ask Windows itself where Downloads is.

    Plenty of machines have it redirected — OneDrive does this by default on
    many setups — so "%USERPROFILE%\\Downloads" is often simply not the right
    place, and may not exist at all. The Known Folder API gives the real one.
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]

        # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
        folder_id = GUID(0x374DE290, 0x123F, 0x4565,
                         (ctypes.c_byte * 8)(0x91, 0x64, 0x39, 0xC4,
                                             0x92, 0x5E, 0x46, 0x7B))
        path_ptr = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr))
        if result == 0 and path_ptr.value:
            found = path_ptr.value
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            return found
    except Exception:
        pass
    return ""


def downloads_dir():
    """Where saved files should land."""
    override = os.environ.get("CATTLE_DOWNLOADS")
    if override and os.path.isdir(override):
        return override

    known = _windows_downloads_folder()
    if known and os.path.isdir(known):
        return known

    home = os.path.expanduser("~")
    for name in ("Downloads", "Download"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate

    # Nothing usable — keep exports beside the database rather than scattering
    # them into the read-only install directory.
    fallback = os.path.join(DATA_DIR, "Exports")
    try:
        os.makedirs(fallback, exist_ok=True)
        return fallback
    except OSError:
        return home


# Windows rejects these outright, and a name ending in a dot or space, and a
# handful of names reserved since DOS. A tag like "BW/01" would otherwise make
# an export fail with nothing to show for it.
_WINDOWS_BAD_CHARS = '<>:"/\\|?*'
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name, default="export.dat"):
    """Make a filename Windows will actually accept.

    Separators are replaced rather than stripped: a cow tagged "BW/001" should
    export as "cow_profile_BW_001.pdf", not lose everything before the slash.
    Replacing them also means no path can escape the Downloads folder.
    """
    name = str(name or "").strip()
    if not name:
        return default
    cleaned = "".join("_" if (ch in _WINDOWS_BAD_CHARS or ord(ch) < 32) else ch
                      for ch in name)
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return default
    stem, ext = os.path.splitext(cleaned)
    if stem.upper() in _WINDOWS_RESERVED:
        stem = "_" + stem
    # Leave room for the collision suffix and a long Downloads path.
    if len(stem) > 120:
        stem = stem[:120]
    return (stem + ext) or default


def _unique_path(folder, filename):
    """Never overwrite an earlier export: report.pdf, report (2).pdf, ..."""
    stem, ext = os.path.splitext(filename)
    path = os.path.join(folder, filename)
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return path


def save_export(data, file_name):
    """Write an export to disk. Returns (ok, path_or_message)."""
    try:
        if hasattr(data, "getvalue"):
            data = data.getvalue()
        if isinstance(data, str):
            data = data.encode("utf-8")
        folder = downloads_dir()
        os.makedirs(folder, exist_ok=True)
        path = _unique_path(folder, safe_filename(file_name))
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())          # the file is on disk before we say so
        return True, path
    except Exception as exc:
        return False, str(exc)


def _dl(container, label, data, file_name=None, mime=None, key=None, **kwargs):
    """Offer a file to the user, whichever way actually works here.

    Every export in the app goes through this, so the desktop behaviour is
    decided in one place rather than at thirty separate buttons.
    """
    container = container if container is not None else st
    if not DESKTOP_MODE:
        return container.download_button(label, data, file_name=file_name,
                                         mime=mime, key=key, **kwargs)

    # Desktop: a plain button that writes the file itself.
    btn_key = key or ("save_" + str(abs(hash((label, file_name))) % 10**9))
    kwargs.pop("on_click", None)
    clicked = container.button(label, key=btn_key,
                               **{k: v for k, v in kwargs.items()
                                  if k in ("use_container_width", "type",
                                           "help", "disabled")})
    slot = st.session_state.setdefault("_saved_exports", {})
    if clicked:
        ok, result = save_export(data, file_name)
        slot[btn_key] = (ok, result)
    if btn_key in slot:
        ok, result = slot[btn_key]
        if ok:
            container.success("Saved to  " + result)
        else:
            container.error("Could not save the file: " + result)
    return clicked


# ── image helpers ─────────────────────────────
def count_images(tag):
    c = get_conn()
    return c.execute("SELECT COUNT(*) FROM cattle_images WHERE tag=?", (tag,)).fetchone()[0]


def get_images(tag):
    c = get_conn()
    rows = c.execute("SELECT id, filename, image FROM cattle_images WHERE tag=? "
                     "ORDER BY id", (tag,)).fetchall()
    return [dict(r) for r in rows]


def _shrink_image(data, max_side=700, quality=82):
    """Downscale a photo before storing it, so the database stays small and
    pages stay fast. Falls back to the original bytes if Pillow is missing."""
    try:
        from PIL import Image as _PILImage
        im = _PILImage.open(io.BytesIO(data))
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True)
        shrunk = out.getvalue()
        return shrunk if len(shrunk) < len(data) else data
    except Exception:
        return data


def add_images(tag, files):
    """Save uploaded files for a cow, up to MAX_IMAGES total. Returns how many
    were saved (the rest are skipped because the limit was reached)."""
    c = get_conn()
    used = count_images(tag)
    saved = 0
    for f in files or []:
        if used + saved >= MAX_IMAGES:
            break
        try:
            raw = f.getvalue()
        except Exception:
            continue
        if not raw:
            continue
        c.execute("INSERT INTO cattle_images(tag, filename, image, created_at) "
                  "VALUES(?,?,?,?)",
                  (tag, getattr(f, "name", "image"), _shrink_image(raw),
                   now_local().isoformat(timespec="seconds")))
        saved += 1
    c.commit()
    return saved


def delete_image(image_id):
    c = get_conn()
    c.execute("DELETE FROM cattle_images WHERE id=?", (image_id,))
    c.commit()


# ── calendar / event helpers ──────────────────
def add_event(d, etype, tag, note):
    c = get_conn()
    c.execute("INSERT INTO events(date, type, tag, note, created_at) VALUES(?,?,?,?,?)",
              (d, etype, tag.strip(), note.strip(),
               now_local().isoformat(timespec="seconds")))
    c.commit()


def has_event(tag, etype):
    c = get_conn()
    return c.execute("SELECT 1 FROM events WHERE tag=? AND type=?",
                     (tag, etype)).fetchone() is not None


def events_on(d):
    c = get_conn()
    rows = c.execute("SELECT * FROM events WHERE date=? ORDER BY id", (d,)).fetchall()
    return [dict(r) for r in rows]


def delete_event(eid):
    c = get_conn()
    c.execute("DELETE FROM events WHERE id=?", (eid,))
    c.commit()


def cattle_by_date(field, d):
    """Cattle whose dob or date_acquired equals d (field is a fixed column name)."""
    if field not in ("dob", "date_acquired"):
        raise ValueError("cattle_by_date: unsupported field")
    c = get_conn()
    rows = c.execute(f"SELECT tag, name FROM cattle WHERE {field}=?", (d,)).fetchall()
    return [dict(r) for r in rows]


def month_activity_counts(year, month):
    """Return {day_number: total activity count} for calendar dot markers."""
    c = get_conn()
    prefix = f"{year:04d}-{month:02d}-"
    counts = {}

    def tally(rows):
        for (dstr,) in rows:
            if dstr and dstr.startswith(prefix):
                try:
                    day = int(dstr[8:10])
                    counts[day] = counts.get(day, 0) + 1
                except ValueError:
                    pass

    tally(c.execute("SELECT date FROM events WHERE date LIKE ?", (prefix + "%",)).fetchall())
    tally(c.execute("SELECT date FROM activities WHERE date LIKE ?",
                    (prefix + "%",)).fetchall())
    tally(c.execute("SELECT date FROM reminders WHERE date LIKE ?",
                    (prefix + "%",)).fetchall())
    tally(c.execute("SELECT due_date FROM breedings WHERE status='Pregnant' "
                    "AND due_date LIKE ?", (prefix + "%",)).fetchall())
    tally(c.execute("SELECT dob FROM cattle WHERE dob LIKE ?", (prefix + "%",)).fetchall())
    tally(c.execute("SELECT date_acquired FROM cattle WHERE date_acquired LIKE ?",
                    (prefix + "%",)).fetchall())
    return counts


# ── billing helpers ───────────────────────────
def next_invoice_no():
    c = get_conn()
    year = now_local().year
    n = c.execute("SELECT COUNT(*) FROM sales WHERE invoice_no LIKE ?",
                  (f"INV-{year}-%",)).fetchone()[0]
    return f"INV-{year}-{n + 1:04d}"


def add_sale(rec):
    c = get_conn()
    c.execute(
        "INSERT INTO sales(invoice_no, date, tag, cow_name, buyer, buyer_contact, "
        "weight, price, payment_method, notes, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (rec["invoice_no"], rec["date"], rec["tag"], rec["cow_name"], rec["buyer"],
         rec["buyer_contact"], rec["weight"], rec["price"], rec["payment_method"],
         rec["notes"], now_local().isoformat(timespec="seconds")))
    c.commit()


def all_sales():
    c = get_conn()
    rows = c.execute("SELECT * FROM sales ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]






def money(amount):
    """Format an amount in the currency chosen in Billing."""
    sym = currency_symbol()
    try:
        return f"{sym} {float(amount or 0):,.2f}"
    except (TypeError, ValueError):
        return f"{sym} 0.00"


# ── farm profile (appears on every report) ────
def get_farm_profile():
    """Farm and farmer details used in the header of every document."""
    c = get_conn()
    row = c.execute("SELECT * FROM farm_profile WHERE id=1").fetchone()
    p = dict(row) if row else {}
    return {
        "farm_name": (p.get("farm_name") or "").strip() or FARM_NAME,
        "farmer_name": (p.get("farmer_name") or "").strip(),
        "location": (p.get("location") or "").strip(),
        "phone": (p.get("phone") or "").strip(),
        "email": (p.get("email") or "").strip(),
    }


def save_farm_profile(rec):
    c = get_conn()
    c.execute("UPDATE farm_profile SET farm_name=?, farmer_name=?, location=?, "
              "phone=?, email=?, updated_at=? WHERE id=1",
              (rec["farm_name"].strip(), rec["farmer_name"].strip(),
               rec["location"].strip(), rec["phone"].strip(), rec["email"].strip(),
               now_local().isoformat(timespec="seconds")))
    c.commit()


def _shrink_logo(data, max_side=420):
    """Scale a logo down for storage, keeping transparency and proportions."""
    try:
        from PIL import Image as _PILImage
        im = _PILImage.open(io.BytesIO(data))
        if im.mode not in ("RGBA", "RGB"):
            im = im.convert("RGBA")
        before = im.size
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        shrunk = out.getvalue()
        # If it was actually resized, always keep the smaller image — otherwise
        # only re-encode when that genuinely saves space.
        if im.size != before:
            return shrunk
        return shrunk if len(shrunk) <= len(data) else data
    except Exception:
        return data


def save_farm_logo(data, filename=""):
    """Store the farm's own logo. Returns (ok, message)."""
    if not data:
        return False, "No image received."
    try:
        from PIL import Image as _PILImage
        _PILImage.open(io.BytesIO(data)).verify()
    except Exception:
        return False, "That file could not be read as an image."
    small = _shrink_logo(data)
    c = get_conn()
    c.execute("UPDATE farm_profile SET logo=?, logo_name=? WHERE id=1",
              (small, (filename or "").strip()))
    c.commit()
    return True, f"Farm logo saved ({len(small) / 1024:.0f} KB). It will appear on " \
                 "every PDF report, invoice and receipt."


def get_farm_logo():
    """The uploaded farm logo, or None if the system logo should be used."""
    c = get_conn()
    try:
        row = c.execute("SELECT logo FROM farm_profile WHERE id=1").fetchone()
    except Exception:
        return None
    return row[0] if row and row[0] else None


def clear_farm_logo():
    c = get_conn()
    c.execute("UPDATE farm_profile SET logo=NULL, logo_name='' WHERE id=1")
    c.commit()


def report_logo(RLImage, mm, max_h_mm=15, max_w_mm=34):
    """Logo flowable for a document header: the farm's own if uploaded,
    otherwise the built-in one. Returns (flowable_or_None, column_width_mm)."""
    data = get_farm_logo()
    if data:
        try:
            ratio = 1.0
            try:
                from PIL import Image as _PILImage
                iw, ih = _PILImage.open(io.BytesIO(data)).size
                ratio = (iw / ih) if ih else 1.0
            except Exception:
                pass
            h = max_h_mm * mm
            w = h * ratio
            if w > max_w_mm * mm:                 # wide logo: fit the width
                w = max_w_mm * mm
                h = w / ratio if ratio else h
            return RLImage(io.BytesIO(data), width=w, height=h), (w / mm) + 3
        except Exception:
            pass
    try:
        return (RLImage(io.BytesIO(base64.b64decode(LOGO_B64)),
                        width=max_h_mm * mm, height=max_h_mm * mm), max_h_mm + 3)
    except Exception:
        return None, 0


def farm_detail_lines(p=None):
    """Farm/farmer detail lines for document headers (skips blank fields)."""
    p = p or get_farm_profile()
    lines = []
    if p["farmer_name"]:
        lines.append(f"Farmer: {p['farmer_name']}")
    if p["location"]:
        lines.append(f"Location: {p['location']}")
    contact = " · ".join(x for x in (p["phone"], p["email"]) if x)
    if contact:
        lines.append(f"Contact: {contact}")
    return lines


def doc_html(number, ddate, tag, cow_name, weight, price, party_label, party_name,
             party_contact, method, notes, subtitle):
    """A clean, printable invoice (sale) or receipt (purchase)."""
    _p = get_farm_profile()
    _farm_lines = "<br>".join(_html.escape(x) for x in farm_detail_lines(_p))
    _farm_block = f"<div class='muted'>{_farm_lines}</div>" if _farm_lines else ""
    party_block = (f"<p><strong>{party_label}:</strong><br>{party_name or '—'}<br>"
                   f"<span class='muted'>{party_contact or ''}</span></p>")
    # Transaction notes only — never the animal's own notes/activity history.
    notes_block = ("<p><strong>Transaction notes:</strong> "
                   + _html.escape(str(notes)) + "</p>") if notes else ""
    desc = (SP["One"] + " — " + cow_name) if cow_name else SP["One"]
    wt = (str(weight) + " kg") if weight else "—"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{number}</title>
<style>
  body {{ font-family:Arial,Helvetica,sans-serif; color:{INK}; max-width:720px;
    margin:32px auto; padding:0 20px; }}
  .head {{ display:flex; justify-content:space-between; align-items:flex-start;
    border-bottom:3px solid {PRIMARY}; padding-bottom:14px; }}
  .farm {{ font-size:1.6rem; font-weight:800; color:{PRIMARY}; }}
  .muted {{ color:{MUTED}; font-size:.9rem; }}
  h2 {{ color:{PRIMARY}; margin:.2rem 0; }}
  table {{ width:100%; border-collapse:collapse; margin-top:22px; }}
  th {{ text-align:left; background:{LIGHT_BG}; color:{PRIMARY}; padding:10px;
    border-bottom:2px solid {GRID}; }}
  td {{ padding:10px; border-bottom:1px solid {GRID}; }}
  .total {{ text-align:right; font-size:1.3rem; font-weight:800; color:{PRIMARY};
    margin-top:18px; }}
  .foot {{ margin-top:40px; color:{MUTED}; font-size:.85rem;
    border-top:1px solid {GRID}; padding-top:12px; }}
</style></head><body>
  <div class="head">
    <div><div class="farm">🐄 {_html.escape(_p['farm_name'])}</div>
      <div class="muted">{subtitle}</div>
      {_farm_block}</div>
    <div style="text-align:right"><h2>{number}</h2>
      <div class="muted">Date: {ddate}</div></div>
  </div>
  {party_block}
  <table>
    <tr><th>Tag</th><th>Description</th><th>Weight</th><th>Amount</th></tr>
    <tr><td>{tag or '—'}</td><td>{desc}</td><td>{wt}</td><td>{money(price)}</td></tr>
  </table>
  <div class="total">Total: {money(price)}</div>
  <p class="muted">Payment method: {method or '—'}</p>
  {notes_block}
  <div class="foot">Generated by {_html.escape(_p['farm_name'])} Cattle Management
  &amp; Inventory System · Powered by Health Data Matrics (HDM Group).</div>
</body></html>"""


def purchase_receipt_html(pur):
    return doc_html(pur["receipt_no"], pur["date"], pur["tag"], pur["cow_name"],
                    pur["weight"], pur["price"], "Purchased from", pur["seller"],
                    pur["seller_contact"], pur["payment_method"], pur["notes"],
                    "Livestock purchase receipt")


# ── purchase helpers ──────────────────────────
def next_receipt_no():
    c = get_conn()
    year = now_local().year
    n = c.execute("SELECT COUNT(*) FROM purchases WHERE receipt_no LIKE ?",
                  (f"PUR-{year}-%",)).fetchone()[0]
    return f"PUR-{year}-{n + 1:04d}"


def add_purchase(rec):
    c = get_conn()
    c.execute(
        "INSERT INTO purchases(receipt_no, date, tag, cow_name, breed, sex, seller, "
        "seller_contact, weight, price, payment_method, notes, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["receipt_no"], rec["date"], rec["tag"], rec["cow_name"], rec["breed"],
         rec["sex"], rec["seller"], rec["seller_contact"], rec["weight"], rec["price"],
         rec["payment_method"], rec["notes"], now_local().isoformat(timespec="seconds")))
    c.commit()


def all_purchases():
    c = get_conn()
    rows = c.execute("SELECT * FROM purchases ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_purchase(pid):
    c = get_conn()
    row = c.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def delete_purchase(pid):
    c = get_conn()
    c.execute("DELETE FROM purchases WHERE id=?", (pid,))
    c.commit()


# ── death / missing helpers ───────────────────
def add_loss(rec):
    c = get_conn()
    c.execute("INSERT INTO losses(tag, cow_name, type, date, cause, created_at) "
              "VALUES(?,?,?,?,?,?)",
              (rec["tag"], rec["cow_name"], rec["type"], rec["date"], rec["cause"],
               now_local().isoformat(timespec="seconds")))
    c.commit()


def all_losses():
    c = get_conn()
    rows = c.execute("SELECT * FROM losses ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def delete_loss(lid):
    c = get_conn()
    c.execute("DELETE FROM losses WHERE id=?", (lid,))
    c.commit()


# ── daily-expense helpers ─────────────────────
def add_expense(rec):
    c = get_conn()
    c.execute("INSERT INTO expenses(date, category, description, amount, "
              "payment_method, created_at) VALUES(?,?,?,?,?,?)",
              (rec["date"], rec["category"], rec["description"], rec["amount"],
               rec["payment_method"], now_local().isoformat(timespec="seconds")))
    c.commit()


def all_expenses():
    c = get_conn()
    rows = c.execute("SELECT * FROM expenses ORDER BY date DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def delete_expense(eid):
    c = get_conn()
    c.execute("DELETE FROM expenses WHERE id=?", (eid,))
    c.commit()


# ── farm-activity helpers ─────────────────────
def append_note_to_tags(tags, text, all_active=False, all_cattle_flag=False):
    """Append a line to the notes of the chosen cattle. Done in SQL so applying an
    activity to the whole herd stays fast. Returns how many records were updated."""
    c = get_conn()
    line = text.strip()
    if not line:
        return 0
    # COALESCE guards against NULL notes; only add a newline when notes aren't empty.
    set_expr = ("notes = CASE WHEN COALESCE(notes,'') = '' THEN ? "
                "ELSE COALESCE(notes,'') || char(10) || ? END")
    if all_cattle_flag:
        cur = c.execute(f"UPDATE cattle SET {set_expr}", (line, line))
    elif all_active:
        cur = c.execute(f"UPDATE cattle SET {set_expr} WHERE status='Active'",
                        (line, line))
    else:
        tags = [t for t in (tags or []) if t]
        if not tags:
            return 0
        updated = 0
        # Chunked so very large selections stay within SQLite's variable limit.
        for i in range(0, len(tags), 400):
            chunk = tags[i:i + 400]
            marks = ",".join("?" * len(chunk))
            cur = c.execute(
                f"UPDATE cattle SET {set_expr} WHERE tag IN ({marks})",
                [line, line] + chunk)
            updated += cur.rowcount or 0
        c.commit()
        return updated
    c.commit()
    return cur.rowcount or 0


def add_activity(rec):
    c = get_conn()
    c.execute("INSERT INTO activities(date, activity, details, scope, tags, tag_count, "
              "created_at) VALUES(?,?,?,?,?,?,?)",
              (rec["date"], rec["activity"], rec["details"], rec["scope"],
               rec["tags"], rec["tag_count"],
               now_local().isoformat(timespec="seconds")))
    c.commit()


def all_activities(limit=200):
    c = get_conn()
    rows = c.execute("SELECT * FROM activities ORDER BY date DESC, id DESC LIMIT ?",
                     (limit,)).fetchall()
    return [dict(r) for r in rows]


def activities_on(d):
    c = get_conn()
    rows = c.execute("SELECT * FROM activities WHERE date=? ORDER BY id", (d,)).fetchall()
    return [dict(r) for r in rows]


def search_activities(date_iso="", text="", limit=500):
    """Filter the activity log by day and/or free text (activity, details, tags)."""
    c = get_conn()
    q = "SELECT * FROM activities WHERE 1=1"
    params = []
    if date_iso:
        q += " AND date=?"
        params.append(date_iso)
    if text and text.strip():
        like = f"%{text.strip().lower()}%"
        q += (" AND (LOWER(activity) LIKE ? OR LOWER(COALESCE(details,'')) LIKE ? "
              "OR LOWER(COALESCE(tags,'')) LIKE ?)")
        params += [like, like, like]
    q += " ORDER BY date DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in c.execute(q, params).fetchall()]


def activities_frame(rows):
    """Consistent table shape for display and CSV export."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)[["date", "activity", "scope", "tag_count", "tags",
                             "details"]]
    df["scope"] = df["scope"].map({"all": "All cattle",
                                   "active": "All active cattle",
                                   "tags": "Selected tags"}).fillna("—")
    return df.rename(columns={"date": "Date", "activity": "Activity",
                              "scope": "Applied to", "tag_count": "Cattle affected",
                              "tags": "Tag numbers", "details": "Details"})


def delete_activity(aid):
    c = get_conn()
    c.execute("DELETE FROM activities WHERE id=?", (aid,))
    c.commit()


# ── reminder / things-to-do helpers ───────────
REMINDER_PRIORITIES = ["Normal", "High"]


def add_reminder(rec):
    """Add a task. Pass a "tag" when the task belongs to one animal, so the
    reminder goes with her if she is ever removed from the herd."""
    c = get_conn()
    c.execute("INSERT INTO reminders(date, task, details, priority, done, "
              "tag, created_at) VALUES(?,?,?,?,0,?,?)",
              (rec["date"], rec["task"], rec["details"], rec["priority"],
               (rec.get("tag") or "").strip(),
               now_local().isoformat(timespec="seconds")))
    c.commit()


def reminders_on(d):
    c = get_conn()
    rows = c.execute("SELECT * FROM reminders WHERE date=? ORDER BY done, id",
                     (d,)).fetchall()
    return [dict(r) for r in rows]


def upcoming_reminders(limit=50):
    """Outstanding tasks from today onwards, soonest first."""
    c = get_conn()
    rows = c.execute("SELECT * FROM reminders WHERE done=0 AND date>=? "
                     "ORDER BY date, id LIMIT ?",
                     (date.today().isoformat(), limit)).fetchall()
    return [dict(r) for r in rows]


def overdue_reminders(limit=50):
    """Outstanding tasks whose day has already passed."""
    c = get_conn()
    rows = c.execute("SELECT * FROM reminders WHERE done=0 AND date<? "
                     "ORDER BY date, id LIMIT ?",
                     (date.today().isoformat(), limit)).fetchall()
    return [dict(r) for r in rows]


def set_reminder_done(rid, done):
    c = get_conn()
    c.execute("UPDATE reminders SET done=? WHERE id=?", (1 if done else 0, rid))
    c.commit()


def delete_reminder(rid):
    c = get_conn()
    c.execute("DELETE FROM reminders WHERE id=?", (rid,))
    c.commit()


def _when_label(d_iso):
    """Day label for a reminder.

    The year is always shown. Without it a task dated "14 Mar" is ambiguous
    the moment a list spans a year end — and an overdue task from last year
    looks like one due next month.
    """
    try:
        d = datetime.fromisoformat(d_iso).date()
    except Exception:
        return d_iso or ""
    today = date.today()
    delta = (d - today).days
    if delta == 0:
        return "Today · " + d.strftime("%d %b %Y")
    if delta == 1:
        return "Tomorrow · " + d.strftime("%d %b %Y")
    if delta == -1:
        return "Yesterday · " + d.strftime("%d %b %Y")
    return d.strftime("%d %b %Y")


# ══════════════════════════════════════════════
# SMART QUESTION & ANSWER
# Reads the herd, calendar, activities, billing and expenses to answer
# free-text questions with a short written summary.
# ══════════════════════════════════════════════
_MONTHS = {m.lower(): i for i, m in enumerate(_cal.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(_cal.month_abbr) if m})


def _qa_period(q):
    """Pull a date range out of the question. Returns (start, end, label) or None."""
    today = date.today()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", q)
    if m:
        return m.group(1), m.group(1), f"on {m.group(1)}"
    if "today" in q:
        i = today.isoformat()
        return i, i, "today"
    if "yesterday" in q:
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat(), "yesterday"
    if "this week" in q:
        s = today - timedelta(days=today.weekday())
        return s.isoformat(), today.isoformat(), "this week"
    if "last week" in q:
        s = today - timedelta(days=today.weekday() + 7)
        e = s + timedelta(days=6)
        return s.isoformat(), e.isoformat(), "last week"
    if "last month" in q:
        first = today.replace(day=1)
        prev_end = first - timedelta(days=1)
        return (prev_end.replace(day=1).isoformat(), prev_end.isoformat(),
                f"in {prev_end.strftime('%B %Y')}")
    if "this month" in q:
        return (today.replace(day=1).isoformat(), today.isoformat(), "this month")
    if "last year" in q:
        y = today.year - 1
        return f"{y}-01-01", f"{y}-12-31", f"in {y}"
    if "this year" in q:
        return f"{today.year}-01-01", today.isoformat(), "this year"
    for name, num in _MONTHS.items():
        if re.search(rf"\b{name}\b", q):
            ym = re.search(r"\b(20\d{2})\b", q)
            year = int(ym.group(1)) if ym else today.year
            last = _cal.monthrange(year, num)[1]
            return (f"{year}-{num:02d}-01", f"{year}-{num:02d}-{last:02d}",
                    f"in {_cal.month_name[num]} {year}")
    ym = re.search(r"\b(20\d{2})\b", q)
    if ym:
        y = ym.group(1)
        return f"{y}-01-01", f"{y}-12-31", f"in {y}"
    return None


def _qa_tag(q):
    """Find a tag from the herd mentioned in the question."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_/]*", q)
    if not tokens:
        return None
    c = get_conn()
    for chunk_start in range(0, len(tokens), 200):
        chunk = tokens[chunk_start:chunk_start + 200]
        marks = ",".join("?" * len(chunk))
        row = c.execute(
            f"SELECT tag FROM cattle WHERE UPPER(tag) IN ({marks}) LIMIT 1",
            [t.upper() for t in chunk]).fetchone()
        if row:
            return row[0]
    return None


def _qa_sum(table, start, end, col="price"):
    c = get_conn()
    if start:
        r = c.execute(f"SELECT COUNT(*), COALESCE(SUM({col}),0) FROM {table} "
                      "WHERE date BETWEEN ? AND ?", (start, end)).fetchone()
    else:
        r = c.execute(f"SELECT COUNT(*), COALESCE(SUM({col}),0) FROM {table}").fetchone()
    return r[0], r[1] or 0


# Words that must never be treated as part of a person's name.
_QA_STOPWORDS = {
    "cattle", "cow", "cows", "calf", "calves", "bull", "farm", "herd", "the", "and",
    "for", "from", "with", "did", "how", "much", "many", "what", "who", "when",
    "sold", "sell", "sale", "sales", "buy", "bought", "purchase", "purchases",
    "spend", "spent", "expense", "expenses", "total", "show", "tell", "about",
    "give", "records", "record", "this", "that", "have", "was", "were", "are",
    # expense categories and farm terms, so they can't be mistaken for a name
    "feed", "veterinary", "medicine", "vet", "labour", "labor", "transport",
    "equipment", "water", "utilities", "vaccination", "dipping", "deworming",
    "branding", "weighing", "activity", "activities", "calendar", "inventory",
    "billing", "profit", "income", "revenue", "born", "birth", "missing", "died",
}

# If the question clearly asks about something else, a partial (one-word)
# name match isn't enough — the full name must appear.
_QA_OTHER_INTENT = (
    "spend", "spent", "expense", "cost", "how many", "total", "profit", "net "
    , "activities", "activity", "born", "calves", "missing", "died", "to do",
    "reminder", "breakdown", "inventory",
)


def _qa_people(q):
    """Buyer/seller names from the records that are mentioned in the question."""
    c = get_conn()
    names = set()
    for (n,) in c.execute("SELECT DISTINCT buyer FROM sales "
                          "WHERE TRIM(COALESCE(buyer,'')) != ''"):
        names.add(n.strip())
    for (n,) in c.execute("SELECT DISTINCT seller FROM purchases "
                          "WHERE TRIM(COALESCE(seller,'')) != ''"):
        names.add(n.strip())
    ql = " " + q.lower().strip() + " "
    other_intent = any(w in ql for w in _QA_OTHER_INTENT)
    hits = []
    for n in names:
        nl = n.lower().strip()
        if len(nl) < 3:
            continue                       # too short to identify anyone safely
        # Whole-name match, but only on word boundaries — otherwise a buyer
        # called "A" or "Co" would match almost any question.
        if re.search(rf"(?<![a-z0-9]){re.escape(nl)}(?![a-z0-9])", ql):
            hits.append((n, 2))
            continue
        if other_intent:                   # ambiguous word + another clear intent
            continue
        for tok in re.findall(r"[a-z]{3,}", nl):   # surname / first name typed
            if tok in _QA_STOPWORDS:
                continue
            if re.search(rf"\b{re.escape(tok)}\b", ql):
                hits.append((n, 1))
                break
    hits.sort(key=lambda x: -x[1])
    return [n for n, _ in hits]


def person_summary(name):
    """Everything a buyer/seller is recorded for."""
    c = get_conn()
    sales = c.execute(
        "SELECT date, tag, price, invoice_no, payment_method, buyer_contact "
        "FROM sales WHERE buyer=? ORDER BY date", (name,)).fetchall()
    purch = c.execute(
        "SELECT date, tag, price, receipt_no, payment_method, seller_contact "
        "FROM purchases WHERE seller=? ORDER BY date", (name,)).fetchall()
    if not sales and not purch:
        return f"No records found for {name}."

    sold_v = sum(r[2] or 0 for r in sales)
    paid_v = sum(r[2] or 0 for r in purch)
    contact = ""
    for r in list(sales) + list(purch):
        if r[5]:
            contact = r[5]
            break

    roles = []
    if sales:
        roles.append("buyer")
    if purch:
        roles.append("seller")
    head = (f"{name} — recorded as your {' and '.join(roles)}"
            + (f" · {contact}" if contact else "") + ".")

    lines = [head, ""]
    if sales:
        lines.append(f"Sold to them: {len(sales)} head for {money(sold_v)}")
        for r in sales[-6:]:
            lines.append(f"- {r[0]} — {r[1]} · {money(r[2])} · {r[4] or '—'} "
                         f"({r[3]})")
    if purch:
        if sales:
            lines.append("")
        lines.append(f"Bought from them: {len(purch)} head for {money(paid_v)}")
        for r in purch[-6:]:
            lines.append(f"- {r[0]} — {r[1]} · {money(r[2])} · {r[4] or '—'} "
                         f"({r[3]})")
    if sales and purch:
        net = sold_v - paid_v
        lines.append("")
        lines.append(f"Net with {name}: {money(net)} "
                     + ("in your favour." if net >= 0 else "against you."))
    return "\n".join(lines)


def qa_people_list(limit=12):
    """Everyone on record, for the assistant's suggestions."""
    c = get_conn()
    rows = c.execute(
        "SELECT name, SUM(n) FROM ("
        "  SELECT buyer AS name, COUNT(*) AS n FROM sales "
        "  WHERE TRIM(COALESCE(buyer,''))!='' GROUP BY buyer "
        "  UNION ALL "
        "  SELECT seller AS name, COUNT(*) AS n FROM purchases "
        "  WHERE TRIM(COALESCE(seller,''))!='' GROUP BY seller"
        ") GROUP BY name ORDER BY SUM(n) DESC LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows]


def answer_question(question):
    # Asked in this herd's words; the rules below are written about cattle.
    question = U(question)
    """Answer a free-text question with a short summary. Returns markdown."""
    q = (question or "").strip().lower()
    if not q:
        return "Ask me something about your herd, sales, expenses or activities."
    c = get_conn()
    period = _qa_period(q)
    start, end, plabel = period if period else (None, None, "overall")
    tag = _qa_tag(question or "")
    when = f" {plabel}" if period else ""

    def has(*words):
        return any(w in q for w in words)

    # ── 1. A specific animal ──────────────────
    if tag:
        cow = get_cow(tag)
        if cow:
            calves = calves_of(tag)
            # Ignore the tag itself when reading intent, so a tag like
            # "CALF-1" doesn't get mistaken for a question about calves.
            qt = q.replace(tag.lower(), " ")

            def hast(*words):
                return any(w in qt for w in words)

            if hast("weigh", "weight", "kg", "gain", "growth", "heavy"):
                _s = weight_stats(tag)
                if not _s:
                    return f"No weighings are recorded for {tag} yet."
                if _s["count"] == 1:
                    return (f"{tag} was weighed once: "
                            f"{_s['last_weight']:,.0f} kg on {_s['last_date']}.")
                _line = (f"{tag} now weighs {_s['last_weight']:,.0f} kg "
                         f"({_s['last_date']}), up from {_s['first_weight']:,.0f} kg "
                         f"on {_s['first_date']} — {_s['gain']:+,.0f} kg over "
                         f"{_s['days']} days")
                if _s["adg"] is not None:
                    _line += f", averaging {_s['adg']:.2f} kg/day"
                return _line + f". {_s['count']} weighings on record."

            if hast("activity", "activities", "vaccin", "treat", "dip", "dewor",
                    "history", "done to", "what was done"):
                acts = c.execute(
                    "SELECT date, activity, details FROM activities "
                    "WHERE scope!='tags' OR tags LIKE ? ORDER BY date DESC LIMIT 5",
                    (f"%{tag}%",)).fetchall()
                if not acts:
                    return f"No activities are recorded for {tag} yet."
                lines = "\n".join(f"- {a[0]} — {a[1]}"
                                  + (f" ({a[2]})" if a[2] else "") for a in acts)
                return f"Recent activities affecting {tag}:\n{lines}"

            sale = c.execute("SELECT * FROM sales WHERE tag=? ORDER BY id DESC LIMIT 1",
                             (tag,)).fetchone()
            if sale and hast("sold", "sell", "sale", "price", "buyer", "bought",
                             "who"):
                s = dict(sale)
                return (f"{tag} was sold on {s['date']} to {s['buyer']} "
                        f"for {money(s['price'])} (invoice {s['invoice_no']}).")

            if hast("where", "come from", "origin", "from", "source"):
                return (f"{tag} is recorded as coming from "
                        f"{cow.get('origin_location') or 'no location on record'}"
                        + (f", acquired {cow['date_acquired']}."
                           if cow["date_acquired"] else "."))

            if hast("calf", "calves", "born", "offspring", "mother", "children"):
                if not calves:
                    return f"{tag} has no calves recorded."
                lst = ", ".join(x["tag"] for x in calves)
                word = "calf" if len(calves) == 1 else "calves"
                return f"{tag} has {len(calves)} {word} recorded: {lst}."

            # General profile summary
            bits = [f"{tag} — {cow['status']}"]
            if cow["breed"]:
                bits.append(cow["breed"])
            if cow["sex"]:
                bits.append(cow["sex"])
            if cow["category"]:
                bits.append(cow["category"])
            if age_str(cow["dob"]):
                bits.append(f"aged {age_str(cow['dob'])}")
            if cow["weight"]:
                bits.append(f"{cow['weight']:.0f} kg")
            summary = " · ".join(bits) + "."
            extra = []
            if cow.get("brand_number"):
                extra.append(f"brand {cow['brand_number']}")
            if cow.get("origin_location"):
                extra.append(f"from {cow['origin_location']}")
            if cow.get("mother_tag"):
                extra.append(f"mother {cow['mother_tag']}")
            if calves:
                extra.append(T(f"{len(calves)} calf/calves"))
            if extra:
                summary += " " + ("Also on record: " + ", ".join(extra) + ".")
            return summary
        return f"I couldn't find a cow with that tag."

    # ── 1b. A person: buyer or seller ─────────
    people = _qa_people(question or "")
    if people:
        parts = [person_summary(p) for p in people[:2]]
        return "\n\n---\n\n".join(parts)

    # ── 2. Breeding & calving ─────────────────
    if has("pregnant", "in calf", "due to calve", "calving", "due date", "served",
           "breeding", "bull", "mated", "gestation", "overdue"):
        served, preg, soon, over = breeding_metrics()
        if has("overdue"):
            rows = overdue_calvings()
            if not rows:
                return "No cows are overdue to calve."
            lines = "\n".join(
                f"- {r['cow_tag']} — due {r['due_date']} "
                f"({abs(days_until(r['due_date']) or 0)} days ago)" for r in rows[:10])
            return f"{len(rows)} cow(s) overdue to calve:\n{lines}"
        if has("due", "calving", "calve"):
            rows = due_between(date.today().isoformat(),
                               (date.today() + timedelta(days=30)).isoformat())
            if not rows:
                return (f"No calvings are due in the next 30 days. "
                        f"{preg} cow(s) are confirmed pregnant.")
            lines = "\n".join(f"- {r['cow_tag']} — due {r['due_date']} "
                              f"(in {days_until(r['due_date'])} days)"
                              for r in rows[:10])
            return (f"{len(rows)} calving(s) due within 30 days:\n{lines}"
                    + (f"\n\n{over} overdue." if over else ""))
        return (f"Breeding summary: {preg} confirmed pregnant, {served} awaiting "
                f"a pregnancy result, {soon} due within 30 days"
                + (f", {over} overdue." if over else "."))

    # ── 2b. Weights & growth ──────────────────
    if has("weigh", "weight", "kg", "gain", "growth", "growing", "heaviest",
           "adg"):
        leaders = weight_leaders(limit=5)
        c2 = get_conn()
        n_weighed = c2.execute("SELECT COUNT(DISTINCT tag) FROM weights").fetchone()[0]
        if has("heaviest", "biggest", "largest"):
            rows = c2.execute("SELECT tag, weight FROM cattle WHERE status='Active' "
                              "AND weight IS NOT NULL ORDER BY weight DESC LIMIT 5"
                              ).fetchall()
            if not rows:
                return "No weights are recorded yet."
            lst = "\n".join(f"- {r[0]} — {r[1]:,.0f} kg" for r in rows)
            return f"Heaviest active cattle:\n{lst}"
        if has("gain", "growth", "growing", "fastest", "adg"):
            if not leaders:
                return ("No animal has two weighings yet, so growth rates can't be "
                        "worked out. Record weights in the Activity tab or on a "
                        "cow's profile.")
            lst = "\n".join(f"- {t} — {adg:+.2f} kg/day (now {w:,.0f} kg)"
                            for t, adg, w in leaders)
            return f"Best daily gain:\n{lst}"
        total_w = active_weight_sum()
        return (f"Weights: {n_weighed:,} animal(s) have weighings on record. "
                f"Active herd weight totals {total_w:,.0f} kg."
                + (f"\n\nBest daily gain: {leaders[0][0]} at "
                   f"{leaders[0][1]:+.2f} kg/day." if leaders else ""))

    # ── 3. Money: sales, purchases, expenses, profit ──
    if has("profit", "net", "balance", "made or spent", "bottom line"):
        _, rev = _qa_sum("sales", start, end)
        _, spend = _qa_sum("purchases", start, end)
        _, exp = _qa_sum("expenses", start, end, "amount")
        net = rev - spend - exp
        verdict = "ahead" if net >= 0 else "behind"
        return (f"Net position{when}: {money(net)} — you're {verdict}.\n"
                f"- Sales income: {money(rev)}\n"
                f"- Cattle purchases: {money(spend)}\n"
                f"- Running expenses: {money(exp)}")

    if has("sold", "sell", "sale", "sales", "income", "revenue", "earn", "made from"):
        n, rev = _qa_sum("sales", start, end)
        if not n:
            return f"No cattle sales are recorded{when or ' yet'}."
        if start:
            rows = c.execute("SELECT tag, buyer, price, date FROM sales "
                             "WHERE date BETWEEN ? AND ? ORDER BY price DESC LIMIT 5",
                             (start, end)).fetchall()
        else:
            rows = c.execute("SELECT tag, buyer, price, date FROM sales "
                             "ORDER BY price DESC LIMIT 5").fetchall()
        top = "\n".join(f"- {r[0]} to {r[1]} — {money(r[2])} ({r[3]})" for r in rows)
        avg = rev / n if n else 0
        return (f"{n} cattle sold{when}, earning {money(rev)} "
                f"(average {money(avg)} per head).\nTop sales:\n{top}")

    if has("bought", "buy", "purchase", "purchases"):
        n, spend = _qa_sum("purchases", start, end)
        if not n:
            return f"No cattle purchases are recorded{when or ' yet'}."
        if start:
            rows = c.execute("SELECT tag, seller, price, date FROM purchases "
                             "WHERE date BETWEEN ? AND ? ORDER BY price DESC LIMIT 5",
                             (start, end)).fetchall()
        else:
            rows = c.execute("SELECT tag, seller, price, date FROM purchases "
                             "ORDER BY price DESC LIMIT 5").fetchall()
        top = "\n".join(f"- {r[0]} from {r[1]} — {money(r[2])} ({r[3]})" for r in rows)
        return (f"{n} cattle bought{when}, costing {money(spend)} "
                f"(average {money(spend / n)} per head).\nRecent purchases:\n{top}")

    if has("expense", "spend", "spent", "cost", "feed", "vet", "labour", "labor",
           "transport", "medicine"):
        n, tot = _qa_sum("expenses", start, end, "amount")
        if not n:
            return f"No expenses are recorded{when or ' yet'}."
        if start:
            rows = c.execute("SELECT category, SUM(amount) FROM expenses "
                             "WHERE date BETWEEN ? AND ? GROUP BY category "
                             "ORDER BY SUM(amount) DESC", (start, end)).fetchall()
        else:
            rows = c.execute("SELECT category, SUM(amount) FROM expenses "
                             "GROUP BY category ORDER BY SUM(amount) DESC").fetchall()
        # If they named a category, answer just that one.
        for cat, amt in rows:
            if cat and cat.lower().split(" /")[0] in q:
                pct = (float(amt or 0) / tot * 100) if tot else 0.0
                return (f"{cat} cost {money(amt)}{when}, which is "
                        f"{pct:.0f}% of the {money(tot)} total.")
        brk = "\n".join(f"- {r[0]}: {money(r[1])}" for r in rows[:6])
        return (f"{n} expense entries{when}, totalling {money(tot)}.\n"
                f"Biggest categories:\n{brk}")

    # ── 3. Activities ─────────────────────────
    if has("activity", "activities", "vaccin", "dip", "dewor", "brand", "weigh",
           "treatment", "done"):
        if start:
            rows = c.execute("SELECT date, activity, tag_count, details FROM activities "
                             "WHERE date BETWEEN ? AND ? ORDER BY date DESC LIMIT 8",
                             (start, end)).fetchall()
        else:
            rows = c.execute("SELECT date, activity, tag_count, details FROM activities "
                             "ORDER BY date DESC LIMIT 8").fetchall()
        if not rows:
            return f"No farm activities are recorded{when or ' yet'}."
        lines = "\n".join(f"- {r[0]} — {r[1]} on {r[2]:,} cattle"
                          + (f" ({r[3]})" if r[3] else "") for r in rows)
        total = c.execute("SELECT COUNT(*) FROM activities"
                          + (" WHERE date BETWEEN ? AND ?" if start else ""),
                          (start, end) if start else ()).fetchone()[0]
        return f"{total} activit{'y' if total == 1 else 'ies'} recorded{when}.\n{lines}"

    # ── 4. Calendar: what happened on a day ───
    if has("happen", "calendar", "what was done", "events"):
        if not start:
            start = end = date.today().isoformat()
            when = " today"
        births = c.execute("SELECT COUNT(*) FROM cattle WHERE dob BETWEEN ? AND ?",
                           (start, end)).fetchone()[0]
        acq = c.execute("SELECT COUNT(*) FROM cattle WHERE date_acquired BETWEEN ? AND ?",
                        (start, end)).fetchone()[0]
        sales_n, sales_v = _qa_sum("sales", start, end)
        pur_n, pur_v = _qa_sum("purchases", start, end)
        acts = c.execute("SELECT COUNT(*) FROM activities WHERE date BETWEEN ? AND ?",
                         (start, end)).fetchone()[0]
        exp_n, exp_v = _qa_sum("expenses", start, end, "amount")
        return (f"Summary{when}:\n"
                f"- {births} born, {acq} acquired\n"
                f"- {sales_n} sold ({money(sales_v)}), {pur_n} bought ({money(pur_v)})\n"
                f"- {acts} farm activit{'y' if acts == 1 else 'ies'} recorded\n"
                f"- {exp_n} expense entries ({money(exp_v)})")

    # ── 5. Things to do ───────────────────────
    if has("to do", "todo", "task", "reminder", "due", "outstanding"):
        over, upc = overdue_reminders(), upcoming_reminders()
        if not over and not upc:
            return "Nothing outstanding — your to-do list is clear."
        lines = [f"- {r['date']} — {r['task']} (overdue)" for r in over]
        lines += [f"- {r['date']} — {r['task']}" for r in upc[:6]]
        return (f"{len(over) + len(upc)} task(s) outstanding"
                + (f", {len(over)} overdue" if over else "") + ":\n"
                + "\n".join(lines))

    # ── 6. Losses ─────────────────────────────
    if has("died", "death", "dead", "missing", "lost"):
        d = c.execute("SELECT COUNT(*) FROM losses WHERE type='Died'"
                      + (" AND date BETWEEN ? AND ?" if start else ""),
                      (start, end) if start else ()).fetchone()[0]
        mi = c.execute("SELECT COUNT(*) FROM losses WHERE type='Missing'"
                       + (" AND date BETWEEN ? AND ?" if start else ""),
                       (start, end) if start else ()).fetchone()[0]
        if not d and not mi:
            return f"No deaths or missing cattle recorded{when or ''}."
        rows = c.execute("SELECT date, type, tag, cause FROM losses "
                         + ("WHERE date BETWEEN ? AND ? " if start else "")
                         + "ORDER BY date DESC LIMIT 5",
                         (start, end) if start else ()).fetchall()
        lines = "\n".join(f"- {r[0]} — {r[1]}: {r[2]}"
                          + (f" ({r[3]})" if r[3] else "") for r in rows)
        return (f"{d} died and {mi} went missing{when}.\n{lines}")

    # ── 7. Births ─────────────────────────────
    if has("born", "birth", "calved", "calves", "calf"):
        if start:
            n = c.execute("SELECT COUNT(*) FROM cattle WHERE dob BETWEEN ? AND ?",
                          (start, end)).fetchone()[0]
        else:
            n = c.execute("SELECT COUNT(*) FROM cattle WHERE mother_tag != ''"
                          ).fetchone()[0]
        top = c.execute("SELECT mother_tag, COUNT(*) FROM cattle "
                        "WHERE mother_tag != '' GROUP BY mother_tag "
                        "ORDER BY COUNT(*) DESC LIMIT 3").fetchall()
        extra = ("\nMost productive cows: "
                 + ", ".join(f"{r[0]} ({r[1]})" for r in top)) if top else ""
        return f"{n} calves recorded{when}.{extra}"

    # ── 8. Herd composition / counts ──────────
    if has("how many", "count", "total", "herd", "cattle", "cows", "breed", "sex",
           "category", "status", "inventory", "stock"):
        total, active, fem, male = herd_metrics()
        for word, col in (("breed", "breed"), ("categor", "category"),
                          ("sex", "sex"), ("status", "status")):
            if word in q:
                counts = group_counts(col, active_only=(col != "status"))
                if not counts:
                    return f"No {col} information recorded yet."
                brk = "\n".join(f"- {k}: {v:,}" for k, v in
                                sorted(counts.items(), key=lambda x: -x[1]))
                scope = "whole herd" if col == "status" else "active cattle"
                return (f"By {col} ({scope}):\n{brk}\n\nTotal "
                        f"{sum(counts.values()):,}.")
        for label, status in (("sold", "Sold"), ("active", "Active"),
                              ("deceased", "Deceased"), ("missing", "Missing")):
            if label in q:
                n = status_count(status)
                return f"{n:,} cattle are recorded as {status}."
        if "female" in q or "cow" in q and "how many" in q:
            return f"You have {fem:,} females ({active:,} active cattle in total)."
        if "male" in q or "bull" in q:
            return f"You have {male:,} males ({active:,} active cattle in total)."
        wt = active_weight_sum()
        return (f"Herd summary: {total:,} cattle on record — {active:,} active "
                f"({fem:,} female, {male:,} male)."
                + (f" Active herd weight: {wt:,.0f} kg." if wt else ""))

    # ── Fallback ──────────────────────────────
    total, active, _, _ = herd_metrics()
    _, rev = _qa_sum("sales", None, None)
    _, exp = _qa_sum("expenses", None, None, "amount")
    return ("I couldn't match that question, so here's an overview:\n"
            f"- Herd: {total:,} on record, {active:,} active\n"
            f"- Sales income: {money(rev)} · Expenses: {money(exp)}\n\n"
            "Try asking things like *how many cattle do I have*, *how much did I "
            "spend on feed this month*, *what did I sell in July*, *what activities "
            "were done yesterday*, or simply type a tag number or a "
            "buyer/seller's name.")


def _md_to_bubble_html(text):
    """Render the assistant's markdown answer safely inside a chat bubble."""
    out, in_list = [], False
    for raw in str(text).splitlines():
        line = _html.escape(raw.strip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", line)
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            if line:
                out.append(f"<div>{line}</div>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


# How soon a task has to be for the card to start flashing.
URGENT_WITHIN_DAYS = 5


def countdown_label(d_iso, today=None):
    """The words shown on the countdown badge.

    Counts down whole days: 5, 4, 3, 2, 1, then "Last day" on the day itself,
    then how late it is once the day has passed.
    """
    n = days_until(d_iso, today)
    if n is None:
        return ""
    if n == 0:
        return "Last day"
    if n < 0:
        return ("1 day late" if n == -1 else f"{abs(n)} days late")
    return ("1 day left" if n == 1 else f"{n} days left")


def urgent_reminders(within_days=URGENT_WITHIN_DAYS):
    """Tasks that are overdue, or fall due within the next few days.

    Overdue counts as urgent: a job that should already have been done is
    more pressing than one due on Friday, not less.
    """
    today = date.today()
    horizon = today + timedelta(days=max(0, within_days))
    urgent = list(overdue_reminders(limit=50))
    for r in upcoming_reminders(limit=50):
        try:
            due = datetime.fromisoformat(r["date"]).date()
        except (ValueError, TypeError):
            continue
        if today <= due <= horizon:
            urgent.append(r)
    return urgent


def todo_card_html(limit=4):
    """The 'Things to do' card, listing the actual tasks (overdue first)."""
    over = overdue_reminders(limit=50)
    upc = upcoming_reminders(limit=50)
    items = [(r, True) for r in over] + [(r, False) for r in upc]
    total = len(items)

    n_urgent = len(urgent_reminders())
    card_cls = "todo-card urgent" if n_urgent else "todo-card"
    # A blinking lamp beside the heading, so the warning still reads on a
    # screen where a soft glow is easy to miss.
    lamp = '<span class="todo-lamp"></span>' if n_urgent else ""

    # The title sits above the card, not inside it, so the panel itself
    # holds nothing but the tasks.
    head = (f'<div class="todo-title"><span>{lamp}Things to do</span>'
            f'<span class="todo-count">{total}</span></div>')
    if not items:
        return ('<div class="todo-block">' + head +
                '<div class="todo-card">'
                '<div class="todo-empty">Nothing scheduled. Add reminders on any '
                'day in the Calendar tab.</div></div></div>')

    rows = []
    for r, is_over in items[:limit]:
        task = _html.escape(r["task"] or "")
        if r["priority"] == "High":
            task = f"<b>{task}</b>"
        cls = "todo-when over" if is_over else "todo-when"

        # A countdown badge, but only while the task is inside the window —
        # a job three weeks out does not need a number ticking beside it.
        badge = ""
        n = days_until(r["date"])
        if n is not None and n <= URGENT_WITHIN_DAYS:
            # Step 0 is the day itself and step 5 the far edge of the window;
            # the CSS reddens as the number falls.
            step = "late" if n < 0 else str(max(0, min(URGENT_WITHIN_DAYS, n)))
            badge = (f'<span class="todo-cd cd-{step}">'
                     f'{_html.escape(countdown_label(r["date"]))}</span>')

        rows.append(f'<div class="todo-item"><span class="{cls}">'
                    f'{_html.escape(_when_label(r["date"]))}</span> '
                    f'{task}{badge}</div>')

    more = ""
    if total > limit:
        more = f'<div class="todo-more">+{total - limit} more</div>'
    if n_urgent:
        # Say why the card is lit, so it is information and not decoration,
        # and lead with the one that runs out first.
        nearest = min((days_until(r["date"]) for r in urgent_reminders()
                       if days_until(r["date"]) is not None), default=None)
        if nearest is None:
            lead = ""
        elif nearest < 0:
            lead = f"Overdue by {abs(nearest)} day" + ("" if nearest == -1 else "s")
        elif nearest == 0:
            lead = "Due today — last day"
        else:
            lead = (f"{nearest} day" + ("" if nearest == 1 else "s")
                    + " until the next one is due")
        # A bar that empties as the days run down.
        pct = 0 if nearest is None else max(
            0, min(100, round((max(0, nearest) / URGENT_WITHIN_DAYS) * 100)))
        more += (f'<div class="todo-cdbar"><span style="width:{pct}%"></span></div>'
                 f'<div class="todo-more"><b style="color:{DANGER}">{lead}</b>'
                 f' · {n_urgent} within {URGENT_WITHIN_DAYS} days</div>')
    return ('<div class="todo-block">' + head
            + f'<div class="{card_cls}">' + "".join(rows) + more
            + '</div></div>')


def day_report_pdf(sel_iso, births, acquisitions, logged, day_acts=None,
                   day_rems=None):
    """Build a one-page PDF activity report for a single day. Returns bytes,
    or None if reportlab isn't installed."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                         TableStyle, Image as RLImage)
    except Exception:
        return None

    pretty = datetime.fromisoformat(sel_iso).strftime("%A, %d %B %Y")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title=f"Activity report {sel_iso}")
    ss = getSampleStyleSheet()
    teal, muted, grid, light = (colors.HexColor(PRIMARY), colors.HexColor(MUTED),
                                colors.HexColor(GRID), colors.HexColor(LIGHT_BG))
    h1 = ParagraphStyle("h1", parent=ss["Title"], textColor=teal, fontSize=18,
                        alignment=0, spaceAfter=0)
    sub = ParagraphStyle("sub", parent=ss["Normal"], textColor=muted, fontSize=9)
    sect = ParagraphStyle("sect", parent=ss["Heading2"], textColor=teal, fontSize=12,
                          spaceBefore=12, spaceAfter=4)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=10, leading=13)
    foot = ParagraphStyle("foot", parent=ss["Normal"], textColor=muted, fontSize=8)

    story = []
    _p = get_farm_profile()
    _fname = _html.escape(_p["farm_name"])
    _details = "<br/>".join(_html.escape(x) for x in farm_detail_lines(_p))
    _hdr_html = (f"<b>{_fname}</b><br/><font size=10 color='#5E7373'>"
                 "Daily Activity Report</font>"
                 + (f"<br/><font size=8 color='#5E7373'>{_details}</font>"
                    if _details else ""))
    try:
        logo, _lw = report_logo(RLImage, mm)
        header = Table([[logo, Paragraph(_hdr_html, h1)]],
                       colWidths=[_lw * mm, (168 - _lw) * mm])
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(header)
    except Exception:
        story.append(Paragraph(_hdr_html, h1))
    story.append(Spacer(1, 4))
    story.append(Paragraph(pretty, sub))
    story.append(Spacer(1, 6))

    def _esc(v):
        # Paragraph parses mini-HTML, so raw & or < in notes would break the build.
        return _html.escape(str(v if v not in (None, "") else "—"))

    def section(title, headers, rows):
        story.append(Paragraph(title, sect))
        if not rows:
            story.append(Paragraph("None recorded.", cell))
            return
        data = [[Paragraph(f"<b>{_html.escape(h)}</b>", cell) for h in headers]]
        for r in rows:
            data.append([Paragraph(_esc(x), cell) for x in r])
        widths = {2: [45 * mm, 129 * mm],
                  3: [40 * mm, 40 * mm, 94 * mm],
                  4: [34 * mm, 30 * mm, 58 * mm, 52 * mm]}[len(headers)]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), light),
            ("TEXTCOLOR", (0, 0), (-1, 0), teal),
            ("GRID", (0, 0), (-1, -1), 0.5, grid),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story.append(t)

    def _tags_text(a):
        """Readable list of the tag numbers an activity was applied to."""
        if a["scope"] == "all":
            return "All cattle in the herd"
        if a["scope"] == "active":
            return "All active cattle"
        tags = [t for t in (a["tags"] or "").split(",") if t]
        if not tags:
            return "—"
        shown = ", ".join(tags[:60])
        if len(tags) > 60:
            shown += f"  (+{len(tags) - 60} more)"
        return shown

    section(T("Calves born"), ["Tag", "Name"],
            [[c["tag"], c["name"] or "—"] for c in births])
    section(T("Cattle acquired"), ["Tag", "Name"],
            [[c["tag"], c["name"] or "—"] for c in acquisitions])
    section("Farm activities", ["Activity", T("Cattle affected"), "Tag numbers",
                                "Details"],
            [[a["activity"], f"{a['tag_count']:,}", _tags_text(a),
              a["details"] or "—"] for a in (day_acts or [])])
    section("Logged activities", ["Type", "Tag", "Note"],
            [[e["type"], e["tag"] or "—", e["note"] or "—"] for e in logged])
    section(T("Calvings expected"), [SP["One"], "Served", "Sire"],
            [[d["cow_tag"], d["service_date"] or "—", d["bull_tag"] or "—"]
             for d in due_between(sel_iso, sel_iso)])
    section("Things to do", ["Task", "Priority", "Status"],
            [[r["task"] + (f" — {r['details']}" if r["details"] else ""),
              r["priority"] or "Normal",
              "Done" if r["done"] else "Outstanding"]
             for r in (day_rems or [])])

    story.append(Spacer(1, 18))
    story.append(Paragraph(
        T(f"Generated {now_local().strftime('%Y-%m-%d %H:%M')} · {_fname} "
        "Cattle &amp; Small Stock System · Powered by Health Data Matrics "
        "(HDM Group)"), foot))
    doc.build(story)
    return buf.getvalue()


def herdiq_answer_pdf(question, answer, conversation=None):
    """Branded PDF of a HerdIQ answer (optionally the whole conversation)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                         TableStyle, Image as RLImage, HRFlowable,
                                         ListFlowable, ListItem)
    except Exception:
        return None

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title="HerdIQ report")
    ss = getSampleStyleSheet()
    teal, muted, grid, light = (colors.HexColor(PRIMARY), colors.HexColor(MUTED),
                                colors.HexColor(GRID), colors.HexColor(LIGHT_BG))
    h1 = ParagraphStyle("h1", parent=ss["Title"], textColor=teal, fontSize=17,
                        alignment=0, spaceAfter=0, leading=20)
    qst = ParagraphStyle("qst", parent=ss["Normal"], fontSize=11.5, leading=15,
                         textColor=colors.white)
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=10, leading=14.5,
                          textColor=colors.HexColor(INK))
    strong = ParagraphStyle("strong", parent=body, fontSize=11, leading=16)
    sub = ParagraphStyle("sub", parent=ss["Normal"], textColor=muted, fontSize=9)
    foot = ParagraphStyle("foot", parent=ss["Normal"], textColor=muted, fontSize=8)

    p = get_farm_profile()
    fname = _html.escape(p["farm_name"])
    details = "<br/>".join(_html.escape(x) for x in farm_detail_lines(p))
    hdr_html = (f"<b>{fname}</b><br/><font size=10 color='#5E7373'>"
                "HerdIQ Assistant — data report</font>"
                + (f"<br/><font size=8 color='#5E7373'>{details}</font>"
                   if details else ""))
    story = []
    try:
        logo, _lw = report_logo(RLImage, mm)
        head = Table([[logo, Paragraph(hdr_html, h1)]],
                     colWidths=[_lw * mm, (168 - _lw) * mm])
        head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(head)
    except Exception:
        story.append(Paragraph(hdr_html, h1))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=2, color=teal, spaceAfter=10))

    def render_pair(q_text, a_text):
        qbox = Table([[Paragraph(_html.escape(q_text), qst)]], colWidths=[174 * mm])
        qbox.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), teal),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        story.append(qbox)
        story.append(Spacer(1, 8))
        bullets = []
        for raw in str(a_text).splitlines():
            line = raw.strip()
            if not line:
                continue
            esc = _html.escape(line)
            esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
            esc = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", esc)
            if line.startswith("- "):
                bullets.append(ListItem(Paragraph(esc[2:], body), leftIndent=12))
                continue
            if bullets:
                story.append(ListFlowable(bullets, bulletType="bullet",
                                          start="•", leftIndent=14))
                story.append(Spacer(1, 4))
                bullets = []
            if line.startswith("---"):
                story.append(HRFlowable(width="100%", thickness=0.5, color=grid,
                                        spaceBefore=6, spaceAfter=6))
            else:
                story.append(Paragraph(esc, strong if "<b>" in esc else body))
        if bullets:
            story.append(ListFlowable(bullets, bulletType="bullet", start="•",
                                      leftIndent=14))
        story.append(Spacer(1, 12))

    if conversation:
        pending_q = None
        for role, text in conversation:
            if role == "user":
                pending_q = text
            else:
                render_pair(pending_q or "Question", text)
                pending_q = None
    else:
        render_pair(question, answer)

    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=0.5, color=grid, spaceAfter=6))
    story.append(Paragraph(
        T(f"Generated {now_local().strftime('%Y-%m-%d %H:%M')} by HerdIQ Assistant · "
        f"{fname} Cattle &amp; Small Stock System · Powered by Health Data "
        "Matrics (HDM Group)"), foot))
    doc.build(story)
    return buf.getvalue()


def inventory_pdf():
    """Full herd inventory report: headline figures, breakdown tables and charts."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                         TableStyle, Image as RLImage, HRFlowable,
                                         KeepTogether)
        from reportlab.graphics.shapes import Drawing, String
        from reportlab.graphics.charts.barcharts import VerticalBarChart
    except Exception:
        return None

    teal, teal2 = colors.HexColor(PRIMARY), colors.HexColor(TEAL2)
    muted, grid = colors.HexColor(MUTED), colors.HexColor(GRID)
    light, ink = colors.HexColor(LIGHT_BG), colors.HexColor(INK)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=15 * mm, bottomMargin=14 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title="Herd inventory report")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], textColor=teal, fontSize=17,
                        alignment=0, spaceAfter=0, leading=20)
    sect = ParagraphStyle("sect", parent=ss["Heading2"], textColor=teal, fontSize=12,
                          spaceBefore=10, spaceAfter=4)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=9.5, leading=12.5,
                          textColor=ink)
    sub = ParagraphStyle("sub", parent=ss["Normal"], textColor=muted, fontSize=9)
    foot = ParagraphStyle("foot", parent=ss["Normal"], textColor=muted, fontSize=8)
    klabel = ParagraphStyle("klabel", parent=ss["Normal"], fontSize=6.8,
                            textColor=muted, leading=8.5)
    kvalue = ParagraphStyle("kvalue", parent=ss["Normal"], fontSize=14,
                            textColor=teal, leading=16)

    story = []

    # ── header with farm details ──────────────
    p = get_farm_profile()
    fname = _html.escape(p["farm_name"])
    details = "<br/>".join(_html.escape(x) for x in farm_detail_lines(p))
    hdr = (f"<b>{fname}</b><br/><font size=10 color='#5E7373'>Herd Inventory "
           "Report</font>" + (f"<br/><font size=8 color='#5E7373'>{details}</font>"
                              if details else ""))
    try:
        logo, _lw = report_logo(RLImage, mm)
        t = Table([[logo, Paragraph(hdr, h1)]],
                  colWidths=[_lw * mm, (174 - _lw) * mm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(t)
    except Exception:
        story.append(Paragraph(hdr, h1))
    story.append(Spacer(1, 3))
    story.append(Paragraph(f"As at {date.today().strftime('%d %B %Y')}", sub))
    story.append(HRFlowable(width="100%", thickness=2, color=teal, spaceBefore=6,
                            spaceAfter=10))

    # ── headline figures (the KPI cards) ──────
    total_all, active_n, fem_all, male_all = herd_metrics()
    sold_n = status_count("Sold")
    dec_n = status_count("Deceased")
    miss_n = status_count("Missing")
    fem_a, male_a = active_sex_counts()
    wt = active_weight_sum()

    def kcard(label, value):
        return Table([[Paragraph(label.upper(), klabel)],
                      [Paragraph(f"<b>{value}</b>", kvalue)]], colWidths=[32 * mm])

    def krow(cards):
        t = Table([cards], colWidths=[34.8 * mm] * len(cards))
        style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("TOPPADDING", (0, 0), (-1, -1), 6),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                 ("LEFTPADDING", (0, 0), (-1, -1), 7)]
        for i in range(len(cards)):
            style += [("BOX", (i, 0), (i, 0), 0.6, grid),
                      ("LINEBEFORE", (i, 0), (i, 0), 2.5, teal),
                      ("BACKGROUND", (i, 0), (i, 0), colors.white)]
        t.setStyle(TableStyle(style))
        return t

    story.append(krow([kcard("Total on record", f"{total_all:,}"),
                       kcard("Active head", f"{active_n:,}"),
                       kcard("Sold", f"{sold_n:,}"),
                       kcard("Deceased", f"{dec_n:,}"),
                       kcard("Missing", f"{miss_n:,}")]))
    story.append(Spacer(1, 5))
    story.append(krow([kcard("Females (active)", f"{fem_a:,}"),
                       kcard("Males (active)", f"{male_a:,}"),
                       kcard("Active herd weight",
                             f"{wt:,.0f} kg" if wt else "—")]))
    story.append(Spacer(1, 4))
    story.append(Paragraph("Breakdowns below count active head only — your current "
                           "stock on hand.", sub))

    # ── chart drawing with values inside the bars ──
    def bar_drawing(labels, values, bar_colour, width=168 * mm, height=46 * mm):
        n = max(1, len(values))
        d = Drawing(width, height)
        ch = VerticalBarChart()
        ch.x, ch.y = 6, 16
        ch.width, ch.height = width - 14, height - 26
        ch.data = [values]
        ch.categoryAxis.categoryNames = labels
        ch.categoryAxis.labels.fontName = "Helvetica"
        ch.categoryAxis.labels.fontSize = 7.5
        ch.categoryAxis.labels.dy = -4
        ch.categoryAxis.strokeColor = grid
        ch.valueAxis.visible = False
        ch.valueAxis.valueMin = 0
        top = max(values) if values and max(values) > 0 else 1
        ch.valueAxis.valueMax = top
        ch.bars[0].fillColor = bar_colour
        ch.bars[0].strokeColor = None
        ch.groupSpacing = 6
        ch.barSpacing = 2
        d.add(ch)
        # Value labels: inside tall bars, just above short ones.
        slot = ch.width / float(n)
        for i, v in enumerate(values):
            cx = ch.x + slot * (i + 0.5)
            bar_h = (v / float(top)) * ch.height if top else 0
            txt = f"{v:,.0f}"
            if bar_h >= 16:
                s = String(cx, ch.y + bar_h - 11, txt, textAnchor="middle")
                s.fillColor = colors.white
            else:
                s = String(cx, ch.y + bar_h + 3, txt, textAnchor="middle")
                s.fillColor = teal
            s.fontName = "Helvetica-Bold"
            s.fontSize = 8
            d.add(s)
        return d

    def section(title, counts, bar_colour, scope_note, col_label):
        if not counts:
            return
        items = sorted(counts.items(), key=lambda x: -x[1])
        total = sum(v for _, v in items) or 1
        data = [[Paragraph(f"<b>{_html.escape(col_label)}</b>", cell),
                 Paragraph("<b>Head</b>", cell), Paragraph("<b>Share</b>", cell)]]
        for k, v in items:
            data.append([Paragraph(_html.escape(str(k)), cell),
                         Paragraph(f"{v:,}", cell),
                         Paragraph(f"{v / total * 100:.1f}%", cell)])
        data.append([Paragraph("<b>Total</b>", cell),
                     Paragraph(f"<b>{total:,}</b>", cell), Paragraph("", cell)])
        tbl = Table(data, colWidths=[86 * mm, 44 * mm, 44 * mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), light),
            ("TEXTCOLOR", (0, 0), (-1, 0), teal),
            ("GRID", (0, 0), (-1, -1), 0.5, grid),
            ("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), light),
            ("TEXTCOLOR", (0, len(data) - 1), (-1, len(data) - 1), teal),
            ("LINEABOVE", (0, len(data) - 1), (-1, len(data) - 1), 1.2, teal),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        chart = bar_drawing([str(k)[:14] for k, _ in items],
                            [float(v) for _, v in items], bar_colour)
        story.append(KeepTogether([Paragraph(title, sect), Paragraph(scope_note, sub),
                                   Spacer(1, 4), tbl, Spacer(1, 6), chart]))

    section("By category", group_counts("category", active_only=True), teal,
            "Active cattle", "Category")
    section("By sex", group_counts("sex", active_only=True), teal, "Active cattle",
            "Sex")
    section("By breed", group_counts("breed", active_only=True), teal,
            "Active cattle", "Breed")
    section("By status", group_counts("status", active_only=False), teal2,
            "Whole herd, including sold, deceased and missing", "Status")

    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=grid, spaceAfter=6))
    story.append(Paragraph(
        T(f"Generated {now_local().strftime('%Y-%m-%d %H:%M')} · {fname} Cattle "
        "&amp; Small Stock System · Powered by Health Data Matrics "
        "(HDM Group)"), foot))
    doc.build(story)
    return buf.getvalue()


def sales_by_invoice(invoice_no):
    """All animals sold under one invoice number."""
    c = get_conn()
    rows = c.execute("SELECT * FROM sales WHERE invoice_no=? ORDER BY id",
                     (invoice_no,)).fetchall()
    return [dict(r) for r in rows]


def invoice_summaries(limit=300):
    """One row per invoice: head count and total, newest first."""
    c = get_conn()
    rows = c.execute(
        "SELECT invoice_no, MIN(date) AS date, buyer, COUNT(*) AS head, "
        "SUM(price) AS total, MAX(id) AS last_id FROM sales "
        "GROUP BY invoice_no ORDER BY last_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _draw_invoice_watermark(canvas, doc):
    """A single, large, very light diagonal farm-name mark across the page.

    Drawn before the invoice content, so every figure stays fully legible while
    a photocopy or re-typed forgery is obvious.
    """
    from reportlab.lib import colors
    text = (getattr(doc, "_wm_text", "") or "FARM").upper()
    subtext = getattr(doc, "_wm_sub", "") or ""
    pw, ph = doc.pagesize
    canvas.saveState()
    canvas.translate(pw / 2.0, ph / 2.0)
    canvas.rotate(38)

    # Scale the text so it spans most of the page diagonal, whatever its length.
    target = ((pw ** 2 + ph ** 2) ** 0.5) * 0.60
    size = 64.0
    width_at_64 = canvas.stringWidth(text, "Helvetica-Bold", 64) or 1
    size = max(22.0, min(76.0, 64.0 * target / width_at_64))

    # NOTE: setFillColor() resets any alpha set with setFillAlpha(), so the
    # transparency has to be carried by the colour itself.
    base = colors.HexColor(PRIMARY)
    try:
        wm_colour = colors.Color(base.red, base.green, base.blue, alpha=0.09)
    except Exception:
        wm_colour = colors.HexColor("#E6F3F3")   # very light tint fallback
    canvas.setFillColor(wm_colour)
    canvas.setFont("Helvetica-Bold", size)
    canvas.drawCentredString(0, -size * 0.30, text)

    if subtext:
        canvas.setFont("Helvetica-Bold", max(9.0, size * 0.20))
        canvas.drawCentredString(0, -size * 0.95, subtext)

    canvas.restoreState()


def sale_invoice_pdf(invoice_no):
    """Watermarked invoice covering every animal on this invoice number."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                         TableStyle, Image as RLImage, HRFlowable)
    except Exception:
        return None

    items = sales_by_invoice(invoice_no)
    if not items:
        return None
    first = items[0]

    teal, muted = colors.HexColor(PRIMARY), colors.HexColor(MUTED)
    grid, light, ink = (colors.HexColor(GRID), colors.HexColor(LIGHT_BG),
                        colors.HexColor(INK))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=15 * mm, bottomMargin=16 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title=f"Invoice {invoice_no}")
    p = get_farm_profile()
    doc._wm_text = p["farm_name"]
    doc._wm_sub = f"ORIGINAL · {invoice_no}"

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], textColor=teal, fontSize=17,
                        alignment=0, spaceAfter=0, leading=20)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=9.5, leading=12.5,
                          textColor=ink)
    right = ParagraphStyle("right", parent=cell, alignment=2)
    sub = ParagraphStyle("sub", parent=ss["Normal"], textColor=muted, fontSize=9)
    foot = ParagraphStyle("foot", parent=ss["Normal"], textColor=muted, fontSize=8)

    fname = _html.escape(p["farm_name"])
    details = "<br/>".join(_html.escape(x) for x in farm_detail_lines(p))
    hdr = (f"<b>{fname}</b><br/><font size=10 color='#5E7373'>Livestock sale "
           "invoice</font>" + (f"<br/><font size=8 color='#5E7373'>{details}</font>"
                               if details else ""))
    story = []
    try:
        logo, _lw = report_logo(RLImage, mm)
        t = Table([[logo, Paragraph(hdr, h1),
                    Paragraph(f"<b><font size=13 color='#006868'>{_html.escape(invoice_no)}"
                              f"</font></b><br/><font size=8 color='#5E7373'>Date: "
                              f"{_html.escape(first['date'] or '')}</font>", right)]],
                   colWidths=[_lw * mm, (124 - _lw) * mm, 50 * mm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(t)
    except Exception:
        story.append(Paragraph(hdr, h1))
    story.append(HRFlowable(width="100%", thickness=2, color=teal, spaceBefore=6,
                            spaceAfter=10))

    story.append(Paragraph(
        f"<b>Billed to:</b> {_html.escape(first['buyer'] or '—')}"
        + (f"<br/><font color='#5E7373'>{_html.escape(first['buyer_contact'])}</font>"
           if first["buyer_contact"] else ""), cell))
    story.append(Spacer(1, 10))

    data = [[Paragraph("<b>#</b>", cell), Paragraph("<b>Tag</b>", cell),
             Paragraph("<b>Description</b>", cell),
             Paragraph("<b>Weight</b>", right), Paragraph("<b>Amount</b>", right)]]
    total = 0.0
    total_wt = 0.0
    for i, it in enumerate(items, 1):
        total += float(it["price"] or 0)
        total_wt += float(it["weight"] or 0)
        desc = (SP["One"] + " — " + it["cow_name"]) if it["cow_name"] \
            else SP["One"]
        data.append([Paragraph(str(i), cell),
                     Paragraph(_html.escape(it["tag"] or "—"), cell),
                     Paragraph(_html.escape(desc), cell),
                     Paragraph(f"{it['weight']:,.0f} kg" if it["weight"] else "—",
                               right),
                     Paragraph(money(it["price"]), right)])
    data.append([Paragraph("", cell), Paragraph("", cell),
                 Paragraph(f"<b>{len(items)} head</b>", cell),
                 Paragraph(f"<b>{total_wt:,.0f} kg</b>" if total_wt else "", right),
                 Paragraph(f"<b>{money(total)}</b>", right)])
    tbl = Table(data, colWidths=[10 * mm, 30 * mm, 74 * mm, 30 * mm, 30 * mm],
                repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), light),
        ("TEXTCOLOR", (0, 0), (-1, 0), teal),
        ("GRID", (0, 0), (-1, -1), 0.5, grid),
        ("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), light),
        ("LINEABOVE", (0, len(data) - 1), (-1, len(data) - 1), 1.2, teal),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(tbl)
    story.append(Spacer(1, 10))

    story.append(Paragraph(
        f"<b><font size=13 color='#006868'>Total due: {money(total)}</font></b>", right))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"Payment method: {_html.escape(first['payment_method'] or '—')}", sub))
    if first["notes"]:
        story.append(Spacer(1, 4))
        story.append(Paragraph("<b>Transaction notes:</b> "
                               + _html.escape(first["notes"]), cell))

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=grid, spaceAfter=6))
    story.append(Paragraph(
        T(f"Invoice {_html.escape(invoice_no)} · generated "
        f"{now_local().strftime('%Y-%m-%d %H:%M')} · {fname} Cattle Management "
        "&amp; Inventory System · Powered by Health Data Matrics (HDM Group). "
        "This document carries a printed watermark; copies without it are not valid."),
        foot))
    doc.build(story, onFirstPage=_draw_invoice_watermark,
              onLaterPages=_draw_invoice_watermark)
    return buf.getvalue()


def purchase_receipt_pdf(pur):
    """Watermarked purchase receipt, styled to match the sale invoice."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                         TableStyle, Image as RLImage, HRFlowable)
    except Exception:
        return None

    if not pur:
        return None

    teal, muted = colors.HexColor(PRIMARY), colors.HexColor(MUTED)
    grid, light, ink = (colors.HexColor(GRID), colors.HexColor(LIGHT_BG),
                        colors.HexColor(INK))
    rno = pur["receipt_no"] or "RECEIPT"
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=15 * mm, bottomMargin=16 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title=f"Receipt {rno}")
    p = get_farm_profile()
    doc._wm_text = p["farm_name"]
    doc._wm_sub = f"ORIGINAL · {rno}"

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], textColor=teal, fontSize=17,
                        alignment=0, spaceAfter=0, leading=20)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=9.5, leading=12.5,
                          textColor=ink)
    right = ParagraphStyle("right", parent=cell, alignment=2)
    sub = ParagraphStyle("sub", parent=ss["Normal"], textColor=muted, fontSize=9)
    foot = ParagraphStyle("foot", parent=ss["Normal"], textColor=muted, fontSize=8)

    fname = _html.escape(p["farm_name"])
    details = "<br/>".join(_html.escape(x) for x in farm_detail_lines(p))
    hdr = (f"<b>{fname}</b><br/><font size=10 color='#5E7373'>Livestock purchase "
           "receipt</font>" + (f"<br/><font size=8 color='#5E7373'>{details}</font>"
                               if details else ""))
    story = []
    try:
        logo, _lw = report_logo(RLImage, mm)
        t = Table([[logo, Paragraph(hdr, h1),
                    Paragraph(f"<b><font size=13 color='#006868'>{_html.escape(rno)}"
                              f"</font></b><br/><font size=8 color='#5E7373'>Date: "
                              f"{_html.escape(pur['date'] or '')}</font>", right)]],
                   colWidths=[_lw * mm, (124 - _lw) * mm, 50 * mm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(t)
    except Exception:
        story.append(Paragraph(hdr, h1))
    story.append(HRFlowable(width="100%", thickness=2, color=teal, spaceBefore=6,
                            spaceAfter=10))

    story.append(Paragraph(
        f"<b>Purchased from:</b> {_html.escape(pur['seller'] or '—')}"
        + (f"<br/><font color='#5E7373'>{_html.escape(pur['seller_contact'])}</font>"
           if pur["seller_contact"] else ""), cell))
    story.append(Spacer(1, 10))

    desc = (SP["One"] + " — " + pur["cow_name"]) if pur["cow_name"] \
        else SP["One"]
    extra = " · ".join(x for x in [pur.get("breed") or "", pur.get("sex") or ""] if x)
    if extra:
        desc += f" ({_html.escape(extra)})"
    total = float(pur["price"] or 0)
    wt = float(pur["weight"] or 0)
    data = [[Paragraph("<b>#</b>", cell), Paragraph("<b>Tag</b>", cell),
             Paragraph("<b>Description</b>", cell),
             Paragraph("<b>Weight</b>", right), Paragraph("<b>Amount</b>", right)],
            [Paragraph("1", cell), Paragraph(_html.escape(pur["tag"] or "—"), cell),
             Paragraph(desc, cell),
             Paragraph(f"{wt:,.0f} kg" if wt else "—", right),
             Paragraph(money(total), right)],
            [Paragraph("", cell), Paragraph("", cell),
             Paragraph("<b>1 head</b>", cell),
             Paragraph(f"<b>{wt:,.0f} kg</b>" if wt else "", right),
             Paragraph(f"<b>{money(total)}</b>", right)]]
    tbl = Table(data, colWidths=[10 * mm, 30 * mm, 74 * mm, 30 * mm, 30 * mm],
                repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), light),
        ("TEXTCOLOR", (0, 0), (-1, 0), teal),
        ("GRID", (0, 0), (-1, -1), 0.5, grid),
        ("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), light),
        ("LINEABOVE", (0, len(data) - 1), (-1, len(data) - 1), 1.2, teal),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(tbl)
    story.append(Spacer(1, 10))

    story.append(Paragraph(
        f"<b><font size=13 color='#006868'>Total paid: {money(total)}</font></b>", right))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"Payment method: {_html.escape(pur['payment_method'] or '—')}", sub))
    if pur["notes"]:
        story.append(Spacer(1, 4))
        story.append(Paragraph("<b>Transaction notes:</b> "
                               + _html.escape(pur["notes"]), cell))

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=grid, spaceAfter=6))
    story.append(Paragraph(
        T(f"Receipt {_html.escape(rno)} · generated "
        f"{now_local().strftime('%Y-%m-%d %H:%M')} · {fname} Cattle Management "
        "&amp; Inventory System · Powered by Health Data Matrics (HDM Group). "
        "This document carries a printed watermark; copies without it are not valid."),
        foot))
    doc.build(story, onFirstPage=_draw_invoice_watermark,
              onLaterPages=_draw_invoice_watermark)
    return buf.getvalue()


def update_invoice_header(invoice_no, buyer, contact, date_iso, method, notes):
    """Update the buyer/date/payment details on every line of an invoice."""
    c = get_conn()
    old = c.execute("SELECT MIN(date) FROM sales WHERE invoice_no=?",
                    (invoice_no,)).fetchone()[0]
    c.execute("UPDATE sales SET buyer=?, buyer_contact=?, date=?, payment_method=?, "
              "notes=? WHERE invoice_no=?",
              (buyer.strip(), contact.strip(), date_iso, method, notes.strip(),
               invoice_no))
    c.commit()
    # Keep the calendar entries in step if the sale date moved.
    if old and old != date_iso:
        for row in c.execute("SELECT tag FROM sales WHERE invoice_no=?",
                             (invoice_no,)).fetchall():
            c.execute("UPDATE events SET date=? WHERE tag=? AND type='Cow sold'",
                      (date_iso, row[0]))
        c.commit()


def update_sale_line(sale_id, price, weight):
    c = get_conn()
    c.execute("UPDATE sales SET price=?, weight=? WHERE id=?",
              (float(price), float(weight) if weight else None, sale_id))
    c.commit()


def remove_sale_line(sale_id, reactivate=True):
    """Take one animal off an invoice and (optionally) return it to the herd."""
    c = get_conn()
    row = c.execute("SELECT tag FROM sales WHERE id=?", (sale_id,)).fetchone()
    tag = row[0] if row else None
    c.execute("DELETE FROM sales WHERE id=?", (sale_id,))
    c.commit()
    if tag and reactivate:
        cow = get_cow(tag)
        if cow:
            update_cow(tag, {**cow, "status": "Active"})
        c.execute("DELETE FROM events WHERE tag=? AND type='Cow sold'", (tag,))
        c.commit()
    return tag


# ── breeding & calving helpers ────────────────
def expected_due(service_date):
    """Expected calving date = service date + average gestation."""
    try:
        d = datetime.fromisoformat(service_date).date()
    except Exception:
        return ""
    return (d + timedelta(days=GESTATION_DAYS)).isoformat()


def add_service(rec):
    c = get_conn()
    c.execute("INSERT INTO breedings(cow_tag, bull_tag, service_date, method, status, "
              "due_date, notes, created_at) VALUES(?,?,?,?,'Served',?,?,?)",
              (rec["cow_tag"], rec["bull_tag"], rec["service_date"], rec["method"],
               expected_due(rec["service_date"]), rec["notes"],
               now_local().isoformat(timespec="seconds")))
    c.commit()


def breeding_records(status=None, tag="", limit=500):
    c = get_conn()
    q = "SELECT * FROM breedings WHERE 1=1"
    p = []
    if status:
        q += " AND status=?"
        p.append(status)
    if tag.strip():
        q += " AND UPPER(cow_tag)=?"
        p.append(tag.strip().upper())
    q += " ORDER BY COALESCE(due_date, service_date) DESC, id DESC LIMIT ?"
    p.append(limit)
    return [dict(r) for r in c.execute(q, p).fetchall()]


def breedings_for(tag):
    c = get_conn()
    rows = c.execute("SELECT * FROM breedings WHERE cow_tag=? "
                     "ORDER BY service_date DESC", (tag,)).fetchall()
    return [dict(r) for r in rows]


def open_services():
    """Services still awaiting a pregnancy result."""
    return breeding_records(status="Served")


def pregnant_records():
    return breeding_records(status="Pregnant")


def set_pregnancy(bid, status, check_date):
    c = get_conn()
    c.execute("UPDATE breedings SET status=?, check_date=? WHERE id=?",
              (status, check_date, bid))
    c.commit()


def record_calving(bid, calving_date, calf_tag=""):
    c = get_conn()
    c.execute("UPDATE breedings SET status='Calved', calving_date=?, calf_tag=? "
              "WHERE id=?", (calving_date, calf_tag or "", bid))
    c.commit()


def delete_breeding(bid):
    c = get_conn()
    c.execute("DELETE FROM breedings WHERE id=?", (bid,))
    c.commit()


def due_between(start_iso, end_iso):
    """Pregnant cows due to calve inside a date range."""
    c = get_conn()
    rows = c.execute("SELECT * FROM breedings WHERE status='Pregnant' "
                     "AND due_date BETWEEN ? AND ? ORDER BY due_date",
                     (start_iso, end_iso)).fetchall()
    return [dict(r) for r in rows]


def overdue_calvings():
    """Pregnant cows whose due date has passed without a calving recorded."""
    c = get_conn()
    rows = c.execute("SELECT * FROM breedings WHERE status='Pregnant' AND due_date<? "
                     "ORDER BY due_date", (date.today().isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def breeding_metrics():
    """(services awaiting result, pregnant, due within 30 days, overdue)."""
    c = get_conn()
    today = date.today()
    served = c.execute("SELECT COUNT(*) FROM breedings WHERE status='Served'"
                       ).fetchone()[0]
    preg = c.execute("SELECT COUNT(*) FROM breedings WHERE status='Pregnant'"
                     ).fetchone()[0]
    soon = c.execute("SELECT COUNT(*) FROM breedings WHERE status='Pregnant' "
                     "AND due_date BETWEEN ? AND ?",
                     (today.isoformat(),
                      (today + timedelta(days=30)).isoformat())).fetchone()[0]
    over = c.execute("SELECT COUNT(*) FROM breedings WHERE status='Pregnant' "
                     "AND due_date<?", (today.isoformat(),)).fetchone()[0]
    return served, preg, soon, over


def calving_interval(tag):
    """Average days between calvings for one cow, or None if fewer than two."""
    c = get_conn()
    rows = c.execute("SELECT calving_date FROM breedings WHERE cow_tag=? "
                     "AND status='Calved' AND COALESCE(calving_date,'')!='' "
                     "ORDER BY calving_date", (tag,)).fetchall()
    dates = []
    for r in rows:
        try:
            dates.append(datetime.fromisoformat(r[0]).date())
        except Exception:
            pass
    if len(dates) < 2:
        return None
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    return sum(gaps) / len(gaps)


def close_calving_reminders(dam_tag):
    """Tick off outstanding 'calving due' reminders once the cow has calved."""
    c = get_conn()
    cur = c.execute("UPDATE reminders SET done=1 WHERE done=0 AND task=?",
                    (f"Calving due — {dam_tag}",))
    c.commit()
    return cur.rowcount or 0


def complete_calving(bid, dam_tag, calving_date, outcome, calf=None):
    """Record a calving and link everything it touches.

    calf (optional dict): tag, sex, weight — registers the calf in the herd.
    Returns (ok, message).
    """
    live = outcome == "Live calf"
    dam = get_cow(dam_tag)
    calf_tag = ""

    # A calving can't happen before the cow was served.
    c = get_conn()
    row = c.execute("SELECT service_date FROM breedings WHERE id=?", (bid,)).fetchone()
    if row and row[0]:
        try:
            served = datetime.fromisoformat(row[0]).date()
            calved_on = datetime.fromisoformat(calving_date).date()
            if calved_on < served:
                return False, (f"That calving date ({calving_date}) is before the "
                               f"service date ({row[0]}). Check the date.")
        except Exception:
            pass

    # 1. The calf joins the herd (and therefore the inventory).
    if live and calf and calf.get("tag"):
        calf_tag = calf["tag"].strip()
        if get_cow(calf_tag):
            return False, f"A cow with tag '{calf_tag}' already exists."
        created = add_cow({
            "tag": calf_tag, "name": "",
            "breed": (dam["breed"] if dam else ""),
            "sex": calf.get("sex", ""), "dob": calving_date, "colour": "",
            "weight": float(calf["weight"]) if calf.get("weight") else None,
            "category": SP["young_cat"], "status": "Active",
            "date_acquired": calving_date, "notes": "",
            "brand_number": "", "mother_tag": dam_tag,
            "origin_location": "Born on farm",
        })
        if not created:
            return False, f"Could not add calf '{calf_tag}' to the herd."

    # 2. Close off the breeding record.
    if live:
        record_calving(bid, calving_date, calf_tag)
    else:
        set_pregnancy(bid, "Lost", calving_date)

    # 3. Calendar: an entry for the dam, and one for the calf itself.
    detail = f"Calving: {outcome}" + (f" — calf {calf_tag}" if calf_tag else "")
    add_event(calving_date, "Calf born" if live else "Note", dam_tag, detail)
    if calf_tag:
        add_event(calving_date, "Calf born", calf_tag,
                  f"Born on farm to {dam_tag}")

    # 4. Written into the dam's own history.
    note = f"[{calving_date}] Calving: {outcome}"
    if calf_tag:
        note += f" — calf {calf_tag}"
    append_note_to_tags([dam_tag], note)

    # 5. The 'calving due' reminder is no longer outstanding.
    closed = close_calving_reminders(dam_tag)

    msg = f"Calving recorded for {dam_tag} ({outcome.lower()})."
    if calf_tag:
        msg += f" Calf {calf_tag} added to the herd and calendar."
    if closed:
        msg += " Calving reminder cleared."
    ci = calving_interval(dam_tag)
    if ci:
        msg += f" Average calving interval: {ci:.0f} days."
    return True, msg


def days_until(d_iso, today=None):
    """Whole days from today until a date. Negative means it has passed.

    `today` can be supplied to work out the count for another day, which is
    what the countdown tests use to step the clock forward.
    """
    try:
        d = datetime.fromisoformat(str(d_iso)[:10]).date()
        return (d - (today or date.today())).days
    except Exception:
        return None


# ══════════════════════════════════════════════
# BACKUP & RESTORE
# ══════════════════════════════════════════════
def set_meta(key, value):
    c = get_conn()
    c.execute("INSERT INTO app_meta(key, value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    c.commit()


def get_meta(key, default=""):
    c = get_conn()
    row = c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


# Tables worth backing up, with a friendly name for each.
BACKUP_TABLES = [
    ("cattle", "Herd records"),
    ("breedings", "Breeding & calving"),
    ("weights", "Weight history"),
    ("events", "Calendar entries"),
    ("activities", "Farm activities"),
    ("sales", "Sales / invoices"),
    ("purchases", "Purchases / receipts"),
    ("expenses", "Daily expenses"),
    ("losses", "Deaths & missing"),
    ("reminders", "Things to do"),
    ("farm_profile", "Farm details"),
    ("cattle_images", "Photo index (metadata only)"),
]


def table_row_counts():
    """Row count for every backed-up table."""
    c = get_conn()
    out = []
    for name, label in BACKUP_TABLES:
        try:
            n = c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        except Exception:
            n = 0
        out.append((name, label, n))
    return out


def table_to_csv_bytes(name):
    """One table as CSV. Photo blobs are excluded — they live in the .db file."""
    c = get_conn()
    if name == "cattle_images":
        q = ("SELECT id, tag, filename, LENGTH(image) AS image_bytes, created_at "
             "FROM cattle_images ORDER BY id")
    else:
        q = f"SELECT * FROM {name}"
    try:
        return pd.read_sql_query(q, c).to_csv(index=False).encode("utf-8")
    except Exception:
        return b""


def db_snapshot_bytes():
    """A consistent copy of the whole database (safe to take while in use)."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dest = sqlite3.connect(path)
        try:
            get_conn().backup(dest)          # SQLite's own online-backup API
        finally:
            dest.close()
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        # Very old Python without Connection.backup(): fall back to a file copy.
        try:
            with open(DB_PATH, "rb") as f:
                return f.read()
        except Exception:
            return b""
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def build_backup_zip(tables=None, include_db=True):
    """A single ZIP holding the chosen CSVs plus (optionally) the database file."""
    import zipfile
    names = [t for t, _ in BACKUP_TABLES] if tables is None else list(tables)
    stamp = now_local().strftime("%Y-%m-%d %H:%M")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            data = table_to_csv_bytes(name)
            if data:
                z.writestr(f"csv/{name}.csv", data)
        if include_db:
            snap = db_snapshot_bytes()
            if snap:
                z.writestr(SP["db_name"], snap)
        profile = get_farm_profile()
        readme = (
            "CATTLE MANAGEMENT & INVENTORY SYSTEM — BACKUP\n"
            "=============================================\n"
            f"Farm      : {profile['farm_name']}\n"
            f"Farmer    : {profile['farmer_name'] or '-'}\n"
            f"Taken     : {stamp}\n\n"
            "csv/       spreadsheet copies of each table (open in Excel)\n"
            "cattle.db  the complete database — use this to RESTORE\n\n"
            "TO RESTORE: open the app, go to the Backup tab, and upload\n"
            "cattle.db under 'Restore from a backup'.\n"
            "Photos are stored inside cattle.db, not in the CSV files.\n")
        z.writestr("README.txt", readme.encode("utf-8"))
    return buf.getvalue()


def detect_drives():
    """Likely USB sticks / external drives mounted on this machine."""
    found = []
    for base in ("/media", "/run/media", "/mnt", "/Volumes"):
        if not os.path.isdir(base):
            continue
        try:
            for entry in sorted(os.listdir(base)):
                p = os.path.join(base, entry)
                if not os.path.isdir(p):
                    continue
                # /media/<user>/<label> — look one level deeper too.
                try:
                    subs = [os.path.join(p, s) for s in sorted(os.listdir(p))
                            if os.path.isdir(os.path.join(p, s))]
                except Exception:
                    subs = []
                if subs and base in ("/media", "/run/media"):
                    found.extend(subs)
                else:
                    found.append(p)
        except Exception:
            pass
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            p = f"{letter}:\\"
            if os.path.exists(p):
                found.append(p)
    # Only keep places we can actually write to.
    return [p for p in dict.fromkeys(found) if os.access(p, os.W_OK)]


def backup_to_folder(dest_folder, tables=None, include_db=True):
    """Write a timestamped backup folder to a USB drive, external disk or
    a mounted server/network share. Returns (ok, message, path)."""
    if not dest_folder or not str(dest_folder).strip():
        return False, "Enter a destination folder.", ""
    dest_folder = os.path.expanduser(str(dest_folder).strip())
    if not os.path.isdir(dest_folder):
        return False, f"That folder does not exist: {dest_folder}", ""
    if not os.access(dest_folder, os.W_OK):
        return False, f"No permission to write to: {dest_folder}", ""
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(dest_folder,
                          f"{SP['key']}-backup-{stamp}")
    try:
        os.makedirs(os.path.join(target, "csv"), exist_ok=True)
        names = [t for t, _ in BACKUP_TABLES] if tables is None else list(tables)
        written = 0
        for name in names:
            data = table_to_csv_bytes(name)
            if data:
                with open(os.path.join(target, "csv", f"{name}.csv"), "wb") as f:
                    f.write(data)
                written += len(data)
        if include_db:
            snap = db_snapshot_bytes()
            if snap:
                with open(os.path.join(target, SP["db_name"]), "wb") as f:
                    f.write(snap)
                written += len(snap)
        set_meta("last_backup", f"{now_local().isoformat(timespec='seconds')}|{target}")
        return True, (f"Backup written to {target} "
                      f"({written / 1024 / 1024:.2f} MB)."), target
    except Exception as e:
        return False, f"Backup failed: {e}", ""


def restore_from_bytes(data):
    """Replace the live database contents with an uploaded backup.

    Uses SQLite's online-backup API to copy the backup *into* the live database
    rather than swapping the file, which would break connections already open.
    Returns (ok, message).
    """
    import tempfile
    if not data:
        return False, "No file received."

    # A backup zip is the file most people keep, so accept it directly and
    # take the database out of it rather than making them unzip first.
    if data[:2] == b"PK":
        try:
            import zipfile as _zf
            with _zf.ZipFile(io.BytesIO(data)) as z:
                inner = [n for n in z.namelist()
                         if n.lower().endswith(".db") and not n.endswith("/")]
                if not inner:
                    return False, ("That zip has no database in it. Use a backup "
                                   f"zip from this system, or the {SP['db_name']} "
                                   "file itself.")
                data = z.read(inner[0])
        except Exception:
            return False, "That zip could not be opened — it may be damaged."

    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)

        # 1. Validate: is it SQLite, does it hold our herd table, and does that
        #    table have the columns this app needs?
        REQUIRED = {"tag", "status", "sex", "dob", "category", "breed"}
        try:
            probe = sqlite3.connect(tmp)
            names = {r[0] for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "cattle" not in names:
                probe.close()
                return False, "That database has no herd table — wrong file?"
            cols = {r[1] for r in probe.execute("PRAGMA table_info(cattle)").fetchall()}
            missing = REQUIRED - cols
            if missing:
                probe.close()
                return False, ("That file doesn't look like a backup from this "
                               "system — its herd table is missing: "
                               + ", ".join(sorted(missing)) + ".")
            head = probe.execute("SELECT COUNT(*) FROM cattle").fetchone()[0]
        except Exception:
            return False, ("That file is not a valid backup database. Upload "
                           f"your backup zip, or the {SP['db_name']} file from "
                           "inside it.")

        # 2. Keep a safety copy of what is there now.
        keep = ""
        rollback = None
        try:
            rollback = db_snapshot_bytes()
            if rollback:
                keep = f"{DB_PATH}.replaced-{now_local().strftime('%Y%m%d-%H%M%S')}"
                with open(keep, "wb") as f:
                    f.write(rollback)
        except Exception:
            keep = ""

        # 3. Copy the backup into the live database, then make sure the app can
        #    still work with it. If not, put the old data straight back.
        try:
            live = get_conn()
            probe.backup(live)
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            live.commit()
        except Exception as e:
            return False, f"Could not write the restored data: {e}"
        finally:
            try:
                probe.close()
            except Exception:
                pass

        try:
            init_db()
        except Exception as e:
            if rollback:
                try:
                    fd2, back = tempfile.mkstemp(suffix=".db")
                    os.close(fd2)
                    with open(back, "wb") as f:
                        f.write(rollback)
                    src_conn = sqlite3.connect(back)
                    src_conn.backup(get_conn())
                    src_conn.close()
                    os.remove(back)
                    init_db()
                except Exception:
                    pass
                return False, ("That backup isn't compatible with this version of "
                               f"the app ({e}). Your existing data has been put "
                               "back and nothing was lost.")
            return False, f"That backup could not be opened: {e}"
        msg = f"Restored successfully — {head:,} cattle records loaded."
        if keep:
            msg += (" Your previous database was kept alongside it as "
                    f"'{os.path.basename(keep)}'.")
        return True, msg
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ── weight history helpers ────────────────────
def _sync_current_weight(tag):
    """Keep cattle.weight showing the most recent weighing."""
    c = get_conn()
    row = c.execute("SELECT weight FROM weights WHERE tag=? "
                    "ORDER BY date DESC, id DESC LIMIT 1", (tag,)).fetchone()
    if row and row[0] is not None:
        c.execute("UPDATE cattle SET weight=? WHERE tag=?", (row[0], tag))
        c.commit()


def add_weight(tag, d_iso, kg, note=""):
    """Record one weighing. Returns (ok, message)."""
    try:
        kg = float(kg)
    except (TypeError, ValueError):
        return False, "Enter a weight in kilograms."
    if kg <= 0:
        return False, "Weight must be greater than zero."
    if kg > 2000:
        return False, ("That weight looks wrong — the heaviest cattle are well "
                       "under 2,000 kg. Check the figure.")
    if not get_cow(tag):
        return False, f"No cow with tag '{tag}'."
    c = get_conn()
    c.execute("INSERT INTO weights(tag, date, weight, note, created_at) "
              "VALUES(?,?,?,?,?)",
              (tag, d_iso, kg, (note or "").strip(),
               now_local().isoformat(timespec="seconds")))
    c.commit()
    _sync_current_weight(tag)
    return True, f"{kg:,.0f} kg recorded for {tag} on {d_iso}."


def weights_for(tag):
    c = get_conn()
    rows = c.execute("SELECT * FROM weights WHERE tag=? ORDER BY date, id",
                     (tag,)).fetchall()
    return [dict(r) for r in rows]


def delete_weight(wid):
    c = get_conn()
    row = c.execute("SELECT tag FROM weights WHERE id=?", (wid,)).fetchone()
    c.execute("DELETE FROM weights WHERE id=?", (wid,))
    c.commit()
    if row:
        _sync_current_weight(row[0])


def weight_stats(tag):
    """Growth summary for one animal: first, latest, gain and daily gain."""
    rows = weights_for(tag)
    if not rows:
        return None
    def _d(v):
        try:
            return datetime.fromisoformat(v).date()
        except Exception:
            return None
    pts = [(_d(r["date"]), float(r["weight"])) for r in rows
           if _d(r["date"]) and r["weight"] is not None]
    if not pts:
        return None
    first_d, first_w = pts[0]
    last_d, last_w = pts[-1]
    days = (last_d - first_d).days
    gain = last_w - first_w
    adg = (gain / days) if days > 0 else None
    # Growth since the previous weighing, which is what you act on.
    recent = None
    if len(pts) >= 2:
        pd_, pw = pts[-2]
        rd = (last_d - pd_).days
        if rd > 0:
            recent = (last_w - pw) / rd
    return {"count": len(pts), "first_date": first_d.isoformat(),
            "first_weight": first_w, "last_date": last_d.isoformat(),
            "last_weight": last_w, "days": days, "gain": gain,
            "adg": adg, "recent_adg": recent}


def bulk_add_weights(entries, d_iso, note=""):
    """entries: {tag: kg}. Returns how many were recorded."""
    n = 0
    for tag, kg in (entries or {}).items():
        try:
            if float(kg) > 0:
                ok, _ = add_weight(tag, d_iso, kg, note)
                n += 1 if ok else 0
        except (TypeError, ValueError):
            continue
    return n


def weight_leaders(limit=5, min_points=2):
    """Active cattle with the best daily gain, for the assistant and reports."""
    c = get_conn()
    tags = [r[0] for r in c.execute(
        "SELECT tag FROM weights GROUP BY tag HAVING COUNT(*)>=? LIMIT 500",
        (min_points,)).fetchall()]
    out = []
    for t in tags:
        s = weight_stats(t)
        if s and s["adg"] is not None:
            cow = get_cow(t)
            if cow and cow["status"] == "Active":
                out.append((t, s["adg"], s["last_weight"]))
    out.sort(key=lambda x: -x[1])
    return out[:limit]


# ══════════════════════════════════════════════
# AUTOMATIC CATEGORY PROGRESSION
# A calf becomes a weaner, then a heifer or young male, and a heifer becomes
# a cow once she has calved. Age thresholds are adjustable.
# ══════════════════════════════════════════════
PROGRESSION_DEFAULTS = {"wean_months": SP["wean_months"],
                        "heifer_months": SP["maiden_months"],
                        "male_default": SP["male_options"][0], "auto": "1"}
# Only these can still move forward; anything else is left alone.
_FEMALE_CHAIN = SP["female_chain"]
_MALE_CHAIN = SP["male_chain"]


def progression_settings():
    s = {}
    for k, v in PROGRESSION_DEFAULTS.items():
        raw = get_meta(f"prog_{k}", "")
        s[k] = raw if raw != "" else v
    for k in ("wean_months", "heifer_months"):
        try:
            s[k] = int(s[k])
        except (TypeError, ValueError):
            s[k] = PROGRESSION_DEFAULTS[k]
    if s["male_default"] not in SP["male_options"]:
        s["male_default"] = SP["male_options"][0]
    return s


def save_progression_settings(wean, heifer, male_default, auto):
    set_meta("prog_wean_months", str(int(wean)))
    set_meta("prog_heifer_months", str(int(heifer)))
    set_meta("prog_male_default", male_default)
    set_meta("prog_auto", "1" if auto else "0")


def age_months(dob):
    """Whole months since date of birth, or None if unknown."""
    if not dob:
        return None
    try:
        d = datetime.fromisoformat(dob).date()
    except Exception:
        return None
    today = date.today()
    months = (today.year - d.year) * 12 + (today.month - d.month)
    if today.day < d.day:
        months -= 1
    return months if months >= 0 else None


def _dams_that_have_calved():
    """Tags of females known to have produced a calf."""
    c = get_conn()
    out = set()
    for (t,) in c.execute("SELECT DISTINCT cow_tag FROM breedings "
                          "WHERE status='Calved' AND COALESCE(cow_tag,'')!=''"):
        out.add(t)
    for (t,) in c.execute("SELECT DISTINCT mother_tag FROM cattle "
                          "WHERE COALESCE(mother_tag,'')!=''"):
        out.add(t)
    return out


def category_progression(apply_changes=True):
    """Work out which animals have outgrown their category.

    Returns a list of (tag, from_category, to_category, reason).
    With apply_changes=False it only previews.
    """
    s = progression_settings()
    c = get_conn()
    # Only animals that could still move forward — keeps this fast on big herds.
    rows = c.execute(
        "SELECT tag, sex, dob, category FROM cattle "
        "WHERE status='Active' AND COALESCE(dob,'')!='' "
        "AND COALESCE(category,'') IN ('', ?, ?, ?)",
        (SP["young_cat"], SP["weaner_cat"], SP["maiden_cat"])).fetchall()
    if not rows:
        return []
    calved = _dams_that_have_calved()
    changes = []
    for tag, sex, dob, cur in rows:
        cur = cur or ""
        sexn = (sex or "").strip().lower()   # tolerate "Female"/"female"/" FEMALE "
        months = age_months(dob)
        if months is None:
            continue
        target, reason = None, ""
        if months < s["wean_months"]:
            target, reason = SP["young_cat"], f"{months} months old"
        elif months < s["heifer_months"]:
            target, reason = SP["weaner_cat"], f"weaned age ({months} months)"
        else:
            if sexn == "female":
                if tag in calved:
                    target, reason = SP["adult_f_cat"], "has calved"
                else:
                    target, reason = SP["maiden_cat"], f"{months} months old"
            elif sexn == "male":
                target, reason = s["male_default"], f"{months} months old"
            else:
                continue          # unknown sex — never guess
        if not target or target == cur:
            continue
        chain = _FEMALE_CHAIN if sexn == "female" else _MALE_CHAIN
        # Only ever move forward, and never touch a category we don't manage.
        if cur and cur not in chain:
            continue
        if target not in chain:
            continue
        cur_i = chain.index(cur) if cur in chain else -1
        if chain.index(target) <= cur_i:
            continue
        changes.append((tag, cur or "(not set)", target, reason))

    if apply_changes and changes:
        today = date.today().isoformat()
        by_target = {}
        for tag, was, target, _r in changes:
            by_target.setdefault(target, []).append((tag, was))
        for target, items in by_target.items():
            c.executemany("UPDATE cattle SET category=? WHERE tag=?",
                          [(target, t) for t, _w in items])
            c.commit()
            append_note_to_tags(
                [t for t, _w in items],
                f"[{today}] Category updated to {target} (automatic progression)")
    return changes


def run_daily_progression():
    """Run the progression once a day, quietly, when the app is opened."""
    s = progression_settings()
    if str(s.get("auto", "1")) != "1":
        return []
    today = date.today().isoformat()
    if get_meta("prog_last_run", "") == today:
        return []
    try:
        changes = category_progression(apply_changes=True)
        set_meta("prog_last_run", today)
        if changes:
            set_meta("prog_last_count", str(len(changes)))
        return changes
    except Exception:
        return []


# ══════════════════════════════════════════════
# FARM INTELLIGENCE
# Rule-based analysis of your own records: which cows breed reliably, whose
# calves grow well, which animals need attention, and what to expect next.
# Every figure is traceable — nothing is guessed.
# ══════════════════════════════════════════════
IDEAL_CALVING_INTERVAL = 365      # days: a calf a year is the usual target
POOR_CALVING_INTERVAL = 500       # beyond this, fertility is a concern
OPEN_DAYS_CONCERN = 120           # days after calving with no new service


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def cow_performance(tag, herd_avg_calf_adg=None):
    """Fertility and production summary for one cow, with a 0-100 score."""
    cow = get_cow(tag)
    if not cow:
        return None
    breedings = breedings_for(tag)
    calvings = [b for b in breedings if b["status"] == "Calved"]
    services = [b for b in breedings if b["status"] in
                ("Served", "Pregnant", "Not pregnant", "Calved", "Lost")]
    pregnant_now = any(b["status"] == "Pregnant" for b in breedings)
    awaiting = any(b["status"] == "Served" for b in breedings)
    calves = calves_of(tag)
    live_calves = [c for c in calves if c["status"] != "Deceased"]
    lost_calves = [c for c in calves if c["status"] == "Deceased"]

    # Growth of her calves — the clearest signal of how well she rears.
    adgs = []
    for c in calves:
        s = weight_stats(c["tag"])
        if s and s["adg"] is not None:
            adgs.append(s["adg"])
    calf_adg = (sum(adgs) / len(adgs)) if adgs else None

    ci = calving_interval(tag)
    months = age_months(cow["dob"])

    # Days since her last calving.
    last_calving = None
    for b in calvings:
        if b["calving_date"] and (last_calving is None
                                  or b["calving_date"] > last_calving):
            last_calving = b["calving_date"]
    days_since_calving = None
    if last_calving:
        try:
            days_since_calving = (date.today()
                                  - datetime.fromisoformat(last_calving).date()).days
        except Exception:
            pass

    # ── Fertility score ──────────────────────
    fert, fert_parts = None, []
    if ci is not None:
        s_ci = _clamp((POOR_CALVING_INTERVAL - ci) /
                      (POOR_CALVING_INTERVAL - IDEAL_CALVING_INTERVAL) * 100)
        fert_parts.append(s_ci)
    if services:
        # Calvings + a confirmed pregnancy count as successes.
        wins = len(calvings) + (1 if pregnant_now else 0)
        fert_parts.append(_clamp(wins / len(services) * 100))
    if not fert_parts and months is not None and months >= 30 and not calvings:
        fert_parts.append(0.0)          # mature but never calved
    if fert_parts:
        fert = sum(fert_parts) / len(fert_parts)
        if pregnant_now:
            fert = _clamp(fert + 5)     # currently in calf is a good sign

    # ── Production score (how well her calves do) ──
    prod, prod_parts = None, []
    if calves:
        survival = len(live_calves) / len(calves) * 100
        prod_parts.append(survival)
    if calf_adg is not None and herd_avg_calf_adg:
        prod_parts.append(_clamp(calf_adg / herd_avg_calf_adg * 70))
    elif calf_adg is not None:
        prod_parts.append(70.0)         # no herd average to compare against yet
    if prod_parts:
        prod = sum(prod_parts) / len(prod_parts)

    parts = [p for p in (fert, prod) if p is not None]
    raw = sum(parts) / len(parts) if parts else None

    # How much evidence is this based on?
    evidence = len(calvings) * 2 + len(services) + len(adgs)
    confidence = ("good" if evidence >= 6 else
                  "fair" if evidence >= 3 else "limited")

    # Thin evidence shouldn't produce a confident rating, so scores are pulled
    # towards the middle until there is history behind them. A cow with one
    # confirmed pregnancy then cannot outrank a proven producer.
    score = raw
    if raw is not None:
        k = 3.0
        score = (raw * evidence + 50.0 * k) / (evidence + k)

    return {
        "tag": tag, "status": cow["status"], "age_months": months,
        "calvings": len(calvings), "services": len(services),
        "pregnant_now": pregnant_now, "awaiting_result": awaiting,
        "calves": len(calves), "live_calves": len(live_calves),
        "lost_calves": len(lost_calves), "calf_adg": calf_adg,
        "calving_interval": ci, "last_calving": last_calving,
        "days_since_calving": days_since_calving,
        "fertility_score": fert, "production_score": prod, "score": score,
        "raw_score": raw, "confidence": confidence, "evidence": evidence,
    }


# ── heat (oestrus) watchlist ──────────────────
# These are arithmetic estimates from the dates already recorded. A cow's
# cycle is not a timetable: the figures below are herd averages and the spread
# around them is wide, so this list says who is worth watching, never who is
# definitely on heat. Only observation can tell you that.
OESTRUS_CYCLE_DAYS = SP["oestrus_days"]
OESTRUS_WINDOW_DAYS = 3          # so we show an expected date give or take 3
POSTPARTUM_HEAT_DAYS = 45        # typical resumption after calving; varies a lot
HEIFER_BREEDING_MONTHS = SP["first_service_months"]
# Past this many cycles from the last real record, the estimate has drifted so
# far that a date would be misleading. Such cows are listed without one.
MAX_PROJECTED_CYCLES = 6


def _project_heat(anchor_iso, offset_days, today=None):
    """Step forward in cycles from an anchor date to the next expected heat.

    Returns (expected_date, cycles_used) or (None, None) if the anchor is
    unusable. cycles_used says how far the estimate has been carried, which
    is what tells the caller how much to trust it.
    """
    if not anchor_iso:
        return None, None
    try:
        anchor = datetime.fromisoformat(str(anchor_iso)[:10]).date()
    except (ValueError, TypeError):
        return None, None
    today = today or date.today()
    expected = anchor + timedelta(days=offset_days)
    cycles = 0
    # Allow a heat that was due yesterday to still count as current.
    while expected < today - timedelta(days=1):
        expected += timedelta(days=OESTRUS_CYCLE_DAYS)
        cycles += 1
        if cycles > 40:                      # never loop on a silly date
            return None, None
    return expected, cycles


def heat_watchlist(days_ahead=7, today=None):
    """Females worth watching for heat in the next few days.

    Each cow is anchored to the most recent thing actually recorded about her:

      * a service still awaiting a pregnancy result — she is due to return to
        heat about 21 days later if she did not hold, which is the single most
        useful thing this list does;
      * her last calving, plus the usual gap before cycling resumes;
      * nothing at all, for a heifer old enough to be cycling — she belongs on
        the list, but no date can be put against her.

    Cows confirmed in calf are left out entirely.
    """
    today = today or date.today()
    horizon = today + timedelta(days=max(0, days_ahead))
    c = get_conn()

    females = [dict(r) for r in c.execute(
        "SELECT tag, name, category, dob, breed FROM cattle "
        "WHERE sex='Female' AND status='Active' ORDER BY tag").fetchall()]

    # Latest breeding record per cow, so we anchor to the most recent fact.
    latest = {}
    for r in c.execute(
            "SELECT cow_tag, service_date, calving_date, status FROM breedings "
            "ORDER BY COALESCE(calving_date, service_date, '') ASC"):
        if r[0]:
            latest.setdefault(r[0], []).append(dict(zip(
                ("cow_tag", "service_date", "calving_date", "status"), r)))

    due, watch, in_calf = [], [], 0
    for cow in females:
        tag = cow["tag"]
        records = latest.get(tag, [])
        pregnant = any(r["status"] == "Pregnant" for r in records)
        if pregnant:
            in_calf += 1
            continue

        months = age_months(cow["dob"])
        category = (cow["category"] or "").strip()
        # A calf or weaner is not a breeding animal, whatever else is recorded.
        if category in (SP["young_cat"], SP["weaner_cat"]):
            continue
        if months is not None and months < HEIFER_BREEDING_MONTHS:
            continue

        served = [r for r in records
                  if r["status"] == "Served" and r["service_date"]]
        calved = [r for r in records if r["calving_date"]]

        expected = cycles = None
        basis = anchor = ""
        if served:
            anchor = max(r["service_date"] for r in served)
            expected, cycles = _project_heat(anchor, OESTRUS_CYCLE_DAYS, today)
            basis = "return to heat after a service"
        elif calved:
            anchor = max(r["calving_date"] for r in calved)
            expected, cycles = _project_heat(anchor, POSTPARTUM_HEAT_DAYS, today)
            basis = "cycling again after calving"
        else:
            basis = ("old enough to be cycling" if months is None
                     else "old enough to be cycling, nothing recorded yet")

        entry = {
            "tag": tag,
            "name": cow["name"] or "",
            "category": category or "—",
            "breed": cow["breed"] or "",
            "age_months": months,
            "basis": basis,
            "anchor": anchor,
            "cycles": cycles,
            "expected": expected.isoformat() if expected else "",
            "window_from": ((expected - timedelta(days=OESTRUS_WINDOW_DAYS))
                            .isoformat() if expected else ""),
            "window_to": ((expected + timedelta(days=OESTRUS_WINDOW_DAYS))
                          .isoformat() if expected else ""),
            "days_away": (expected - today).days if expected else None,
            "after_service": bool(served),
        }

        # An estimate carried too many cycles is worse than no estimate, and
        # so is one from a date so old the projection gave up entirely.
        _too_far = (cycles or 0) > MAX_PROJECTED_CYCLES
        if expected is None or _too_far:
            entry["expected"] = entry["window_from"] = entry["window_to"] = ""
            entry["days_away"] = None
            if anchor and (_too_far or cycles is None):
                entry["basis"] = ("that service is too old to estimate from"
                                  if served else
                                  "the last calving is too old to estimate from")
            watch.append(entry)
        elif expected <= horizon:
            due.append(entry)
        # A cow whose next heat falls beyond the horizon is simply not due yet.

    due.sort(key=lambda r: (r["days_away"], r["tag"]))
    watch.sort(key=lambda r: r["tag"])
    return {"due": due, "watch": watch, "in_calf": in_calf,
            "females": len(females), "horizon": horizon.isoformat(),
            "today": today.isoformat()}


def herd_intelligence(limit=400):
    """Analyse the breeding females and pull out the picture of the herd."""
    c = get_conn()
    # Females that are actually part of the breeding herd.
    tags = [r[0] for r in c.execute(
        "SELECT DISTINCT tag FROM cattle WHERE sex='Female' AND status='Active' "
        "AND COALESCE(category,'') IN (?,?) LIMIT ?",
        (SP["adult_f_cat"], SP["maiden_cat"], limit)).fetchall()]
    # Include any female with breeding history even if her category differs.
    for r in c.execute("SELECT DISTINCT cow_tag FROM breedings LIMIT ?", (limit,)):
        if r[0] and r[0] not in tags:
            cw = get_cow(r[0])
            if cw and cw["status"] == "Active":
                tags.append(r[0])

    # Herd average calf growth, used as the yardstick.
    all_adg = []
    for t in tags:
        for cf in calves_of(t):
            s = weight_stats(cf["tag"])
            if s and s["adg"] is not None:
                all_adg.append(s["adg"])
    herd_adg = (sum(all_adg) / len(all_adg)) if all_adg else None

    rows = [cow_performance(t, herd_adg) for t in tags]
    rows = [r for r in rows if r]

    scored = [r for r in rows if r["score"] is not None]
    scored.sort(key=lambda r: -r["score"])

    # Herd-level figures.
    cis = [r["calving_interval"] for r in rows if r["calving_interval"]]
    total_services = sum(r["services"] for r in rows)
    total_wins = sum(r["calvings"] + (1 if r["pregnant_now"] else 0) for r in rows)
    herd = {
        "analysed": len(rows),
        "avg_interval": (sum(cis) / len(cis)) if cis else None,
        "conception_rate": (total_wins / total_services * 100)
        if total_services else None,
        "avg_calf_adg": herd_adg,
        "pregnant": sum(1 for r in rows if r["pregnant_now"]),
        "never_calved": sum(1 for r in rows if r["calvings"] == 0),
    }

    # ── Alerts: things worth acting on ───────
    alerts = []
    for r in rows:
        t = r["tag"]
        if (r["days_since_calving"] is not None
                and r["days_since_calving"] > OPEN_DAYS_CONCERN
                and not r["pregnant_now"] and not r["awaiting_result"]):
            alerts.append(("Open too long", t,
                           T(f"{r['days_since_calving']} days since calving with no "
                           "new service recorded")))
        if (r["calving_interval"] is not None
                and r["calving_interval"] > POOR_CALVING_INTERVAL):
            alerts.append((T("Long calving interval"), t,
                           T(f"averaging {r['calving_interval']:.0f} days between "
                           "calves")))
        if (r["age_months"] is not None and r["age_months"] >= 30
                and r["calvings"] == 0 and not r["pregnant_now"]):
            alerts.append((T("Never calved"), t,
                           T(f"{r['age_months']} months old with no calf recorded")))
        if r["services"] >= 3 and r["calvings"] == 0 and not r["pregnant_now"]:
            alerts.append(("Repeat breeder", t,
                           T(f"{r['services']} services without a calf")))
        if r["lost_calves"]:
            alerts.append((T("Calf losses"), t,
                           T(f"{r['lost_calves']} calf/calves recorded as deceased")))
    for r in overdue_calvings():
        alerts.append((T("Overdue to calve"), r["cow_tag"],
                       f"was due {r['due_date']}"))
    # Any animal losing weight.
    for t in [x[0] for x in c.execute(
            "SELECT tag FROM weights GROUP BY tag HAVING COUNT(*)>1 LIMIT 300")]:
        s = weight_stats(t)
        if s and s["recent_adg"] is not None and s["recent_adg"] < -0.05:
            cw = get_cow(t)
            if cw and cw["status"] == "Active":
                alerts.append(("Losing weight", t,
                               f"{s['recent_adg']:.2f} kg/day since "
                               f"{s['last_date']}"))

    # ── Predictions ──────────────────────────
    preds = {}
    today = date.today()
    preds["due_90"] = due_between(today.isoformat(),
                                  (today + timedelta(days=90)).isoformat())
    # Expected calves in the next 12 months: those in calf, plus cows likely to
    # come round again based on their own average interval.
    likely = []
    for r in rows:
        if r["pregnant_now"]:
            continue
        if r["last_calving"] and r["calving_interval"]:
            try:
                nxt = (datetime.fromisoformat(r["last_calving"]).date()
                       + timedelta(days=int(r["calving_interval"])))
                if today <= nxt <= today + timedelta(days=365):
                    likely.append((r["tag"], nxt.isoformat()))
            except Exception:
                pass
    likely.sort(key=lambda x: x[1])
    preds["likely_calvings"] = likely
    preds["expected_calves_12m"] = herd["pregnant"] + len(likely)

    # Weight projections for growing stock.
    proj = []
    for t in [x[0] for x in c.execute(
            "SELECT tag FROM weights GROUP BY tag HAVING COUNT(*)>1 LIMIT 300")]:
        s = weight_stats(t)
        cw = get_cow(t)
        if not (s and cw and cw["status"] == "Active"):
            continue
        if s["adg"] and s["adg"] > 0.05:
            in90 = s["last_weight"] + s["adg"] * 90
            proj.append((t, s["last_weight"], in90, s["adg"]))
    proj.sort(key=lambda x: -x[3])
    preds["weight_projection"] = proj[:10]

    return {"rows": rows, "scored": scored, "herd": herd,
            "alerts": alerts, "predictions": preds}


def performance_band(score):
    if score is None:
        return "Not enough data"
    if score >= 75:
        return "Strong"
    if score >= 55:
        return "Steady"
    if score >= 35:
        return "Watch"
    return "Poor"


def cow_profile_pdf(cow):
    """Build a one-page PDF profile for a single cow (details + photos).
    Returns bytes, or None if reportlab isn't installed."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                         TableStyle, Image as RLImage)
    except Exception:
        return None

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title=f"Cow profile {cow['tag']}")
    ss = getSampleStyleSheet()
    teal, muted, grid, light = (colors.HexColor(PRIMARY), colors.HexColor(MUTED),
                                colors.HexColor(GRID), colors.HexColor(LIGHT_BG))
    h1 = ParagraphStyle("h1", parent=ss["Title"], textColor=teal, fontSize=18,
                        alignment=0, spaceAfter=0)
    tagst = ParagraphStyle("tag", parent=ss["Heading1"], textColor=teal, fontSize=15,
                           spaceBefore=6, spaceAfter=2)
    sect = ParagraphStyle("sect", parent=ss["Heading2"], textColor=teal, fontSize=12,
                          spaceBefore=12, spaceAfter=4)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=10, leading=13)
    foot = ParagraphStyle("foot", parent=ss["Normal"], textColor=muted, fontSize=8)

    story = []
    _p = get_farm_profile()
    _fname = _html.escape(_p["farm_name"])
    _details = "<br/>".join(_html.escape(x) for x in farm_detail_lines(_p))
    _hdr_html = (f"<b>{_fname}</b><br/><font size=10 color='#5E7373'>"
                 "Cattle Profile</font>"
                 + (f"<br/><font size=8 color='#5E7373'>{_details}</font>"
                    if _details else ""))
    try:
        logo, _lw = report_logo(RLImage, mm)
        header = Table([[logo, Paragraph(_hdr_html, h1)]],
                       colWidths=[_lw * mm, (168 - _lw) * mm])
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(header)
    except Exception:
        story.append(Paragraph(_hdr_html, h1))

    story.append(Paragraph(f"🏷️ {cow['tag']}", tagst))

    imgs = get_images(cow["tag"])
    if imgs:  # passport-size photo right under the tag, before the details
        try:
            w, h = 30 * mm, 40 * mm
            try:
                from PIL import Image as PILImage
                pim = PILImage.open(io.BytesIO(imgs[0]["image"]))
                iw, ih = pim.size
                ratio = ih / iw if iw else 1.3
                w = 30 * mm
                h = w * ratio
                if h > 42 * mm:
                    h = 42 * mm
                    w = h / ratio
            except Exception:
                pass
            story.append(RLImage(io.BytesIO(imgs[0]["image"]), width=w, height=h))
            story.append(Spacer(1, 6))
        except Exception:
            pass

    def row(label, value):
        return [Paragraph(f"<b>{_html.escape(label)}</b>", cell),
                Paragraph(_html.escape(str(value if value not in (None, "") else "—")),
                          cell)]

    weight = (str(cow["weight"]) + " kg") if cow["weight"] else ""
    _ws = weight_stats(cow["tag"])
    _wtrend = ""
    if _ws and _ws["count"] > 1:
        _wtrend = (f"{_ws['first_weight']:,.0f} kg ({_ws['first_date']}) to "
                   f"{_ws['last_weight']:,.0f} kg ({_ws['last_date']}) — "
                   f"{_ws['gain']:+,.0f} kg over {_ws['days']} days")
        if _ws["adg"] is not None:
            _wtrend += f", {_ws['adg']:.2f} kg/day"
    elif _ws:
        _wtrend = f"one weighing on {_ws['last_date']}"
    data = [
        row("Name", cow["name"]), row("Breed", cow["breed"]), row("Sex", cow["sex"]),
        row("Category", cow["category"]), row("Colour", cow["colour"]),
    ] + ([row("Brand number", cow.get("brand_number", ""))]
         if SP["branded"] else []) + [
        row("Mother's tag", cow.get("mother_tag", "")),
        row("Came from", cow.get("origin_location", "")),
        row("Date of birth", cow["dob"]), row("Age", age_str(cow["dob"])),
        row("Status", cow["status"]), row("Weight", weight),
        row("Weight trend", _wtrend),
        row("Date acquired", cow["date_acquired"]),
    ]
    t = Table(data, colWidths=[45 * mm, 129 * mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, grid),
        ("BACKGROUND", (0, 0), (0, -1), light),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(t)

    _calves = calves_of(cow["tag"])
    if _calves:
        story.append(Paragraph(T(f"Calves — {len(_calves)} born"), sect))
        for _cf in _calves:
            _txt = _cf["tag"] + (f" — {_cf['name']}" if _cf["name"] else "")
            _txt += (f" (born {_cf['dob']})" if _cf["dob"] else "")
            _txt += f" — {_cf['status']}"
            story.append(Paragraph(_html.escape(_txt), cell))

    if cow["notes"]:
        # Notes hold the animal's activity history, one dated line per entry.
        story.append(Paragraph("Notes &amp; activity history", sect))
        for _ln in str(cow["notes"]).splitlines():
            if _ln.strip():
                story.append(Paragraph(_html.escape(_ln.strip()), cell))

    story.append(Spacer(1, 18))
    story.append(Paragraph(
        T(f"Generated {now_local().strftime('%Y-%m-%d %H:%M')} · {_fname} "
        "Cattle &amp; Small Stock System · Powered by Health Data Matrics "
        "(HDM Group)"), foot))
    doc.build(story)
    return buf.getvalue()


def get_cow(tag):
    c = get_conn()
    row = c.execute("SELECT * FROM cattle WHERE tag=?", (tag,)).fetchone()
    return dict(row) if row else None




def search_cattle(term):
    """Search primarily by tag; also matches name so a quick lookup still works."""
    c = get_conn()
    like = f"%{term.strip()}%"
    rows = c.execute(
        "SELECT * FROM cattle WHERE tag LIKE ? OR name LIKE ? ORDER BY tag LIMIT 500",
        (like, like)).fetchall()
    return [dict(r) for r in rows]


# ── scalable helpers (built for very large herds) ─────────────
def total_cattle_count():
    return get_conn().execute("SELECT COUNT(*) FROM cattle").fetchone()[0]


def herd_metrics():
    """Header counts computed in SQL so we never load the whole herd."""
    c = get_conn()
    total = c.execute("SELECT COUNT(*) FROM cattle").fetchone()[0]
    active = c.execute("SELECT COUNT(*) FROM cattle WHERE status='Active'").fetchone()[0]
    fem = c.execute("SELECT COUNT(*) FROM cattle WHERE sex='Female'").fetchone()[0]
    male = c.execute("SELECT COUNT(*) FROM cattle WHERE sex='Male'").fetchone()[0]
    return total, active, fem, male


def image_counts_map(tags):
    """One query for the photo counts of a page of tags (avoids per-row queries)."""
    if not tags:
        return {}
    c = get_conn()
    marks = ",".join("?" * len(tags))
    rows = c.execute(f"SELECT tag, COUNT(*) FROM cattle_images WHERE tag IN ({marks}) "
                     "GROUP BY tag", list(tags)).fetchall()
    return {r[0]: r[1] for r in rows}


# What the list filter looks at. Text is matched anywhere in the field, so
# "bra" finds Brahman and "2023" finds every animal born or bought that year.
_LIST_FILTER_COLUMNS = ("tag", "name", "breed", "sex", "category", "colour",
                        "status", "dob", "date_acquired", "origin_location",
                        "brand_number")

def _years_ago(years, today=None):
    """The date this many whole years back, safe on 29 February."""
    today = today or date.today()
    try:
        return today.replace(year=today.year - years)
    except ValueError:                       # 29 Feb in a common year
        return today.replace(year=today.year - years, month=2, day=28)


def _list_filter_sql(term):
    """WHERE clause and parameters for a filter typed into the herd list."""
    term = term.strip()
    if not term:
        return "", []

    # A bare one or two digit number is read as an age in years, not as text
    # to find inside a date — otherwise "4" would drag in everything born in
    # 2024. Longer terms such as "2023" still match dates as text.
    digits = term.lstrip("+-")
    as_age = digits.isdigit() and len(digits) <= 2

    cols = [c for c in _LIST_FILTER_COLUMNS
            if not (as_age and c in ("dob", "date_acquired"))]
    like = f"%{term}%"
    parts = [f"COALESCE({col},'') LIKE ?" for col in cols]
    params = [like] * len(cols)

    if as_age:
        # Turned N on or before today, but not yet N+1: a calendar age, the
        # same one the profile shows.
        n = int(digits)
        newest = _years_ago(n).isoformat()
        oldest = (_years_ago(n + 1) + timedelta(days=1)).isoformat()
        parts.append("(COALESCE(dob,'') != '' AND dob BETWEEN ? AND ?)")
        params += [oldest, newest]
    return " WHERE " + " OR ".join(parts), params


def cattle_page(limit, offset, term=""):
    """One page of the herd, filtered on anything the person typed."""
    c = get_conn()
    where, params = _list_filter_sql(term)
    rows = c.execute(
        "SELECT * FROM cattle" + where + " ORDER BY tag LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    return [dict(r) for r in rows]


def cattle_match_count(term=""):
    if not term.strip():
        return total_cattle_count()
    c = get_conn()
    where, params = _list_filter_sql(term)
    return c.execute("SELECT COUNT(*) FROM cattle" + where, params).fetchone()[0]


def group_counts(column, active_only):
    """{value: head count} grouped by a column, in SQL. `column` is a fixed name."""
    if column not in ("category", "sex", "breed", "status"):
        raise ValueError("group_counts: unsupported column")
    c = get_conn()
    where = " WHERE status='Active'" if active_only else ""
    q = (f"SELECT COALESCE(NULLIF(TRIM({column}),''),'—') AS k, COUNT(*) "
         f"FROM cattle{where} GROUP BY k ORDER BY k")
    return {r[0]: r[1] for r in c.execute(q).fetchall()}


def active_weight_sum():
    return get_conn().execute(
        "SELECT COALESCE(SUM(weight),0) FROM cattle WHERE status='Active'").fetchone()[0]


def active_sex_counts():
    c = get_conn()
    fem = c.execute("SELECT COUNT(*) FROM cattle WHERE status='Active' AND sex='Female'"
                    ).fetchone()[0]
    male = c.execute("SELECT COUNT(*) FROM cattle WHERE status='Active' AND sex='Male'"
                     ).fetchone()[0]
    return fem, male


def status_count(status):
    return get_conn().execute("SELECT COUNT(*) FROM cattle WHERE status=?",
                              (status,)).fetchone()[0]


def active_tag_matches(term, limit=200):
    """Tags of active cattle matching a search term (capped), for sale/loss pickers."""
    c = get_conn()
    if term.strip():
        like = f"%{term.strip()}%"
        rows = c.execute("SELECT tag FROM cattle WHERE status='Active' AND tag LIKE ? "
                         "ORDER BY tag LIMIT ?", (like, limit)).fetchall()
    else:
        rows = c.execute("SELECT tag FROM cattle WHERE status='Active' ORDER BY tag "
                         "LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows]


def has_active_cattle():
    return get_conn().execute(
        "SELECT 1 FROM cattle WHERE status='Active' LIMIT 1").fetchone() is not None


def calves_of(tag):
    """Cattle whose recorded mother is this tag (the cow's calves)."""
    if not tag:
        return []
    c = get_conn()
    rows = c.execute("SELECT tag, name, dob, status FROM cattle WHERE mother_tag=? "
                     "ORDER BY dob, tag", (tag,)).fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def age_str(dob):
    """Turn a date-of-birth string into a friendly age, e.g. '3 yr 2 mo'."""
    if not dob:
        return ""
    try:
        d = datetime.fromisoformat(dob).date()
    except Exception:
        return ""
    today = date.today()
    months = (today.year - d.year) * 12 + (today.month - d.month)
    if today.day < d.day:
        months -= 1
    if months < 0:
        return ""
    years, rem = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} yr")
    parts.append(f"{rem} mo")
    return " ".join(parts)


def est_dob_from_age(years):
    """Estimate a date of birth from an age in years (used when the exact birth
    date is unknown). Stored as a real dob so the age keeps updating each year."""
    try:
        years = int(years)
    except (TypeError, ValueError):
        return ""
    if years <= 0:
        return ""
    today = date.today()
    try:
        d = today.replace(year=today.year - years)
    except ValueError:  # e.g. 29 Feb on a non-leap target year
        d = today.replace(year=today.year - years, day=28)
    return d.isoformat()


def styled_breakdown_table(container, series, label, value_header="Head",
                           value_fmt=None, show_share=True):
    """A clean, branded breakdown table with share bars and a totals row."""
    total = float(sum(series.values)) if len(series) else 0.0
    head = (f"<thead><tr><th>{_html.escape(label)}</th>"
            f"<th class='num'>{_html.escape(value_header)}</th>"
            + ("<th>Share</th>" if show_share else "") + "</tr></thead>")
    body = []
    for k, v in series.items():
        v = float(v)
        pct = (v / total * 100) if total else 0.0
        txt = value_fmt(v) if value_fmt else f"{int(round(v)):,}"
        share = ""
        if show_share:
            share = ("<td><div style='display:flex;align-items:center;gap:8px'>"
                     "<div class='share-wrap'><div class='share-bar' "
                     f"style='width:{pct:.1f}%'></div></div>"
                     f"<span class='pct'>{pct:.1f}%</span></div></td>")
        body.append(f"<tr><td class='lbl'>{_html.escape(str(k))}</td>"
                    f"<td class='num'>{_html.escape(txt)}</td>{share}</tr>")
    total_txt = value_fmt(total) if value_fmt else f"{int(round(total)):,}"
    foot = (f"<tfoot><tr><td>Total</td><td class='num'>{_html.escape(total_txt)}</td>"
            + ("<td></td>" if show_share else "") + "</tr></tfoot>")
    container.markdown(
        f"<table class='dtable'>{head}<tbody>{''.join(body)}</tbody>{foot}</table>",
        unsafe_allow_html=True)


# ── monthly money flow (Billing) ──────────────
def _month_key(value):
    """The YYYY-MM prefix of a stored date, or "" if it isn't usable."""
    text = str(value or "").strip()
    if len(text) >= 7 and text[4] == "-":
        head = text[:7]
        try:
            year = int(head[:4])
            month = int(head[5:7])
        except ValueError:
            return ""
        if 1000 <= year and 1 <= month <= 12:
            return head
    return ""


def _month_label(key):
    """Turn 2026-03 into 'Mar 26' for a compact axis."""
    try:
        year, month = key.split("-")
        return _cal.month_abbr[int(month)] + " " + year[2:]
    except Exception:
        return key


def _day_key(value):
    """The YYYY-MM-DD of a stored date, or "" if it isn't usable.

    A date recorded as just YYYY-MM is treated as the first of that month, so
    older part-filled records still fall inside a range rather than vanishing.
    """
    text = str(value or "").strip()
    month = _month_key(text)
    if not month:
        return ""
    if len(text) >= 10 and text[7] == "-":
        try:
            day = int(text[8:10])
        except ValueError:
            return month + "-01"
        if 1 <= day <= 31:
            return month + "-%02d" % day
    return month + "-01"


# A range longer than this makes the chart unreadable, so we stop adding bars.
MAX_FLOW_MONTHS = 120


def _month_span(start_key, end_key):
    """Every YYYY-MM from start to end inclusive, oldest first."""
    keys = []
    try:
        year, month = int(start_key[:4]), int(start_key[5:7])
        end_year, end_month = int(end_key[:4]), int(end_key[5:7])
    except (ValueError, IndexError):
        return [end_key]
    while (year, month) <= (end_year, end_month) and len(keys) < MAX_FLOW_MONTHS:
        keys.append("%04d-%02d" % (year, month))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return keys or [end_key]


def monthly_money_flow(sales, purchases, months=12, end_key=None,
                       start_iso=None, end_iso=None):
    """Sales and purchase totals per calendar month, most recent last.

    Either give an explicit start_iso/end_iso date range, or leave them out and
    get the last `months` calendar months. With a range, rows are filtered by
    the actual day, so a range starting mid-month counts only that part of it —
    but the bars are still whole months, because that is what a month chart is.

    Every month in the window appears, including quiet ones, so the gaps in
    trading are visible rather than silently closed up. Rows that fall outside
    the window, or carry an unusable date, are counted separately instead of
    being dropped without trace.
    """
    ranged = bool(start_iso and end_iso)
    if ranged:
        if start_iso > end_iso:                     # tolerate a reversed range
            start_iso, end_iso = end_iso, start_iso
        keys = _month_span(start_iso[:7], end_iso[:7])
    else:
        end_key = end_key or now_local().strftime("%Y-%m")
        try:
            end_year, end_month = int(end_key[:4]), int(end_key[5:7])
        except (ValueError, IndexError):
            today = date.today()
            end_year, end_month = today.year, today.month
        keys = []
        year, month = end_year, end_month
        for _ in range(max(1, months)):
            keys.append("%04d-%02d" % (year, month))
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        keys.reverse()

    sold = dict.fromkeys(keys, 0.0)
    bought = dict.fromkeys(keys, 0.0)
    sold_n = dict.fromkeys(keys, 0)
    bought_n = dict.fromkeys(keys, 0)
    undated = {"sales": 0, "purchases": 0}
    earlier = {"sales": 0.0, "purchases": 0.0}
    later = {"sales": 0.0, "purchases": 0.0}

    for rows, totals, counts, name in ((sales, sold, sold_n, "sales"),
                                       (purchases, bought, bought_n, "purchases")):
        for row in rows or []:
            key = _month_key(row.get("date"))
            if not key:
                undated[name] += 1
                continue
            try:
                amount = float(row.get("price") or 0)
            except (TypeError, ValueError):
                amount = 0.0

            if ranged:
                day = _day_key(row.get("date"))
                if day < start_iso:
                    earlier[name] += amount
                    continue
                if day > end_iso:
                    later[name] += amount
                    continue

            if key in totals:
                totals[key] += amount
                counts[key] += 1
            elif key < keys[0]:
                earlier[name] += amount
            else:
                later[name] += amount

    return {
        "keys": keys,
        "labels": [_month_label(k) for k in keys],
        "sales": [sold[k] for k in keys],
        "purchases": [bought[k] for k in keys],
        "sales_count": [sold_n[k] for k in keys],
        "purchases_count": [bought_n[k] for k in keys],
        "undated": undated,
        "earlier": earlier,
        "later": later,
        "truncated": ranged and len(keys) >= MAX_FLOW_MONTHS,
    }


def monthly_flow_chart(container, flow, height=260, key=None):
    """Sales and purchases side by side, one pair of bars per month."""
    rows = []
    for i, label in enumerate(flow["labels"]):
        rows.append({"month": label, "kind": "Sales",
                     "amount": flow["sales"][i], "order": i})
        rows.append({"month": label, "kind": "Purchases",
                     "amount": flow["purchases"][i], "order": i})
    df = pd.DataFrame(rows)
    df["_k"] = key or ""                 # keeps chart identities distinct
    try:
        import altair as alt
        chart = alt.Chart(df).mark_bar(
            cornerRadiusTopLeft=3, cornerRadiusTopRight=3
        ).encode(
            x=alt.X("month:N", sort=None, title=None,
                    axis=alt.Axis(labelAngle=0, labelColor=INK, labelFontSize=10,
                                  domainColor=GRID, ticks=False)),
            xOffset=alt.XOffset("kind:N", sort=["Sales", "Purchases"]),
            y=alt.Y("amount:Q", title=None,
                    axis=alt.Axis(labelColor=MUTED, labelFontSize=10,
                                  gridColor=GRID, domainColor=GRID, ticks=False)),
            color=alt.Color("kind:N", sort=["Sales", "Purchases"],
                            scale=alt.Scale(domain=["Sales", "Purchases"],
                                            range=[PRIMARY, WARN]),
                            legend=alt.Legend(title=None, orient="top",
                                              labelColor=INK, labelFontSize=11)),
            tooltip=[alt.Tooltip("month:N", title="Month"),
                     alt.Tooltip("kind:N", title=""),
                     alt.Tooltip("amount:Q", title="Amount", format=",.2f")],
        ).properties(height=height).configure_view(strokeWidth=0)
        container.altair_chart(chart, use_container_width=True)
    except Exception:
        # Charting library unavailable — a plain table still tells the story.
        container.dataframe(
            pd.DataFrame({"Month": flow["labels"],
                          "Sales": flow["sales"],
                          "Purchases": flow["purchases"]}),
            use_container_width=True, hide_index=True)


def value_bar_chart(container, series, color=None, value_fmt=None, height=250,
                    key=None):
    """Bar chart with each bar's value written inside it.

    Uses Altair (installed alongside Streamlit) so the labels always render;
    falls back to Streamlit's built-in chart if anything goes wrong.
    """
    color = color or PRIMARY
    df = pd.DataFrame({
        "label": [str(i) for i in series.index],
        "value": [float(v) for v in series.values],
    })
    df["text"] = [value_fmt(v) if value_fmt else f"{v:,.0f}" for v in df["value"]]
    df["mid"] = df["value"] / 2          # centres the label inside the bar
    df["_k"] = key or ""                 # keeps chart identities distinct
    try:
        import altair as alt
        x_enc = alt.X("label:N", sort=None, title=None,
                      axis=alt.Axis(labelAngle=0, labelColor=INK, labelFontSize=11,
                                    domainColor=GRID, ticks=False))
        bars = alt.Chart(df).mark_bar(
            color=color, size=44, cornerRadiusTopLeft=4, cornerRadiusTopRight=4
        ).encode(x=x_enc, y=alt.Y("value:Q", title=None, axis=None),
                 tooltip=[alt.Tooltip("label:N", title=""),
                          alt.Tooltip("text:N", title="")])
        labels = alt.Chart(df).mark_text(
            color="#FFFFFF", fontWeight="bold", fontSize=12, baseline="middle"
        ).encode(x=x_enc, y=alt.Y("mid:Q", title=None, axis=None),
                 text=alt.Text("text:N"))
        chart = (bars + labels).properties(height=height).configure_view(strokeWidth=0)
        container.altair_chart(chart, use_container_width=True)
    except Exception:
        # Charting library unavailable — fall back to the built-in chart.
        container.bar_chart(series, color=color, height=height)


def status_chip(status):
    colour = {"Active": OK_GREEN, "Sold": MUTED, "Deceased": DANGER,
              "Missing": WARN}.get(status, MUTED)
    return f'<span class="chip" style="background:{colour}">{status}</span>'


def to_dataframe(rows, photo_map=None):
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["age"] = df["dob"].apply(age_str)
    if photo_map is None:
        photo_map = image_counts_map([r["tag"] for r in rows])
    df["photos"] = df["tag"].map(lambda t: photo_map.get(t, 0))
    cols = ["tag", "name", "breed", "sex", "category"] \
        + (["brand_number"] if SP["branded"] else []) \
        + ["dob", "age", "colour", "weight", "status", "date_acquired",
           "photos", "notes"]
    df = df[[c for c in cols if c in df.columns]]
    df = df.rename(columns={
        "tag": "Tag", "name": "Name", "breed": "Breed", "sex": "Sex",
        "category": "Category", "brand_number": "Brand no.",
        "dob": "Date of birth", "age": "Age",
        "colour": "Colour", "weight": "Weight (kg)", "status": "Status",
        "date_acquired": "Date acquired", "photos": "Photos", "notes": "Notes"})
    return df


def profile_view(cow):
    """Full read-only profile for one cow, laid out like the load form."""
    top = st.columns([1, 4])
    if top[0].button(T("⬅️ Back to herd"), key="back_to_herd"):
        st.session_state["selected_tag"] = None
        st.rerun()
    top[1].markdown(T(f'<div class="section">Cow profile — 🏷️ {cow["tag"]}</div>'),
                    unsafe_allow_html=True)

    _pdf = cow_profile_pdf(cow)
    if _pdf:
        _safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cow["tag"])
        _dl(st, "Download profile (PDF)", _pdf,
                           file_name=f"cow_profile_{_safe}.pdf",
                           mime="application/pdf", type="primary",
                           key=f"cowpdf_{cow['tag']}")
    else:
        st.caption("Install reportlab to enable PDF download (pip install reportlab).")

    def field(col, label, value):
        col.markdown(f"<div style='color:{MUTED};font-size:.8rem;font-weight:600'>"
                     f"{label}</div><div style='font-size:1.05rem;font-weight:600;"
                     f"margin-bottom:.6rem'>{value or '—'}</div>",
                     unsafe_allow_html=True)

    # Mirror the load form's row layout.
    r1 = st.columns(3)
    field(r1[0], "Tag", cow["tag"])
    field(r1[1], "Name", cow["name"])
    field(r1[2], "Breed", cow["breed"])

    imgs = get_images(cow["tag"])
    if imgs:
        st.image(imgs[0]["image"], width=150, caption=f"🏷️ {cow['tag']}")

    r2 = st.columns(3)
    field(r2[0], "Sex", cow["sex"])
    field(r2[1], "Category", cow["category"])
    field(r2[2], "Colour", cow["colour"])

    r3 = st.columns(3)
    field(r3[0], "Date of birth", cow["dob"])
    field(r3[1], "Age", age_str(cow["dob"]))
    r3[2].markdown(f"<div style='color:{MUTED};font-size:.8rem;font-weight:600'>Status"
                   "</div>", unsafe_allow_html=True)
    r3[2].markdown(status_chip(cow["status"]), unsafe_allow_html=True)

    r4 = st.columns(3)
    field(r4[0], "Weight (kg)", f"{cow['weight']} kg" if cow["weight"] else "")
    field(r4[1], "Date acquired", cow["date_acquired"])
    field(r4[2], "Photos on file", str(count_images(cow["tag"])))

    r5 = st.columns(3)
    if SP["branded"]:
        field(r5[0], "Brand number", cow.get("brand_number", ""))
    _mtag = cow.get("mother_tag", "")
    if _mtag:
        _mcow = get_cow(_mtag)
        _mstatus = f" ({_mcow['status']})" if _mcow else " (not in herd)"
        field(r5[1], "Mother's tag", f"{_mtag}{_mstatus}")
    else:
        r5[1].markdown("&nbsp;", unsafe_allow_html=True)
    field(r5[2], "Came from", cow.get("origin_location", ""))

    field(st, "Notes", cow["notes"])

    # Offspring: any cattle recorded with this cow as their mother.
    _calves = calves_of(cow["tag"])
    if _calves:
        st.markdown(T('<div class="section">Calves — this cow has given birth to '
                    f'{len(_calves)}</div>'), unsafe_allow_html=True)
        for _cf in _calves:
            _line = f"• 🏷️ {_cf['tag']}"
            if _cf["name"]:
                _line += f" — {_cf['name']}"
            if _cf["dob"]:
                _line += f" (born {_cf['dob']})"
            _line += f" — {_cf['status']}"
            st.write(_line)

    # Breeding history for females.
    # Weight history and growth rate.
    _wrows = weights_for(cow["tag"])
    _wst = weight_stats(cow["tag"])
    st.markdown('<div class="section">Weight history</div>', unsafe_allow_html=True)
    if _wst:
        w1, w2, w3, w4 = st.columns(4)
        w1.metric("Latest weight", f"{_wst['last_weight']:,.0f} kg")
        w2.metric("Total gain", f"{_wst['gain']:+,.0f} kg",
                  delta=f"over {_wst['days']} days" if _wst["days"] else None,
                  delta_color="off")
        w3.metric("Daily gain (overall)",
                  f"{_wst['adg']:.2f} kg/day" if _wst["adg"] is not None else "—")
        w4.metric("Since last weighing",
                  f"{_wst['recent_adg']:.2f} kg/day"
                  if _wst["recent_adg"] is not None else "—")
        if len(_wrows) >= 2:
            _wser = pd.Series(
                [float(r["weight"]) for r in _wrows],
                index=[r["date"] for r in _wrows])
            try:
                import altair as _alt
                _wdf = pd.DataFrame({"Date": list(_wser.index),
                                     "Weight": list(_wser.values)})
                _line = _alt.Chart(_wdf).mark_line(
                    color=PRIMARY, strokeWidth=2.5, point=_alt.OverlayMarkDef(
                        color=PRIMARY, size=55)).encode(
                    x=_alt.X("Date:T", title=None),
                    y=_alt.Y("Weight:Q", title="kg",
                             scale=_alt.Scale(zero=False)),
                    tooltip=["Date:T", "Weight:Q"])
                _lbl = _alt.Chart(_wdf).mark_text(
                    dy=-13, color=INK, fontWeight="bold", fontSize=11).encode(
                    x="Date:T", y="Weight:Q",
                    text=_alt.Text("Weight:Q", format=",.0f"))
                st.altair_chart((_line + _lbl).properties(height=240),
                                use_container_width=True)
            except Exception:
                st.line_chart(_wser, height=240)
        st.dataframe(pd.DataFrame([{
            "Date": r["date"], "Weight (kg)": f"{float(r['weight']):,.0f}",
            "Note": r["note"] or "—"} for r in reversed(_wrows)]),
            use_container_width=True, hide_index=True)
        if len(_wrows) > 0:
            with st.expander("Remove a weighing"):
                _wl = {f"{r['date']} — {float(r['weight']):,.0f} kg": r["id"]
                       for r in reversed(_wrows)}
                _wp = st.selectbox("Entry to remove", list(_wl.keys()),
                                   key=f"pw_del_pick_{cow['tag']}", format_func=T)
                if st.button("Remove this weighing",
                             key=f"pw_del_{cow['tag']}"):
                    delete_weight(_wl[_wp])
                    st.success("Weighing removed; the current weight has been "
                               "reset to the most recent remaining entry.")
                    st.rerun()
    else:
        st.caption("No weighings recorded yet for this animal.")

    _wv = st.session_state.get("weigh_version", 0)
    with st.expander("Record a weighing"):
        wc1, wc2, wc3 = st.columns([1, 1, 2])
        _wdate = wc1.date_input("Date", value=date.today(),
                                key=f"pw_date_{_wv}_{cow['tag']}")
        _wkg = wc2.number_input("Weight (kg)", min_value=0.0, value=0.0, step=1.0,
                                key=f"pw_kg_{_wv}_{cow['tag']}")
        _wnote = wc3.text_input("Note (optional)",
                                key=f"pw_note_{_wv}_{cow['tag']}",
                                placeholder="e.g. after weaning, sale weight")
        if st.button("Save weighing", type="primary",
                     key=f"pw_save_{_wv}_{cow['tag']}"):
            _ok, _msg = add_weight(cow["tag"], _wdate.isoformat(), _wkg, _wnote)
            if _ok:
                add_event(_wdate.isoformat(), "Note", cow["tag"],
                          f"Weighed {float(_wkg):,.0f} kg")
                st.session_state["weigh_version"] = _wv + 1
                st.success(_msg)
                st.rerun()
            else:
                st.warning(_msg)

    _breed_hist = breedings_for(cow["tag"])
    if _breed_hist:
        _ci = calving_interval(cow["tag"])
        _hdr = "Breeding history"
        if _ci:
            _hdr += f" — average calving interval {_ci:.0f} days"
        st.markdown(f'<div class="section">{_hdr}</div>', unsafe_allow_html=True)
        _open = [b for b in _breed_hist if b["status"] in ("Served", "Pregnant")]
        if _open:
            _b0 = _open[0]
            _dleft = days_until(_b0["due_date"] or "")
            if _b0["status"] == "Pregnant" and _dleft is not None:
                _txt = ("overdue by " + str(abs(_dleft)) + " days"
                        if _dleft < 0 else f"due in {_dleft} days")
                st.info(T(f"Currently pregnant — expected to calve "
                        f"{_b0['due_date']} ({_txt})."))
            else:
                st.info(T(f"Served {_b0['service_date']} — awaiting pregnancy result. "
                        f"Expected calving {_b0['due_date']}."))
        st.dataframe(pd.DataFrame([{
            "Served": b["service_date"],
            "Sire": b["bull_tag"] or "—",
            "Method": b["method"] or "—",
            "Status": b["status"],
            "Due": b["due_date"] or "—",
            T("Calved"): b["calving_date"] or "—",
            SP["Young"]: b["calf_tag"] or "—",
        } for b in _breed_hist]), use_container_width=True, hide_index=True)

    if len(imgs) > 1:
        st.markdown('<div class="section">More photos</div>', unsafe_allow_html=True)
        extra = imgs[1:]
        img_cols = st.columns(len(extra))
        for col, im in zip(img_cols, extra):
            col.image(im["image"], caption=im["filename"], width=150)
    elif not imgs:
        st.caption("No photos on file. Add some in the Edit / Remove tab.")


# ──────────────────────────────────────────────
# APP
# ──────────────────────────────────────────────
init_db()
# Move calves on to weaner, heifer and so on as they age — checked once a day.
_auto_moved = run_daily_progression()

# ── Activation ────────────────────────────────
# Nothing below is drawn until the system has been unlocked on this computer.
# It is asked for once; after that this block falls straight through.
if not is_activated():
    st.markdown(f"""
    <style>
      .block-container {{ padding-top:9vh !important; max-width:560px; }}
      .stApp {{ background:#FFFFFF url("{WELCOME_BG_URI}") left top/cover
                no-repeat fixed !important; }}
      [data-testid="stAppViewContainer"], [data-testid="stMain"],
      [data-testid="stMainBlockContainer"], .main {{
          background:transparent !important; }}
      .act-mark {{ display:block; margin:0 auto .8rem auto;
          width:clamp(92px,17vw,132px); height:auto; }}
      .act-name {{ text-align:center; font-size:clamp(1.25rem,4vw,1.9rem);
          font-weight:800; letter-spacing:.1em; color:{PRIMARY};
          text-transform:uppercase; line-height:1.15; margin:0 0 .2rem 0; }}
      .act-sub {{ text-align:center; color:{MUTED}; font-size:.82rem;
          letter-spacing:.04em; margin-bottom:1.6rem; }}
      .act-foot {{ text-align:center; color:{MUTED}; font-size:.7rem;
          margin-top:1.6rem; letter-spacing:.03em; line-height:1.7; }}
    </style>
    <img class="act-mark" src="{HDM_MARK_URI}" alt="Health Data Matrics"/>
    <div class="act-name">{SYSTEM_WORD}</div>
    <div class="act-sub">Enter your access key to set this computer up</div>
    """, unsafe_allow_html=True)

    _ac = st.columns([1, 6, 1])
    with _ac[1]:
        with st.form("activation"):
            _key_typed = st.text_input(
                "Access key", type="password", placeholder="Access key",
                label_visibility="collapsed",
                help="Supplied with your copy of the system. It is asked for "
                     "once on each computer.")
            _unlock = st.form_submit_button("Unlock this computer",
                                            type="primary",
                                            use_container_width=True)

        # A wrong key costs a little more each time, so the box cannot be
        # worked through quickly.
        _tries = st.session_state.get("activation_tries", 0)
        if _unlock:
            if key_is_correct(_key_typed):
                _saved, _where = record_activation()
                st.session_state["activation_tries"] = 0
                if _saved:
                    st.success("Unlocked. Starting the system…")
                    st.rerun()
                else:
                    st.error("The key is right, but the unlock could not be "
                             "saved: " + _where)
            else:
                st.session_state["activation_tries"] = _tries + 1
                if _tries + 1 >= 3:
                    import time as _time
                    _time.sleep(min(8, (_tries + 1) - 2))
                st.error("That key is not right. Check it and try again.")

        st.caption("Lost your key? Ask Health Data Matrics (HDM Group) for it. "
                   "Your records are not touched by this screen.")

    st.markdown('<div class="act-foot">Health Data Matrics (HDM Group)'
                '<br/>&copy; ' + str(date.today().year)
                + ' All Rights Reserved</div>', unsafe_allow_html=True)
    st.stop()


# ── Welcome screen ────────────────────────────
# The program opens on two icons side by side — the cow for cattle, the goat
# for small stock. Nothing else is drawn until one is pressed, which gives the
# app a front door rather than dropping straight into a wall of tabs.
# st.stop() below halts the script, so the rest of the page — the header, the
# dashboard, every tab — is never built on this run.
if not st.session_state.get("entered_app"):
    st.markdown(T(f"""
    <style>
      /* The whole welcome page is a single button, so styling every button
         here is safe: there is exactly one on screen. */
      /* The halftone wash. Anchored top-left so the dense corner stays put,
         sized to cover, and fixed so it does not slide when the page
         scrolls. The panels Streamlit stacks on top are cleared to
         transparent, otherwise their own white would hide it. */
      .stApp {{
          background:#FFFFFF url("{WELCOME_BG_URI}") left top/cover no-repeat
                     fixed !important; }}
      [data-testid="stAppViewContainer"], [data-testid="stMain"],
      [data-testid="stMainBlockContainer"], .main, .block-container {{
          background:transparent !important; }}
      .block-container {{ padding-top:6vh !important; }}
      .welcome-mark {{ display:block; margin:0 auto .7rem auto;
          width:clamp(84px,16vw,124px); height:auto; }}
      .welcome-word {{ text-align:center; font-size:clamp(1.5rem,5.2vw,3.1rem);
          font-weight:800; letter-spacing:.12em; color:{PRIMARY};
          text-transform:uppercase; margin:0 0 .25rem 0; line-height:1.1;
          text-wrap:balance; }}
      .welcome-sub {{ text-align:center; color:{MUTED}; font-size:.82rem;
          letter-spacing:.05em; margin-bottom:1.6rem; }}
      .welcome-foot {{ text-align:center; color:{MUTED}; font-size:.7rem;
          margin-top:1.4rem; letter-spacing:.03em; line-height:1.7; }}
      .welcome-pick {{ text-align:center; font-size:.82rem; font-weight:800;
          letter-spacing:.14em; text-transform:uppercase; color:{PRIMARY};
          margin:.75rem 0 0 0; }}
      div[data-testid="stButton"] > button {{
          display:block; margin:0 auto;
          width:min(185px, 20vw); height:min(185px, 20vw);
          border-radius:50%; border:none; padding:0;
          background:#FFFFFF url("{LOGO_DISC_URI}") center/100% 100% no-repeat;
          box-shadow:0 6px 26px rgba(6,52,58,.14);
          color:transparent !important; font-size:0 !important;
          cursor:pointer;
          transition:box-shadow .18s ease, transform .12s ease; }}
      /* Streamlit tints a button on hover and focus using the theme colour,
         which washes over the animal. Every state is pinned to the same white
         backing and the same image, so the mark never changes colour — only
         the shadow moves, to show it can be pressed. */
      div[data-testid="stButton"] > button:hover,
      div[data-testid="stButton"] > button:focus,
      div[data-testid="stButton"] > button:focus-visible,
      div[data-testid="stButton"] > button:active,
      div[data-testid="stButton"] > button:disabled {{
          background:#FFFFFF url("{LOGO_DISC_URI}")
                     center/100% 100% no-repeat !important;
          background-color:#FFFFFF !important;
          border:none !important; outline:none !important;
          color:transparent !important; filter:none !important;
          opacity:1 !important; }}
      div[data-testid="stButton"] > button:hover {{
          box-shadow:0 14px 34px rgba(6,52,58,.22) !important; }}
      div[data-testid="stButton"] > button:active {{
          transform:scale(.98);
          box-shadow:0 4px 14px rgba(6,52,58,.18) !important; }}
      /* No blue flash when tapped on a touchscreen either. */
      div[data-testid="stButton"] > button {{
          -webkit-tap-highlight-color:transparent; }}
      div[data-testid="stButton"] > button p {{ display:none; }}

      /* The other doors. The rules above paint the cow on every button, so
         each of the others is named here and given its own badge. Streamlit
         tags a widget's container with st-key-<key>; builds too old for that
         are covered by the column position — goats sit in the third column
         of the row below, sheep in the fourth. */
      .st-key-enter_goat div[data-testid="stButton"] > button,
      .st-key-enter_goat button,
      .st-key-enter_goat div[data-testid="stButton"] > button:hover,
      .st-key-enter_goat div[data-testid="stButton"] > button:focus,
      .st-key-enter_goat div[data-testid="stButton"] > button:focus-visible,
      .st-key-enter_goat div[data-testid="stButton"] > button:active,
      .st-key-enter_goat div[data-testid="stButton"] > button:disabled,
      div[data-testid="stHorizontalBlock"] > div:nth-child(3) div[data-testid="stButton"] > button,
      div[data-testid="stHorizontalBlock"] > div:nth-child(3) div[data-testid="stButton"] > button:hover,
      div[data-testid="stHorizontalBlock"] > div:nth-child(3) div[data-testid="stButton"] > button:focus,
      div[data-testid="stHorizontalBlock"] > div:nth-child(3) div[data-testid="stButton"] > button:focus-visible,
      div[data-testid="stHorizontalBlock"] > div:nth-child(3) div[data-testid="stButton"] > button:active,
      div[data-testid="stHorizontalBlock"] > div:nth-child(3) div[data-testid="stButton"] > button:disabled {{
          background:#FFFFFF url("{GOAT_DISC_URI}")
                     center/100% 100% no-repeat !important;
          background-color:#FFFFFF !important; }}
      .st-key-enter_sheep div[data-testid="stButton"] > button,
      .st-key-enter_sheep button,
      .st-key-enter_sheep div[data-testid="stButton"] > button:hover,
      .st-key-enter_sheep div[data-testid="stButton"] > button:focus,
      .st-key-enter_sheep div[data-testid="stButton"] > button:focus-visible,
      .st-key-enter_sheep div[data-testid="stButton"] > button:active,
      .st-key-enter_sheep div[data-testid="stButton"] > button:disabled,
      div[data-testid="stHorizontalBlock"] > div:nth-child(4) div[data-testid="stButton"] > button,
      div[data-testid="stHorizontalBlock"] > div:nth-child(4) div[data-testid="stButton"] > button:hover,
      div[data-testid="stHorizontalBlock"] > div:nth-child(4) div[data-testid="stButton"] > button:focus,
      div[data-testid="stHorizontalBlock"] > div:nth-child(4) div[data-testid="stButton"] > button:focus-visible,
      div[data-testid="stHorizontalBlock"] > div:nth-child(4) div[data-testid="stButton"] > button:active,
      div[data-testid="stHorizontalBlock"] > div:nth-child(4) div[data-testid="stButton"] > button:disabled {{
          background:#FFFFFF url("{SHEEP_DISC_URI}")
                     center/100% 100% no-repeat !important;
          background-color:#FFFFFF !important; }}
      .st-key-enter_pig div[data-testid="stButton"] > button,
      .st-key-enter_pig button,
      .st-key-enter_pig div[data-testid="stButton"] > button:hover,
      .st-key-enter_pig div[data-testid="stButton"] > button:focus,
      .st-key-enter_pig div[data-testid="stButton"] > button:focus-visible,
      .st-key-enter_pig div[data-testid="stButton"] > button:active,
      .st-key-enter_pig div[data-testid="stButton"] > button:disabled,
      div[data-testid="stHorizontalBlock"] > div:nth-child(5) div[data-testid="stButton"] > button,
      div[data-testid="stHorizontalBlock"] > div:nth-child(5) div[data-testid="stButton"] > button:hover,
      div[data-testid="stHorizontalBlock"] > div:nth-child(5) div[data-testid="stButton"] > button:focus,
      div[data-testid="stHorizontalBlock"] > div:nth-child(5) div[data-testid="stButton"] > button:focus-visible,
      div[data-testid="stHorizontalBlock"] > div:nth-child(5) div[data-testid="stButton"] > button:active,
      div[data-testid="stHorizontalBlock"] > div:nth-child(5) div[data-testid="stButton"] > button:disabled {{
          background:#FFFFFF url("{PIG_DISC_URI}")
                     center/100% 100% no-repeat !important;
          background-color:#FFFFFF !important; }}
    </style>
    <img class="welcome-mark" src="{HDM_MARK_URI}" alt="Health Data Matrics"/>
    <div class="welcome-word">{SYSTEM_WORD}</div>
    <div class="welcome-sub">Management &amp; Inventory System</div>
    """), unsafe_allow_html=True)

    # Three front doors, side by side. The spacer columns on either side keep
    # them centred on a wide screen and let them sit close together on a
    # phone. The order here is the order the CSS above expects.
    _doors = [("enter_app", "cattle", "Cattle",
               "Open the cattle management system"),
              ("enter_goat", "goat", "Goat",
               "Open the goat management system"),
              ("enter_sheep", "sheep", "Sheep",
               "Open the sheep management system"),
              ("enter_pig", "pig", "Piggery",
               "Open the piggery management system")]
    _wc = st.columns([1, 3, 3, 3, 3, 1], gap="small")

    # The labels are kept for screen readers and for the keyboard; the CSS
    # above hides the text and paints the animal on top.
    _picked = None
    for _col, (_key, _sec, _name, _label) in zip(_wc[1:5], _doors):
        with _col:
            if st.button(_label, key=_key, use_container_width=True):
                _picked = _sec
            st.markdown(f'<div class="welcome-pick">{_name}</div>',
                        unsafe_allow_html=True)

    if _picked:
        st.session_state["entered_app"] = True
        st.session_state["section"] = _picked
        st.rerun()

    st.markdown(T('<div class="welcome-sub" style="margin-top:1.1rem">'
                'Press an animal to open its herd</div>'),
                unsafe_allow_html=True)
    # Copyright line. The year comes from the clock so it never goes stale.
    st.markdown('<div class="welcome-foot">Health Data Matrics (HDM Group)'
                '<br/>&copy; ' + str(date.today().year)
                + ' All Rights Reserved</div>', unsafe_allow_html=True)
    st.stop()

# The title row, with a way back to the welcome screen beside the badge.
_hdr, _home = st.columns([9, 1])
_hdr.markdown(f"""
<div style="display:flex;align-items:center;gap:14px;margin-bottom:.9rem">
  <img src="{SP["disc"]}" alt="logo"
       style="width:54px;height:54px;border-radius:50%;object-fit:cover;
              box-shadow:0 2px 8px rgba(6,52,58,.18)"/>
  <span class="big-title" style="display:inline-block;margin:0">{SP["heading"]}</span>
</div>""", unsafe_allow_html=True)
# Back to the opening screen, level with the title.
if _home.button("⌂  Home", key="back_to_welcome", use_container_width=True,
                help="Back to the opening screen"):
    st.session_state["entered_app"] = False
    st.session_state["section"] = "cattle"
    st.rerun()

(tab_herd, tab_inv, tab_load, tab_edit, tab_cal, tab_act, tab_breed, tab_bill,
 tab_exp, tab_loss, tab_analytics, tab_backup) = st.tabs(
    [T("Herd"), "Inventory", T("Load Cattle"), "Edit / Remove",
     "Calendar", "Activity", "Breeding", "Billing", "Expenses",
     "Death / Missing", "Analytics", "Backup"])

# ── HERD SHEET + SEARCH ───────────────────────
with tab_herd:
    # The counts open the tab: the tab strip belongs directly under the
    # heading, and the screen explains itself without a strapline.
    _total, _active, _fem, _male = herd_metrics()

    # If a tag was clicked anywhere, the profile takes the place of the list.
    sel = st.session_state.get("selected_tag")
    _showing_profile = bool(sel and get_cow(sel))

    # The counts and the search box stack down the left; the things-to-do
    # card takes the column beside them and runs the height of both, ending
    # level with the top of the list below.
    _sum_l, _sum_r = st.columns([4, 2.2], gap="medium")

    with _sum_l:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(T("Total cattle"), f"{_total:,}")
        m2.metric("Active", f"{_active:,}")
        m3.metric("Females", f"{_fem:,}")
        m4.metric("Males", f"{_male:,}")

        if not _showing_profile:
            st.session_state["selected_tag"] = None
            st.markdown('<div class="section" style="margin:.5rem 0 .45rem 0">'
                        'Search by tag</div>', unsafe_allow_html=True)
            # Version counter clears the box by giving it a new key next run.
            _tv = st.session_state.get("tag_search_version", 0)
            # The last column is empty on purpose: the search box and its two
            # buttons stop under the Females card rather than running the
            # width of the page.
            sc1, sc2, sc3, _ssp = st.columns([2.6, 1, 1, 1.4])
            term = sc1.text_input("Tag", key=f"search_term_{_tv}",
                                  label_visibility="collapsed",
                                  placeholder="Type a tag (or name)…")
            do_search = sc2.button("Search", type="primary",
                                   use_container_width=True)
            clear = sc3.button("Clear", use_container_width=True)

        # Ticking tasks off belongs under the search, in the same column, so
        # the card beside it has something to run down to.
        _outstanding = overdue_reminders(limit=50) + upcoming_reminders(limit=50)
        if _outstanding:
            with st.expander(f"✔️ Mark a task as done "
                             f"({len(_outstanding)} outstanding)"):
                for _r in _outstanding:
                    _dc = st.columns([0.5, 9])
                    if _dc[0].checkbox("done", value=False,
                                       key=f"hdrdone_{_r['id']}",
                                       label_visibility="collapsed"):
                        set_reminder_done(_r["id"], True)
                        st.success(f"Marked done: {_r['task']}")
                        st.rerun()
                    _lbl = f"{_when_label(_r['date'])} — {_r['task']}"
                    if _r["priority"] == "High":
                        _lbl += " 🔴"
                    if _r["details"]:
                        _lbl += f" · {_r['details']}"
                    _dc[1].markdown(_lbl)
                st.caption("Ticking a task marks it complete and removes it "
                           "from the card. Completed tasks stay on their day "
                           "in the Calendar tab.")

    with _sum_r:
        st.markdown(todo_card_html(), unsafe_allow_html=True)

    if _showing_profile:
        profile_view(get_cow(sel))
    else:
        if clear:
            st.session_state["search_active"] = False
            st.session_state["search_query"] = ""
            st.session_state["tag_search_version"] = _tv + 1
            st.rerun()

        if do_search:
            st.session_state["search_active"] = True
            st.session_state["search_query"] = term

        _sq = st.session_state.get("search_query", "")
        if st.session_state.get("search_active") and _sq.strip():
            results = search_cattle(_sq)
            if results:
                st.success(f"Found {len(results)} match"
                           + ("es" if len(results) != 1 else "") + ".")
                for cow in results:
                    with st.container(border=True):
                        rc = st.columns([2, 1])
                        rc[0].markdown(f"### 🏷️ {cow['tag']}")
                        rc[0].markdown(status_chip(cow["status"]), unsafe_allow_html=True)
                        if rc[1].button("Open profile ➡️", key=f"searchopen_{cow['tag']}",
                                        use_container_width=True):
                            st.session_state["selected_tag"] = cow["tag"]
                            st.rerun()
                        thumbs = get_images(cow["tag"])
                        if thumbs:
                            st.image(thumbs[0]["image"], width=110)
            else:
                st.warning(T(f"No cow found matching '{_sq}'."))
            st.divider()

        st.markdown(T('<div class="section">Full herd</div>'),
                    unsafe_allow_html=True)
        # Optional filter so a big herd stays navigable.
        list_filter = st.text_input(
            "Filter the list (optional)", key="herd_list_filter",
            placeholder=T("Tag, name, breed, sex, category, age, colour or a "
                          "date — leave blank for the whole herd…"),
            help="Matches anywhere in the tag, name, breed, sex, category, "
                 "colour, status, origin or either date. A bare number is "
                 "read as an age in years, so 4 finds the four-year-olds, "
                 "and 2023 finds everything born or acquired that year."
            ).strip()
        total_matches = cattle_match_count(list_filter)

        if total_matches:
            PAGE = 25
            pages = (total_matches + PAGE - 1) // PAGE
            pkey = "herd_page"
            page = st.session_state.get(pkey, 1)
            page = max(1, min(page, pages))

            nav = st.columns([1, 1, 3, 1, 1])
            if nav[0].button("⏮ First", use_container_width=True, disabled=page <= 1,
                             key="herd_first"):
                st.session_state[pkey] = 1
                st.rerun()
            if nav[1].button("◀ Prev", use_container_width=True, disabled=page <= 1,
                             key="herd_prev"):
                st.session_state[pkey] = page - 1
                st.rerun()
            nav[2].markdown(
                T(f"<div style='text-align:center;color:{MUTED};font-weight:600;"
                f"padding-top:6px'>Page {page:,} of {pages:,} · {total_matches:,} "
                "cattle</div>"), unsafe_allow_html=True)
            if nav[3].button("Next ▶", use_container_width=True, disabled=page >= pages,
                             key="herd_next"):
                st.session_state[pkey] = page + 1
                st.rerun()
            if nav[4].button("Last ⏭", use_container_width=True, disabled=page >= pages,
                             key="herd_last"):
                st.session_state[pkey] = pages
                st.rerun()

            rows = cattle_page(PAGE, (page - 1) * PAGE, list_filter)
            photo_map = image_counts_map([r["tag"] for r in rows])

            # A clean, aligned table. On Streamlit 1.35+ a row can be selected to
            # open that animal; older versions fall back to a button per tag.
            _list_df = pd.DataFrame([{
                "Tag": r["tag"],
                "Breed": r["breed"] or "—",
                "Sex": r["sex"] or "—",
                "Category": r["category"] or "—",
                **({"Brand": r.get("brand_number") or "—"}
                   if SP["branded"] else {}),
                "Age": age_str(r["dob"]) or "—",
                "Status": r["status"],
                "Photo": "Yes" if photo_map.get(r["tag"], 0) else "—",
            } for r in rows])

            _cfg = {}
            if hasattr(st, "column_config"):
                try:
                    _T = st.column_config.TextColumn
                    _cfg = {"Tag": _T("Tag", width="medium"),
                            "Breed": _T("Breed", width="medium"),
                            "Sex": _T("Sex", width="small"),
                            "Category": _T("Category", width="small"),
                            "Age": _T("Age", width="small"),
                            "Status": _T("Status", width="small"),
                            "Photo": _T("Photo", width="small")}
                except Exception:
                    _cfg = {}

            _evt, _selectable = None, False
            if "on_select" in inspect.signature(st.dataframe).parameters:
                try:
                    st.caption("Select a row to open that animal's profile.")
                    _evt = st.dataframe(
                        _list_df, use_container_width=True, hide_index=True,
                        on_select="rerun", selection_mode="single-row",
                        key=f"herd_table_{page}_{list_filter}", column_config=_cfg)
                    _selectable = True
                except TypeError:
                    _selectable = False

            if _selectable:
                try:
                    _picked = list(_evt.selection["rows"])
                except Exception:
                    _picked = []
                if _picked and 0 <= _picked[0] < len(rows):
                    st.session_state["selected_tag"] = rows[_picked[0]]["tag"]
                    st.rerun()
            else:
                st.dataframe(_list_df, use_container_width=True, hide_index=True,
                             column_config=_cfg)
                st.caption("Open a profile:")
                _bcols = st.columns(5)
                for _i, cow in enumerate(rows):
                    if _bcols[_i % 5].button(cow["tag"], key=f"open_{cow['tag']}",
                                             use_container_width=True):
                        st.session_state["selected_tag"] = cow["tag"]
                        st.rerun()

            st.caption("Export")
            ec1, ec2, _ecsp = st.columns([1.5, 1.8, 3])
            _dl(ec1,
                "Download page (CSV)",
                to_dataframe(rows, photo_map).to_csv(index=False).encode("utf-8"),
                file_name=f"herd_page_{page}_{date.today().isoformat()}.csv",
                mime="text/csv", key="dl_herd_page")

            def _full_herd_csv():
                return pd.read_sql_query(
                    "SELECT tag, name, breed, sex, category, "
                    + ("brand_number, " if SP["branded"] else "")
                    + "dob, colour, weight, status, date_acquired, mother_tag, "
                    "origin_location, notes FROM cattle ORDER BY tag",
                    get_conn()).to_csv(index=False).encode("utf-8")

            _all_n = total_cattle_count()
            if _all_n <= 5000:
                _dl(ec2,
                    T(f"Download all {_all_n:,} cattle (CSV)"), _full_herd_csv(),
                    file_name=f"herd_full_{date.today().isoformat()}.csv",
                    mime="text/csv", key="dl_herd_full")
            else:
                # Very large herd: build the file only when asked for.
                if ec2.button("Prepare full export", key="prep_full_csv"):
                    st.session_state["herd_full_csv"] = True
                if st.session_state.get("herd_full_csv"):
                    _dl(st,
                        T(f"Download all {_all_n:,} cattle (CSV)"), _full_herd_csv(),
                        file_name=f"herd_full_{date.today().isoformat()}.csv",
                        mime="text/csv", key="dl_herd_full_prepared")
        elif list_filter:
            st.warning(T(f"No cattle match '{list_filter}'."))
        else:
            st.info(T("No cattle loaded yet. Add some in the Load Cattle tab."))

# ── INVENTORY OVERVIEW ────────────────────────
with tab_inv:
    # Farm & farmer details — printed on the header of every report.
    _fp = get_farm_profile()
    _fp_set = any([_fp["farmer_name"], _fp["location"], _fp["phone"], _fp["email"]])
    _fp_summary = " · ".join(x for x in [_fp["farm_name"], _fp["farmer_name"],
                                         _fp["location"]] if x)
    with st.expander(f"🏠 Farm details — {_fp_summary}" if _fp_set
                     else "🏠 Farm details — add your farm and contact information",
                     expanded=not _fp_set):
        st.caption("These details appear at the top of every PDF report, invoice "
                   "and receipt you download.")
        with st.form("farm_profile_form"):
            fp1, fp2 = st.columns(2)
            f_farm = fp1.text_input("Farm name", value=_fp["farm_name"],
                                    placeholder=T("e.g. Tswana Cattle Farm"))
            f_farmer = fp2.text_input("Farmer's name", value=_fp["farmer_name"],
                                      placeholder="e.g. K. Rampa")
            f_loc = st.text_input("Farm location", value=_fp["location"],
                                  placeholder="e.g. Plot 452, Serowe, Central District")
            fp3, fp4 = st.columns(2)
            f_phone = fp3.text_input("Contact number", value=_fp["phone"],
                                     placeholder="e.g. +267 71 234 567")
            f_email = fp4.text_input("Email", value=_fp["email"],
                                     placeholder="e.g. farm@example.com")
            if st.form_submit_button("Save farm details", type="primary"):
                save_farm_profile({"farm_name": f_farm, "farmer_name": f_farmer,
                                   "location": f_loc, "phone": f_phone,
                                   "email": f_email})
                st.success("Farm details saved — they'll appear on every report.")
                st.rerun()

        # ── Farm logo ────────────────────────
        st.markdown("Farm logo")
        _logo = get_farm_logo()
        _lv = st.session_state.get("logo_version", 0)
        lg1, lg2 = st.columns([1, 2])
        if _logo:
            lg1.image(_logo, width=110)
            lg1.caption("Currently used on documents")
        else:
            lg1.image(base64.b64decode(LOGO_B64), width=90)
            lg1.caption("Built-in logo (no farm logo uploaded)")
        _up_logo = lg2.file_uploader(
            "Upload your farm logo", type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=False, key=f"farm_logo_{_lv}",
            help="Appears on every PDF report, invoice and receipt. A square or "
                 "wide image both work; PNG keeps a transparent background.")
        lb1, lb2 = lg2.columns(2)
        if lb1.button("Save logo", type="primary", key=f"save_logo_{_lv}"):
            if not _up_logo:
                st.warning("Choose an image first.")
            else:
                _ok, _msg = save_farm_logo(_up_logo.getvalue(),
                                           getattr(_up_logo, "name", ""))
                if _ok:
                    st.session_state["logo_version"] = _lv + 1
                    st.success(_msg)
                    st.rerun()
                else:
                    st.error(_msg)
        if _logo and lb2.button("Remove logo", key=f"rm_logo_{_lv}"):
            clear_farm_logo()
            st.session_state["logo_version"] = _lv + 1
            st.success("Farm logo removed — documents will use the built-in logo.")
            st.rerun()

    # ── Automatic category progression ───────
    _ps = progression_settings()
    _pending = category_progression(apply_changes=False)
    _psum = (f"{_ps['wean_months']} months to weaner · "
             f"{_ps['heifer_months']} months to "
             f"{SP['maiden_cat'].lower()} / {_ps['male_default'].lower()}")
    with st.expander(T(f"Category progression — {_psum}")
                     + (f" · {len(_pending)} due to move" if _pending else "")):
        st.caption(
            f"{SP['Many']} move up a category as they age: "
            f"{SP['young_cat'].lower()} → {SP['weaner_cat'].lower()} → "
            f"{SP['maiden_cat'].lower()} or young male. A "
            f"{SP['maiden_cat'].lower()} becomes a "
            f"{SP['adult_f_cat'].lower()} once she has "
            + T("calved. Runs automatically once a day; categories you set "
                "yourself are never moved backwards."))
        with st.form("progression_form"):
            pf1, pf2, pf3 = st.columns(3)
            _wean = pf1.number_input(T("Calf until (months)"), min_value=1,
                                     max_value=24, value=int(_ps["wean_months"]),
                                     step=1)
            _heif = pf2.number_input("Weaner until (months)", min_value=2,
                                     max_value=48, value=int(_ps["heifer_months"]),
                                     step=1)
            _maled = pf3.selectbox(
                "Males become", SP["male_options"],
                index=0 if _ps["male_default"] == SP["male_options"][0] else 1,
                help=("Castration isn't tracked, so young males move to this "
                      "category. Animals already marked "
                      + " or ".join(SP["male_options"])
                      + " are left alone."), format_func=T)
            _auto = st.checkbox("Update categories automatically each day",
                                value=str(_ps.get("auto", "1")) == "1")
            if st.form_submit_button("Save progression settings", type="primary"):
                if _heif <= _wean:
                    st.warning(T("The weaner age must be greater than the calf age."))
                else:
                    save_progression_settings(_wean, _heif, _maled, _auto)
                    st.success("Progression settings saved.")
                    st.rerun()

        if _pending:
            st.markdown(f"{len(_pending)} animal(s) are due to move category")
            st.dataframe(pd.DataFrame([{
                "Tag": t, "From": was, "To": to, "Why": why}
                for t, was, to, why in _pending[:200]]),
                use_container_width=True, hide_index=True)
            if st.button("Update these now", type="primary", key="prog_run"):
                _done = category_progression(apply_changes=True)
                set_meta("prog_last_run", date.today().isoformat())
                st.success(f"{len(_done)} animal(s) moved to their new category.")
                st.rerun()
        else:
            st.success("Every animal is in the right category for its age.")
        _lr = get_meta("prog_last_run", "")
        if _lr:
            st.caption(f"Last checked: {_lr}")

    st.markdown(T('<div class="section">Herd inventory</div>'), unsafe_allow_html=True)
    if total_cattle_count() == 0:
        st.info(T("No cattle loaded yet. Add some in the Load Cattle tab."))
    else:
        total_all = total_cattle_count()
        active_n = status_count("Active")
        sold_n = status_count("Sold")
        dec_n = status_count("Deceased")

        # Headline counts — the live stock position of the herd.
        i1, i2, i3, i4 = st.columns(4)
        i1.metric("Total on record", f"{total_all:,}")
        i2.metric("Active head", f"{active_n:,}")
        i3.metric("Sold", f"{sold_n:,}")
        i4.metric("Deceased", f"{dec_n:,}")

        total_weight = active_weight_sum()
        fem_n, male_n = active_sex_counts()
        j1, j2, j3 = st.columns(3)
        j1.metric("Females (active)", f"{fem_n:,}")
        j2.metric("Males (active)", f"{male_n:,}")
        j3.metric(T("Active herd weight"), f"{total_weight:,.0f} kg" if total_weight else "—")

        st.caption("Breakdowns below count active head only — your current stock on hand.")

        def breakdown(label, column):
            counts = pd.Series(group_counts(column, active_only=True))
            if counts.empty:
                st.info(f"No {label.lower()} recorded yet.")
                return
            counts = counts.sort_values(ascending=False)
            cols = st.columns([1.15, 2])
            styled_breakdown_table(cols[0], counts, label)
            value_bar_chart(cols[1], counts, color=PRIMARY, height=240,
                            key=f"inv_chart_{column}")

        st.markdown('<div class="section">By category</div>', unsafe_allow_html=True)
        breakdown("Category", "category")

        st.markdown('<div class="section">By sex</div>', unsafe_allow_html=True)
        breakdown("Sex", "sex")

        st.markdown('<div class="section">By breed</div>', unsafe_allow_html=True)
        breakdown("Breed", "breed")

        st.markdown(T('<div class="section">By status (whole herd)</div>'),
                    unsafe_allow_html=True)
        status_counts = pd.Series(group_counts("status", active_only=False)
                                  ).sort_values(ascending=False)
        scols = st.columns([1.15, 2])
        styled_breakdown_table(scols[0], status_counts, "Status")
        value_bar_chart(scols[1], status_counts, color=TEAL2, height=240,
                        key="inv_chart_status")

        st.divider()
        _inv_pdf = inventory_pdf()
        if _inv_pdf:
            _dl(st,
                "Download inventory report (PDF)", _inv_pdf,
                file_name=f"herd_inventory_{date.today().isoformat()}.pdf",
                mime="application/pdf", key="dl_inventory_pdf")
            st.caption("Includes the headline figures, every breakdown table and "
                       "its chart.")
        else:
            st.caption("Install reportlab to enable the inventory PDF report.")


# ── LOAD CATTLE ───────────────────────────────
with tab_load:
    _load_one, _load_many = st.tabs(["One at a time", "Bulk import"])

    with _load_one:
        st.markdown(T('<div class="section">Load a cow by tag</div>'), unsafe_allow_html=True)
        st.caption(T("Each cow is identified by its tag. Add as many as you like — the form "
                   "clears after each one so you can keep going."))
        with st.form("load_cattle", clear_on_submit=True):
            r1c1, r1c2 = st.columns(2)
            tag = r1c1.text_input("Tag *", placeholder="e.g. BW-0421")
            breed = r1c2.text_input("Breed",
                                    placeholder=f"e.g. {SP['breeds'][0]}")

            r2c1, r2c2, r2c3 = st.columns(3)
            sex = r2c1.selectbox("Sex", [""] + SEXES, format_func=T)
            category = r2c2.selectbox("Category", [""] + CATEGORIES, format_func=T)
            colour = r2c3.text_input("Colour", placeholder="e.g. Brown & white")

            r3c1, r3c2, r3c3 = st.columns(3)
            dob = r3c1.date_input(
                "Date of birth", value=None,
                min_value=earliest_selectable(years_back=DOB_YEARS_BACK),
                max_value=date.today(), format="DD/MM/YYYY",
                help="Click the month and year at the top of the calendar to "
                     "jump straight to the year she was born.")
            weight = r3c2.number_input("Weight (kg)", min_value=0.0, value=0.0, step=1.0)
            status = r3c3.selectbox("Status", STATUSES, index=0, format_func=T)

            # Cattle are branded; goats and sheep are not, so the field
            # is simply not there for them.
            brand_number = st.text_input(
                "Brand number",
                placeholder="e.g. A042 or 4B21 (letters & numbers)"
            ) if SP["branded"] else ""

            approx_age = st.number_input(
                "Approximate age in years (only used if date of birth is unknown)",
                min_value=0, max_value=40, value=0, step=1,
                help="If you don't know the exact birth date, enter the age and the "
                     "system will keep it up to date automatically every year.")

            acquired = st.date_input(
                "Date acquired", value=date.today(),
                min_value=earliest_selectable(), max_value=date.today(),
                format="DD/MM/YYYY")
            notes = st.text_area("Notes (optional)",
                                 placeholder="e.g. mother tag, health notes, purchase source…")

            st.markdown(T("Acquisition — how did this cow join the herd?"))
            aq1, aq2 = st.columns(2)
            acquisition = aq1.selectbox("Acquisition type",
                                        ["Born on farm", "Bought", "Other"], format_func=T)
            buy_price = aq2.number_input(f"If bought — price ({currency_symbol()})",
                                         min_value=0.0, value=0.0, step=50.0)
            aq3, aq4 = st.columns(2)
            buy_seller = aq3.text_input("If bought — seller name",
                                        placeholder="e.g. K. Rampa")
            buy_method = aq4.selectbox("If bought — payment method", PAYMENT_METHODS, format_func=T)
            buy_contact = st.text_input("If bought — seller contact",
                                        placeholder="phone / email")
            mother_tag = st.text_input("If born on farm — mother's tag (optional)",
                                       placeholder="e.g. BW-0421")
            origin_location = st.text_input(
                T("Location the cow is coming from"),
                placeholder=T("e.g. Serowe cattle post, Molepolole auction, own farm"))
            st.caption(T("Choosing Bought records a purchase receipt in Billing and logs "
                       "a Purchase activity on the Calendar for the *Date acquired*. "
                       "The mother's tag links this calf to its mother's profile."))

            photo = st.file_uploader(
                T("Photo of this cow (one passport-size image)"),
                type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=False,
                key="load_cow_photo",
                help=T("Attach one clear, passport-style photo of this cow."))

            submit = st.form_submit_button(T("Load cow into herd"), type="primary")

        if submit:
            brand_num = brand_number.strip()
            if not tag.strip():
                st.warning(T("A tag is required to load a cow."))
            elif brand_num and not brand_num.isalnum():
                st.warning("Brand number can contain letters and numbers only.")
            else:
                dob_iso = dob.isoformat() if dob else ""
                if not dob_iso and approx_age:
                    dob_iso = est_dob_from_age(approx_age)
                rec = {
                    "tag": tag.strip(), "name": "", "breed": breed.strip(),
                    "sex": sex, "dob": dob_iso,
                    "colour": colour.strip(), "weight": float(weight) if weight else None,
                    "category": category, "status": status,
                    "date_acquired": acquired.isoformat() if acquired else "",
                    "notes": notes.strip(),
                    "brand_number": brand_num,
                    "mother_tag": mother_tag.strip() if acquisition == "Born on farm" else "",
                    "origin_location": origin_location.strip(),
                }
                if add_cow(rec):
                    msg = f"✅ Loaded cow with tag '{rec['tag']}' into the herd."
                    if rec["weight"]:
                        # First entry in this animal's weight history.
                        add_weight(rec["tag"],
                                   rec["date_acquired"] or date.today().isoformat(),
                                   rec["weight"], "Weight at loading")
                    if photo:
                        add_images(rec["tag"], [photo])
                        msg += " Photo saved."
                    if acquisition == "Bought":
                        ev_date = rec["date_acquired"] or date.today().isoformat()
                        receipt = next_receipt_no()
                        add_purchase({
                            "receipt_no": receipt, "date": ev_date, "tag": rec["tag"],
                            "cow_name": rec["name"], "breed": rec["breed"], "sex": rec["sex"],
                            "seller": buy_seller.strip(), "seller_contact": buy_contact.strip(),
                            "weight": rec["weight"], "price": float(buy_price),
                            "payment_method": buy_method, "notes": "",
                        })
                        detail = (("Bought from " + buy_seller.strip())
                                  if buy_seller.strip() else "Bought")
                        if buy_price:
                            detail += f" — {money(buy_price)}"
                        add_event(ev_date, "Purchase", rec["tag"], detail)
                        msg += (f" Purchase receipt {receipt} created and a Purchase "
                                "activity was logged on the calendar.")
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(T(f"⚠️ A cow with tag '{rec['tag']}' already exists. "
                             "Tags must be unique — edit it in the Edit / Remove tab."))


    # ── BULK IMPORT FROM A SPREADSHEET ────────
    with _load_many:
        st.markdown('<div class="section">Bulk import from Excel or CSV</div>',
                    unsafe_allow_html=True)
        st.caption(T("Load a whole herd in one go: download the template, fill it "
                   "in, upload it, check the preview, then confirm. Nothing is "
                   "saved until you press the import button."))

        # Outcome of the last import, kept on screen until dismissed.
        _done = st.session_state.get("bulk_import_result")
        if _done:
            st.success(_done["message"])
            _d1, _d2 = st.columns([2, 1])
            if _done.get("report"):
                _dl(_d1,
                    "Download the rows that were not loaded",
                    _done["report"],
                    file_name=f"cattle_import_issues_{date.today().isoformat()}.csv",
                    mime="text/csv", use_container_width=True,
                    key="bulk_issue_report")
            if _d2.button("Dismiss", use_container_width=True, key="bulk_dismiss"):
                st.session_state["bulk_import_result"] = None
                st.rerun()
            st.divider()

        _tpl = import_template_frame()
        _t1, _t2 = st.columns(2)
        _dl(_t1, "Template (CSV)",
                            _tpl.to_csv(index=False).encode("utf-8"),
                            file_name="cattle_import_template.csv",
                            mime="text/csv", use_container_width=True,
                            key="bulk_tpl_csv")
        _tpl_xlsx = import_template_xlsx_bytes()
        if _tpl_xlsx:
            _dl(_t2,
                "Template (Excel)", _tpl_xlsx,
                file_name="cattle_import_template.xlsx",
                mime=("application/vnd.openxmlformats-officedocument"
                      ".spreadsheetml.sheet"),
                use_container_width=True, key="bulk_tpl_xlsx")
        else:
            _t2.caption("The Excel template needs the openpyxl package. "
                        "The CSV template works either way.")

        with st.expander("Which columns can I use?"):
            st.markdown(
                "Tag is the only column you must have. Everything else is "
                "optional, and any extra columns are simply ignored.\n\n"
                "| Column | Accepts |\n|---|---|\n"
                "| Tag | also *Tag no*, *Ear tag*, *Animal tag* |\n"
                "| Name | free text |\n"
                "| Breed | free text |\n"
                "| Sex | " + " / ".join(SEXES) + " (F and M work too) |\n"
                "| Category | " + ", ".join(CATEGORIES) + " |\n"
                "| Status | " + ", ".join(STATUSES) + " (blank uses the "
                "default below) |\n"
                "| Date of birth | 14/03/2022 or 2022-03-14 |\n"
                "| Age (years) | used only when the birth date is blank |\n"
                "| Weight (kg) | a number; *kg* in the cell is fine |\n"
                "| Date acquired | blank uses the default below |\n"
                + ("| Brand number | letters and numbers only |\n"
                   if SP["branded"] else "")
                + T("| Mother tag | also *Dam*; links a calf to its "
                    "mother |\n")
                + "| Origin location | also *Cattle post*, *Source* |\n"
                "| Colour, Notes | free text |\n\n"
                "Headings are matched loosely, so capitals, spaces, underscores "
                "and brackets do not matter. Slash dates are read day-first, so "
                "03/04/2024 means 3 April 2024. A weight in the sheet also "
                "starts that animal's weight history.")

        _o1, _o2 = st.columns(2)
        _bulk_status = _o1.selectbox("Status for rows that leave it blank",
                                     STATUSES, index=0, key="bulk_default_status", format_func=T)
        _bulk_acq = _o2.date_input("Date acquired for rows that leave it blank",
                                   value=date.today(),
                                   min_value=earliest_selectable(),
                                   max_value=date.today(),
                                   format="DD/MM/YYYY",
                                   key="bulk_default_acq")
        _bulk_dupes = st.radio(
            T("If a tag is already in the herd"),
            ["Leave the existing record alone",
             "Update it with the values from the sheet"],
            key="bulk_dup_mode", horizontal=True, format_func=T)
        _update_dupes = _bulk_dupes.startswith("Update")

        _upload = st.file_uploader(
            "Choose an Excel or CSV file",
            type=["xlsx", "xlsm", "xltx", "csv"],
            key="bulk_upload_" + str(st.session_state.get("bulk_upload_version", 0)),
            help="Old .xls files should be saved as .xlsx first.")

        if _upload is not None:
            _frame = None
            try:
                _frame = read_tabular_upload(_upload)
            except Exception as _exc:
                st.error("That file could not be read: " + str(_exc))

            if _frame is not None and len(_frame) > MAX_IMPORT_ROWS:
                st.error(f"That file has {len(_frame):,} rows. Please split it "
                         f"into files of {MAX_IMPORT_ROWS:,} rows or fewer.")
            elif _frame is not None and _frame.empty:
                st.warning("That file has headings but no rows in it.")
            elif _frame is not None:
                _colmap, _ignored = map_import_columns(_frame)
                if "tag" not in _colmap:
                    st.error("No Tag column found. Rename the column holding "
                             "the tags to `Tag` and upload again.")
                    st.caption("Columns found: "
                               + ", ".join(str(c) for c in _frame.columns))
                else:
                    _rows = build_import_rows(_frame, _colmap, {
                        "status": _bulk_status,
                        "date_acquired": (_bulk_acq.isoformat() if _bulk_acq else ""),
                    })
                    _rows = [r for r in _rows if r["verdict"] != "blank"]
                    _new = [r for r in _rows if r["verdict"] == "new"]
                    _dupes = [r for r in _rows if r["verdict"] == "duplicate"]
                    _bad = [r for r in _rows if r["verdict"] == "skip"]

                    if not _rows:
                        st.warning("Every row in that file is empty.")
                    else:
                        _s1, _s2, _s3 = st.columns(3)
                        _s1.metric("Ready to load", len(_new))
                        _s2.metric(T("Already in herd"), len(_dupes))
                        _s3.metric("Cannot load", len(_bad))
                        if _ignored:
                            st.caption("Columns ignored: "
                                       + ", ".join(str(c) for c in _ignored))

                        _marks = {"new": "✅ New",
                                  "duplicate": "🔁 Already in herd",
                                  "skip": "❌ Cannot load"}
                        _preview = pd.DataFrame([{
                            "Row": r["line"],
                            "Check": _marks[r["verdict"]],
                            "Tag": r["rec"]["tag"] or "—",
                            "Breed": r["rec"]["breed"],
                            "Sex": r["rec"]["sex"],
                            "Category": r["rec"]["category"],
                            "Status": r["rec"]["status"],
                            "Born": r["rec"]["dob"],
                            "Weight": r["rec"]["weight"],
                            "Acquired": r["rec"]["date_acquired"],
                            **({"Brand": r["rec"]["brand_number"]}
                               if SP["branded"] else {}),
                            "Mother": r["rec"]["mother_tag"],
                            "Notes on this row": "; ".join(r["issues"]),
                        } for r in _rows])
                        st.markdown('<div class="section">Preview</div>',
                                    unsafe_allow_html=True)
                        st.dataframe(_preview, use_container_width=True,
                                     hide_index=True, height=330)

                        _planned = len(_new) + (len(_dupes) if _update_dupes else 0)
                        _b1, _b2 = st.columns([3, 1])
                        _go = _b1.button(
                            T(f"Import {_planned} record(s) into the herd"),
                            type="primary", use_container_width=True,
                            disabled=(_planned == 0), key="bulk_go")
                        if _b2.button("Clear file", use_container_width=True,
                                      key="bulk_clear"):
                            st.session_state["bulk_upload_version"] = (
                                st.session_state.get("bulk_upload_version", 0) + 1)
                            st.rerun()

                        if _go:
                            _added = _updated = _failed = _weighed = 0
                            for r in _new:
                                _rec = r["rec"]
                                if add_cow(_rec):
                                    _added += 1
                                    if _rec["weight"]:
                                        _ok, _ = add_weight(
                                            _rec["tag"],
                                            _rec["date_acquired"] or date.today().isoformat(),
                                            _rec["weight"], "Weight at loading")
                                        if _ok:
                                            _weighed += 1
                                else:
                                    _failed += 1
                            if _update_dupes:
                                for r in _dupes:
                                    update_cow(r["rec"]["tag"], r["rec"])
                                    _updated += 1

                            _parts = [f"{_added} new cow(s) loaded"]
                            if _weighed:
                                _parts.append(f"{_weighed} weight(s) recorded")
                            if _updated:
                                _parts.append(f"{_updated} existing record(s) updated")
                            if _dupes and not _update_dupes:
                                _parts.append(T(f"{len(_dupes)} left alone "
                                              "(tag already in herd)"))
                            if _bad:
                                _parts.append(f"{len(_bad)} row(s) skipped")
                            if _failed:
                                _parts.append(f"{_failed} row(s) failed to insert")

                            _left = [r for r in _rows if r["verdict"] == "skip"
                                     or (r["verdict"] == "duplicate"
                                         and not _update_dupes)]
                            _report = None
                            if _left:
                                _report = pd.DataFrame([{
                                    "Row": r["line"],
                                    "Tag": r["rec"]["tag"],
                                    "Outcome": _marks[r["verdict"]],
                                    "Reason": "; ".join(r["issues"]),
                                } for r in _left]).to_csv(index=False).encode("utf-8")

                            if _added or _updated:
                                add_activity({
                                    "date": date.today().isoformat(),
                                    "activity": "Other",
                                    "details": ("Bulk import from "
                                                + (getattr(_upload, "name", "")
                                                   or "a spreadsheet")
                                                + f" — {_added} loaded, "
                                                + f"{_updated} updated"),
                                    "scope": "tags",
                                    "tags": ", ".join(
                                        [r["rec"]["tag"] for r in _new][:50]),
                                    "tag_count": _added + _updated,
                                })

                            st.session_state["bulk_import_result"] = {
                                "message": "Import finished — " + ", ".join(_parts) + ".",
                                "report": _report,
                            }
                            st.session_state["bulk_upload_version"] = (
                                st.session_state.get("bulk_upload_version", 0) + 1)
                            st.rerun()


# ── EDIT / REMOVE ─────────────────────────────
with tab_edit:
    st.markdown(T('<div class="section">Edit or remove a cow</div>'),
                unsafe_allow_html=True)
    if total_cattle_count() == 0:
        st.info(T("No cattle loaded yet."))
    else:
        esrch = st.text_input("🔎 Search a tag to edit", key="edit_search",
                              placeholder="Start typing a tag or name…").strip()
        matches = [c["tag"] for c in cattle_page(50, 0, esrch)]
        if esrch and not matches:
            st.warning(T(f"No cattle match '{esrch}'."))
            pick = None
        elif len(matches) == 1:
            pick = matches[0]
            st.success(f"Selected 🏷️ {pick}")
        else:
            hint = (f"{len(matches)} match" + ("es" if len(matches) != 1 else "")
                    + " (showing up to 50) — pick one:" if esrch
                    else "Pick a tag (showing first 50 — search to narrow):")
            pick = st.selectbox(hint, matches, key="edit_pick", format_func=T)
        cow = get_cow(pick) if pick else None
        if cow:
            with st.form("edit_cattle"):
                e1, e2, e3 = st.columns(3)
                name = e1.text_input("Name", value=cow["name"])
                breed = e2.text_input("Breed", value=cow["breed"])
                colour = e3.text_input("Colour", value=cow["colour"])

                e4, e5, e6 = st.columns(3)
                sex = e4.selectbox("Sex", [""] + SEXES,
                                   index=([""] + SEXES).index(cow["sex"])
                                   if cow["sex"] in SEXES else 0, format_func=T)
                category = e5.selectbox("Category", [""] + CATEGORIES,
                                        index=([""] + CATEGORIES).index(cow["category"])
                                        if cow["category"] in CATEGORIES else 0, format_func=T)
                status = e6.selectbox("Status", STATUSES,
                                      index=STATUSES.index(cow["status"])
                                      if cow["status"] in STATUSES else 0, format_func=T)

                e7, e8 = st.columns(2)
                dob_val = None
                if cow["dob"]:
                    try:
                        dob_val = datetime.fromisoformat(cow["dob"]).date()
                    except Exception:
                        dob_val = None
                dob = e7.date_input(
                    "Date of birth", value=dob_val,
                    min_value=earliest_selectable(dob_val,
                                                  years_back=DOB_YEARS_BACK),
                    max_value=date.today(), format="DD/MM/YYYY",
                    help="Click the month and year at the top of the calendar "
                         "to jump to another year.")
                weight = e8.number_input("Weight (kg)", min_value=0.0,
                                         value=float(cow["weight"] or 0.0), step=1.0)

                acq_val = None
                if cow["date_acquired"]:
                    try:
                        acq_val = datetime.fromisoformat(cow["date_acquired"]).date()
                    except Exception:
                        acq_val = None
                acquired = st.date_input(
                    "Date acquired", value=acq_val,
                    min_value=earliest_selectable(acq_val),
                    max_value=date.today(), format="DD/MM/YYYY")
                notes = st.text_area("Notes", value=cow["notes"])

                edit_brand_number = st.text_input(
                    "Brand number", value=cow.get("brand_number", ""),
                    placeholder="e.g. A042 or 4B21 (letters & numbers)"
                ) if SP["branded"] else cow.get("brand_number", "")

                edit_mother_tag = st.text_input(
                    T("Mother's tag (for cows born on the farm)"),
                    value=cow.get("mother_tag", ""), placeholder="e.g. BW-0421")

                edit_origin = st.text_input(
                    T("Location the cow came from"),
                    value=cow.get("origin_location", ""),
                    placeholder=T("e.g. Serowe cattle post, Molepolole auction"))

                edit_approx_age = st.number_input(
                    "Approximate age in years (only used if date of birth is blank)",
                    min_value=0, max_value=40, value=0, step=1,
                    help="Fills in an estimated birth date so the age keeps updating "
                         "automatically each year.")

                event_date = st.date_input(
                    "If sold or deceased — date it happened (added to the calendar)",
                    value=date.today())

                save = st.form_submit_button("Save changes", type="primary")

            if save:
                dob_iso = dob.isoformat() if dob else ""
                if not dob_iso and edit_approx_age:
                    dob_iso = est_dob_from_age(edit_approx_age)
                ebn = edit_brand_number.strip()
                if ebn and not ebn.isalnum():
                    st.warning("Brand number can contain letters and numbers only.")
                else:
                    update_cow(pick, {
                        "name": name.strip(), "breed": breed.strip(), "sex": sex,
                        "dob": dob_iso, "colour": colour.strip(),
                        "weight": float(weight) if weight else None,
                        "category": category, "status": status,
                        "date_acquired": acquired.isoformat() if acquired else "",
                        "notes": notes.strip(),
                        "brand_number": ebn, "brand_type": "",
                        "mother_tag": edit_mother_tag.strip(),
                        "origin_location": edit_origin.strip(),
                    })
                    # Record the movement on the calendar (once per cow).
                    ed = event_date.isoformat() if event_date else date.today().isoformat()
                    if status == "Sold" and not has_event(pick, "Cow sold"):
                        add_event(ed, "Cow sold", pick, f"{cow['name'] or ''}".strip())
                    if status == "Deceased" and not has_event(pick, "Death"):
                        add_event(ed, "Death", pick, f"{cow['name'] or ''}".strip())
                    st.success(f"Saved changes for '{pick}'.")
                    st.rerun()

            st.divider()
            existing = get_images(pick)
            st.markdown("Photo")
            if existing:
                im = existing[0]
                pc = st.columns([1, 2])
                pc[0].image(im["image"], width=150, caption=im["filename"])
                if pc[1].button("🗑️ Delete photo", key=f"delimg_{im['id']}"):
                    delete_image(im["id"])
                    st.rerun()
                pc[1].caption("Delete the current photo to upload a new one.")
            else:
                new_photo = st.file_uploader(
                    "Add a passport-size photo", type=["png", "jpg", "jpeg", "webp"],
                    accept_multiple_files=False, key=f"add_photo_{pick}")
                if st.button("Add photo", key=f"add_photo_btn_{pick}"):
                    if new_photo:
                        add_images(pick, [new_photo])
                        st.success("Photo added.")
                        st.rerun()
                    else:
                        st.warning("Choose an image first.")

            st.divider()
            st.markdown("Danger zone")
            confirm = st.checkbox(T(f"I want to permanently remove cow '{pick}'"))
            if st.button(T("🗑️ Remove this cow"), disabled=not confirm):
                delete_cow(pick)
                st.success(T(f"Removed '{pick}' from the herd."))
                st.rerun()

        st.divider()
        # ── REMOVE SEVERAL CATTLE AT ONCE ──────
        st.markdown(T('<div class="section">Remove several cattle</div>'),
                    unsafe_allow_html=True)
        st.warning(T("Removing is permanent — there is no undo. If a cow was "
                   "sold or died, change her Status above instead. That "
                   "keeps her history and her calves' parentage, and she stops "
                   "counting as part of the active herd."))

        _rm_done = st.session_state.get("bulk_remove_result")
        if _rm_done:
            st.success(_rm_done)
            if st.button("Dismiss", key="rm_dismiss"):
                st.session_state["bulk_remove_result"] = None
                st.rerun()

        _rf1, _rf2, _rf3 = st.columns(3)
        _rm_term = _rf1.text_input("Filter by tag or name", key="rm_term",
                                   placeholder="e.g. BW-04").strip()
        _rm_status = _rf2.selectbox("Filter by status", ["Any"] + STATUSES,
                                    key="rm_status", format_func=T)
        _rm_cat = _rf3.selectbox("Filter by category", ["Any"] + CATEGORIES,
                                 key="rm_cat", format_func=T)

        _rm_pool = tags_for_removal(
            _rm_term,
            "" if _rm_status == "Any" else _rm_status,
            "" if _rm_cat == "Any" else _rm_cat)

        if not _rm_pool:
            st.info(T("No cattle match those filters."))
        else:
            _labels = {}
            for _r in _rm_pool:
                _bits = [_r["tag"]]
                if _r["name"]:
                    _bits.append(_r["name"])
                _bits.append((_r["category"] or "—") + " · "
                             + (_r["status"] or "—"))
                _labels[" · ".join(_bits)] = _r["tag"]

            st.caption(str(len(_rm_pool)) + " cow(s) match. Tick the ones to "
                       "remove, or use Select all to take every match.")
            _sa1, _sa2 = st.columns([1, 3])
            _select_all = _sa1.checkbox("Select all " + str(len(_rm_pool)),
                                        key="rm_all")
            _chosen_labels = st.multiselect(
                "Tags to remove",
                list(_labels.keys()),
                default=list(_labels.keys()) if _select_all else [],
                key="rm_pick_" + str(st.session_state.get("rm_version", 0))
                + ("_all" if _select_all else ""),
                disabled=_select_all,
                help="Type part of a tag to find it quickly.", format_func=T)
            _rm_tags = ([_labels[k] for k in _labels] if _select_all
                        else [_labels[k] for k in _chosen_labels])

            if _rm_tags:
                _imp = removal_impact(_rm_tags)
                st.markdown("This would permanently delete:")
                _i1, _i2, _i3, _i4 = st.columns(4)
                _i1.metric(T("Cattle"), _imp["cattle"])
                _i2.metric("Weight records", _imp["weights"])
                _i3.metric("Breeding records", _imp["breedings"])
                _i4.metric("Diary entries", _imp["events"])

                _notes = []
                if _imp["images"]:
                    _notes.append(str(_imp["images"]) + " photo(s)")
                if _imp["losses"]:
                    _notes.append(str(_imp["losses"])
                                  + " death / missing record(s)")
                if _imp["reminders"]:
                    _notes.append(str(_imp["reminders"])
                                  + " task(s) from Things to do")
                if _notes:
                    st.caption("Also removed: " + ", ".join(_notes) + ".")
                if _imp["calves_unlinked"]:
                    st.caption("⚠️ " + str(_imp["calves_unlinked"])
                               + " calf/calves name one of these cows as their "
                                 "mother. They stay in the herd, but that link "
                                 "is lost.")
                if _imp["sales_kept"] or _imp["purchases_kept"]:
                    st.caption("Kept: " + str(_imp["sales_kept"])
                               + " sale(s) and " + str(_imp["purchases_kept"])
                               + " purchase(s). Money that changed hands stays "
                                 "on record, so your invoices and totals do "
                                 "not change.")

                with st.expander("Show the " + str(len(_rm_tags))
                                 + " cow(s) to be removed"):
                    st.dataframe(pd.DataFrame([
                        {"Tag": r["tag"], "Name": r["name"],
                         "Sex": r["sex"], "Category": r["category"],
                         "Status": r["status"]}
                        for r in _rm_pool if r["tag"] in set(_rm_tags)]),
                        use_container_width=True, hide_index=True)

                # A big deletion has to be typed out, not just clicked.
                _big = len(_rm_tags) >= 10
                _ok_to_go = st.checkbox(
                    "I understand this cannot be undone", key="rm_confirm")
                if _big:
                    _typed = st.text_input(
                        "This removes " + str(len(_rm_tags))
                        + " cattle. Type REMOVE to confirm.",
                        key="rm_typed", placeholder="REMOVE")
                    _ok_to_go = _ok_to_go and _typed.strip().upper() == "REMOVE"

                _rb1, _rb2 = st.columns([3, 1])
                if _rb1.button("🗑️ Remove " + str(len(_rm_tags)) + " cow(s)",
                               type="primary", use_container_width=True,
                               disabled=not _ok_to_go, key="rm_go"):
                    _gone = delete_cattle(_rm_tags)
                    add_activity({
                        "date": date.today().isoformat(),
                        "activity": "Other",
                        "details": str(_gone) + " cattle removed from the system",
                        "scope": "tags",
                        "tags": ", ".join(_rm_tags[:50]),
                        "tag_count": _gone,
                    })
                    st.session_state["bulk_remove_result"] = (
                        str(_gone) + " cow(s) removed. "
                        + str(_imp["sales_kept"] + _imp["purchases_kept"])
                        + " sale/purchase record(s) were kept.")
                    st.session_state["rm_version"] = (
                        st.session_state.get("rm_version", 0) + 1)
                    st.rerun()
                if _rb2.button("Clear selection", use_container_width=True,
                               key="rm_clear"):
                    st.session_state["rm_version"] = (
                        st.session_state.get("rm_version", 0) + 1)
                    st.rerun()

                st.caption("Tip: take a backup first — the Backup tab writes a "
                           "copy you can restore if you change your mind.")

# ── CALENDAR ──────────────────────────────────
with tab_cal:
    st.markdown('<div class="section">Farm calendar</div>', unsafe_allow_html=True)

    _over = overdue_reminders()
    _upc = upcoming_reminders()
    if _over or _upc:
        with st.expander(
                f"⏰ Things to do — {len(_over) + len(_upc)} outstanding"
                + (f" · {len(_over)} overdue" if _over else ""),
                expanded=bool(_over)):
            if _over:
                st.markdown("Overdue")
                for _r in _over:
                    st.markdown(
                        f"• 🔴 {_r['date']} — {_r['task']}"
                        + (f" ({_r['details']})" if _r["details"] else ""))
            if _upc:
                st.markdown("Coming up")
                for _r in _upc:
                    _flag = "🔴 " if _r["priority"] == "High" else ""
                    _when = ("today" if _r["date"] == date.today().isoformat()
                             else _r["date"])
                    st.markdown(f"• {_flag}{_when} — {_r['task']}"
                                + (f" ({_r['details']})" if _r["details"] else ""))
            st.caption("Open a day below to tick tasks off, edit or add new ones.")

    today = date.today()
    cal_year = st.session_state.setdefault("cal_year", today.year)
    cal_month = st.session_state.setdefault("cal_month", today.month)

    nav = st.columns([1, 3, 1, 1])
    if nav[0].button("◀ Prev", use_container_width=True):
        cal_month -= 1
        if cal_month < 1:
            cal_month, cal_year = 12, cal_year - 1
        st.session_state["cal_month"], st.session_state["cal_year"] = cal_month, cal_year
        st.rerun()
    nav[1].markdown(
        f"<div style='text-align:center;font-size:1.3rem;font-weight:800;color:{PRIMARY};"
        f"padding-top:4px'>{_cal.month_name[cal_month]} {cal_year}</div>",
        unsafe_allow_html=True)
    if nav[2].button("Next ▶", use_container_width=True):
        cal_month += 1
        if cal_month > 12:
            cal_month, cal_year = 1, cal_year + 1
        st.session_state["cal_month"], st.session_state["cal_year"] = cal_month, cal_year
        st.rerun()
    if nav[3].button("Today", use_container_width=True):
        st.session_state["cal_month"], st.session_state["cal_year"] = today.month, today.year
        st.session_state["cal_selected"] = today.isoformat()
        st.rerun()

    counts = month_activity_counts(cal_year, cal_month)

    # Weekday header (Monday-first).
    head = st.columns(7)
    for col, name in zip(head, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        col.markdown(f"<div style='text-align:center;color:{MUTED};font-weight:700;"
                     f"font-size:.8rem'>{name}</div>", unsafe_allow_html=True)

    selected = st.session_state.get("cal_selected")
    grid = _cal.Calendar(firstweekday=0)
    for week in grid.monthdayscalendar(cal_year, cal_month):
        cols = st.columns(7)
        for col, day in zip(cols, week):
            if day == 0:
                col.markdown("&nbsp;", unsafe_allow_html=True)
                continue
            iso = f"{cal_year:04d}-{cal_month:02d}-{day:02d}"
            has_act = counts.get(day, 0) > 0
            label = f"{day} •" if has_act else f"{day}"
            is_sel = selected == iso
            is_today = iso == today.isoformat()
            if col.button(label, key=f"day_{iso}", use_container_width=True,
                          type="primary" if is_sel else "secondary",
                          help="Today" if is_today else None):
                st.session_state["cal_selected"] = iso
                st.rerun()

    st.caption("• marks days with activity. Click a day to see or add its details.")
    st.divider()

    # ── Selected-day detail ───────────────────
    if not selected:
        st.info("Pick a day above to view its activities and add a note.")
    else:
        sel_d = datetime.fromisoformat(selected).date()
        st.markdown(f'<div class="section">{sel_d.strftime("%A, %d %B %Y")}</div>',
                    unsafe_allow_html=True)

        births = cattle_by_date("dob", selected)
        acquisitions = cattle_by_date("date_acquired", selected)
        day_acts = activities_on(selected)
        logged = events_on(selected)
        # Older databases wrote both an activity row and an "Activity" event
        # for the same job, which listed it twice on the day. Drop the event
        # copy when the activity itself is already being shown.
        if day_acts:
            _act_names = {(a["activity"] or "").lower() for a in day_acts}
            logged = [e for e in logged
                      if not (e["type"] == "Activity"
                              and any(n and n in (e["note"] or "").lower()
                                      for n in _act_names))]
        day_due = due_between(selected, selected)

        any_activity = (births or acquisitions or logged or day_acts
                        or reminders_on(selected) or day_due)
        if not any_activity:
            st.write("Nothing recorded on this day yet. Anything you log with "
                     "this date — an activity, a sale, an expense, a weighing "
                     "— appears here automatically.")
        else:
            # One line saying what happened, so the day reads at a glance.
            _tally = []
            if day_acts:
                _tally.append(str(len(day_acts)) + " farm activit"
                              + ("y" if len(day_acts) == 1 else "ies"))
            if logged:
                _tally.append(str(len(logged)) + " diary entr"
                              + ("y" if len(logged) == 1 else "ies"))
            if births:
                _tally.append(str(len(births)) + " birth(s)")
            if acquisitions:
                _tally.append(str(len(acquisitions)) + " acquired")
            if day_due:
                _tally.append(str(len(day_due)) + " calving(s) expected")
            if _tally:
                st.caption("On this day: " + ", ".join(_tally) + ".")

        if day_due:
            st.markdown(T("Calvings expected"))
            for _d in day_due:
                st.markdown(f"- {_d['cow_tag']} — served {_d['service_date']}"
                            + (f" by {_d['bull_tag']}" if _d["bull_tag"] else ""))

        if day_acts:
            st.markdown("🧰 Farm activities")
            for _a in day_acts:
                _scope = (f"all cattle" if _a["scope"] == "all"
                          else "all active cattle" if _a["scope"] == "active"
                          else f"{_a['tag_count']} selected tag"
                               + ("s" if (_a["tag_count"] or 0) != 1 else ""))
                _l = f"• {_a['activity']} — applied to {_scope}"
                if _a["details"]:
                    _l += f" — {_a['details']}"
                st.markdown(_l)
                if _a["tags"]:
                    _shown = _a["tags"].split(",")
                    _preview = ", ".join(_shown[:12])
                    if len(_shown) > 12:
                        _preview += f" … (+{len(_shown) - 12} more)"
                    st.caption(f"🏷️ {_preview}")

        if births:
            st.markdown(T("🐄 Calves born"))
            for c_ in births:
                st.write(f"• 🏷️ {c_['tag']}" + (f" — {c_['name']}" if c_['name'] else ""))
        if acquisitions:
            st.markdown(T("📥 Cattle acquired"))
            for c_ in acquisitions:
                st.write(f"• 🏷️ {c_['tag']}" + (f" — {c_['name']}" if c_['name'] else ""))

        if logged:
            st.markdown("Logged activities")
            for ev in logged:
                icon = EVENT_ICON.get(ev["type"], "•")
                line = f"{icon} {ev['type']}"
                if ev["tag"]:
                    line += f" — 🏷️ {ev['tag']}"
                if ev["note"]:
                    line += f" — {ev['note']}"
                ecols = st.columns([6, 1])
                ecols[0].markdown(line)
                if ecols[1].button("🗑️", key=f"delev_{ev['id']}"):
                    delete_event(ev["id"])
                    st.rerun()

        if any_activity:
            st.write("")
            pdf_bytes = day_report_pdf(selected, births, acquisitions, logged,
                                       day_acts, reminders_on(selected))
            if pdf_bytes:
                _dl(st,
                    "Download day report (PDF)",
                    pdf_bytes, file_name=f"activity_report_{selected}.pdf",
                    mime="application/pdf", type="primary",
                    key=f"dl_dayreport_{selected}")
            else:
                st.caption("Install reportlab to enable PDF downloads "
                           "(pip install reportlab).")

        st.divider()
        _day_rem = reminders_on(selected)
        _open_rem = [r for r in _day_rem if not r["done"]]
        st.markdown(f"⏰ Things to do on this day "
                    f"({len(_open_rem)} outstanding of {len(_day_rem)})")
        if _day_rem:
            for _r in _day_rem:
                rc = st.columns([0.5, 6, 0.7])
                _checked = rc[0].checkbox("done", value=bool(_r["done"]),
                                          key=f"remdone_{_r['id']}",
                                          label_visibility="collapsed")
                if _checked != bool(_r["done"]):
                    set_reminder_done(_r["id"], _checked)
                    st.rerun()
                _txt = _r["task"]
                if _r["priority"] == "High":
                    _txt = f"🔴 {_txt}"
                if _r["done"]:
                    _txt = f"~~{_txt}~~"
                if _r["details"]:
                    _txt += f" — {_r['details']}"
                rc[1].markdown(_txt)
                if rc[2].button("🗑️", key=f"remdel_{_r['id']}"):
                    delete_reminder(_r["id"])
                    st.rerun()
        else:
            st.caption("Nothing scheduled for this day yet.")

        with st.form("add_reminder_form", clear_on_submit=True):
            rr = st.columns([3, 1])
            r_task = rr[0].text_input("Reminder / task",
                                      placeholder=T("e.g. Vaccinate the calves"))
            r_prio = rr[1].selectbox("Priority", REMINDER_PRIORITIES, format_func=T)
            r_details = st.text_input("Details (optional)",
                                      placeholder="e.g. order vaccine beforehand")
            _rem_tags = [c_["tag"] for c_ in cattle_page(300, 0, "")]
            r_tag = st.selectbox(
                T("About a particular cow? (optional)"), [""] + _rem_tags,
                help=T("Linking the task to a cow means it disappears with her "
                     "if she is ever removed from the herd."), format_func=T)
            if st.form_submit_button("Add reminder for this day", type="primary"):
                if not r_task.strip():
                    st.warning("Enter a task first.")
                else:
                    add_reminder({"date": selected, "task": r_task.strip(),
                                  "details": r_details.strip(),
                                  "priority": r_prio, "tag": r_tag})
                    st.success("Reminder added.")
                    st.rerun()

        st.divider()
        st.markdown("➕ Add a note or activity for this day")
        with st.form("add_event_form", clear_on_submit=True):
            ac = st.columns([2, 2])
            etype = ac[0].selectbox("Type", EVENT_TYPES, format_func=T)
            etag = ac[1].text_input("Tag (optional)", placeholder="e.g. BW-0421")
            enote = st.text_area("Note", placeholder="What happened on this day?")
            if st.form_submit_button("Add to this day", type="primary"):
                if etype == "Note" and not enote.strip() and not etag.strip():
                    st.warning("Add a note or a tag first.")
                else:
                    add_event(selected, etype, etag, enote)
                    st.success("Added to the calendar.")
                    st.rerun()

# ── BILLING (sales + purchases) ───────────────
with tab_bill:
    st.markdown('<div class="section">Billing</div>', unsafe_allow_html=True)

    # ── Currency ──────────────────────────────
    with st.expander("💱 Currency — " + currency_code() + " ("
                     + currency_name() + ")"):
        st.caption("Choose the money used throughout the app: sales, "
                   "purchases, expenses, invoices, receipts and every report.")
        _cur_now = currency_code()
        _cur_labels = [f"{c} · {n} ({sym})" for c, sym, n in CURRENCIES]
        _cur_codes = [c for c, _, _ in CURRENCIES]
        _picked = st.selectbox(
            "Currency", _cur_labels,
            index=_cur_codes.index(_cur_now) if _cur_now in _cur_codes else 0,
            key="currency_pick", label_visibility="collapsed", format_func=T)
        _picked_code = _cur_codes[_cur_labels.index(_picked)]

        if _picked_code != _cur_now:
            st.warning("This changes the label only, not the numbers. "
                       "Amounts already recorded stay exactly as they are — "
                       f"an entry of 9,000 stays 9,000, shown as "
                       f"{currency_symbol(_picked_code)} 9,000.00. Nothing is "
                       "converted at an exchange rate.")
            if st.button("Use " + _picked_code + " from now on",
                         type="primary", key="currency_save"):
                set_currency(_picked_code)
                add_event(date.today().isoformat(), "Note", "",
                          "Currency changed from " + _cur_now
                          + " to " + _picked_code)
                st.rerun()
        else:
            st.caption("Amounts show as " + money(1234.5) + ".")
            if not _pdf_safe(CURRENCY_BY_CODE[_cur_now][0]):
                st.caption("The " + CURRENCY_BY_CODE[_cur_now][0]
                           + " sign cannot be drawn in a PDF, so invoices and "
                             "reports show " + _cur_now + " instead.")

    sales = all_sales()
    purchases = all_purchases()
    revenue = sum(s["price"] or 0 for s in sales)
    spent = sum(p["price"] or 0 for p in purchases)

    b1, b2, b3 = st.columns(3)
    b1.metric("Revenue (sales)", money(revenue))
    b2.metric("Spent (purchases)", money(spent))
    b3.metric("Net position", money(revenue - spent))

    # ── Monthly money flow ────────────────────
    st.markdown('<div class="section">Sales &amp; purchases by month</div>',
                unsafe_allow_html=True)
    if not sales and not purchases:
        st.caption("Once you record a sale or a purchase, the monthly totals "
                   "appear here.")
    else:
        # Opens on the last twelve months; the calendar then overrides it.
        _today = date.today()
        _default_from = date(_today.year - 1, _today.month, 1)
        # Version counter gives the pickers fresh keys on the next run, which
        # is how "reset" works without writing to a live widget's state.
        _cal_v = str(st.session_state.get("bill_flow_version", 0))

        with st.expander("📅 Calendar — choose the dates to show"):
            st.caption("Pick the first and last day you want counted. The bars "
                       "stay whole months, so a range starting mid-month counts "
                       "only the part you chose.")
            _c1, _c2 = st.columns(2)
            _from = _c1.date_input("From", value=_default_from,
                                   key="bill_flow_from_" + _cal_v,
                                   format="DD/MM/YYYY")
            _to = _c2.date_input("To", value=_today,
                                 key="bill_flow_to_" + _cal_v,
                                 format="DD/MM/YYYY")
            if st.button("Back to the last 12 months",
                         key="bill_flow_reset_" + _cal_v,
                         use_container_width=True):
                st.session_state["bill_flow_version"] = int(_cal_v) + 1
                st.rerun()

        _swapped = _from > _to
        if _swapped:
            _from, _to = _to, _from
        _flow = monthly_money_flow(sales, purchases,
                                   start_iso=_from.isoformat(),
                                   end_iso=_to.isoformat())
        st.caption("Showing " + _from.strftime("%d %b %Y") + " to "
                   + _to.strftime("%d %b %Y") + " — "
                   + str(len(_flow["keys"])) + " month(s).")
        if _swapped:
            st.caption("Those two dates were the wrong way round, so they have "
                       "been swapped.")
        if _flow.get("truncated"):
            st.caption("That range is very long, so only the first "
                       + str(MAX_FLOW_MONTHS) + " months are charted.")

        _sold_total = sum(_flow["sales"])
        _bought_total = sum(_flow["purchases"])
        _active = [i for i in range(len(_flow["keys"]))
                   if _flow["sales"][i] or _flow["purchases"][i]]

        _f1, _f2, _f3 = st.columns(3)
        _f1.metric("Sales in these dates", money(_sold_total))
        _f2.metric("Purchases in these dates", money(_bought_total))
        _f3.metric("Net in these dates", money(_sold_total - _bought_total))

        monthly_flow_chart(st, _flow, key="bill_flow")

        # Best and busiest months are the two questions a farmer actually asks.
        if _active:
            _best = max(_active, key=lambda i: _flow["sales"][i])
            _busiest = max(_active, key=lambda i: _flow["sales_count"][i])
            _avg = _sold_total / len(_active)
            _bits = []
            if _flow["sales"][_best]:
                _bits.append("Best month for sales was "
                             + _flow["labels"][_best] + " at "
                             + money(_flow["sales"][_best]) + ".")
            if _flow["sales_count"][_busiest]:
                _bits.append("Most cattle sold in "
                             + _flow["labels"][_busiest] + " ("
                             + str(_flow["sales_count"][_busiest]) + " head).")
            _bits.append("Average of " + money(_avg)
                         + " across the " + str(len(_active))
                         + " month(s) with any trading.")
            st.caption(" ".join(_bits))

        with st.expander("Month by month"):
            _table = pd.DataFrame({
                "Month": _flow["labels"],
                "Head sold": _flow["sales_count"],
                "Sales": [money(v) for v in _flow["sales"]],
                "Head bought": _flow["purchases_count"],
                "Purchases": [money(v) for v in _flow["purchases"]],
                "Net": [money(_flow["sales"][i] - _flow["purchases"][i])
                        for i in range(len(_flow["keys"]))],
            })
            st.dataframe(_table, use_container_width=True, hide_index=True)
            _dl(st,
                "Download this table (CSV)",
                _table.to_csv(index=False).encode("utf-8"),
                file_name=("monthly_sales_purchases_" + _from.isoformat()
                           + "_to_" + _to.isoformat() + ".csv"),
                mime="text/csv", key="bill_flow_csv")

        # Say plainly what the chart is not showing.
        _notes = []
        if _flow["undated"]["sales"] or _flow["undated"]["purchases"]:
            _notes.append(str(_flow["undated"]["sales"]
                              + _flow["undated"]["purchases"])
                          + " record(s) have no usable date and are not counted")
        _outside = (_flow["earlier"]["sales"] + _flow["earlier"]["purchases"]
                    + _flow["later"]["sales"] + _flow["later"]["purchases"])
        if _outside:
            _notes.append("records outside these dates are not counted ("
                          + money(_flow["earlier"]["sales"]
                                  + _flow["earlier"]["purchases"])
                          + " before, "
                          + money(_flow["later"]["sales"]
                                  + _flow["later"]["purchases"])
                          + " after)")
        if _notes:
            st.caption("Note: " + "; ".join(_notes) + ".")

    sales_tab, buy_tab = st.tabs(["Sales", "Purchases"])

    # ═══════════ SALES ═══════════
    with sales_tab:
        this_month = now_local().strftime("%Y-%m")
        month_rev = sum(s["price"] or 0 for s in sales
                        if (s["date"] or "").startswith(this_month))
        _inv_list = invoice_summaries()
        sm1, sm2, sm3 = st.columns(3)
        sm1.metric("Invoices", len(_inv_list))
        sm2.metric("Head sold", len(sales))
        sm3.metric("Revenue this month", money(month_rev))

        st.markdown('<div class="section">Record a sale</div>', unsafe_allow_html=True)
        st.caption("Sell one animal or many to the same buyer — they all appear on a "
                   "single invoice.")
        if not has_active_cattle():
            st.info(T("No active cattle to sell. Load some in the Load Cattle tab first."))
        else:
            # A version counter gives every widget a fresh key after a sale, which
            # clears the form without writing to widget state (Streamlit forbids that).
            _sv = st.session_state.get("sale_form_version", 0)
            srch = st.text_input(
                "Search tags to sell", key=f"sale_search_{_sv}",
                placeholder="Type part of a tag to narrow the list…").strip()
            options = active_tag_matches(srch, limit=300)
            if srch and not options:
                st.warning(T(f"No active cow matches '{srch}'."))
            sel_tags = st.multiselect(
                T(f"Cattle to sell ({len(options)} shown)"), options, key=f"sale_tags_{_sv}", format_func=T)

            prices, weights = {}, {}
            if sel_tags:
                st.markdown("Price and weight per animal")
                hc = st.columns([2, 2, 2])
                hc[0].caption("Tag")
                hc[1].caption(f"Sale price ({currency_symbol()})")
                hc[2].caption("Weight at sale (kg)")
                for t in sel_tags:
                    _c = get_cow(t)
                    rc = st.columns([2, 2, 2])
                    rc[0].markdown(
                        f"{t}  \n<span style='color:{MUTED};font-size:.78rem'>"
                        f"{(_c['breed'] if _c else '') or '—'} · "
                        f"{(_c['sex'] if _c else '') or '—'}</span>",
                        unsafe_allow_html=True)
                    prices[t] = rc[1].number_input(
                        f"price_{t}", min_value=0.0, value=0.0, step=50.0,
                        key=f"sale_price_{_sv}_{t}", label_visibility="collapsed")
                    weights[t] = rc[2].number_input(
                        f"weight_{t}", min_value=0.0,
                        value=float(_c["weight"] or 0.0) if _c else 0.0, step=1.0,
                        key=f"sale_weight_{_sv}_{t}", label_visibility="collapsed")
                _running = sum(v for v in prices.values() if v)
                st.markdown(f"<div style='font-weight:700;color:{PRIMARY}'>"
                            f"{len(sel_tags)} head · Invoice total: {money(_running)}"
                            "</div>", unsafe_allow_html=True)

            b1, b2, b3 = st.columns(3)
            buyer = b1.text_input("Buyer name *", key=f"sale_buyer_{_sv}",
                                  placeholder="e.g. J. Moremi")
            buyer_contact = b2.text_input("Buyer contact", key=f"sale_contact_{_sv}",
                                          placeholder="phone / email")
            sale_date = b3.date_input("Sale date", value=date.today(),
                                     min_value=earliest_selectable(),
                                     max_value=date.today(),
                                     format="DD/MM/YYYY",
                                      key=f"sale_date_{_sv}")
            b4, b5 = st.columns([1, 2])
            method = b4.selectbox("Payment method", PAYMENT_METHODS, key=f"sale_method_{_sv}", format_func=T)
            note = b5.text_input("Transaction notes (appear on the invoice)",
                                 key=f"sale_note_{_sv}",
                                 placeholder="e.g. delivery arranged, deposit paid…")

            if st.button("Record sale & create invoice", type="primary",
                         key=f"sale_submit_{_sv}"):
                priced = [t for t in sel_tags if prices.get(t, 0) > 0]
                if not sel_tags:
                    st.warning("Select at least one animal to sell.")
                elif not buyer.strip():
                    st.warning("A buyer name is required.")
                elif not priced:
                    st.warning("Enter a sale price for at least one animal.")
                elif len(priced) != len(sel_tags):
                    st.warning("Every selected animal needs a price greater than zero.")
                else:
                    inv = next_invoice_no()
                    total = 0.0
                    for t in sel_tags:
                        cow = get_cow(t)
                        if not cow:
                            continue
                        total += float(prices[t])
                        add_sale({
                            "invoice_no": inv, "date": sale_date.isoformat(), "tag": t,
                            "cow_name": cow["name"], "buyer": buyer.strip(),
                            "buyer_contact": buyer_contact.strip(),
                            "weight": float(weights.get(t) or 0) or None,
                            "price": float(prices[t]), "payment_method": method,
                            "notes": note.strip(),
                        })
                        update_cow(t, {
                            "name": cow["name"], "breed": cow["breed"],
                            "sex": cow["sex"], "dob": cow["dob"],
                            "colour": cow["colour"], "weight": cow["weight"],
                            "category": cow["category"], "status": "Sold",
                            "date_acquired": cow["date_acquired"],
                            "notes": cow["notes"],
                        })
                        if not has_event(t, "Cow sold"):
                            add_event(sale_date.isoformat(), "Cow sold", t,
                                      f"Sold to {buyer.strip()} — {money(prices[t])}")
                    # New widget keys next run = a cleared form.
                    st.session_state["sale_form_version"] = _sv + 1
                    st.success(f"Sale recorded. Invoice {inv} created for "
                               f"{len(sel_tags)} head totalling {money(total)}.")
                    st.rerun()

        st.divider()
        st.markdown('<div class="section">Sales invoices</div>', unsafe_allow_html=True)
        if not _inv_list:
            st.info("No sales recorded yet.")
        else:
            _idf = pd.DataFrame([{
                "Invoice": r["invoice_no"], "Date": r["date"], "Buyer": r["buyer"],
                "Head": r["head"], "Total": money(r["total"]),
            } for r in _inv_list])
            st.dataframe(_idf, use_container_width=True, hide_index=True)
            _dl(st,
                "Download sales (CSV)",
                pd.DataFrame(sales).to_csv(index=False).encode("utf-8"),
                file_name=f"cattle_sales_{date.today().isoformat()}.csv",
                mime="text/csv", key="dl_sales")

            st.markdown("View an invoice")
            labels = {f"{r['invoice_no']} · {r['buyer']} · {r['head']} head · "
                      f"{money(r['total'])}": r["invoice_no"] for r in _inv_list}
            pick = st.selectbox("Pick an invoice", list(labels.keys()), key="pick_sale", format_func=T)
            inv_no = labels[pick]
            items = sales_by_invoice(inv_no)
            if items:
                head = items[0]
                with st.container(border=True):
                    st.markdown(f"### {inv_no}")
                    ic = st.columns(2)
                    ic[0].markdown(f"Date: {head['date']}  \n"
                                   f"Buyer: {head['buyer'] or '—'}  \n"
                                   f"Contact: {head['buyer_contact'] or '—'}")
                    ic[1].markdown(f"Payment: {head['payment_method'] or '—'}  \n"
                                   f"Animals: {len(items)}")
                    st.dataframe(pd.DataFrame([{
                        "Tag": i["tag"],
                        "Weight (kg)": i["weight"] or "—",
                        "Amount": money(i["price"]),
                    } for i in items]), use_container_width=True, hide_index=True)
                    _tot = sum(i["price"] or 0 for i in items)
                    st.markdown(f"<div style='font-size:1.3rem;font-weight:800;"
                                f"color:{PRIMARY}'>Total: {money(_tot)}</div>",
                                unsafe_allow_html=True)
                    if head["notes"]:
                        st.caption(f"Transaction notes: {head['notes']}")
                dl, rm = st.columns([2, 1])
                _ipdf = sale_invoice_pdf(inv_no)
                if _ipdf:
                    _dl(dl, "Download invoice (PDF)", _ipdf,
                                       file_name=f"{inv_no}.pdf",
                                       mime="application/pdf", key="dl_inv")
                else:
                    dl.caption("Install reportlab to download invoices.")

                # ── Correct mistakes on this invoice ──
                _iv = st.session_state.get("inv_edit_version", 0)
                with st.expander("Edit this invoice"):
                    st.caption("Correct the buyer details, prices or weights, remove "
                               "an animal, or add more to the same invoice.")
                    try:
                        _dval = datetime.fromisoformat(head["date"]).date()
                    except Exception:
                        _dval = date.today()
                    e1, e2, e3 = st.columns(3)
                    _eb = e1.text_input("Buyer name", value=head["buyer"] or "",
                                        key=f"inv_buyer_{_iv}")
                    _ec = e2.text_input("Buyer contact",
                                        value=head["buyer_contact"] or "",
                                        key=f"inv_contact_{_iv}")
                    _ed = e3.date_input("Sale date", value=_dval,
                                        min_value=earliest_selectable(_dval),
                                        max_value=date.today(),
                                        format="DD/MM/YYYY",
                                        key=f"inv_date_{_iv}")
                    e4, e5 = st.columns([1, 2])
                    _emeth = e4.selectbox(
                        "Payment method", PAYMENT_METHODS,
                        index=PAYMENT_METHODS.index(head["payment_method"])
                        if head["payment_method"] in PAYMENT_METHODS else 0,
                        key=f"inv_method_{_iv}", format_func=T)
                    _en = e5.text_input("Transaction notes", value=head["notes"] or "",
                                        key=f"inv_note_{_iv}")

                    st.markdown("Animals on this invoice")
                    _new_vals, _to_remove = {}, []
                    for it in items:
                        lc = st.columns([2, 2, 2, 1.4])
                        lc[0].markdown(f"{it['tag']}")
                        _new_vals[it["id"]] = (
                            lc[1].number_input(
                                f"price_{it['id']}", min_value=0.0,
                                value=float(it["price"] or 0), step=50.0,
                                key=f"inv_p_{_iv}_{it['id']}",
                                label_visibility="collapsed"),
                            lc[2].number_input(
                                f"weight_{it['id']}", min_value=0.0,
                                value=float(it["weight"] or 0), step=1.0,
                                key=f"inv_w_{_iv}_{it['id']}",
                                label_visibility="collapsed"))
                        if lc[3].checkbox("Remove", key=f"inv_rm_{_iv}_{it['id']}"):
                            _to_remove.append(it["id"])
                    st.caption(T("Columns: tag · price · weight · remove. Removing an "
                               "animal returns it to the herd as Active."))

                    _extra = active_tag_matches("", limit=300)
                    _add_tags = st.multiselect(
                        "Add more animals to this invoice", _extra,
                        key=f"inv_add_{_iv}", format_func=T)
                    _add_vals = {}
                    for t in _add_tags:
                        ac = st.columns([2, 2, 2])
                        ac[0].markdown(f"{t}")
                        _c2 = get_cow(t)
                        _add_vals[t] = (
                            ac[1].number_input(f"aprice_{t}", min_value=0.0,
                                               value=0.0, step=50.0,
                                               key=f"inv_ap_{_iv}_{t}",
                                               label_visibility="collapsed"),
                            ac[2].number_input(
                                f"aweight_{t}", min_value=0.0,
                                value=float(_c2["weight"] or 0.0) if _c2 else 0.0,
                                step=1.0, key=f"inv_aw_{_iv}_{t}",
                                label_visibility="collapsed"))

                    if st.button("Save changes to invoice", type="primary",
                                 key=f"inv_save_{_iv}"):
                        if not _eb.strip():
                            st.warning("A buyer name is required.")
                        elif len(_to_remove) == len(items) and not _add_tags:
                            st.warning("That would empty the invoice — use "
                                       "'Void / delete invoice' instead.")
                        elif any(v[0] <= 0 for k, v in _new_vals.items()
                                 if k not in _to_remove):
                            st.warning("Every animal kept on the invoice needs a "
                                       "price greater than zero.")
                        elif any(v[0] <= 0 for v in _add_vals.values()):
                            st.warning("Enter a price for each animal you are adding.")
                        else:
                            for sid in _to_remove:
                                remove_sale_line(sid)
                            for sid, (pv, wv) in _new_vals.items():
                                if sid not in _to_remove:
                                    update_sale_line(sid, pv, wv)
                            for t, (pv, wv) in _add_vals.items():
                                cow2 = get_cow(t)
                                if not cow2:
                                    continue
                                add_sale({
                                    "invoice_no": inv_no,
                                    "date": _ed.isoformat(), "tag": t,
                                    "cow_name": cow2["name"], "buyer": _eb.strip(),
                                    "buyer_contact": _ec.strip(),
                                    "weight": float(wv) if wv else None,
                                    "price": float(pv), "payment_method": _emeth,
                                    "notes": _en.strip()})
                                update_cow(t, {**cow2, "status": "Sold"})
                                if not has_event(t, "Cow sold"):
                                    add_event(_ed.isoformat(), "Cow sold", t,
                                              f"Sold to {_eb.strip()} — {money(pv)}")
                            update_invoice_header(inv_no, _eb, _ec,
                                                  _ed.isoformat(), _emeth, _en)
                            st.session_state["inv_edit_version"] = _iv + 1
                            st.success(f"{inv_no} updated.")
                            st.rerun()

                if rm.button("Void / delete invoice", key="rm_sale"):
                    for i in items:
                        remove_sale_line(i["id"])
                    st.success(T(f"Deleted {inv_no}. "
                               f"{len(items)} animal(s) returned to the herd "
                               "as Active."))
                    st.rerun()


    # ═══════════ PURCHASES ═══════════
    with buy_tab:
        this_month = now_local().strftime("%Y-%m")
        month_spent = sum(p["price"] or 0 for p in purchases
                          if (p["date"] or "").startswith(this_month))
        pm1, pm2 = st.columns(2)
        pm1.metric("Purchases recorded", len(purchases))
        pm2.metric("Spent this month", money(month_spent))

        st.markdown('<div class="section">Record a purchase</div>',
                    unsafe_allow_html=True)
        st.caption(T("Buying a cow adds it to your herd (as active) and logs the cost here."))

        with st.form("record_purchase", clear_on_submit=True):
            p1, p2, p3 = st.columns(3)
            tag = p1.text_input("New tag *", placeholder="e.g. BW-0777")
            name = p2.text_input("Name (optional)", placeholder="e.g. Duke")
            breed = p3.text_input("Breed",
                                  placeholder=f"e.g. {SP['breeds'][2]}")

            p4, p5, p6 = st.columns(3)
            sex = p4.selectbox("Sex", [""] + SEXES, format_func=T)
            category = p5.selectbox("Category", [""] + CATEGORIES, format_func=T)
            weight = p6.number_input("Weight (kg)", min_value=0.0, value=0.0, step=1.0)

            p7, p8, p9 = st.columns(3)
            seller = p7.text_input("Seller name *", placeholder="e.g. K. Rampa")
            seller_contact = p8.text_input("Seller contact", placeholder="phone / email")
            buy_date = p9.date_input("Purchase date", value=date.today(),
                                      min_value=earliest_selectable(),
                                      max_value=date.today(),
                                      format="DD/MM/YYYY")

            p10, p11 = st.columns(2)
            price = p10.number_input(f"Purchase price ({currency_symbol()}) *", min_value=0.0,
                                     value=0.0, step=50.0)
            method = p11.selectbox("Payment method", PAYMENT_METHODS, format_func=T)
            note = st.text_area("Transaction notes (appear on the receipt)",
                                placeholder="e.g. bought at auction, paid in full…")
            submit_buy = st.form_submit_button("Record purchase & create receipt",
                                               type="primary")

        if submit_buy:
            if not tag.strip():
                st.warning(T("A tag is required to add the purchased cow."))
            elif not seller.strip():
                st.warning("A seller name is required.")
            elif price <= 0:
                st.warning("Enter a purchase price greater than zero.")
            elif get_cow(tag.strip()):
                st.error(T(f"A cow with tag '{tag.strip()}' already exists in the herd."))
            else:
                t = tag.strip()
                add_cow({
                    "tag": t, "name": name.strip(), "breed": breed.strip(), "sex": sex,
                    "dob": "", "colour": "", "weight": float(weight) if weight else None,
                    "category": category, "status": "Active",
                    "date_acquired": buy_date.isoformat(), "notes": note.strip(),
                })
                receipt = next_receipt_no()
                add_purchase({
                    "receipt_no": receipt, "date": buy_date.isoformat(), "tag": t,
                    "cow_name": name.strip(), "breed": breed.strip(), "sex": sex,
                    "seller": seller.strip(), "seller_contact": seller_contact.strip(),
                    "weight": float(weight) if weight else None, "price": float(price),
                    "payment_method": method, "notes": note.strip(),
                })
                add_event(buy_date.isoformat(), "Purchase", t,
                          f"Bought from {seller.strip()} — {money(price)}")
                st.success(T(f"✅ Purchase recorded. Receipt {receipt} created, "
                           f"'{t}' added to the herd."))
                st.rerun()

        st.divider()
        # ── BULK IMPORT OF PURCHASES ──────────
        st.markdown('<div class="section">Bulk import purchases</div>',
                    unsafe_allow_html=True)
        st.caption(T("Bought a lot of cattle at once? Load the whole lot from a "
                   "spreadsheet. Each row adds the cow to your herd and records "
                   "the cost, exactly as the form above does."))

        _pdone = st.session_state.get("purchase_import_result")
        if _pdone:
            st.success(_pdone["message"])
            _pd1, _pd2 = st.columns([2, 1])
            if _pdone.get("report"):
                _dl(_pd1,
                    "Download the rows that were not recorded",
                    _pdone["report"],
                    file_name=f"purchase_import_issues_{date.today().isoformat()}.csv",
                    mime="text/csv", use_container_width=True,
                    key="pbulk_issue_report")
            if _pd2.button("Dismiss", use_container_width=True,
                           key="pbulk_dismiss"):
                st.session_state["purchase_import_result"] = None
                st.rerun()

        _ptpl = purchase_template_frame()
        _pt1, _pt2 = st.columns(2)
        _dl(_pt1, "Template (CSV)",
                             _ptpl.to_csv(index=False).encode("utf-8"),
                             file_name="purchase_import_template.csv",
                             mime="text/csv", use_container_width=True,
                             key="pbulk_tpl_csv")
        _ptpl_xlsx = purchase_template_xlsx_bytes()
        if _ptpl_xlsx:
            _dl(_pt2,
                "Template (Excel)", _ptpl_xlsx,
                file_name="purchase_import_template.xlsx",
                mime=("application/vnd.openxmlformats-officedocument"
                      ".spreadsheetml.sheet"),
                use_container_width=True, key="pbulk_tpl_xlsx")
        else:
            _pt2.caption("The Excel template needs the openpyxl package. "
                         "The CSV template works either way.")

        with st.expander("Which columns can I use?"):
            st.markdown(
                "Tag, Price and Seller are the three you must have "
                "— though you can set one seller below to cover every blank "
                "row, which is what you want after an auction. Any extra "
                "columns are ignored.\n\n"
                "| Column | Accepts |\n|---|---|\n"
                "| Tag | also *New tag*, *Ear tag* |\n"
                "| Price | also *Cost*, *Amount paid*; `P 9,500` is fine |\n"
                "| Seller | also *Vendor*, *Sold by*, *Supplier* |\n"
                "| Seller contact | phone or email |\n"
                "| Purchase date | 14/03/2026 or 2026-03-14 |\n"
                "| Payment method | " + ", ".join(PAYMENT_METHODS) + " |\n"
                "| Sex | " + " / ".join(SEXES) + " (F and M work too) |\n"
                "| Category | " + ", ".join(CATEGORIES) + " |\n"
                "| Weight (kg) | a number; *kg* in the cell is fine |\n"
                "| Name, Breed, Notes | free text |\n\n"
                "Each row gets its own receipt number, so you can print any "
                "one of them afterwards.")

        _pdc1, _pdc2 = st.columns(2)
        _pdef_seller = _pdc1.text_input(
            "Seller for rows that leave it blank",
            key="pbulk_default_seller",
            placeholder="e.g. Lobatse auction")
        _pdef_date = _pdc2.date_input("Date for rows that leave it blank",
                                      value=date.today(),
                                      min_value=earliest_selectable(),
                                      max_value=date.today(),
                                      format="DD/MM/YYYY",
                                      key="pbulk_default_date")
        _pdc3, _pdc4 = st.columns(2)
        _pdef_method = _pdc3.selectbox("Payment method for rows that leave it blank",
                                       PAYMENT_METHODS, index=0,
                                       key="pbulk_default_method", format_func=T)
        _pdef_contact = _pdc4.text_input("Seller contact for rows that leave it blank",
                                         key="pbulk_default_contact",
                                        placeholder="phone / email")

        _pupload = st.file_uploader(
            "Choose an Excel or CSV file of purchases",
            type=["xlsx", "xlsm", "xltx", "csv"],
            key="pbulk_upload_" + str(st.session_state.get("pbulk_version", 0)),
            help="Old .xls files should be saved as .xlsx first.")

        if _pupload is not None:
            _pframe = None
            try:
                _pframe = read_tabular_upload(_pupload)
            except Exception as _pexc:
                st.error("That file could not be read: " + str(_pexc))

            if _pframe is not None and len(_pframe) > MAX_IMPORT_ROWS:
                st.error(f"That file has {len(_pframe):,} rows. Please split it "
                         f"into files of {MAX_IMPORT_ROWS:,} rows or fewer.")
            elif _pframe is not None and _pframe.empty:
                st.warning("That file has headings but no rows in it.")
            elif _pframe is not None:
                _pmap, _pignored = {}, []
                for _col in _pframe.columns:
                    _f = PURCHASE_HEADER_ALIASES.get(_norm_header(_col))
                    if _f and _f != "amount_alias" and _f not in _pmap:
                        _pmap[_f] = _col
                    else:
                        _pignored.append(_col)

                _missing = [n for n, f in (("Tag", "tag"), ("Price", "price"))
                            if f not in _pmap]
                if _missing:
                    st.error("No " + " and ".join(_missing)
                             + " column found. Rename the relevant column(s) "
                               "and upload again.")
                    st.caption("Columns found: "
                               + ", ".join(str(c) for c in _pframe.columns))
                else:
                    _prows = build_purchase_import_rows(_pframe, _pmap, {
                        "seller": _pdef_seller.strip(),
                        "seller_contact": _pdef_contact.strip(),
                        "date": (_pdef_date.isoformat() if _pdef_date else
                                 date.today().isoformat()),
                        "payment_method": _pdef_method,
                    })
                    _prows = [r for r in _prows if r["verdict"] != "blank"]
                    _pnew = [r for r in _prows if r["verdict"] == "new"]
                    _pdupe = [r for r in _prows if r["verdict"] == "duplicate"]
                    _pbad = [r for r in _prows if r["verdict"] == "skip"]

                    if not _prows:
                        st.warning("Every row in that file is empty.")
                    else:
                        _pcost = sum(r["rec"]["price"] or 0 for r in _pnew)
                        _ps1, _ps2, _ps3 = st.columns(3)
                        _ps1.metric("Ready to record", len(_pnew))
                        _ps2.metric("Total cost", money(_pcost))
                        _ps3.metric("Cannot record",
                                    len(_pbad) + len(_pdupe))
                        if _pignored:
                            st.caption("Columns ignored: "
                                       + ", ".join(str(c) for c in _pignored))

                        _pmarks = {"new": "✅ Ready",
                                   "duplicate": "🔁 Tag already in herd",
                                   "skip": "❌ Cannot record"}
                        _ppreview = pd.DataFrame([{
                            "Row": r["line"],
                            "Check": _pmarks[r["verdict"]],
                            "Tag": r["rec"]["tag"] or "—",
                            "Breed": r["rec"]["breed"],
                            "Sex": r["rec"]["sex"],
                            "Category": r["rec"]["category"],
                            "Weight": r["rec"]["weight"],
                            "Price": (money(r["rec"]["price"])
                                      if r["rec"]["price"] else "—"),
                            "Seller": r["rec"]["seller"],
                            "Date": r["rec"]["date"],
                            "Payment": r["rec"]["payment_method"],
                            "Notes on this row": "; ".join(r["issues"]),
                        } for r in _prows])
                        st.markdown('<div class="section">Preview</div>',
                                    unsafe_allow_html=True)
                        st.dataframe(_ppreview, use_container_width=True,
                                     hide_index=True, height=330)

                        _pb1, _pb2 = st.columns([3, 1])
                        _pgo = _pb1.button(
                            f"Record {len(_pnew)} purchase(s) — {money(_pcost)}",
                            type="primary", use_container_width=True,
                            disabled=(len(_pnew) == 0), key="pbulk_go")
                        if _pb2.button("Clear file", use_container_width=True,
                                       key="pbulk_clear"):
                            st.session_state["pbulk_version"] = (
                                st.session_state.get("pbulk_version", 0) + 1)
                            st.rerun()

                        if _pgo:
                            _padded = _pfailed = 0
                            _pspent = 0.0
                            _pweighed = 0
                            for r in _pnew:
                                _r = r["rec"]
                                if not add_cow({
                                    "tag": _r["tag"], "name": _r["name"],
                                    "breed": _r["breed"], "sex": _r["sex"],
                                    "dob": "", "colour": "",
                                    "weight": _r["weight"],
                                    "category": _r["category"],
                                    "status": "Active",
                                    "date_acquired": _r["date"],
                                    "notes": _r["notes"],
                                }):
                                    _pfailed += 1
                                    continue
                                add_purchase({
                                    "receipt_no": next_receipt_no(),
                                    "date": _r["date"], "tag": _r["tag"],
                                    "cow_name": _r["name"], "breed": _r["breed"],
                                    "sex": _r["sex"], "seller": _r["seller"],
                                    "seller_contact": _r["seller_contact"],
                                    "weight": _r["weight"],
                                    "price": float(_r["price"]),
                                    "payment_method": _r["payment_method"],
                                    "notes": _r["notes"],
                                })
                                add_event(_r["date"], "Purchase", _r["tag"],
                                          "Bought from " + _r["seller"]
                                          + " — " + money(_r["price"]))
                                if _r["weight"]:
                                    _wok, _ = add_weight(_r["tag"], _r["date"],
                                                         _r["weight"],
                                                         "Weight at purchase")
                                    if _wok:
                                        _pweighed += 1
                                _padded += 1
                                _pspent += float(_r["price"])

                            _pparts = [str(_padded) + " purchase(s) recorded",
                                       money(_pspent) + " spent"]
                            if _pweighed:
                                _pparts.append(str(_pweighed) + " weight(s) recorded")
                            if _pdupe:
                                _pparts.append(str(len(_pdupe))
                                               + " skipped (tag already in herd)")
                            if _pbad:
                                _pparts.append(str(len(_pbad)) + " row(s) skipped")
                            if _pfailed:
                                _pparts.append(str(_pfailed) + " row(s) failed")

                            _pleft = [r for r in _prows if r["verdict"] != "new"]
                            _preport = None
                            if _pleft:
                                _preport = pd.DataFrame([{
                                    "Row": r["line"],
                                    "Tag": r["rec"]["tag"],
                                    "Outcome": _pmarks[r["verdict"]],
                                    "Reason": "; ".join(r["issues"]),
                                } for r in _pleft]).to_csv(index=False).encode("utf-8")

                            st.session_state["purchase_import_result"] = {
                                "message": ("Import finished — "
                                            + ", ".join(_pparts) + "."),
                                "report": _preport,
                            }
                            st.session_state["pbulk_version"] = (
                                st.session_state.get("pbulk_version", 0) + 1)
                            st.rerun()

        st.divider()
        st.markdown('<div class="section">Purchase receipts</div>',
                    unsafe_allow_html=True)
        if not purchases:
            st.info("No purchases recorded yet.")
        else:
            df = pd.DataFrame(purchases)[
                ["receipt_no", "date", "tag", "seller", "payment_method", "price"]]
            df["price"] = df["price"].apply(money)
            df = df.rename(columns={"receipt_no": "Receipt", "date": "Date", "tag": "Tag",
                                    "seller": "Seller", "payment_method": "Payment",
                                    "price": "Amount"})
            st.dataframe(df, use_container_width=True, hide_index=True)
            _dl(st,
                "Download purchases (CSV)",
                pd.DataFrame(purchases).to_csv(index=False).encode("utf-8"),
                file_name=f"cattle_purchases_{date.today().isoformat()}.csv",
                mime="text/csv", key="dl_purchases")

            st.markdown("View / print a receipt")
            labels = {f"{p['receipt_no']} · {p['tag']} · {p['seller']} · {money(p['price'])}":
                      p["id"] for p in purchases}
            pick = st.selectbox("Pick a receipt", list(labels.keys()), key="pick_pur", format_func=T)
            pur = get_purchase(labels[pick])
            if pur:
                with st.container(border=True):
                    st.markdown(f"### {pur['receipt_no']}")
                    ic = st.columns(2)
                    ic[0].markdown(
                        T(f"Date: {pur['date']}  \nTag: {pur['tag'] or '—'}  \n"
                        f"Cow: {pur['cow_name'] or '—'}  \n"
                        f"Weight: {(str(pur['weight']) + ' kg') if pur['weight'] else '—'}"))
                    ic[1].markdown(
                        f"Seller: {pur['seller'] or '—'}  \n"
                        f"Contact: {pur['seller_contact'] or '—'}  \n"
                        f"Payment: {pur['payment_method'] or '—'}")
                    st.markdown(f"<div style='font-size:1.4rem;font-weight:800;color:{PRIMARY}'>"
                                f"Total: {money(pur['price'])}</div>", unsafe_allow_html=True)
                    if pur["notes"]:
                        st.caption(f"📝 {pur['notes']}")
                dl, rm = st.columns([2, 1])
                _rpdf = purchase_receipt_pdf(pur)
                if _rpdf:
                    _dl(dl, "Download receipt (PDF)", _rpdf,
                                       file_name=f"{pur['receipt_no']}.pdf",
                                       mime="application/pdf", type="primary",
                                       use_container_width=True, key="dl_rec_pdf")
                else:
                    _dl(dl, "Download receipt (HTML)",
                                       purchase_receipt_html(pur).encode("utf-8"),
                                       file_name=f"{pur['receipt_no']}.html",
                                       mime="text/html",
                                       use_container_width=True, key="dl_rec")
                if rm.button("🗑️ Void / delete", use_container_width=True, key="rm_pur"):
                    delete_purchase(pur["id"])
                    st.success(f"Deleted {pur['receipt_no']}.")
                    st.rerun()

# ── DEATH / MISSING ───────────────────────────
with tab_loss:
    st.markdown('<div class="section">Death / Missing register</div>',
                unsafe_allow_html=True)

    losses = all_losses()
    lm1, lm2, lm3 = st.columns(3)
    lm1.metric("Records", len(losses))
    lm2.metric("Deaths", sum(1 for l in losses if l["type"] == "Died"))
    lm3.metric("Missing", sum(1 for l in losses if l["type"] == "Missing"))

    st.markdown(T('<div class="section">Record a death or missing cow</div>'),
                unsafe_allow_html=True)

    if not has_active_cattle():
        st.info(T("No active cattle to record."))
    else:
        lsrch = st.text_input(T("🔎 Search cow tag"), key="loss_search",
                              placeholder="Start typing a tag, e.g. BW-04…").strip()
        matches = active_tag_matches(lsrch, limit=200)

        if lsrch and not matches:
            st.warning(T(f"No active cow matches '{lsrch}'."))
            sel_tag = None
        elif len(matches) == 1:
            sel_tag = matches[0]
            st.success(f"Selected 🏷️ {sel_tag}")
        else:
            hint = (f"{len(matches)} matches — pick one:" if lsrch
                    else "Or pick from the list:")
            sel_tag = st.selectbox(hint, matches, key="loss_tag", format_func=T)

        cow = get_cow(sel_tag) if sel_tag else None
        if cow:
            st.caption(f"{cow['name'] or 'Unnamed'} · {cow['breed'] or 'breed n/a'} · "
                       f"{cow['sex'] or 'sex n/a'}")

        with st.form("record_loss", clear_on_submit=True):
            lc1, lc2 = st.columns(2)
            loss_type = lc1.selectbox("What happened?", ["Died", "Missing"], format_func=T)
            loss_date = lc2.date_input("Date it died / went missing", value=date.today(),
            min_value=earliest_selectable(), max_value=date.today(),
            format="DD/MM/YYYY")
            cause = st.text_area("Cause / details (optional)",
                                 placeholder="e.g. illness, predator, strayed from kraal…")
            submit_loss = st.form_submit_button("Record", type="primary")

        if submit_loss:
            if not sel_tag or not cow:
                st.warning(T("Search and select a cow first."))
            else:
                new_status = "Deceased" if loss_type == "Died" else "Missing"
                update_cow(sel_tag, {
                    "name": cow["name"], "breed": cow["breed"], "sex": cow["sex"],
                    "dob": cow["dob"], "colour": cow["colour"], "weight": cow["weight"],
                    "category": cow["category"], "status": new_status,
                    "date_acquired": cow["date_acquired"], "notes": cow["notes"],
                })
                add_loss({"tag": sel_tag, "cow_name": cow["name"], "type": loss_type,
                          "date": loss_date.isoformat(), "cause": cause.strip()})
                ev_type = "Death" if loss_type == "Died" else "Missing"
                if not has_event(sel_tag, ev_type):
                    add_event(loss_date.isoformat(), ev_type, sel_tag, cause.strip())
                verb = "marked as deceased" if loss_type == "Died" else "marked as missing"
                st.success(f"✅ '{sel_tag}' {verb} and logged on the calendar.")
                st.rerun()

    st.divider()
    st.markdown('<div class="section">Register</div>', unsafe_allow_html=True)
    if not losses:
        st.info(T("No deaths or missing cattle recorded yet."))
    else:
        df = pd.DataFrame(losses)[["date", "type", "tag", "cow_name", "cause"]]
        df = df.rename(columns={"date": "Date", "type": "Type", "tag": "Tag",
                                "cow_name": "Name", "cause": "Cause / details"})
        st.dataframe(df, use_container_width=True, hide_index=True)
        _dl(st,
            "Download register (CSV)",
            pd.DataFrame(losses).to_csv(index=False).encode("utf-8"),
            file_name=f"death_missing_{date.today().isoformat()}.csv", mime="text/csv",
            key="dl_losses")

        st.markdown("Update a record")
        labels = {f"{l['date']} · {l['type']} · 🏷️ {l['tag']}"
                  f"{(' — ' + l['cow_name']) if l['cow_name'] else ''}": l
                  for l in losses}
        pick = st.selectbox("Pick a record", list(labels.keys()), key="pick_loss", format_func=T)
        rec = labels[pick]
        cols = st.columns(2)
        if rec["type"] == "Missing":
            if cols[0].button(T("✅ Mark found (reactivate cow)"), use_container_width=True):
                found = get_cow(rec["tag"])
                if found:
                    update_cow(rec["tag"], {
                        "name": found["name"], "breed": found["breed"],
                        "sex": found["sex"], "dob": found["dob"],
                        "colour": found["colour"], "weight": found["weight"],
                        "category": found["category"], "status": "Active",
                        "date_acquired": found["date_acquired"], "notes": found["notes"],
                    })
                add_event(date.today().isoformat(), "Note", rec["tag"],
                          "Found / returned to herd")
                delete_loss(rec["id"])
                st.success(f"'{rec['tag']}' reactivated and removed from the register.")
                st.rerun()
        if cols[1].button("🗑️ Delete this record", use_container_width=True):
            delete_loss(rec["id"])
            st.success(T("Record deleted. (The cow's status was left unchanged.)"))
            st.rerun()

# ── DAILY EXPENSES ────────────────────────────
with tab_exp:
    st.markdown('<div class="section">Daily expenses</div>', unsafe_allow_html=True)
    st.caption(T("Track day-to-day running costs — feed, veterinary care, labour, "
               "transport and more. Cattle purchases stay in Billing."))

    expenses = all_expenses()
    this_month = now_local().strftime("%Y-%m")
    today_iso = date.today().isoformat()
    total_exp = sum(e["amount"] or 0 for e in expenses)
    month_exp = sum(e["amount"] or 0 for e in expenses
                    if (e["date"] or "").startswith(this_month))
    today_exp = sum(e["amount"] or 0 for e in expenses if e["date"] == today_iso)

    e1, e2, e3 = st.columns(3)
    e1.metric("Total expenses", money(total_exp))
    e2.metric("This month", money(month_exp))
    e3.metric("Today", money(today_exp))

    st.markdown('<div class="section">Record an expense</div>', unsafe_allow_html=True)
    st.caption("Pick one or more categories and enter an amount for each — every "
               "category is saved as its own line.")

    # Version counter clears the form after saving, without writing widget state.
    _ev = st.session_state.get("exp_form_version", 0)
    xc1, xc2 = st.columns([2, 1])
    exp_date = xc1.date_input("Date", value=date.today(), key=f"exp_date_{_ev}")
    method = xc2.selectbox("Payment method", PAYMENT_METHODS,
                           key=f"exp_method_{_ev}", format_func=T)
    categories = st.multiselect("Categories", EXPENSE_CATEGORIES,
                                default=[EXPENSE_CATEGORIES[0]], key=f"exp_cats_{_ev}", format_func=T)
    description = st.text_input("Description (optional)", key=f"exp_desc_{_ev}",
                                placeholder="e.g. hay delivery, deworming, wages…")

    amounts = {}
    if categories:
        # One row per category, listed in the order they were picked. The old
        # three-column grid filled downwards, so with five categories they read
        # in an odd order and squeezed on a narrow window. A list always shows
        # every category picked, however many there are.
        st.markdown(f"Amount per category ({currency_symbol()}) — "
                    f"{len(categories)} selected")
        for catname in categories:
            _rl, _ri = st.columns([2, 3])
            _rl.markdown(
                f"<div style='padding-top:.55rem;font-weight:600;color:{INK}'>"
                f"{_html.escape(catname)}</div>", unsafe_allow_html=True)
            amounts[catname] = _ri.number_input(
                catname, min_value=0.0, value=0.0, step=10.0,
                key=f"exp_amt_{_ev}_{catname}", label_visibility="collapsed",
                placeholder="0.00")
        running = sum(v for v in amounts.values() if v)
        _filled = sum(1 for v in amounts.values() if v and v > 0)
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;"
            f"align-items:center;border-top:1px solid {GRID};margin-top:.4rem;"
            f"padding-top:.5rem'>"
            f"<span style='color:{MUTED};font-size:.8rem'>{_filled} of "
            f"{len(categories)} filled in</span>"
            f"<span style='font-weight:800;color:{PRIMARY}'>"
            f"Total: {money(running)}</span></div>", unsafe_allow_html=True)

    if st.button("Add expense", type="primary", key=f"exp_add_btn_{_ev}"):
        if not categories:
            st.warning("Select at least one category.")
        elif sum(v for v in amounts.values() if v) <= 0:
            st.warning("Enter an amount for at least one category.")
        else:
            lines = [(cat, float(amt)) for cat, amt in amounts.items() if amt and amt > 0]
            for cat, amt in lines:
                add_expense({
                    "date": exp_date.isoformat(), "category": cat,
                    "description": description.strip(), "amount": amt,
                    "payment_method": method,
                })
            total = sum(a for _, a in lines)
            detail = ", ".join(f"{cat} {money(amt)}" for cat, amt in lines)
            if description.strip():
                detail += f" — {description.strip()}"
            add_event(exp_date.isoformat(), "Expense", "",
                      f"{detail} · total {money(total)}")
            # New widget keys next run = a cleared form.
            st.session_state["exp_form_version"] = _ev + 1
            st.success(f"✅ Recorded {len(lines)} expense line"
                       + ("s" if len(lines) != 1 else "")
                       + f", total {money(total)}, and logged it on the calendar.")
            st.rerun()

    st.divider()
    st.markdown('<div class="section">Filter expenses by date</div>',
                unsafe_allow_html=True)
    st.caption("Look at a single day, or any stretch of days — a week, a month, "
               "a whole season.")

    # Version counter gives the pickers fresh keys on the next run, which is
    # how "clear" works without writing to a live widget's state.
    _fv = str(st.session_state.get("exp_filter_version", 0))

    # A quick-pick button only seeds the pickers' starting values; once the
    # farmer moves a date themselves, that choice wins.
    _preset = st.session_state.get("exp_filter_preset")
    if _preset == "week":
        _seed_from, _seed_to = date.today() - timedelta(days=6), date.today()
    elif _preset == "today":
        _seed_from = _seed_to = date.today()
    else:
        _seed_from, _seed_to = date.today().replace(day=1), date.today()

    with st.expander("📅 Calendar — choose the day or days to show"):
        _one_day = st.checkbox("Just one day", value=(_preset == "today"),
                               key="exp_one_day_" + _fv)
        # Both branches share the "from" key on purpose: toggling between one
        # day and a range then keeps the date the farmer already picked.
        if _one_day:
            _from_d = st.date_input("Day", value=_seed_to,
                                    key="exp_from_" + _fv, format="DD/MM/YYYY")
            _to_d = _from_d
        else:
            _fc1, _fc2 = st.columns(2)
            _from_d = _fc1.date_input("From", value=_seed_from,
                                      key="exp_from_" + _fv,
                                      format="DD/MM/YYYY")
            _to_d = _fc2.date_input("To", value=_seed_to,
                                    key="exp_to_" + _fv, format="DD/MM/YYYY")

        _qc1, _qc2, _qc3, _qc4 = st.columns(4)
        for _label, _tag, _col in (("Today", "today", _qc1),
                                   ("Last 7 days", "week", _qc2),
                                   ("This month", "month", _qc3),
                                   ("Reset", None, _qc4)):
            if _col.button(_label, use_container_width=True,
                           key="exp_q_" + str(_tag) + "_" + _fv):
                st.session_state["exp_filter_preset"] = _tag
                st.session_state["exp_filter_version"] = int(_fv) + 1
                st.rerun()

    if _from_d > _to_d:                       # tolerate a reversed range
        _from_d, _to_d = _to_d, _from_d
    _from_iso, _to_iso = _from_d.isoformat(), _to_d.isoformat()

    _picked = [e for e in expenses
               if _from_iso <= (e["date"] or "") <= _to_iso]
    _same_day = _from_iso == _to_iso
    if _same_day:
        _pretty = _from_d.strftime("%A, %d %B %Y")
    else:
        _pretty = (_from_d.strftime("%d %b %Y") + " to "
                   + _to_d.strftime("%d %b %Y")
                   + " (" + str((_to_d - _from_d).days + 1) + " days)")

    if not _picked:
        st.info("No expenses recorded for " + _pretty + ".")
    else:
        _ptotal = sum(e["amount"] or 0 for e in _picked)
        _pdays = len({e["date"] for e in _picked})
        _pm1, _pm2, _pm3 = st.columns(3)
        _pm1.metric("Expense lines", len(_picked))
        _pm2.metric("Total", money(_ptotal))
        _pm3.metric("Daily average" if not _same_day else "Days with spending",
                    money(_ptotal / max(1, (_to_d - _from_d).days + 1))
                    if not _same_day else str(_pdays))
        st.success(str(len(_picked)) + " expense"
                   + ("s" if len(_picked) != 1 else "")
                   + " for " + _pretty + " · total " + money(_ptotal))

        # Spending by category over whatever was picked.
        _dparts = {}
        for e in _picked:
            _cats = [c.strip() for c in (e["category"] or "Other").split(",")
                     if c.strip()] or ["Other"]
            _share = (e["amount"] or 0) / len(_cats)
            for _cname in _cats:
                _dparts[_cname] = _dparts.get(_cname, 0) + _share
        _dcat = pd.Series(_dparts).sort_values(ascending=False)
        _dc = st.columns([1, 2])
        _dtbl = _dcat.rename_axis("Category").reset_index(name="Amount")
        _dtbl["Amount"] = _dtbl["Amount"].apply(money)
        _dc[0].dataframe(_dtbl, use_container_width=True, hide_index=True)
        value_bar_chart(_dc[1], _dcat, color=TEAL2, height=220,
                        value_fmt=money, key="exp_chart_range")

        # Over several days, the day-by-day shape is worth seeing on its own.
        if not _same_day:
            _byday = {}
            for e in _picked:
                _byday[e["date"]] = _byday.get(e["date"], 0) + (e["amount"] or 0)
            if len(_byday) > 1:
                _dser = pd.Series({k: _byday[k] for k in sorted(_byday)})
                _dser.index = [datetime.fromisoformat(k).strftime("%d %b")
                               for k in sorted(_byday)]
                st.markdown("Day by day")
                value_bar_chart(st, _dser, color=PRIMARY, height=200,
                                value_fmt=money, key="exp_chart_byday")

        _ddf = pd.DataFrame(_picked)[
            ["date", "category", "description", "payment_method", "amount"]]
        _ddf["amount"] = _ddf["amount"].apply(money)
        _ddf = _ddf.rename(columns={"date": "Date", "category": "Category",
                                    "description": "Description",
                                    "payment_method": "Payment",
                                    "amount": "Amount"})
        st.dataframe(_ddf, use_container_width=True, hide_index=True)
        _dl(st,
            "Download these expenses (CSV)",
            pd.DataFrame(_picked).to_csv(index=False).encode("utf-8"),
            file_name=("expenses_" + _from_iso
                       + ("" if _same_day else "_to_" + _to_iso) + ".csv"),
            mime="text/csv", key="dl_range_exp")

    st.divider()
    st.markdown('<div class="section">Spending by category (this month)</div>',
                unsafe_allow_html=True)
    month_rows = [e for e in expenses if (e["date"] or "").startswith(this_month)]
    if month_rows:
        parts = {}
        for e in month_rows:
            cats = [c.strip() for c in (e["category"] or "Other").split(",")
                    if c.strip()] or ["Other"]
            share = (e["amount"] or 0) / len(cats)
            for cname in cats:
                parts[cname] = parts.get(cname, 0) + share
        cat = pd.Series(parts).sort_values(ascending=False)
        cc = st.columns([1, 2])
        tbl = cat.rename_axis("Category").reset_index(name="Amount")
        tbl["Amount"] = tbl["Amount"].apply(money)
        cc[0].dataframe(tbl, use_container_width=True, hide_index=True)
        value_bar_chart(cc[1], cat, color=PRIMARY, height=240,
                        value_fmt=money, key="exp_chart_month")
        st.caption("Expenses tagged with several categories are split evenly across them.")
    else:
        st.caption("No expenses recorded this month yet.")

    st.divider()
    st.markdown('<div class="section">Expense log</div>', unsafe_allow_html=True)
    if not expenses:
        st.info("No expenses recorded yet.")
    else:
        df = pd.DataFrame(expenses)[
            ["date", "category", "description", "payment_method", "amount"]]
        df["amount"] = df["amount"].apply(money)
        df = df.rename(columns={"date": "Date", "category": "Category",
                                "description": "Description",
                                "payment_method": "Payment", "amount": "Amount"})
        st.dataframe(df, use_container_width=True, hide_index=True)
        _dl(st,
            "Download expenses (CSV)",
            pd.DataFrame(expenses).to_csv(index=False).encode("utf-8"),
            file_name=f"daily_expenses_{date.today().isoformat()}.csv",
            mime="text/csv", key="dl_expenses")

        st.markdown("Delete an entry")
        labels = {f"{e['date']} · {e['category']} · {money(e['amount'])}"
                  f"{(' — ' + e['description']) if e['description'] else ''}": e["id"]
                  for e in expenses}
        pick = st.selectbox("Pick an expense", list(labels.keys()), key="pick_exp", format_func=T)
        if st.button("🗑️ Delete this expense", key="rm_exp"):
            delete_expense(labels[pick])
            st.success("Expense deleted.")
            st.rerun()

# ── FARM ACTIVITY ─────────────────────────────
with tab_act:
    st.markdown('<div class="section">Farm activity</div>', unsafe_allow_html=True)
    st.caption(T("Record work done on the farm and apply it to the whole herd or to "
               "selected tags. The activity is written into each chosen cow's notes "
               "and appears on the calendar for that day."))

    if total_cattle_count() == 0:
        st.info(T("No cattle loaded yet. Add some in the Load Cattle tab."))
    else:
        ac1, ac2 = st.columns([1, 2])
        act_date = ac1.date_input("Date of activity", value=date.today(),
                                  key="act_date")
        act_type = ac2.selectbox("Activity", ACTIVITY_TYPES, key="act_type", format_func=T)
        act_custom = st.text_input(
            "Activity name (only if you chose 'Other')", key="act_custom",
            placeholder="e.g. Hoof trimming")
        act_details = st.text_area(
            "Details (optional)", key="act_details",
            placeholder="e.g. Blackleg vaccine, 2 ml per head, administered by K. Rampa")

        st.markdown("Apply this activity to")
        scope_label = st.radio(
            "Selection", ["All active cattle", "All cattle (including sold/deceased)",
                          "Specific tags only"],
            key="act_scope", horizontal=False, label_visibility="collapsed", format_func=T)

        chosen_tags = []
        if scope_label == "Specific tags only":
            tsrch = st.text_input("🔎 Filter tags to choose from", key="act_tag_search",
                                  placeholder="Type part of a tag to narrow the list…"
                                  ).strip()
            options = [c["tag"] for c in cattle_page(300, 0, tsrch)]
            if not options:
                st.warning(T("No cattle match that filter."))
            chosen_tags = st.multiselect(
                f"Select tags ({len(options)} shown — refine the filter to see others)",
                options, key="act_tags", format_func=T)
            if st.checkbox("Select every tag currently listed above",
                           key="act_select_shown") and options:
                chosen_tags = options
            st.caption(f"{len(chosen_tags)} tag"
                       + ("s" if len(chosen_tags) != 1 else "") + " selected.")
        else:
            _n = (status_count("Active") if scope_label == "All active cattle"
                  else total_cattle_count())
            st.caption(T(f"This will be applied to {_n:,} cattle."))

        if st.button("Record activity", type="primary", key="act_add_btn"):
            act_name = (act_custom.strip() if act_type == "Other" and act_custom.strip()
                        else act_type)
            scope = ("active" if scope_label == "All active cattle"
                     else "all" if scope_label.startswith("All cattle")
                     else "tags")
            if scope == "tags" and not chosen_tags:
                st.warning(T("Select at least one tag, or choose a herd-wide option."))
            else:
                note_line = f"[{act_date.isoformat()}] {act_name}"
                if act_details.strip():
                    note_line += f": {act_details.strip()}"
                updated = append_note_to_tags(
                    chosen_tags, note_line,
                    all_active=(scope == "active"), all_cattle_flag=(scope == "all"))
                add_activity({
                    "date": act_date.isoformat(), "activity": act_name,
                    "details": act_details.strip(), "scope": scope,
                    "tags": ",".join(chosen_tags) if scope == "tags" else "",
                    "tag_count": updated,
                })
                scope_txt = ("all cattle" if scope == "all"
                             else "all active cattle" if scope == "active"
                             else f"{len(chosen_tags)} selected tag"
                                  + ("s" if len(chosen_tags) != 1 else ""))
                # No add_event here: add_activity() above already puts this
                # on the calendar. Writing both listed it twice on the day.
                st.success(T(f"✅ '{act_name}' recorded, added to the notes of "
                           f"{updated:,} cattle, and logged on the calendar for "
                           f"{act_date.isoformat()}."))
                st.rerun()

    # ── Weighing session: weigh several animals in one go ──
    st.divider()
    st.markdown('<div class="section">Weighing session</div>',
                unsafe_allow_html=True)
    st.caption("Weigh a group in one go. Each entry is added to that animal's "
               "weight history, so you can track growth over time.")
    if total_cattle_count() == 0:
        st.info(T("No cattle loaded yet."))
    else:
        _sv = st.session_state.get("weigh_sess_version", 0)
        ws1, ws2 = st.columns([1, 2])
        _wsdate = ws1.date_input("Date weighed", value=date.today(),
                                 min_value=earliest_selectable(),
                                 max_value=date.today(),
                                 format="DD/MM/YYYY",
                                 key=f"ws_date_{_sv}")
        _wsnote = ws2.text_input("Note applied to all (optional)",
                                 key=f"ws_note_{_sv}",
                                 placeholder="e.g. weaning weights, pre-sale")
        _wsearch = st.text_input("Filter tags", key=f"ws_search_{_sv}",
                                 placeholder="Type part of a tag to narrow the list…"
                                 ).strip()
        _wopts = active_tag_matches(_wsearch, limit=300)
        _wtags = st.multiselect(T(f"Cattle to weigh ({len(_wopts)} shown)"), _wopts,
                                key=f"ws_tags_{_sv}", format_func=T)
        _entries = {}
        if _wtags:
            st.markdown("Weight per animal (kg)")
            for _t in _wtags:
                _prev = weight_stats(_t)
                _rc = st.columns([2, 2, 3])
                _rc[0].markdown(f"{_t}")
                _entries[_t] = _rc[1].number_input(
                    f"w_{_t}", min_value=0.0, value=0.0, step=1.0,
                    key=f"ws_kg_{_sv}_{_t}", label_visibility="collapsed")
                if _prev:
                    _rc[2].caption(f"last {_prev['last_weight']:,.0f} kg on "
                                   f"{_prev['last_date']}")
                else:
                    _rc[2].caption("no previous weighing")
            _done = [t for t, v in _entries.items() if v and v > 0]
            st.caption(f"{len(_done)} of {len(_wtags)} entered.")

        if st.button("Save weighing session", type="primary",
                     key=f"ws_save_{_sv}"):
            _valid = {t: v for t, v in _entries.items() if v and v > 0}
            if not _valid:
                st.warning("Enter a weight for at least one animal.")
            else:
                _n = bulk_add_weights(_valid, _wsdate.isoformat(), _wsnote.strip())
                append_note_to_tags(
                    list(_valid.keys()),
                    f"[{_wsdate.isoformat()}] Weighed"
                    + (f": {_wsnote.strip()}" if _wsnote.strip() else ""))
                add_activity({"date": _wsdate.isoformat(), "activity": "Weighing",
                              "details": _wsnote.strip(), "scope": "tags",
                              "tags": ",".join(_valid.keys()), "tag_count": _n})
                # add_activity() above is what the calendar reads; a second
                # event here would show the same session twice.
                st.session_state["weigh_sess_version"] = _sv + 1
                st.success(f"{_n} weight(s) recorded and added to each animal's "
                           "history.")
                st.rerun()

    st.divider()
    st.markdown('<div class="section">Activity log</div>', unsafe_allow_html=True)
    recent = all_activities(limit=5)
    if not recent:
        st.info("No activities recorded yet.")
    else:
        st.caption("The five most recent activities.")
        st.dataframe(activities_frame(recent), use_container_width=True,
                     hide_index=True)

        st.divider()
        st.markdown("Search the activity log")
        st.caption("Filter by day, by activity, or both — then download exactly "
                   "what you searched for.")
        _av = st.session_state.get("act_search_version", 0)
        as1, as2, as3, as4 = st.columns([1.3, 2, 1, 1])
        _use_date = as1.checkbox("Filter by date", key=f"act_use_date_{_av}")
        _sdate = as1.date_input("Date", value=date.today(),
                                key=f"act_sdate_{_av}",
                                label_visibility="collapsed",
                                disabled=not _use_date)
        _stext = as2.text_input("Activity or detail", key=f"act_stext_{_av}",
                                placeholder="e.g. vaccination, dipping, a tag no.…")
        if as3.button("Search", type="primary", use_container_width=True,
                      key=f"act_search_btn_{_av}"):
            st.session_state["act_search_on"] = True
            st.session_state["act_q_date"] = (_sdate.isoformat() if _use_date else "")
            st.session_state["act_q_text"] = _stext.strip()
        if as4.button("Clear", use_container_width=True,
                      key=f"act_clear_btn_{_av}"):
            st.session_state["act_search_on"] = False
            st.session_state["act_search_version"] = _av + 1
            st.rerun()

        if st.session_state.get("act_search_on"):
            _qd = st.session_state.get("act_q_date", "")
            _qt = st.session_state.get("act_q_text", "")
            found = search_activities(_qd, _qt)
            _bits = []
            if _qd:
                _bits.append(f"on {_qd}")
            if _qt:
                _bits.append(f"matching '{_qt}'")
            _desc = " ".join(_bits) if _bits else "(all activities)"
            if not found:
                st.warning(f"No activities found {_desc}.")
            else:
                _total_head = sum(a["tag_count"] or 0 for a in found)
                st.success(f"{len(found)} activit"
                           + ("y" if len(found) == 1 else "ies")
                           + f" found {_desc} · {_total_head:,} cattle affected.")
                _fdf = activities_frame(found)
                st.dataframe(_fdf, use_container_width=True, hide_index=True)
                _fname = "activities"
                if _qd:
                    _fname += f"_{_qd}"
                if _qt:
                    _fname += "_" + re.sub(r"[^A-Za-z0-9]+", "-", _qt)[:20]
                _dl(st,
                    f"Download these {len(found)} result"
                    + ("s" if len(found) != 1 else "") + " (CSV)",
                    _fdf.to_csv(index=False).encode("utf-8"),
                    file_name=f"{_fname}.csv", mime="text/csv", key="dl_acts_found")

        st.divider()
        st.markdown("Remove a log entry")
        st.caption(T("This deletes the log record only — notes already written to each "
                   "cow are kept as part of that animal's history."))
        _pool = all_activities(limit=50)
        alabels = {f"{a['date']} · {a['activity']} · {a['tag_count']} cattle": a["id"]
                   for a in _pool}
        apick = st.selectbox("Pick an entry", list(alabels.keys()), key="pick_act", format_func=T)
        if st.button("Delete log entry", key="rm_act"):
            delete_activity(alabels[apick])
            st.success("Log entry deleted.")
            st.rerun()


# ── BREEDING & CALVING ────────────────────────
with tab_breed:
    st.markdown(T('<div class="section">Breeding &amp; calving</div>'),
                unsafe_allow_html=True)
    st.caption(T("Record services, confirm pregnancies and log calvings. Expected "
               f"calving dates are worked out at {GESTATION_DAYS} days' gestation "
               "and appear on the calendar."))

    _served, _preg, _soon, _over = breeding_metrics()
    bm1, bm2, bm3, bm4 = st.columns(4)
    bm1.metric("Awaiting result", f"{_served:,}")
    bm2.metric("Confirmed pregnant", f"{_preg:,}")
    bm3.metric("Due within 30 days", f"{_soon:,}")
    bm4.metric("Overdue", f"{_over:,}",
               delta=f"{_over} past due" if _over else None,
               delta_color="inverse" if _over else "off")

    _bv = st.session_state.get("breed_form_version", 0)

    # ── Due / overdue watchlist ──────────────
    _overdue = overdue_calvings()
    _due30 = due_between(date.today().isoformat(),
                         (date.today() + timedelta(days=30)).isoformat())
    if _overdue or _due30:
        with st.expander(T(f"Calving watchlist — {len(_overdue)} overdue, "
                         f"{len(_due30)} due within 30 days"),
                         expanded=bool(_overdue)):
            for r in _overdue:
                st.markdown(f"- {r['cow_tag']} was due {r['due_date']} "
                            f"({abs(days_until(r['due_date']) or 0)} days ago)")
            for r in _due30:
                _d = days_until(r["due_date"])
                _when = "today" if _d == 0 else f"in {_d} days"
                st.markdown(f"- {r['cow_tag']} due {r['due_date']} ({_when})")

    st.divider()
    _b_rec, _b_check, _b_calve = st.tabs(
        ["Record a service", "Pregnancy check", T("Record a calving")])

    # ── 1. Record a service ──────────────────
    with _b_rec:
        _females = [c["tag"] for c in cattle_page(400, 0, "")
                    if c["status"] == "Active" and c["sex"] == "Female"]
        if not _females:
            st.info(T("No active female cattle on record yet."))
        else:
            r1, r2 = st.columns(2)
            _dam = r1.selectbox(T("Cow served (dam)"), _females,
                                key=f"br_dam_{_bv}", format_func=T)
            _bull = r2.text_input(T("Bull / sire (tag or name)"),
                                  key=f"br_bull_{_bv}",
                                  placeholder="e.g. BW-0900 or AI straw code")
            r3, r4 = st.columns(2)
            _sdate = r3.date_input("Service date", value=date.today(),
                                 min_value=earliest_selectable(),
                                 max_value=date.today(),
                                 format="DD/MM/YYYY",
                                   key=f"br_sdate_{_bv}")
            _method = r4.selectbox("Method", BREEDING_METHODS,
                                   key=f"br_method_{_bv}", format_func=T)
            _bnote = st.text_input("Notes (optional)", key=f"br_note_{_bv}",
                                   placeholder="e.g. second service, synchronised")

            _due_preview = expected_due(_sdate.isoformat())
            st.markdown(T(f"<div style='font-weight:700;color:{PRIMARY}'>"
                        f"Expected calving: {_due_preview}</div>"),
                        unsafe_allow_html=True)
            _mk_reminder = st.checkbox(
                T("Add a calendar reminder for the expected calving date"),
                value=True, key=f"br_remind_{_bv}")

            if st.button("Record service", type="primary",
                         key=f"br_save_{_bv}"):
                _open = [b for b in breedings_for(_dam)
                         if b["status"] in ("Served", "Pregnant")]
                if _open:
                    st.warning(T(f"{_dam} already has an open breeding record "
                               f"({_open[0]['status']}, served "
                               f"{_open[0]['service_date']}). Close it off with a "
                               "pregnancy result or calving first."))
                else:
                    add_service({"cow_tag": _dam, "bull_tag": _bull.strip(),
                                 "service_date": _sdate.isoformat(),
                                 "method": _method, "notes": _bnote.strip()})
                    add_event(_sdate.isoformat(), "Note", _dam,
                              f"Served by {_bull.strip() or 'unrecorded sire'} "
                              f"({_method}) — due {_due_preview}")
                    if _mk_reminder and _due_preview:
                        add_reminder({"date": _due_preview,
                                      "task": f"Calving due — {_dam}",
                                      "details": f"Served {_sdate.isoformat()}",
                                      "priority": "High", "tag": _dam})
                    st.session_state["breed_form_version"] = _bv + 1
                    st.success(T(f"Service recorded for {_dam}. Expected calving "
                               f"{_due_preview}."))
                    st.rerun()

    # ── 2. Pregnancy check ───────────────────
    with _b_check:
        _open_list = open_services()
        if not _open_list:
            st.info("No services are awaiting a pregnancy result.")
        else:
            _lbl = {f"{b['cow_tag']} · served {b['service_date']} · "
                    f"{b['bull_tag'] or 'sire n/a'}": b["id"] for b in _open_list}
            _pick = st.selectbox("Service to check", list(_lbl.keys()),
                                 key=f"br_check_pick_{_bv}", format_func=T)
            c1, c2 = st.columns(2)
            _result = c1.selectbox("Result", ["Pregnant", "Not pregnant"],
                                   key=f"br_result_{_bv}", format_func=T)
            _cdate = c2.date_input("Check date", value=date.today(),
                                   key=f"br_cdate_{_bv}")
            if st.button("Save result", type="primary", key=f"br_check_save_{_bv}"):
                _bid = _lbl[_pick]
                _rec = [b for b in _open_list if b["id"] == _bid][0]
                set_pregnancy(_bid, _result, _cdate.isoformat())
                add_event(_cdate.isoformat(), "Note", _rec["cow_tag"],
                          f"Pregnancy check: {_result}")
                st.session_state["breed_form_version"] = _bv + 1
                st.success(f"{_rec['cow_tag']} recorded as {_result.lower()}.")
                st.rerun()

    # ── 3. Record a calving ──────────────────
    with _b_calve:
        _preg_list = pregnant_records()
        if not _preg_list:
            st.info(T("No cows are currently confirmed pregnant."))
        else:
            _lbl2 = {f"{b['cow_tag']} · due {b['due_date']}": b["id"]
                     for b in _preg_list}
            _pick2 = st.selectbox(T("Cow that calved"), list(_lbl2.keys()),
                                  key=f"br_calve_pick_{_bv}", format_func=T)
            k1, k2 = st.columns(2)
            _kdate = k1.date_input(T("Calving date"), value=date.today(),
                                   key=f"br_kdate_{_bv}")
            _outcome = k2.selectbox("Outcome", ["Live calf", "Stillborn",
                                                "Lost / aborted"],
                                    key=f"br_outcome_{_bv}", format_func=T)
            _add_calf = st.checkbox(T("Register the calf in the herd now"),
                                    value=True, key=f"br_addcalf_{_bv}")
            _ctag = _csex = _cbreed = ""
            _cweight = 0.0
            if _add_calf and _outcome == "Live calf":
                g1, g2, g3 = st.columns(3)
                _ctag = g1.text_input(T("Calf tag *"), key=f"br_ctag_{_bv}",
                                      placeholder="e.g. BW-0451")
                _csex = g2.selectbox(T("Calf sex"), [""] + SEXES, key=f"br_csex_{_bv}", format_func=T)
                _cweight = g3.number_input("Birth weight (kg)", min_value=0.0,
                                           value=0.0, step=1.0,
                                           key=f"br_cwt_{_bv}")

            if st.button(T("Record calving"), type="primary", key=f"br_calve_save_{_bv}"):
                _bid = _lbl2[_pick2]
                _rec = [b for b in _preg_list if b["id"] == _bid][0]
                _dam_tag = _rec["cow_tag"]
                _live = _outcome == "Live calf"
                _calf_payload = None
                if _live and _add_calf:
                    _calf_payload = {"tag": _ctag.strip(), "sex": _csex,
                                     "weight": _cweight}
                if _live and _add_calf and not _ctag.strip():
                    st.warning(T("Enter a tag for the calf, or untick "
                               "'Register the calf in the herd now'."))
                else:
                    _ok, _msg = complete_calving(
                        _bid, _dam_tag, _kdate.isoformat(), _outcome, _calf_payload)
                    if not _ok:
                        st.error(_msg)
                    else:
                        st.session_state["breed_form_version"] = _bv + 1
                        st.success(_msg)
                        st.rerun()

    # ── Breeding register ────────────────────
    st.divider()
    st.markdown('<div class="section">Breeding register</div>',
                unsafe_allow_html=True)
    f1, f2 = st.columns([1, 1])
    _fstatus = f1.selectbox("Filter by status", ["All"] + BREEDING_STATUSES,
                            key="br_filter_status", format_func=T)
    _ftag = f2.text_input(T("Filter by cow tag"), key="br_filter_tag",
                          placeholder="e.g. BW-0421")
    _rows = breeding_records(None if _fstatus == "All" else _fstatus, _ftag)
    if not _rows:
        st.info("No breeding records match.")
    else:
        _bdf = pd.DataFrame([{
            SP["One"]: r["cow_tag"],
            "Sire": r["bull_tag"] or "—",
            "Served": r["service_date"],
            "Method": r["method"] or "—",
            "Status": r["status"],
            "Due": r["due_date"] or "—",
            T("Calved"): r["calving_date"] or "—",
            SP["Young"]: r["calf_tag"] or "—",
        } for r in _rows])
        st.dataframe(_bdf, use_container_width=True, hide_index=True)
        _dl(st,
            f"Download these {len(_rows)} record"
            + ("s" if len(_rows) != 1 else "") + " (CSV)",
            _bdf.to_csv(index=False).encode("utf-8"),
            file_name=f"breeding_records_{date.today().isoformat()}.csv",
            mime="text/csv", key="dl_breed")

        st.markdown("Remove a breeding record")
        _rl = {f"{r['cow_tag']} · served {r['service_date']} · {r['status']}": r["id"]
               for r in _rows[:60]}
        _rp = st.selectbox("Pick a record", list(_rl.keys()), key="br_del_pick", format_func=T)
        if st.button("Delete record", key="br_del"):
            delete_breeding(_rl[_rp])
            st.success("Breeding record deleted.")
            st.rerun()


# ── ANALYTICS: HERD INTELLIGENCE ──────────────
with tab_analytics:
    st.markdown(T('<div class="section">Analytics — herd intelligence</div>'),
                unsafe_allow_html=True)
    st.caption(T("Which cows breed reliably, whose calves grow well, what needs "
               "attention and what to expect next — worked out from your own "
               "records. Every figure can be traced back to a calving, a "
               "weighing or a service you entered."))

    _an = herd_intelligence()
    _h = _an["herd"]

    if not _an["rows"]:
        st.info(T("No breeding females on record yet. Add cows and heifers, then "
                "record services and calvings in the Breeding tab — the analysis "
                "builds itself from there."))
    else:
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("Breeding females", f"{_h['analysed']:,}")
        a2.metric(T("Average calving interval"),
                  f"{_h['avg_interval']:.0f} days" if _h["avg_interval"] else "—",
                  delta=("target 365" if _h["avg_interval"] else None),
                  delta_color="off")
        a3.metric("Conception rate",
                  f"{_h['conception_rate']:.0f}%" if _h["conception_rate"]
                  is not None else "—")
        a4.metric(T("Average calf growth"),
                  f"{_h['avg_calf_adg']:.2f} kg/day" if _h["avg_calf_adg"] else "—")

        _scored = _an["scored"]
        _known = [r for r in _scored if r["score"] is not None]

        # ── Performers ───────────────────────
        st.markdown('<div class="section">Best performers</div>',
                    unsafe_allow_html=True)
        st.caption(T("Ranked on fertility (calving interval and conception) and on "
                   "how well their calves survive and grow."))
        _top = [r for r in _known if r["score"] >= 55][:10]
        if _top:
            st.dataframe(pd.DataFrame([{
                SP["One"]: r["tag"],
                "Rating": performance_band(r["score"]),
                "Score": f"{r['score']:.0f}",
                T("Calves"): r["calves"],
                T("Calving interval"): (f"{r['calving_interval']:.0f} d"
                                     if r["calving_interval"] else "—"),
                T("Calf growth"): (f"{r['calf_adg']:.2f} kg/d"
                                if r["calf_adg"] else "—"),
                T("In calf"): "Yes" if r["pregnant_now"] else "—",
                "Based on": r["confidence"],
            } for r in _top]), use_container_width=True, hide_index=True)
        else:
            st.caption(T("No cow has enough recorded history to rate yet."))

        # ── Needs attention ──────────────────
        st.markdown('<div class="section">Not performing</div>',
                    unsafe_allow_html=True)
        st.caption(T("Low fertility, long gaps between calves, or calves that grow "
                   "poorly. Check these before culling — a low score with "
                   "'limited' evidence just means few records."))
        _poor = [r for r in _known if r["score"] < 55][::-1][:10]
        if _poor:
            st.dataframe(pd.DataFrame([{
                SP["One"]: r["tag"],
                "Rating": performance_band(r["score"]),
                "Score": f"{r['score']:.0f}",
                T("Calves"): r["calves"],
                "Services": r["services"],
                T("Calving interval"): (f"{r['calving_interval']:.0f} d"
                                     if r["calving_interval"] else "—"),
                T("Calf growth"): (f"{r['calf_adg']:.2f} kg/d"
                                if r["calf_adg"] else "—"),
                "Based on": r["confidence"],
            } for r in _poor]), use_container_width=True, hide_index=True)
        else:
            st.success(T("No cow is currently rating poorly."))

        # ── Fertility split ──────────────────
        st.markdown('<div class="section">Fertility</div>', unsafe_allow_html=True)
        _fert_known = [r for r in _an["rows"] if r["fertility_score"] is not None]
        _fertile = [r for r in _fert_known if r["fertility_score"] >= 60]
        _borderline = [r for r in _fert_known if 35 <= r["fertility_score"] < 60]
        _infertile = [r for r in _fert_known if r["fertility_score"] < 35]
        f1, f2, f3 = st.columns(3)
        f1.metric("Fertile", f"{len(_fertile):,}")
        f2.metric("Borderline", f"{len(_borderline):,}")
        f3.metric("Problem breeders", f"{len(_infertile):,}")
        if _fert_known:
            value_bar_chart(
                st, pd.Series({"Fertile": len(_fertile),
                               "Borderline": len(_borderline),
                               "Problem": len(_infertile)}),
                color=PRIMARY, height=200, key="analytics_fert_chart")
        if _infertile:
            with st.expander(f"Problem breeders ({len(_infertile)})"):
                st.dataframe(pd.DataFrame([{
                    SP["One"]: r["tag"],
                    "Fertility score": f"{r['fertility_score']:.0f}",
                    T("Calvings"): r["calvings"], "Services": r["services"],
                    T("Days since calving"): r["days_since_calving"] or "—",
                    "Age (months)": r["age_months"] or "—",
                } for r in sorted(_infertile,
                                  key=lambda x: x["fertility_score"])]),
                    use_container_width=True, hide_index=True)

        # ── Alerts ───────────────────────────
        st.markdown('<div class="section">Needs your attention</div>',
                    unsafe_allow_html=True)
        _alerts = _an["alerts"]
        if not _alerts:
            st.success(T("Nothing flagged — no overdue calvings, long open periods "
                       "or animals losing condition."))
        else:
            _by_kind = {}
            for kind, tag, detail in _alerts:
                _by_kind.setdefault(kind, []).append((tag, detail))
            for kind, items in _by_kind.items():
                with st.expander(f"{kind} — {len(items)}",
                                 expanded=kind in ("Overdue to calve",
                                                   "Losing weight")):
                    for tag, detail in items[:40]:
                        st.markdown(f"- {tag} — {detail}")

        # ── Predictions ──────────────────────
        st.markdown('<div class="section">What to expect</div>',
                    unsafe_allow_html=True)
        _p = _an["predictions"]
        p1, p2 = st.columns(2)
        p1.metric(T("Calvings due within 90 days"), f"{len(_p['due_90']):,}")
        p2.metric(T("Calves expected in 12 months"),
                  f"{_p['expected_calves_12m']:,}")
        st.caption(T("Confirmed pregnancies plus cows likely to calve again based "
                   "on their own average interval. A projection, not a promise."))

        if _p["due_90"]:
            with st.expander(f"Due within 90 days ({len(_p['due_90'])})",
                             expanded=True):
                for r in _p["due_90"]:
                    _d = days_until(r["due_date"])
                    st.markdown(f"- {r['cow_tag']} — {r['due_date']} "
                                f"({'today' if _d == 0 else f'in {_d} days'})")
        if _p["likely_calvings"]:
            with st.expander(T(f"Likely to calve again "
                             f"({len(_p['likely_calvings'])})")):
                st.caption(T("Based on each cow's own calving interval — she is not "
                           "confirmed in calf."))
                for tag, when in _p["likely_calvings"][:30]:
                    st.markdown(f"- {tag} — around {when}")
        if _p["weight_projection"]:
            with st.expander(f"Weight in 90 days "
                             f"({len(_p['weight_projection'])} animals)"):
                st.caption("Projected from each animal's own daily gain so far.")
                st.dataframe(pd.DataFrame([{
                    "Tag": t, "Now (kg)": f"{now_kg:,.0f}",
                    "In 90 days (kg)": f"{then_kg:,.0f}",
                    "Daily gain": f"{adg:+.2f} kg/d",
                } for t, now_kg, then_kg, adg in _p["weight_projection"]]),
                    use_container_width=True, hide_index=True)

        # ── Heat watchlist ───────────────────
        st.markdown(T('<div class="section">Cows to watch for heat</div>'),
                    unsafe_allow_html=True)
        st.caption(T("Worked out from each cow's own dates — her last service, "
                   "her last calving, or her age. These are estimates to tell "
                   "you who to watch, not a timetable of who is on heat."))

        _hw_days = st.radio("Look ahead", ["Next 3 days", "Next 7 days",
                                           "Next 14 days", "Next 21 days"],
                            index=1, horizontal=True, key="heat_horizon",
                            label_visibility="collapsed", format_func=T)
        _hw = heat_watchlist(days_ahead={"Next 3 days": 3, "Next 7 days": 7,
                                         "Next 14 days": 14,
                                         "Next 21 days": 21}[_hw_days])

        _hm1, _hm2, _hm3 = st.columns(3)
        _hm1.metric("Expected in this window", len(_hw["due"]))
        _hm2.metric("Watch, no date possible", len(_hw["watch"]))
        _hm3.metric(T("In calf (left out)"), _hw["in_calf"])

        if not _hw["due"] and not _hw["watch"]:
            st.info(T("No cows to flag. Either every breeding female is in calf, "
                    "or there are not enough dates recorded yet — record "
                    "services and calvings in the Breeding tab and this fills "
                    "itself in."))
        else:
            if _hw["due"]:
                _returns = [r for r in _hw["due"] if r["after_service"]]
                if _returns:
                    st.warning("" + str(len(_returns)) + " cow(s) served "
                               "recently are due to return to heat. If one "
                               "of them comes on heat, she did not hold to "
                               "that service — serve her again and update the "
                               "Breeding tab.")

                st.dataframe(pd.DataFrame([{
                    SP["One"]: r["tag"],
                    "Name": r["name"],
                    "Category": r["category"],
                    "Expected": (datetime.fromisoformat(r["expected"])
                                 .strftime("%a %d %b")),
                    "Watch between": (datetime.fromisoformat(r["window_from"])
                                      .strftime("%d %b") + " – "
                                      + datetime.fromisoformat(r["window_to"])
                                      .strftime("%d %b")),
                    "In": ("today" if r["days_away"] == 0
                           else ("overdue" if r["days_away"] < 0
                                 else str(r["days_away"]) + " days")),
                    "Worked out from": r["basis"],
                    "Last record": r["anchor"] or "—",
                } for r in _hw["due"]]), use_container_width=True,
                    hide_index=True)

            if _hw["watch"]:
                with st.expander("Cows to watch, but no date can be estimated ("
                                 + str(len(_hw["watch"])) + ")"):
                    st.caption(T("Either nothing has been recorded for these cows "
                               "yet, or the last record is too old to count "
                               "cycles from without the estimate drifting. "
                               "Watch them the ordinary way."))
                    st.dataframe(pd.DataFrame([{
                        SP["One"]: r["tag"],
                        "Name": r["name"],
                        "Category": r["category"],
                        "Age": (str(r["age_months"]) + " months"
                                if r["age_months"] is not None else "unknown"),
                        "Why": r["basis"],
                        "Last record": r["anchor"] or "—",
                    } for r in _hw["watch"]]), use_container_width=True,
                        hide_index=True)

            _hrows = _hw["due"] + _hw["watch"]
            _dl(st,
                "Download the heat watchlist (CSV)",
                pd.DataFrame([{
                    SP["One"]: r["tag"], "Name": r["name"],
                    "Category": r["category"],
                    "Expected heat": r["expected"] or "",
                    "Watch from": r["window_from"] or "",
                    "Watch to": r["window_to"] or "",
                    "Worked out from": r["basis"],
                    "Last record": r["anchor"] or "",
                    "Cycles counted": (r["cycles"] if r["cycles"] is not None
                                       else ""),
                } for r in _hrows]).to_csv(index=False).encode("utf-8"),
                file_name=f"heat_watchlist_{date.today().isoformat()}.csv",
                mime="text/csv", key="dl_heat")

        with st.expander("How this list is worked out — and what it cannot do"):
            st.markdown(
                T("Watch your cattle. This list does not replace that. A cow "
                "in standing heat is the only reliable sign there is, and it "
                "shows for roughly half a day, often overnight. Check the herd "
                "twice a day, morning and evening. If a cow is standing to be "
                "mounted, serve her — whatever this page says.\n\n"
                f"The cycle. Cows come on heat about every "
                f"{OESTRUS_CYCLE_DAYS} days, but anywhere from 18 to 24 days is "
                f"normal, which is why each cow is given a window of give or "
                f"take {OESTRUS_WINDOW_DAYS} days rather than a single date.\n\n"
                "After a service. A cow that did not conceive usually "
                f"returns to heat about {OESTRUS_CYCLE_DAYS} days later. That "
                "return is the most useful thing here: it is how you find out "
                "early that a service did not take, instead of waiting for a "
                "pregnancy check.\n\n"
                f"After calving. Cycling usually resumes around "
                f"{POSTPARTUM_HEAT_DAYS} days after calving, but this varies "
                "widely with condition, suckling and feed. A thin cow suckling "
                "a calf on poor grazing may take far longer.\n\n"
                f"Heifers. A heifer is included once she reaches "
                f"{HEIFER_BREEDING_MONTHS} months, the usual target for a first "
                "service. Breed, weight and condition matter more than age "
                "alone — a heifer should also be near two-thirds of her mature "
                "weight.\n\n"
                "Where the estimate stops. Every cycle counted from an old "
                f"date adds error. Past {MAX_PROJECTED_CYCLES} cycles the "
                "arithmetic is meaningless, so those cows are listed with no "
                "date at all rather than a made-up one.\n\n"
                "Cows confirmed in calf are left out — mark a pregnancy in "
                "the Breeding tab and she drops off this list.\n\n"
                "This is arithmetic on the dates you entered, not a prediction "
                "model, and it knows nothing about condition, nutrition, "
                "disease or the weather. The judgement stays yours."))

        # ── How this is worked out ───────────
        with st.expander("How these ratings are worked out"):
            st.markdown(
                T("Fertility combines two things: the average gap between a "
                f"cow's calvings (target {IDEAL_CALVING_INTERVAL} days, a concern "
                f"beyond {POOR_CALVING_INTERVAL}), and how many of her services "
                "led to a calf or a confirmed pregnancy. Being in calf now adds "
                "a little.\n\n"
                "Production looks at how her calves actually do: the share "
                "that survive, and their daily weight gain measured against the "
                "herd average.\n\n"
                "Score is the average of the two, from 0 to 100 — Strong 75+, "
                "Steady 55+, Watch 35+, Poor below that.\n\n"
                "Evidence weighting. A rating built on one service carries "
                "far less weight than one built on three calvings and a dozen "
                "weighings, so thin records are pulled towards the middle until "
                "there is history behind them. A cow confirmed in calf for the "
                "first time will not outrank a proven producer.\n\n"
                "Based on tells you how much evidence sits behind a rating. "
                "A cow marked *limited* has very little history, so treat her "
                "score cautiously — record more calvings and weighings and it "
                "sharpens up.\n\n"
                "This is arithmetic on your own records, not a prediction model. "
                "It surfaces what your data already shows; the judgement stays "
                "yours."))

        # ── Export ───────────────────────────
        _exp_rows = [{
            SP["One"]: r["tag"], "Score": (f"{r['score']:.0f}"
                                       if r["score"] is not None else ""),
            "Rating": performance_band(r["score"]),
            "Fertility": (f"{r['fertility_score']:.0f}"
                          if r["fertility_score"] is not None else ""),
            "Production": (f"{r['production_score']:.0f}"
                           if r["production_score"] is not None else ""),
            "Calvings": r["calvings"], "Services": r["services"],
            "Calves": r["calves"], "Live calves": r["live_calves"],
            "Calving interval (days)": (f"{r['calving_interval']:.0f}"
                                        if r["calving_interval"] else ""),
            "Calf growth (kg/day)": (f"{r['calf_adg']:.3f}"
                                     if r["calf_adg"] else ""),
            "In calf": "Yes" if r["pregnant_now"] else "",
            "Evidence": r["confidence"],
        } for r in _an["rows"]]
        _dl(st,
            T("Download herd analysis (CSV)"),
            pd.DataFrame(_exp_rows).to_csv(index=False).encode("utf-8"),
            file_name=f"herd_analytics_{date.today().isoformat()}.csv",
            mime="text/csv", key="dl_analytics")


# ── BACKUP & RESTORE ──────────────────────────
with tab_backup:
    st.markdown('<div class="section">Backup &amp; restore</div>',
                unsafe_allow_html=True)

    # ── Status card ──────────────────────────
    _counts = table_row_counts()
    _total_rows = sum(n for _, _, n in _counts)
    _img_bytes = get_conn().execute(
        "SELECT COALESCE(SUM(LENGTH(image)),0) FROM cattle_images").fetchone()[0] or 0
    _last = get_meta("last_backup", "")
    _cls, _icon, _title, _sub = "bad", "!", "No backup taken yet", (
        "Your records exist only on this computer. Take a backup below.")
    if _last and "|" in _last:
        _when, _where = _last.split("|", 1)
        try:
            _n = (date.today() - datetime.fromisoformat(_when).date()).days
        except Exception:
            _n = 0
        _ago = ("today" if _n == 0 else "yesterday" if _n == 1 else f"{_n} days ago")
        _cls = "" if _n <= 7 else ("warn" if _n <= 30 else "bad")
        _icon = "✔" if _n <= 7 else "!"
        _title = f"Last backup {_ago}"
        _sub = f"{_when[:16].replace('T', ' ')} → {_html.escape(_where)}"
    st.markdown(
        f'<div class="bk-card {_cls}"><div class="bk-icon">{_icon}</div>'
        f'<div><div class="bk-title">{_title}</div>'
        f'<div class="bk-sub">{_sub}</div></div></div>', unsafe_allow_html=True)

    with st.expander(f"What gets backed up — {len(_counts)} tables · "
                     f"{_total_rows:,} records · photos "
                     f"{_img_bytes / 1024 / 1024:.1f} MB"):
        st.dataframe(
            pd.DataFrame([{"Data": lbl, "Table": nm, "Records": f"{n:,}"}
                          for nm, lbl, n in _counts]),
            use_container_width=True, hide_index=True)
        st.caption("Photos live inside the database file, not in the CSVs — so "
                   "keep the database file in your backup if you want them.")

    # ── 1. Download ──────────────────────────
    st.markdown('<div class="section">Download a backup</div>',
                unsafe_allow_html=True)
    _all_names = [t for t, _ in BACKUP_TABLES]
    _label_of = {t: lbl for t, lbl in BACKUP_TABLES}

    o1, o2 = st.columns([1.6, 1])
    _mode = o1.radio("Include", ["Everything (recommended)", "Choose tables"],
                     key="bk_mode", horizontal=True, format_func=T)
    _incl_db = o2.checkbox("Database file", value=True, key="bk_incl_db",
                           help="Needed to restore, and holds the photos.")
    _chosen = _all_names
    if _mode.startswith("Choose"):
        _chosen = st.multiselect(
            "Tables to include", _all_names, default=_all_names,
            format_func=lambda t: _label_of.get(t, t), key="bk_tables",
            label_visibility="collapsed")

    if not _chosen and not _incl_db:
        st.info("Select at least one table, or include the database file.")
    else:
        _zip = build_backup_zip(_chosen, include_db=_incl_db)
        _dbb = db_snapshot_bytes() if _incl_db else b""
        b1, b2, _bsp = st.columns([1.5, 1.5, 2])
        _dl(b1,
            f"Download ZIP · {len(_zip) / 1024 / 1024:.1f} MB", _zip,
            file_name=T(f"cattle-backup-{now_local().strftime('%Y%m%d-%H%M')}.zip"),
            mime="application/zip", key="bk_dl_zip")
        if _dbb:
            _dl(b2,
                f"Database only · {len(_dbb) / 1024 / 1024:.1f} MB", _dbb,
                file_name=T("cattle.db"), mime="application/octet-stream",
                key="bk_dl_db")
        st.markdown('<div class="bk-note">The ZIP holds one CSV per table plus '
                    'the database file and a README explaining how to restore.'
                    '</div>', unsafe_allow_html=True)

    # ── 2. Save to a drive or server folder ──
    st.markdown('<div class="section">Save to a drive or server folder</div>',
                unsafe_allow_html=True)
    _drives = detect_drives()
    s1, s2 = st.columns([1, 1.6])
    if _drives:
        _pick_drive = s1.selectbox("Detected drives",
                                   ["(type a path)"] + _drives, key="bk_drive", format_func=T)
    else:
        _pick_drive = "(type a path)"
        s1.selectbox("Detected drives", ["(none found)"], key="bk_drive_none",
                     disabled=True, format_func=T)
    _dest = s2.text_input(
        "Destination folder",
        value="" if _pick_drive.startswith("(") else _pick_drive,
        key="bk_dest",
        placeholder=T("D:\\backups   ·   E:\\   ·   \\\\server\\cattle"))
    if st.button("Back up to this location", type="primary", key="bk_write"):
        _ok, _msg, _path = backup_to_folder(_dest, _chosen, include_db=_incl_db)
        if _ok:
            st.success(_msg)
            st.rerun()
        else:
            st.error(_msg)
    st.markdown('<div class="bk-note">A network or server share works here too — '
                'mount it on this computer first, then give its folder path. Every '
                'backup goes into its own dated sub-folder, so nothing is '
                'overwritten.</div>', unsafe_allow_html=True)

    # ── 3. Restore ───────────────────────────
    with st.expander("Restore from a backup"):
        st.caption(T("Upload a backup zip, or the cattle.db file from inside "
                   "one. This replaces everything currently in the app — your "
                   "existing data is kept alongside it as a '.replaced-…' file "
                   "first."))
        _up = st.file_uploader(T("Backup zip or database file (cattle.db)"),
                               type=["zip", "db", "sqlite", "sqlite3"],
                               accept_multiple_files=False, key="bk_upload")
        r1, r2 = st.columns([2, 1])
        _confirm = r1.checkbox("I understand this replaces all current data",
                               key="bk_confirm")
        if r2.button("Restore now", key="bk_restore", use_container_width=True):
            if not _up:
                st.warning("Choose a backup file first.")
            elif not _confirm:
                st.warning("Tick the confirmation box before restoring.")
            else:
                _ok, _msg = restore_from_bytes(_up.getvalue())
                if _ok:
                    st.success(_msg)
                    st.rerun()
                else:
                    st.error(_msg)

    st.markdown(T('<div class="bk-note">Tip: back up at the end of any day you '
                'recorded sales, calvings or new cattle — and keep at least one '
                'copy off this computer.</div>'), unsafe_allow_html=True)


# ══════════════════════════════════════════════
# HerdIQ ASSISTANT — floating launcher, bottom-right
# ══════════════════════════════════════════════
_ASSISTANT_NAME = "HerdIQ Assistant"
_EXAMPLES = [
    "How many cattle do I have?",
    "How much did I spend on feed this month?",
    "What did I sell this year?",
    "What activities were done yesterday?",
    "What is my net position this year?",
    "What do I need to do?",
]


def render_herdiq():
    """The assistant panel: header, conversation, suggestions and input."""
    chat = st.session_state.setdefault("qa_chat", [])

    st.markdown(f"""
<div class="bot-shell">
  <div class="bot-bar">
    <img src="{SP["avatar"]}" alt="HerdIQ" class="bot-face-img"/>
    <div>
      <div class="bot-id">{_ASSISTANT_NAME}</div>
      <div class="bot-sub"><span class="bot-dot"></span>
        Online · answers from your own farm records</div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)

    if chat:
        rows = []
        for role, text in chat[-12:]:
            if role == "user":
                rows.append(
                    '<div class="msg-row me"><div class="msg-av me">🧑‍🌾</div>'
                    f'<div class="bubble me">{_html.escape(text)}</div></div>')
            else:
                rows.append(
                    f'<div class="msg-row"><img src="{SP["avatar"]}" '
                    f'alt="HerdIQ" class="msg-av-img"/>'
                    f'<div class="bubble">{_md_to_bubble_html(text)}</div></div>')
        st.markdown(f'<div class="bot-shell"><div class="bot-body">{"".join(rows)}'
                    '</div></div>', unsafe_allow_html=True)
    else:
        st.markdown(
            T('<div class="bot-shell"><div class="bot-body"><div class="msg-row">'
            f'<img src="{SP["avatar"]}" alt="HerdIQ" class="msg-av-img"/>'
            '<div class="bubble">'
            "<div>Hello. I read your herd, calendar, activities, billing and "
            "expenses. Ask me anything — for example <b>how many cattle do I "
            "have</b>, or type a <b>tag number</b> or a <b>buyer or seller's "
            "name</b> to see everything they're recorded for.</div>"
            '</div></div></div></div>'), unsafe_allow_html=True)

    with st.form("qa_form", clear_on_submit=True):
        ic, bc = st.columns([5, 1])
        typed = ic.text_input("Ask", label_visibility="collapsed",
                              placeholder=f"Message {_ASSISTANT_NAME}…")
        sent = bc.form_submit_button("➤", type="primary",
                                     use_container_width=True)

    with st.expander("💡 Suggestions"):
        for i, ex in enumerate(_EXAMPLES):
            if st.button(ex, key=f"qa_ex_{i}", use_container_width=True):
                st.session_state["qa_pending"] = ex
                st.rerun()
        people = qa_people_list(limit=6)
        if people:
            st.caption("People on record — tap for their full history:")
            pc = st.columns(2)
            for i, p in enumerate(people):
                if pc[i % 2].button(p, key=f"qa_person_{i}",
                                    use_container_width=True):
                    st.session_state["qa_pending"] = p
                    st.rerun()

    question = None
    if sent and typed.strip():
        question = typed.strip()
    elif st.session_state.get("qa_pending"):
        question = st.session_state.pop("qa_pending")

    if question:
        chat.append(("user", question))
        with st.spinner("Reading your records…"):
            try:
                reply = T(answer_question(question))
            except Exception:
                reply = ("I couldn't work that one out. Try rephrasing, or ask "
                         "about cattle numbers, sales, purchases, expenses or "
                         "activities.")
        chat.append(("bot", reply))
        st.session_state["qa_chat"] = chat[-40:]
        st.rerun()

    if chat:
        _last_q = next((t for r, t in reversed(chat) if r == "user"), "Question")
        _last_a = next((t for r, t in reversed(chat) if r == "bot"), "")
        _pdf = herdiq_answer_pdf(_last_q, _last_a)
        if _pdf:
            _dl(st, "Download answer (PDF)", _pdf,
                               file_name=f"herdiq_answer_{date.today().isoformat()}.pdf",
                               mime="application/pdf", key="qa_pdf_last",
                               use_container_width=True)
            if len([1 for r, _ in chat if r == "bot"]) > 1:
                _pdf_all = herdiq_answer_pdf(None, None, conversation=chat)
                if _pdf_all:
                    _dl(st,
                        "Download conversation (PDF)", _pdf_all,
                        file_name=f"herdiq_session_{date.today().isoformat()}.pdf",
                        mime="application/pdf", key="qa_pdf_all",
                        use_container_width=True)
        else:
            st.caption("Install reportlab to enable PDF downloads.")

    if chat and st.button("🧹 Clear conversation", key="qa_clear",
                          use_container_width=True):
        st.session_state["qa_chat"] = []
        st.rerun()


# The launcher sits at the far right; CSS floats it over the bottom-right corner.
_lc1, _lc2 = st.columns([9, 1])
if hasattr(st, "popover"):
    with _lc2.popover("HerdIQ", help="Ask HerdIQ about your farm records"):
        render_herdiq()
else:
    # Older Streamlit: fall back to a toggle button and an inline panel.
    if _lc2.button(SP["emoji"], key="herdiq_toggle",
                   help="Ask HerdIQ about your farm records"):
        st.session_state["herdiq_open"] = not st.session_state.get("herdiq_open",
                                                                   False)
    if st.session_state.get("herdiq_open"):
        st.divider()
        render_herdiq()

'''

# Exactly what the app imports. A hosting platform (e.g. Streamlit Cloud)
# reads this file to know what to install.
#
# Every line has an upper bound, and numpy is named although nothing imports
# it directly — pandas does. An unbounded "pandas>=1.5" quietly pulled in a
# numpy the Windows build could not package, and the fault only appeared once
# the program was installed on someone's computer. A major release should
# fail the build here, where it can be seen, rather than on a farm.
_REQUIREMENTS = '''streamlit>=1.31,<2
pandas>=1.5,<4
numpy>=1.23,<3
altair>=5.0,<6
reportlab>=3.6,<5
pillow>=9.0,<14
openpyxl>=3.1.2,<4
'''

with open("cattlemanagementapp.py", "w", encoding="utf-8") as f:
    f.write(app_code)
print("cattlemanagementapp.py created successfully")

with open("requirements.txt", "w", encoding="utf-8") as f:
    f.write(_REQUIREMENTS)
print("requirements.txt written")

# Streamlit paints its own controls — checkboxes, radios, sliders, the loading
# bar — with whatever it has been told is the accent colour, and its default is
# red. This puts the teal of the LIVESTOCK wordmark in its place, so every
# section reads as one system.
_THEME = '''[theme]
base = "light"
primaryColor = "#006868"
backgroundColor = "#FFFFFF"
secondaryBackgroundColor = "#F0FAFA"
textColor = "#06343A"
font = "sans serif"

[client]
toolbarMode = "minimal"
'''

os.makedirs(".streamlit", exist_ok=True)
with open(os.path.join(".streamlit", "config.toml"), "w", encoding="utf-8") as f:
    f.write(_THEME)
print(".streamlit/config.toml written")
