# Λαβράκι

Εφαρμογή παρακολούθησης τιμών για ταξίδι μέσα στο 2027.
Το `data.json` ενημερώνεται κάθε μέρα από την προγραμματισμένη αναζήτηση του Claude.

## Αυτόματος έλεγχος τιμών

Το `engine/run.py` τρέχει κάθε 3 ώρες στο GitHub Actions (δωρεάν για δημόσια repos) και γράφει το `auto.json`, που η εφαρμογή διαβάζει μαζί με το `data.json`.

- Πτήσεις: Aviasales Data API (Travelpayouts), μυστικό `TRAVELPAYOUTS_TOKEN`.
- Ξενοδοχεία (προαιρετικό): LiteAPI, μυστικό `LITEAPI_KEY`.
- Δοκιμές χωρίς δίκτυο: `python3 -m unittest discover -s engine/tests`
