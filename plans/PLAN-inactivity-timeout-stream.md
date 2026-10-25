# PLAN — Untaetigkeitsgrenze statt Gesamt-Timeout in `hermes_client.ask` (hermes-mcp 0.4.0+herz.3)

Karte: t_86e7a708 · Repo: BytePickers/hermes-mcp · Zielzweig: `pr/profile-routing-and-ask-offload` (Kopf c87afbf)

## 1. Befund (gemessen, 25.10.2026)

- `src/hermes_mcp/hermes_client.py` Z. 65–69 setzt `"stream": False`, Z. 88–94 ruft
  `httpx.post(..., timeout=self._timeout)`; Z. 95–96 macht daraus
  `hermes gateway timed out after {n}s`. Der Wert `self._timeout` ist EIN Gesamt-Timeout
  über den gesamten agentischen Turn.
- Journal der Bruecken-Unit `hermes-mcp-gate` heute: genau 4 Zeilen dieser Klasse
  (08:34:03 zu Job cd7f4fb8, 11:22:24 zu 009260ae, 12:09:42 zu 3c65d737, 12:25:09 zu f1ee4330),
  jeweils 900 s nach dem Einreihen (Startzeiten 08:19:03 / 11:07:23 / 11:54:42 / 12:10:09).
  Keine andere Fehlerklasse im heutigen Journal.
- `HERMES_REQUEST_TIMEOUT_SECONDS=900` (config.py Z. 126–135, Default 300, `<= 0` verboten).

## 2. Live-Probe Schritt 0 (Ergebnis, siehe Kartenkommentar)

1. **Streaming**: `POST /v1/chat/completions` mit `"stream": true` und
   `X-Hermes-Session-Id` → HTTP 200, 38 `data:`-Zeilen, `[DONE]`, `finish_reason=stop`,
   3,26 s Gesamtdauer, 90 Zeichen Text. Bei einem Turn mit 22 s Werkzeuglauf kamen
   2 `: keepalive`-Zeilen (Gateway-Takt 10 s) — Deltas UND Keepalives sind belegt.
2. **Turn-Vergabe**: zweiter Auftrag auf DERSELBEN Sitzung wird nicht abgewiesen
   (HTTP 200), sondern serialisiert: erstes Ereignis sofort, Text aber erst nach Ende
   des ersten Turns (24,73 s). Kein Warteschlangen-Bau noetig.
3. **Nachholbarkeit**: Client mit 5 s Lesegrenze bricht ab (`ReadTimeout`), der Turn laeuft
   weiter und das Ergebnis steht danach in der Sitzungsablage
   (`GET /api/sessions/<id>/messages`: 5 Nachrichten, letzte Assistentenantwort 82 Zeichen).

## 3. Die Aenderung

### 3.1 `hermes_client.py` (Kern der Aenderung)

- Body: `"stream": True` statt `False`.
- Aufruf ueber `httpx.stream("POST", self._endpoint, json=body, headers=headers,
  timeout=self._timeout_obj, follow_redirects=False)`.
- `self._timeout` (Sekunden) wird zur **Untaetigkeitsgrenze**: `httpx.Timeout(connect=CONNECT_TIMEOUT,
  read=self._timeout, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT)` — `read` ist die maximale Zeit
  zwischen zwei eintreffenden Bytes. Damit greift die Grenze nur, wenn wirklich nichts kommt.
- Zusaetzlich optional `self._max_seconds` (`HERMES_REQUEST_MAX_SECONDS`, Default 0 = aus):
  harte Obergrenze fuer Betreiber, die eine Wand wollen. Durchgesetzt als Pruefung der
  Gesamtdauer nach jedem Ereignis (kein Timer-Thread; der Abbruch erfolgt beim naechsten Ereignis).
- Zusammenbau der Antwort: Zeilen mit `:` (Keepalive-Kommentar) ueberspringen, `data: [DONE]`
  beendet, sonst JSON lesen und `choices[0].delta.content` (nur `str`) aneinanderhaengen;
  `finish_reason` beobachten und als `length`/`stop` fuer die Fehlerdiagnose behalten.
- Fehlertexte neu und unterscheidbar:
  - Stille: `hermes gateway sent no events for {n}s`
  - Verbindungsaufbau: `hermes gateway could not be reached within {n}s`
  - harte Obergrenze: `hermes gateway turn exceeded HERMES_REQUEST_MAX_SECONDS ({n}s)`
  - 401 / non-200 / malformed bleiben wortgleich.
- Unveraendert: Profil-Routing-Endpoint, Bearer-Header, `X-Hermes-Session-Id`,
  401-Behandlung, „Body nicht echoen“, Prompt nicht auf INFO loggen.

### 3.2 `config.py`

- `HERMES_REQUEST_TIMEOUT_SECONDS` behaelt Namen und Validierung (`> 0`), Kommentar/Docstring
  neu: Bedeutung = Untaetigkeit.
- Neu `HERMES_REQUEST_MAX_SECONDS` (Default `0`), Validierung `>= 0`, Feld
  `hermes_request_max_seconds` in `Config`.
- `__main__.py` reicht den neuen Wert an `HermesClient(...)` weiter.

### 3.3 Doku

- `README.md`: Zeile „Max wall-clock per hermes_ask call“ richtigstellen (Untaetigkeit),
  neuen Wert dokumentieren.
- `CHANGELOG.md`: neuer Abschnitt mit Fehlerbild, neuer Semantik und Fehlertexten.
- `.env.example`: beide Werte mit Bedeutung.

## 4. Tests (pytest, lokal)

Neu/erweitert in `tests/test_hermes_client.py` (Transport nachgebaut, kein Netz):

1. `stream: True` im Body (Regressionsschutz gegen den heutigen Fehler).
2. Mehrere Deltas → zusammengesetzter Text; `.strip()` am Ende.
3. `: keepalive`-Zeilen landen NICHT im Text.
4. `[DONE]` beendet das Lesen; `finish_reason: "length"` fuehrt nicht zu Absturz.
5. Leerer Text (nur Deltas ohne Inhalt) → leerer String, kein Fehler.
6. Untaetigkeits-Semantik: Transport liefert Ereignisse ueber laengere Zeit als `read`,
   Gesamtdauer ueberschreitet den Wert → KEIN Timeout (beweist: kein Gesamt-Timeout mehr).
   Zusaetzlich: `httpx.Timeout`-Objekt hat `read` gesetzt und kein Gesamt-`timeout`-Skalar.
7. Stille: Transport liefert nichts → `HermesError` mit `sent no events for`.
8. `HERMES_REQUEST_MAX_SECONDS>0`: Gesamtdauer ueberschritten → Fehler mit `exceeded`.
9. 401 / non-200 / malformed / kein Body-Echo wie bisher (bestehende Tests bleiben gruen).

`tests/test_config.py`: neuer Wert Default 0; negativer Wert wird abgelehnt;
`HERMES_REQUEST_TIMEOUT_SECONDS <= 0` weiter abgelehnt.

Rot-vorher-Nachweis: Test 1 (stream: True) und Test 6 scheitern auf dem alten Stand
(alter Code sendet `stream: False` und setzt einen Gesamt-Timeout) — Nachweis als
Lauf gegen `git stash`-Stand bzw. gegen den Ausgangs-Knoten dokumentiert.

## 5. Klasse und Wirksamkeits-Kennzahl (Owner-Regel Schutzeinrichtungen)

- **Klasse**: agentische Turns, die laenger dauern als die konfigurierte Wand, ABER dabei
  regelmaessig Ereignisse liefern (Werkzeuglaeufe, mehrere Modellrunden). Heute: 4 Faelle
  am Tag, alle an derselben 900-s-Wand gestorben.
- **Belegfall fuer die Klasse**: Live-Probe mit 22 s Werkzeuglauf (2 Keepalives, Turn
  ueberlebt eine 20-s-`read`-Grenze) plus Test 6 (Transport liefert ueber die Grenze hinaus
  Ereignisse, Aufruf darf nicht abbrechen).
- **Kennzahl**: Zahl der Journal-Zeilen `sent no events for` (neue Klasse) und
  `timed out after` (alte Klasse) je Tag; Abnahme nach dem Ausrollen: 0 Treffer alte Klasse,
  neue Klasse nur bei echter Stille. Ausserdem bleibt `finish_reason` in der Antwortdiagnose
  sichtbar.

## 6. Versionierung und Wheel-Rezept

- Neu `deploy/build-cubolt-wheel.sh <version>`: kopiert den Baum in ein temporaeres
  Verzeichnis, setzt dort `version = "<version>"` in `pyproject.toml`, baut mit
  `python3 -m build --wheel` (Rueckfall `pip wheel`), schreibt `<wheel>.sha256` und
  `<wheel>.gitknoten.txt` daneben.
- Ablage: `/home/cubolt/.hermes-mcp-gate/dist/` (wird von der Rollout-Karte benutzt).

## 7. PR und Zweitpruefung

- Zweig `fix/inactivity-timeout-stream` von `origin/pr/profile-routing-and-ask-offload`,
  PR-Ziel `pr/profile-routing-and-ask-offload`.
- Vor dem ersten Push: Actions im Fork abschalten
  (`gh api -X PUT repos/BytePickers/hermes-mcp/actions/permissions -F enabled=false`),
  Antwort auf die Karte. Kein Tag, kein Release.
- Zweitpruefung: unabhaengiges Review des Diffs (Zweitpruefer-Profil/Codex) mit den drei
  Pflichtpunkten: (a) bei laufenden Ereignissen kein Gesamt-Timeout, (b) Keepalives nicht im
  Text, (c) `HERMES_REQUEST_MAX_SECONDS` optional (Default 0).

## 8. Grenzen

- Kein Hermes-Kern, keine Unit, kein Dienst-Neustart, keine Env-Aenderung, keine Schluessel
  lesen/ausgeben. Kein Rollout (eigene Karte t_a4be9162).
