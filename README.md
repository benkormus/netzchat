# NetzChat – Kommandozeilen-Clients

Standard-Server: `http://13.49.225.2:5874` (änderbar mit `--url`).

## js/ (Node.js 20+, keine Abhängigkeiten)
```
cd js
npm start                  # oder: node client.js
npm start -- --user benkormos
```
Unter Windows reicht ein Doppelklick auf `client_starten.bat`.

## python/
```
cd python
pip install -r requirements.txt
python client.py           # mit Bondrucker (!print, !testprint, !printset)
python client_ohne_druck.py
```

Befehle in beiden Clients: `#kanal`, `/server`, `/dms`, `/users`, `/members`, `/freund`, `/annehmen`, `/join`, `/laden`, `/help`, `/quit`.
