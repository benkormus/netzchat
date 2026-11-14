"""
Kommandozeilen-Client für den Messenger (app.py).

Installation:   pip install requests python-escpos pyusb
Start:          python client.py                       (fragt Benutzername/Passwort ab)
                python client.py --url http://13.49.225.2:5874 --user benkormos
                (Passwort optional über die Umgebungsvariable MESSENGER_PASSWORD)

Bondrucker: Beim Start ist Drucken AUS. Erst nach  !print  werden eingehende Nachrichten
anderer auf dem Epson TM-T88V (USB 04b8:0e02) ausgedruckt (nochmal !print = wieder aus).
Andere Geräte:  --usb VVVV:PPPP      Druckfunktion ganz abschalten:  --no-print
Windows: dem Drucker per Zadig den WinUSB/libusb-Treiber geben (sonst findet pyusb ihn nicht).
Linux: Zugriff per udev-Regel erlauben oder mit sudo starten.

Befehle:
    #kanal              Kanal im aktuellen Server wechseln  ("#" allein = Kanalliste)
    /server [nr|name]   Server wechseln (ohne Argument: Liste)
    /dms [username]     Privatchat öffnen (ohne Argument: Nutzerliste)
    /users              alle Nutzer anzeigen
    /members            Mitglieder des aktuellen Servers
    /freund <name>      Freundschaftsanfrage senden
    /annehmen <name>    Freundschaftsanfrage annehmen
    /join <code|link>   Server per Einladungscode beitreten
    /laden              Verlauf neu laden
    /help               Hilfe
    /quit               Beenden
    //text              sendet eine Nachricht, die mit "/" beginnt
    !print [an|aus]     Drucken ein-/ausschalten (beim Start aus)
    !testprint [opt]    Testdruck, z. B.  !testprint stärke=2 größe=2   (ohne Optionen: Vergleichsbon)
    !printset [opt]     Druck-Stil für Nachrichten dauerhaft setzen (ohne Optionen: anzeigen)

Alles andere wird als Nachricht in den aktuellen Kanal bzw. Privatchat gesendet.
Beim Öffnen eines Chats werden die letzten 20 Nachrichten angezeigt.
Bilder/Videos/Dateien werden nicht angezeigt, nur als [Bild: name] usw.
"""
import argparse
import base64
import getpass
import json
import os
import queue
import re
import sys
import threading
import zlib
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Bitte zuerst installieren:  pip install requests")

POLL_SECONDS = 2.0
HISTORY_LIMIT = 20      # so viele alte Nachrichten werden beim Öffnen eines Chats angezeigt

# Farben (ANSI)
RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
CYAN, GREEN, YELLOW, RED, MAGENTA = "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m"


class ApiError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


def fmt_size(n):
    if not n:
        return ""
    n = float(n)
    for unit in ("Bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:g} {unit}" if n == int(n) else f"{n:.1f} {unit}"
        n /= 1024


def parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
    except (TypeError, ValueError):
        return None


def pick(items, key, query):
    """Treffer per exaktem Namen, Präfix oder Teilstring (Groß/Klein egal)."""
    q = query.strip().lower()
    for test in (lambda v: v == q, lambda v: v.startswith(q), lambda v: q in v):
        found = [i for i in items if test(str(key(i)).lower())]
        if found:
            return found
    return []


def printable(text):
    """Emojis und Steuerzeichen entfernen, die der Bondrucker nicht drucken kann."""
    text = re.sub(r"[\U00010000-\U0010FFFF\u200d\ufe0f\u2600-\u27bf]", "", text or "")
    return "".join(ch for ch in text if ch == "\n" or ch.isprintable())


ALIGN = {"links": 0, "mitte": 1, "rechts": 2}
STYLE_DEFAULT = {"staerke": 0, "groesse": 1, "font": "a", "align": "links", "dichte": None}
STYLE_HELP = """Optionen (als name=wert, mehrere mit Leerzeichen):
  stärke=0-2          0 normal, 1 fett, 2 fett + Doppelanschlag (bei Thermodruckern oft wie fett)
  größe=1-8           Zeichengröße (Breite und Höhe)
  font=a|b            Schriftart (b ist kleiner)
  ausrichtung=links|mitte|rechts
  dichte=0-8          Druckdichte, 4 = normal (experimentell, nicht jeder Drucker unterstützt das)
Beispiel:  !testprint stärke=2 größe=2"""


def _int(val, lo, hi):
    n = int(val)
    if not lo <= n <= hi:
        raise ValueError
    return n


def parse_style(args, base):
    """'stärke=2 größe=2 ...' -> (neuer Stil, Fehlermeldung)."""
    st = dict(base)
    for tok in args.split():
        key, sep, val = tok.partition("=")
        if not sep:                                   # nur eine Zahl = Stärke
            key, val = "staerke", tok
        key = key.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
        try:
            if key in ("staerke", "stark"):
                st["staerke"] = _int(val, 0, 2)
            elif key in ("groesse", "gross", "size"):
                st["groesse"] = _int(val, 1, 8)
            elif key == "dichte":
                st["dichte"] = _int(val, 0, 8)
            elif key == "font" and val.lower() in ("a", "b"):
                st["font"] = val.lower()
            elif key in ("ausrichtung", "align") and val.lower() in ALIGN:
                st["align"] = val.lower()
            else:
                return None, f"Unbekannte Option oder Wert: {tok}"
        except ValueError:
            return None, f"Ungültiger Wert: {tok}"
    return st, None


def describe_style(st):
    txt = f"stärke={st['staerke']} größe={st['groesse']} font={st['font']} ausrichtung={st['align']}"
    return txt + (f" dichte={st['dichte']}" if st["dichte"] is not None else "")


class ReceiptPrinter:
    """Druckt per USB auf einem ESC/POS-Bondrucker (Epson TM-T88V). Stil wird mit rohen ESC/POS-Befehlen gesetzt."""

    def __init__(self, vendor, product, on_error=None):
        self.vendor, self.product, self.on_error = vendor, product, on_error
        self.printer = None
        self.last_error = None
        self.style = dict(STYLE_DEFAULT)
        self.jobs = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def connect(self):
        if self.printer is not None:
            return
        try:
            from escpos.printer import Usb
        except ImportError:
            raise RuntimeError("python-escpos fehlt:  pip install python-escpos pyusb")
        self.printer = Usb(self.vendor, self.product, profile="TM-T88V")

    def close(self):
        try:
            self.printer.close()
        except Exception:
            pass
        self.printer = None

    def test(self):
        try:
            self.connect()
            return True, "Bondrucker verbunden."
        except Exception as e:
            self.close()
            return False, f"Bondrucker nicht erreichbar ({e or e.__class__.__name__}) – es wird bei jeder Nachricht erneut versucht."

    # --- Aufträge (laufen im Hintergrund-Thread)
    def submit(self, where, name, when, text):
        self.jobs.put(lambda p, st=dict(self.style): self._print_message(p, st, (where, name, when, text)))

    def test_print(self, st=None):
        self.jobs.put(lambda p: self._print_test(p, st))

    @staticmethod
    def _style(p, bold=False, strike=False, size=1, font="a", align="links"):
        p._raw(b"\x1bE" + bytes([bool(bold)]))                      # fett
        p._raw(b"\x1bG" + bytes([bool(strike)]))                    # Doppelanschlag
        p._raw(b"\x1d!" + bytes([((size - 1) << 4) | (size - 1)]))  # Zeichengröße
        p._raw(b"\x1bM" + bytes([font == "b"]))                     # Schriftart
        p._raw(b"\x1ba" + bytes([ALIGN[align]]))                    # Ausrichtung

    def _body(self, p, st):
        self._style(p, bold=st["staerke"] >= 1, strike=st["staerke"] >= 2, size=st["groesse"],
                    font=st["font"], align=st["align"])

    @staticmethod
    def _density(p, st):
        if st["dichte"] is not None:
            p._raw(b"\x1d|" + bytes([st["dichte"]]))

    def _print_message(self, p, st, job):
        where, name, when, text = (printable(x) for x in job)
        self._density(p, st)
        self._style(p, bold=True, align="mitte")
        p.text(where + "\n")
        self._style(p)
        p.text(when + "\n")
        self._style(p, bold=True)
        p.text(name + ":\n")
        self._body(p, st)
        p.text(text + "\n\n")
        self._style(p)
        p.cut()

    def _print_test(self, p, st):
        sample = "Beispieltext ÄÖÜ äöü ß 0123456789"
        cur = self.style
        self._density(p, st or cur)
        self._style(p, bold=True, size=2, align="mitte")
        p.text("TESTDRUCK\n")
        self._style(p, align="mitte")
        p.text(datetime.now().strftime("%d.%m.%Y %H:%M") + "\n\n")
        if st is None:                                       # Vergleichsbon mit allen Stufen
            self._style(p, bold=True); p.text("Stärke\n")
            for n in (0, 1, 2):
                self._style(p, bold=n >= 1, strike=n >= 2); p.text(f"{n}: {sample}\n")
            self._style(p, bold=True); p.text("\nGröße\n")
            for n in (1, 2, 3):
                self._style(p, size=n); p.text(f"{n}: Größe {n}\n")
            self._style(p, bold=True); p.text("\nSchrift\n")
            for f in ("a", "b"):
                self._style(p, font=f); p.text(f"{f}: {sample}\n")
            self._style(p, bold=True); p.text("\nAusrichtung\n")
            for a in ALIGN:
                self._style(p, align=a); p.text(f"{a}\n")
            self._style(p, bold=True)
            p.text("\nAktueller Stil:\n")
            self._style(p)
            p.text(describe_style(cur) + "\n")
        else:
            self._style(p, align="mitte")
            p.text(describe_style(st) + "\n\n")
            self._body(p, st)
            p.text(sample + "\n" + sample + "\n")
        self._style(p)
        p.text("\n")
        p.cut()

    def _loop(self):
        while True:
            job = self.jobs.get()
            for attempt in (0, 1):                   # bei Fehler einmal neu verbinden
                try:
                    self.connect()
                    job(self.printer)
                    self.last_error = None
                    break
                except Exception as e:
                    self.close()
                    if attempt:
                        msg = str(e) or e.__class__.__name__
                        if msg != self.last_error and self.on_error:
                            self.on_error(f"Drucken fehlgeschlagen: {msg}")
                        self.last_error = msg


def media_tag(m):
    if not m.get("media"):
        return ""
    kind = {"image": "Bild", "video": "Video", "audio": "Audio"}.get(m.get("media_type"), "Datei")
    size = fmt_size(m.get("media_size"))
    return f"[{kind}: {m.get('media_name') or 'Anhang'}{', ' + size if size else ''}]"


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.http = requests.Session()
        self.api_lock = threading.Lock()
        self.print_lock = threading.Lock()
        self.vlock = threading.RLock()
        self.stop = threading.Event()
        self.username = ""
        self.servers = []
        self.srv = None                 # Daten des aktuellen Servers
        self.last_channel = {}          # server_id -> zuletzt genutzter Kanal
        self.view = None                # ("channel", id) | ("dm", id)
        self.label = ""
        self.last_id = 0
        self.last_date = None
        self.gen = 0
        self.act_last = 0
        self.loaded = False             # erste Ladung der Ansicht abgeschlossen?
        self.print_on = False           # Drucken erst nach !print
        self.printer = None

    # ------------------------------------------------------------ Ausgabe
    def prompt(self):
        mark = f"{YELLOW}[Druck]{RESET}" if self.print_on else ""
        return f"{GREEN}[{self.label}]{RESET}{mark} > " if self.label else "> "

    def say(self, text=""):
        with self.print_lock:
            print(text)

    def notify(self, text):
        """Ausgabe aus dem Hintergrund-Thread: Eingabezeile löschen, Text, Prompt neu zeichnen."""
        with self.print_lock:
            sys.stdout.write("\r\033[K" + text + "\n" + self.prompt())
            sys.stdout.flush()

    # ---------------------------------------------------------------- HTTP
    def csrf(self):
        """CSRF-Token steht im (signierten, aber lesbaren) Flask-Session-Cookie."""
        raw = self.http.cookies.get("session")
        if not raw:
            return ""
        try:
            compressed = raw.startswith(".")
            payload = raw.lstrip(".").split(".")[0]
            data = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            if compressed:
                data = zlib.decompress(data)
            return json.loads(data).get("csrf", "")
        except Exception:
            return ""

    def api(self, method, path, **kw):
        headers = kw.pop("headers", {})
        with self.api_lock:
            if method != "GET":
                headers["X-CSRF-Token"] = self.csrf()
            try:
                r = self.http.request(method, self.base + path, headers=headers, timeout=10, **kw)
            except requests.RequestException as e:
                raise ApiError(f"Verbindungsfehler: {e.__class__.__name__}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            raise ApiError((data or {}).get("error") or f"HTTP {r.status_code}", r.status_code)
        return data

    def login(self, username, password):
        with self.api_lock:
            self.http.get(self.base + "/login", timeout=10)      # setzt Session + CSRF
        token = self.csrf()
        with self.api_lock:
            r = self.http.post(self.base + "/login", timeout=10, allow_redirects=False,
                               data={"username": username, "password": password, "csrf_token": token})
        if r.status_code not in (301, 302, 303) or "chat" not in r.headers.get("Location", ""):
            raise ApiError("Login fehlgeschlagen (Name/Passwort falsch oder zu viele Versuche).")
        self.username = username
        self.servers = self.api("GET", "/api/servers")           # erzeugt auch das neue CSRF-Token

    # ------------------------------------------------------- Nachrichten
    def fmt_msg(self, m):
        lines = []
        dt = parse_ts(m.get("created_at"))
        if dt:
            if dt.date() != self.last_date:
                self.last_date = dt.date()
                lines.append(f"{DIM}─── {dt.strftime('%d.%m.%Y')} ───{RESET}")
            ts = dt.strftime("%H:%M")
        else:
            ts = "--:--"
        own = m["username"].lower() == self.username.lower()
        name = m["username"] + (" ✓" if m.get("is_verified") else "") + (" [ADMIN]" if m.get("is_admin") else "")
        head = f"{DIM}{ts}{RESET} {(GREEN if own else CYAN) + BOLD}{name}{RESET}: "
        indent = " " * 7
        body_lines = (m.get("body") or "").splitlines() or []
        if m.get("media"):
            kind = {"image": "Bild", "video": "Video", "audio": "Audio"}.get(m.get("media_type"), "Datei")
            size = fmt_size(m.get("media_size"))
            body_lines.append(f"{MAGENTA}[{kind}: {m.get('media_name') or 'Anhang'}{', ' + size if size else ''}]{RESET}")
        if not body_lines:
            body_lines = [""]
        lines.append(head + body_lines[0])
        lines += [indent + b for b in body_lines[1:]]
        if m.get("reactions"):
            lines.append(indent + DIM + "↳ " + "  ".join(f"{r['emoji']} {r['count']}" for r in m["reactions"]) + RESET)
        return lines

    def fetch(self, view):
        kind, ident = view
        return self.api("GET", f"/api/channels/{ident}" if kind == "channel" else f"/api/dm/{ident}")

    def refresh(self, background):
        """Neue Nachrichten der aktuellen Ansicht holen und ausgeben."""
        with self.vlock:
            view, gen = self.view, self.gen
        if view is None:
            return
        msgs = self.fetch(view)
        with self.vlock:
            if gen != self.gen:
                return
            first = not self.loaded
            self.loaded = True
            if first:                       # erstes Laden: nur die letzten Nachrichten zeigen
                msgs = msgs[-HISTORY_LIMIT:]
            lines = []
            for m in msgs:
                if m["id"] > self.last_id:
                    lines += self.fmt_msg(m)
                    self.last_id = m["id"]
                    if self.print_on and self.printer and not first and m["username"].lower() != self.username.lower():
                        dt = parse_ts(m.get("created_at"))
                        self.printer.submit(self.label, m["username"],
                                            dt.strftime("%d.%m.%Y %H:%M") if dt else "",
                                            "\n".join(x for x in ((m.get("body") or ""), media_tag(m)) if x))
            if lines:
                (self.notify if background else self.say)("\n".join(lines))

    def set_view(self, kind, ident, label):
        with self.vlock:
            self.view, self.label = (kind, ident), label
            self.last_id, self.last_date = 0, None
            self.loaded = False
            self.gen += 1

    def open_view(self, kind, ident, label, title):
        self.set_view(kind, ident, label)
        self.say(f"\n{BOLD}═══ {title} ═══{RESET}")
        self.refresh(False)
        if self.last_id == 0:
            self.say(f"{DIM}(noch keine Nachrichten){RESET}")

    # ------------------------------------------------- Server / Kanäle
    def server_name(self, sid):
        for s in self.servers:
            if s["id"] == sid:
                return s["name"]
        return "Server"

    def load_server(self, sid, channel_id=None):
        self.srv = self.api("GET", f"/api/servers/{sid}")
        channels = self.srv["channels"]
        if not channels:
            self.say("Dieser Server hat keine Kanäle.")
            return
        ch = next((c for c in channels if c["id"] == (channel_id or self.last_channel.get(sid))), channels[0])
        self.enter_channel(ch)

    def enter_channel(self, ch):
        sid = self.srv["server"]["id"]
        self.last_channel[sid] = ch["id"]
        name = self.srv["server"]["name"]
        self.open_view("channel", ch["id"], f"{name} #{ch['name']}", f"{name} · #{ch['name']}")

    def cmd_server(self, arg):
        self.servers = self.api("GET", "/api/servers")
        if not arg:
            cur = self.srv["server"]["id"] if self.srv else None
            self.say(f"{BOLD}Deine Server:{RESET}")
            for i, s in enumerate(self.servers, 1):
                mark = " ←" if s["id"] == cur else ""
                self.say(f"  {i}. {s['name']}{mark}")
            self.say(f"{DIM}Wechseln mit /server <nr|name>{RESET}")
            return
        if arg.isdigit() and 1 <= int(arg) <= len(self.servers):
            target = self.servers[int(arg) - 1]
        else:
            found = pick(self.servers, lambda s: s["name"], arg)
            if not found:
                return self.say(f"{RED}Server nicht gefunden.{RESET} (/server zeigt die Liste)")
            if len(found) > 1:
                return self.say("Mehrdeutig: " + ", ".join(s["name"] for s in found))
            target = found[0]
        self.load_server(target["id"])

    def cmd_channel(self, name):
        if not self.srv:
            return self.say("Kein Server ausgewählt.")
        if not name:
            return self.list_channels()
        for attempt in (0, 1):
            found = pick(self.srv["channels"], lambda c: c["name"], name.lstrip("#"))
            if found or attempt:
                break
            self.srv = self.api("GET", f"/api/servers/{self.srv['server']['id']}")   # evtl. neuer Kanal
        if not found:
            return self.say(f"{RED}Kanal nicht gefunden.{RESET} (# zeigt die Liste)")
        if len(found) > 1:
            return self.say("Mehrdeutig: " + ", ".join("#" + c["name"] for c in found))
        self.enter_channel(found[0])

    def list_channels(self):
        self.say(f"{BOLD}Kanäle in {self.srv['server']['name']}:{RESET}")
        cats = {c["id"]: c["name"] for c in self.srv["categories"]}
        groups = [(cats[cid], [c for c in self.srv["channels"] if c["category_id"] == cid]) for cid in cats]
        groups.append(("ohne Kategorie", [c for c in self.srv["channels"] if c["category_id"] not in cats]))
        for title, chans in groups:
            if chans:
                self.say(f"  {DIM}{title}{RESET}")
                for c in chans:
                    self.say(f"    #{c['name']}")

    # --------------------------------------------------------- Privat
    def users(self):
        return self.api("GET", "/api/users")

    def cmd_dms(self, arg):
        users = self.users()
        if not arg:
            return self.print_users(users)
        found = pick(users, lambda u: u["username"], arg.lstrip("@"))
        if not found:
            return self.say(f"{RED}Nutzer nicht gefunden.{RESET}")
        if len(found) > 1:
            return self.say("Mehrdeutig: " + ", ".join(u["username"] for u in found))
        u = found[0]
        self.open_view("dm", u["id"], f"DM {u['username']}", f"Privatchat mit {u['username']}")
        if not u["can_dm"]:
            self.say(f"{YELLOW}Hinweis: {u['username']} akzeptiert private Nachrichten nur von Freunden. "
                     f"Sende zuerst /freund {u['username']}.{RESET}")

    def print_users(self, users):
        rel = {"friend": "Freund", "pending_out": "Anfrage gesendet", "pending_in": "Anfrage erhalten", "none": ""}
        self.say(f"{BOLD}Nutzer:{RESET}")
        for u in users:
            dot = f"{GREEN}●{RESET}" if u["online"] else f"{DIM}○{RESET}"
            extra = rel.get(u["relation"], "")
            if not u["can_dm"] and u["relation"] != "friend":
                extra = (extra + " " if extra else "") + "(nur Freunde)"
            self.say(f"  {dot} {u['username']}{' ' + DIM + extra + RESET if extra else ''}")
        self.say(f"{DIM}Chat öffnen mit /dms <username>{RESET}")

    def user_by_name(self, name):
        found = pick(self.users(), lambda u: u["username"], name.lstrip("@"))
        if len(found) != 1:
            self.say(f"{RED}Nutzer nicht gefunden oder mehrdeutig.{RESET}")
            return None
        return found[0]

    def cmd_friend(self, arg, accept=False):
        if not arg:
            return self.say("Benutzername angeben.")
        u = self.user_by_name(arg)
        if u:
            data = self.api("POST", f"/api/friends/{'accept' if accept else 'request'}/{u['id']}")
            self.say(f"Status mit {u['username']}: {data.get('relation')}")

    def cmd_members(self):
        if not self.srv:
            return
        self.srv = self.api("GET", f"/api/servers/{self.srv['server']['id']}")
        mem = sorted(self.srv["members"], key=lambda m: (not m["online"], m["username"].lower()))
        self.say(f"{BOLD}Mitglieder von {self.srv['server']['name']} ({len(mem)}):{RESET}")
        for m in mem:
            dot = f"{GREEN}●{RESET}" if m["online"] else f"{DIM}○{RESET}"
            tag = {"owner": " (Besitzer)", "admin": " (Admin)"}.get(m["role"], "")
            self.say(f"  {dot} {m['username']}{tag}")

    def cmd_join(self, code):
        if not code:
            return self.say("Einladungscode oder -link angeben.")
        data = self.api("POST", "/api/servers/join", json={"code": code})
        self.servers = self.api("GET", "/api/servers")
        self.say(f"Beigetreten: {data['server']['name']}")
        self.load_server(data["server"]["id"])

    # ---------------------------------------------------------- Senden
    def send(self, text):
        if not self.view:
            return self.say("Kein Chat ausgewählt.")
        kind, ident = self.view
        self.api("POST", f"/api/channels/{ident}" if kind == "channel" else f"/api/dm/{ident}", json={"body": text})
        self.refresh(False)

    # ---------------------------------------------------- Hintergrund
    def poll_activity(self):
        data = self.api("GET", "/api/activity", params={"since": self.act_last})
        self.act_last = data["last"]
        with self.vlock:
            view = self.view
        for it in data["items"]:
            if view and ((view[0] == "channel" and it["channel_id"] == view[1])
                         or (view[0] == "dm" and it["channel_id"] is None and it["sender_id"] == view[1])):
                continue                                   # steht schon in der aktuellen Ansicht
            where = (f"#{it['channel_name']} ({self.server_name(it['server_id'])})"
                     if it["channel_id"] else "DM")
            ping = " @DU" if it["mention"] else ""
            self.notify(f"{YELLOW}[neu{ping}] {where} – {it['username']}: {it['preview']}{RESET}")
            if self.print_on and self.printer:
                self.printer.submit(where, it["username"], datetime.now().strftime("%d.%m.%Y %H:%M"), it["preview"])

    def poll_loop(self):
        warned = False
        while not self.stop.wait(POLL_SECONDS):
            try:
                self.refresh(True)
                self.poll_activity()
                warned = False
            except ApiError as e:
                if e.status == 401:
                    self.notify(f"{RED}Sitzung abgelaufen – bitte Client neu starten.{RESET}")
                    return
                if not warned:
                    self.notify(f"{RED}[{e}]{RESET}")
                    warned = True

    # ------------------------------------------------------ Eingabe
    HELP = __doc__.split("Befehle:")[1].split("Alles andere")[0].rstrip()

    def cmd_printer(self, cmd, arg):
        pr = self.printer
        if pr is None:
            return self.say("Die Druckfunktion ist per --no-print abgeschaltet.")
        if arg.lower() in ("hilfe", "help", "?") and cmd != "print":
            return self.say(STYLE_HELP)
        if cmd == "print":
            a = arg.lower()
            if a in ("an", "on", "ein", "1"):
                new = True
            elif a in ("aus", "off", "0"):
                new = False
            elif not a:
                new = not self.print_on
            else:
                return self.say("Benutzung: !print [an|aus]")
            if new:
                ok, msg = pr.test()
                self.say((GREEN if ok else YELLOW) + msg + RESET)
            self.print_on = new
            return self.say(f"Drucken ist jetzt {BOLD}{'AN' if new else 'AUS'}{RESET}.")
        if cmd == "printset":
            if not arg:
                self.say(f"Aktueller Druck-Stil: {describe_style(pr.style)}")
                return self.say(STYLE_HELP)
            st, e = parse_style(arg, pr.style)
            if e:
                return self.say(f"{RED}{e}{RESET}  (!printset hilfe)")
            pr.style = st
            return self.say(f"Druck-Stil gesetzt: {describe_style(st)}")
        # testprint
        if not arg:
            pr.test_print(None)
        else:
            st, e = parse_style(arg, pr.style)
            if e:
                return self.say(f"{RED}{e}{RESET}  (!testprint hilfe)")
            pr.test_print(st)
        self.say("Testdruck gesendet.")

    def handle(self, line):
        if line.startswith("!"):
            cmd, _, arg = line[1:].partition(" ")
            if cmd.lower() in ("print", "testprint", "printset"):
                return self.cmd_printer(cmd.lower(), arg.strip())
        if line.startswith("//"):
            return self.send(line[1:])
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            cmd, arg = cmd.lower(), arg.strip()
            if cmd in ("quit", "exit", "q"):
                raise EOFError
            if cmd in ("server", "s"):
                return self.cmd_server(arg)
            if cmd in ("dms", "dm", "d"):
                return self.cmd_dms(arg)
            if cmd == "users":
                return self.print_users(self.users())
            if cmd == "members":
                return self.cmd_members()
            if cmd in ("freund", "friend"):
                return self.cmd_friend(arg)
            if cmd in ("annehmen", "accept"):
                return self.cmd_friend(arg, accept=True)
            if cmd == "join":
                return self.cmd_join(arg)
            if cmd in ("laden", "reload"):
                if self.view:
                    kind, ident = self.view
                    self.open_view(kind, ident, self.label, self.label)
                return
            if cmd in ("help", "hilfe", "?"):
                return self.say("Befehle:" + self.HELP)
            return self.say(f"Unbekannter Befehl /{cmd}. /help zeigt alle Befehle.")
        if line.startswith("#") and " " not in line:
            return self.cmd_channel(line[1:])
        self.send(line)

    def run(self):
        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.say(f"{DIM}/help für alle Befehle{RESET}")
        while True:
            try:
                line = input(self.prompt()).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            try:
                self.handle(line)
            except EOFError:
                break
            except ApiError as e:
                self.say(f"{RED}Fehler: {e}{RESET}")
        self.stop.set()
        print("\nTschüss!")


def main():
    ap = argparse.ArgumentParser(description="Messenger-Client für die Kommandozeile")
    ap.add_argument("--url", default=os.environ.get("MESSENGER_URL", "http://13.49.225.2:5874"))
    ap.add_argument("--user")
    ap.add_argument("--usb", default="04b8:0e02", help="USB-ID des Bondruckers (Standard: Epson TM-T88V)")
    ap.add_argument("--no-print", action="store_true", help="Bondrucker deaktivieren")
    args = ap.parse_args()

    os.system("")   # aktiviert ANSI-Farben in der Windows-Konsole
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    user = args.user or input("Benutzername: ").strip()
    password = os.environ.get("MESSENGER_PASSWORD") or getpass.getpass("Passwort: ")
    c = Client(args.url)
    try:
        c.login(user, password)
        c.act_last = c.api("GET", "/api/activity")["last"]
        c.load_server(c.servers[0]["id"])       # Hauptserver, erster Kanal
    except ApiError as e:
        sys.exit(f"Fehler: {e}")
    if not args.no_print:
        try:
            vid, pid = (int(x, 16) for x in args.usb.split(":"))
        except ValueError:
            sys.exit("--usb muss das Format VVVV:PPPP haben, z. B. 04b8:0e02")
        c.printer = ReceiptPrinter(vid, pid, on_error=lambda m: c.notify(f"{RED}{m}{RESET}"))
        print("Bondrucker bereit – Drucken ist AUS. Mit !print einschalten, !testprint für einen Testdruck.")
    c.run()


if __name__ == "__main__":
    main()
