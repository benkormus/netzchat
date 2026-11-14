#!/usr/bin/env node
/*
 * Kommandozeilen-Client für den Messenger (app.py) – JavaScript/Node.js-Edition.
 *
 * Voraussetzung: Node.js 20 oder neuer (keine zusätzlichen Pakete nötig).
 * Start:   node client.js                       (fragt Benutzername/Passwort ab)
 *          node client.js --url http://13.49.225.2:5874 --user benkormos
 *          (Passwort optional über die Umgebungsvariable MESSENGER_PASSWORD)
 *
 * Befehle:
 *     #kanal              Kanal im aktuellen Server wechseln  ("#" allein = Kanalliste)
 *     /server [nr|name]   Server wechseln (ohne Argument: Liste)
 *     /dms [username]     Privatchat öffnen (ohne Argument: Nutzerliste)
 *     /users              alle Nutzer anzeigen
 *     /members            Mitglieder des aktuellen Servers
 *     /freund <name>      Freundschaftsanfrage senden
 *     /annehmen <name>    Freundschaftsanfrage annehmen
 *     /join <code|link>   Server per Einladungscode beitreten
 *     /laden              Verlauf neu laden
 *     /help               Hilfe
 *     /quit               Beenden
 *     //text              sendet eine Nachricht, die mit "/" beginnt
 *
 * Alles andere wird als Nachricht in den aktuellen Kanal bzw. Privatchat gesendet.
 * Beim Öffnen eines Chats werden die letzten 20 Nachrichten angezeigt.
 * Bilder/Videos/Dateien werden nicht angezeigt, nur als [Bild: name] usw.
 */
"use strict";

const readline = require("readline");
const zlib = require("zlib");

const DEFAULT_URL = "http://13.49.225.2:5874";
const POLL_MS = 2000;
const HISTORY_LIMIT = 20; // so viele alte Nachrichten werden beim Öffnen eines Chats angezeigt

// Farben (ANSI)
const RESET = "\x1b[0m", DIM = "\x1b[2m", BOLD = "\x1b[1m";
const CYAN = "\x1b[36m", GREEN = "\x1b[32m", YELLOW = "\x1b[33m", RED = "\x1b[31m", MAGENTA = "\x1b[35m";

const QUIT = Symbol("quit");

class ApiError extends Error {
  constructor(msg, status = 0) {
    super(msg);
    this.status = status;
  }
}

// ------------------------------------------------------------------ HTTP
let BASE = DEFAULT_URL;
const jar = new Map(); // Cookies (fetch hat keinen eigenen Cookie-Speicher)

function storeCookies(res) {
  const list = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [];
  for (const line of list) {
    const [pair, ...attrs] = line.split(";");
    const i = pair.indexOf("=");
    if (i < 0) continue;
    const name = pair.slice(0, i).trim();
    const value = pair.slice(i + 1).trim();
    const expired = attrs.some((a) => /^\s*max-age\s*=\s*0\s*$/i.test(a) || /^\s*expires\s*=.*1970/i.test(a));
    if (!value || expired) jar.delete(name);
    else jar.set(name, value);
  }
}

const cookieHeader = () => [...jar].map(([k, v]) => `${k}=${v}`).join("; ");

/** CSRF-Token steht im (signierten, aber lesbaren) Flask-Session-Cookie. */
function csrf() {
  const raw = jar.get("session");
  if (!raw) return "";
  try {
    const compressed = raw.startsWith(".");
    const payload = raw.replace(/^\./, "").split(".")[0];
    let data = Buffer.from(payload, "base64url");
    if (compressed) data = zlib.inflateSync(data);
    return JSON.parse(data.toString("utf8")).csrf || "";
  } catch {
    return "";
  }
}

async function http(method, path, { json, form, params, redirect = "follow" } = {}) {
  let url = BASE + path;
  if (params) url += "?" + new URLSearchParams(params);
  const headers = { Cookie: cookieHeader() };
  if (method !== "GET") headers["X-CSRF-Token"] = csrf();
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    headers["Content-Type"] = "application/x-www-form-urlencoded";
    body = new URLSearchParams(form).toString();
  }
  let res;
  try {
    res = await fetch(url, { method, headers, body, redirect, signal: AbortSignal.timeout(10000) });
  } catch (e) {
    throw new ApiError("Verbindungsfehler: " + ((e.cause && e.cause.code) || e.name));
  }
  storeCookies(res);
  return res;
}

async function api(method, path, opts) {
  const res = await http(method, path, opts);
  let data = {};
  try {
    data = await res.json();
  } catch {
    /* kein JSON */
  }
  if (res.status >= 400) throw new ApiError((data && data.error) || `HTTP ${res.status}`, res.status);
  return data;
}

// ------------------------------------------------------------- Zustand
const S = {
  username: "",
  servers: [],
  srv: null, // Daten des aktuellen Servers
  lastChannel: {}, // server_id -> zuletzt genutzter Kanal
  view: null, // { kind: "channel" | "dm", id }
  label: "",
  lastId: 0,
  lastDate: null,
  gen: 0,
  loaded: false, // erste Ladung der Ansicht abgeschlossen?
  actLast: 0,
};
let rl = null;

// -------------------------------------------------------------- Ausgabe
const promptStr = () => (S.label ? `${GREEN}[${S.label}]${RESET} > ` : "> ");

function say(text = "") {
  console.log(text);
}

/** Ausgabe aus dem Hintergrund: Zeile löschen, Text ausgeben, Prompt samt Eingabe neu zeichnen. */
function notify(text) {
  readline.cursorTo(process.stdout, 0);
  readline.clearLine(process.stdout, 0);
  console.log(text);
  if (rl) rl.prompt(true);
}

// -------------------------------------------------------------- Helfer
function fmtSize(n) {
  if (!n) return "";
  for (const unit of ["Bytes", "KB", "MB", "GB"]) {
    if (n < 1024 || unit === "GB") return Number.isInteger(n) ? `${n} ${unit}` : `${n.toFixed(1)} ${unit}`;
    n /= 1024;
  }
}

function parseTs(s) {
  if (!s) return null;
  const d = new Date(s.replace(" ", "T") + "Z"); // Server speichert UTC
  return isNaN(d) ? null : d;
}

/** Treffer per exaktem Namen, Präfix oder Teilstring (Groß/Klein egal). */
function pick(items, key, query) {
  const q = query.trim().toLowerCase();
  const tests = [(v) => v === q, (v) => v.startsWith(q), (v) => v.includes(q)];
  for (const t of tests) {
    const found = items.filter((i) => t(String(key(i)).toLowerCase()));
    if (found.length) return found;
  }
  return [];
}

function fmtMsg(m) {
  const lines = [];
  const dt = parseTs(m.created_at);
  let ts = "--:--";
  if (dt) {
    const day = dt.toDateString();
    if (day !== S.lastDate) {
      S.lastDate = day;
      lines.push(`${DIM}─── ${dt.toLocaleDateString("de-DE")} ───${RESET}`);
    }
    ts = dt.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  }
  const own = m.username.toLowerCase() === S.username.toLowerCase();
  const name = m.username + (m.is_verified ? " ✓" : "") + (m.is_admin ? " [ADMIN]" : "");
  const head = `${DIM}${ts}${RESET} ${(own ? GREEN : CYAN) + BOLD}${name}${RESET}: `;
  const indent = " ".repeat(7);
  const body = m.body ? m.body.split(/\r?\n/) : [];
  if (m.media) {
    const kind = { image: "Bild", video: "Video", audio: "Audio" }[m.media_type] || "Datei";
    const size = fmtSize(m.media_size);
    body.push(`${MAGENTA}[${kind}: ${m.media_name || "Anhang"}${size ? ", " + size : ""}]${RESET}`);
  }
  if (!body.length) body.push("");
  lines.push(head + body[0]);
  for (const b of body.slice(1)) lines.push(indent + b);
  if (m.reactions && m.reactions.length) {
    lines.push(indent + DIM + "↳ " + m.reactions.map((r) => `${r.emoji} ${r.count}`).join("  ") + RESET);
  }
  return lines;
}

// ------------------------------------------------------- Nachrichten
const fetchView = (v) => api("GET", v.kind === "channel" ? `/api/channels/${v.id}` : `/api/dm/${v.id}`);

/** Neue Nachrichten der aktuellen Ansicht holen und ausgeben. */
async function refresh(background) {
  const view = S.view;
  const gen = S.gen;
  if (!view) return;
  let msgs = await fetchView(view);
  if (gen !== S.gen) return; // Ansicht wurde inzwischen gewechselt
  const first = !S.loaded;
  S.loaded = true;
  if (first) msgs = msgs.slice(-HISTORY_LIMIT); // erstes Laden: nur die letzten Nachrichten
  const lines = [];
  for (const m of msgs) {
    if (m.id > S.lastId) {
      lines.push(...fmtMsg(m));
      S.lastId = m.id;
    }
  }
  if (lines.length) (background ? notify : say)(lines.join("\n"));
}

async function openView(kind, id, label, title) {
  S.view = { kind, id };
  S.label = label;
  S.lastId = 0;
  S.lastDate = null;
  S.loaded = false;
  S.gen++;
  say(`\n${BOLD}═══ ${title} ═══${RESET}`);
  await refresh(false);
  if (S.lastId === 0) say(`${DIM}(noch keine Nachrichten)${RESET}`);
}

async function send(text) {
  if (!S.view) return say("Kein Chat ausgewählt.");
  const v = S.view;
  await api("POST", v.kind === "channel" ? `/api/channels/${v.id}` : `/api/dm/${v.id}`, { json: { body: text } });
  await refresh(false);
}

// ------------------------------------------------- Server / Kanäle
const serverName = (sid) => (S.servers.find((s) => s.id === sid) || { name: "Server" }).name;

async function loadServer(sid) {
  S.srv = await api("GET", `/api/servers/${sid}`);
  const channels = S.srv.channels;
  if (!channels.length) return say("Dieser Server hat keine Kanäle.");
  const wanted = S.lastChannel[sid];
  await enterChannel(channels.find((c) => c.id === wanted) || channels[0]);
}

async function enterChannel(ch) {
  const sid = S.srv.server.id;
  S.lastChannel[sid] = ch.id;
  const name = S.srv.server.name;
  await openView("channel", ch.id, `${name} #${ch.name}`, `${name} · #${ch.name}`);
}

async function cmdServer(arg) {
  S.servers = await api("GET", "/api/servers");
  if (!arg) {
    const cur = S.srv ? S.srv.server.id : null;
    say(`${BOLD}Deine Server:${RESET}`);
    S.servers.forEach((s, i) => say(`  ${i + 1}. ${s.name}${s.id === cur ? " ←" : ""}`));
    return say(`${DIM}Wechseln mit /server <nr|name>${RESET}`);
  }
  let target;
  if (/^\d+$/.test(arg) && +arg >= 1 && +arg <= S.servers.length) {
    target = S.servers[+arg - 1];
  } else {
    const found = pick(S.servers, (s) => s.name, arg);
    if (!found.length) return say(`${RED}Server nicht gefunden.${RESET} (/server zeigt die Liste)`);
    if (found.length > 1) return say("Mehrdeutig: " + found.map((s) => s.name).join(", "));
    target = found[0];
  }
  await loadServer(target.id);
}

async function cmdChannel(name) {
  if (!S.srv) return say("Kein Server ausgewählt.");
  if (!name) return listChannels();
  const query = name.replace(/^#/, "");
  let found = pick(S.srv.channels, (c) => c.name, query);
  if (!found.length) {
    S.srv = await api("GET", `/api/servers/${S.srv.server.id}`); // evtl. neuer Kanal
    found = pick(S.srv.channels, (c) => c.name, query);
  }
  if (!found.length) return say(`${RED}Kanal nicht gefunden.${RESET} (# zeigt die Liste)`);
  if (found.length > 1) return say("Mehrdeutig: " + found.map((c) => "#" + c.name).join(", "));
  await enterChannel(found[0]);
}

function listChannels() {
  say(`${BOLD}Kanäle in ${S.srv.server.name}:${RESET}`);
  const cats = S.srv.categories;
  const ids = new Set(cats.map((c) => c.id));
  const groups = cats.map((c) => [c.name, S.srv.channels.filter((ch) => ch.category_id === c.id)]);
  groups.push(["ohne Kategorie", S.srv.channels.filter((ch) => !ids.has(ch.category_id))]);
  for (const [title, chans] of groups) {
    if (!chans.length) continue;
    say(`  ${DIM}${title}${RESET}`);
    for (const c of chans) say(`    #${c.name}`);
  }
}

// ------------------------------------------------------------ Privat
const getUsers = () => api("GET", "/api/users");

function printUsers(users) {
  const rel = { friend: "Freund", pending_out: "Anfrage gesendet", pending_in: "Anfrage erhalten", none: "" };
  say(`${BOLD}Nutzer:${RESET}`);
  for (const u of users) {
    const dot = u.online ? `${GREEN}●${RESET}` : `${DIM}○${RESET}`;
    let extra = rel[u.relation] || "";
    if (!u.can_dm && u.relation !== "friend") extra = (extra ? extra + " " : "") + "(nur Freunde)";
    say(`  ${dot} ${u.username}${extra ? " " + DIM + extra + RESET : ""}`);
  }
  say(`${DIM}Chat öffnen mit /dms <username>${RESET}`);
}

async function cmdDms(arg) {
  const users = await getUsers();
  if (!arg) return printUsers(users);
  const found = pick(users, (u) => u.username, arg.replace(/^@/, ""));
  if (!found.length) return say(`${RED}Nutzer nicht gefunden.${RESET}`);
  if (found.length > 1) return say("Mehrdeutig: " + found.map((u) => u.username).join(", "));
  const u = found[0];
  await openView("dm", u.id, `DM ${u.username}`, `Privatchat mit ${u.username}`);
  if (!u.can_dm) {
    say(`${YELLOW}Hinweis: ${u.username} akzeptiert private Nachrichten nur von Freunden. Sende zuerst /freund ${u.username}.${RESET}`);
  }
}

async function cmdFriend(arg, accept = false) {
  if (!arg) return say("Benutzername angeben.");
  const found = pick(await getUsers(), (u) => u.username, arg.replace(/^@/, ""));
  if (found.length !== 1) return say(`${RED}Nutzer nicht gefunden oder mehrdeutig.${RESET}`);
  const u = found[0];
  const data = await api("POST", `/api/friends/${accept ? "accept" : "request"}/${u.id}`);
  say(`Status mit ${u.username}: ${data.relation}`);
}

async function cmdMembers() {
  if (!S.srv) return;
  S.srv = await api("GET", `/api/servers/${S.srv.server.id}`);
  const mem = [...S.srv.members].sort(
    (a, b) => Number(b.online) - Number(a.online) || a.username.toLowerCase().localeCompare(b.username.toLowerCase())
  );
  say(`${BOLD}Mitglieder von ${S.srv.server.name} (${mem.length}):${RESET}`);
  for (const m of mem) {
    const dot = m.online ? `${GREEN}●${RESET}` : `${DIM}○${RESET}`;
    const tag = { owner: " (Besitzer)", admin: " (Admin)" }[m.role] || "";
    say(`  ${dot} ${m.username}${tag}`);
  }
}

async function cmdJoin(code) {
  if (!code) return say("Einladungscode oder -link angeben.");
  const data = await api("POST", "/api/servers/join", { json: { code } });
  S.servers = await api("GET", "/api/servers");
  say(`Beigetreten: ${data.server.name}`);
  await loadServer(data.server.id);
}

// -------------------------------------------------------- Hintergrund
async function pollActivity() {
  const data = await api("GET", "/api/activity", { params: { since: S.actLast } });
  S.actLast = data.last;
  const view = S.view;
  for (const it of data.items) {
    if (
      view &&
      ((view.kind === "channel" && it.channel_id === view.id) ||
        (view.kind === "dm" && it.channel_id === null && it.sender_id === view.id))
    ) {
      continue; // steht schon in der aktuellen Ansicht
    }
    const where = it.channel_id ? `#${it.channel_name} (${serverName(it.server_id)})` : "DM";
    const ping = it.mention ? " @DU" : "";
    notify(`${YELLOW}[neu${ping}] ${where} – ${it.username}: ${it.preview}${RESET}`);
  }
}

function startPolling() {
  let busy = false;
  let warned = false;
  const timer = setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      await refresh(true);
      await pollActivity();
      warned = false;
    } catch (e) {
      if (e.status === 401) {
        notify(`${RED}Sitzung abgelaufen – bitte Client neu starten.${RESET}`);
        clearInterval(timer);
      } else if (!warned) {
        notify(`${RED}[${e.message}]${RESET}`);
        warned = true;
      }
    } finally {
      busy = false;
    }
  }, POLL_MS);
}

// ------------------------------------------------------------ Eingabe
const HELP = `Befehle:
  #kanal              Kanal wechseln ("#" allein = Kanalliste)
  /server [nr|name]   Server wechseln (ohne Argument: Liste)
  /dms [username]     Privatchat öffnen (ohne Argument: Nutzerliste)
  /users              alle Nutzer anzeigen
  /members            Mitglieder des aktuellen Servers
  /freund <name>      Freundschaftsanfrage senden
  /annehmen <name>    Freundschaftsanfrage annehmen
  /join <code|link>   Server per Einladungscode beitreten
  /laden              Verlauf neu laden
  /quit               Beenden
  //text              sendet eine Nachricht, die mit "/" beginnt`;

async function handle(line) {
  if (line.startsWith("//")) return send(line.slice(1));
  if (line.startsWith("/")) {
    const sp = line.indexOf(" ");
    const cmd = (sp < 0 ? line.slice(1) : line.slice(1, sp)).toLowerCase();
    const arg = sp < 0 ? "" : line.slice(sp + 1).trim();
    switch (cmd) {
      case "quit":
      case "exit":
      case "q":
        throw QUIT;
      case "server":
      case "s":
        return cmdServer(arg);
      case "dms":
      case "dm":
      case "d":
        return cmdDms(arg);
      case "users":
        return printUsers(await getUsers());
      case "members":
        return cmdMembers();
      case "freund":
      case "friend":
        return cmdFriend(arg);
      case "annehmen":
      case "accept":
        return cmdFriend(arg, true);
      case "join":
        return cmdJoin(arg);
      case "laden":
      case "reload":
        if (S.view) await openView(S.view.kind, S.view.id, S.label, S.label);
        return;
      case "help":
      case "hilfe":
      case "?":
        return say(HELP);
      default:
        return say(`Unbekannter Befehl /${cmd}. /help zeigt alle Befehle.`);
    }
  }
  if (line.startsWith("#") && !line.includes(" ")) return cmdChannel(line.slice(1));
  return send(line);
}

function ask(question) {
  return new Promise((resolve) => {
    const tmp = readline.createInterface({ input: process.stdin, output: process.stdout });
    tmp.question(question, (a) => {
      tmp.close();
      resolve(a.trim());
    });
  });
}

function askHidden(question) {
  if (!process.stdin.isTTY) return ask(question);
  return new Promise((resolve) => {
    const stdin = process.stdin;
    let buf = "";
    process.stdout.write(question);
    stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding("utf8");
    const onData = (chunk) => {
      for (const c of chunk) {
        if (c === "\r" || c === "\n") {
          stdin.setRawMode(false);
          stdin.pause();
          stdin.removeListener("data", onData);
          process.stdout.write("\n");
          return resolve(buf);
        }
        if (c === "\u0003") process.exit(130); // Strg+C
        if (c === "\u007f" || c === "\b") buf = buf.slice(0, -1);
        else buf += c;
      }
    };
    stdin.on("data", onData);
  });
}

async function login(username, password) {
  await http("GET", "/login"); // setzt Session + CSRF-Cookie
  const res = await http("POST", "/login", {
    form: { username, password, csrf_token: csrf() },
    redirect: "manual",
  });
  const loc = res.headers.get("location") || "";
  if (![301, 302, 303].includes(res.status) || !loc.includes("chat")) {
    throw new ApiError("Login fehlgeschlagen (Name/Passwort falsch oder zu viele Versuche).");
  }
  S.username = username;
  S.servers = await api("GET", "/api/servers"); // erzeugt auch das neue CSRF-Token
}

function parseArgs(argv) {
  const out = { url: process.env.MESSENGER_URL || DEFAULT_URL, user: "" };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--url") out.url = argv[++i];
    else if (argv[i] === "--user") out.user = argv[++i];
    else if (argv[i] === "-h" || argv[i] === "--help") {
      console.log("Aufruf: node client.js [--url http://host:5874] [--user name]\n\n" + HELP);
      process.exit(0);
    }
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  BASE = args.url.replace(/\/+$/, "");

  const user = args.user || (await ask("Benutzername: "));
  const password = process.env.MESSENGER_PASSWORD || (await askHidden("Passwort: "));
  try {
    await login(user, password);
    S.actLast = (await api("GET", "/api/activity")).last;
    await loadServer(S.servers[0].id); // Hauptserver, erster Kanal
  } catch (e) {
    console.error("Fehler: " + e.message);
    process.exit(1);
  }

  rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: !!process.stdin.isTTY });
  say(`${DIM}/help für alle Befehle${RESET}`);
  startPolling();
  rl.setPrompt(promptStr());
  rl.prompt();

  let chain = Promise.resolve(); // Eingaben nacheinander abarbeiten
  rl.on("line", (raw) => {
    chain = chain.then(async () => {
      const line = raw.trim();
      if (line) {
        try {
          await handle(line);
        } catch (e) {
          if (e === QUIT) return rl.close();
          say(`${RED}Fehler: ${e.message}${RESET}`);
        }
      }
      rl.setPrompt(promptStr());
      rl.prompt();
    });
  });
  rl.on("close", () => {
    console.log("\nTschüss!");
    process.exit(0);
  });
}

main();
