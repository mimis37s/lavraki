#!/usr/bin/env python3
"""Λαβράκι price engine.

Runs on GitHub Actions (free for public repos). Every run it:
  1. pulls the cheapest round-trip fares from Athens for each destination and month
     (Aviasales Data API via Travelpayouts, free, prices seen by users in the last ~48h),
  2. drops low-cost airlines and trips that are too short/long or not direct where required,
  3. keeps a price history per route+month, so "λαβράκι" is measured against what the
     same trip usually costs, not against a guess,
  4. if a LiteAPI key exists, prices a hotel (8+, enough reviews) for the best trips,
  5. writes auto.json, which the app merges with the hand-checked data.json.

Secrets (GitHub → Settings → Secrets and variables → Actions):
  TRAVELPAYOUTS_TOKEN  required for flights
  LITEAPI_KEY          optional, adds hotels

Only the Python standard library is used, so the workflow needs no installs.
"""
import json, os, statistics, sys, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
HISTORY_PATH = os.path.join(HERE, "history.json")
OUT_PATH = os.path.join(ROOT, "auto.json")
ATHENS = timezone(timedelta(hours=2))

TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
LITE_URL = "https://api.liteapi.travel/v3.0/hotels/rates"


# ---------------------------------------------------------------- http
class Http:
    """Thin wrapper so tests can swap in canned responses."""
    def get_json(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_json(self, url, body, headers=None):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- helpers
def months_ahead(today, n):
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        out.append(f"{y:04d}-{m:02d}")
    return out


def d10(s):
    return str(s or "")[:10]


def nights_between(a, b):
    try:
        return (date.fromisoformat(d10(b)) - date.fromisoformat(d10(a))).days
    except ValueError:
        return -1


def median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def airline_name(code):
    return CFG["airline_names"].get(code, code or "")


# ---------------------------------------------------------------- flights
def fetch_flights(http, token, dest, month):
    q = {
        "origin": CFG["origin"], "destination": dest["iata"], "departure_at": month,
        "one_way": "false", "direct": "true" if dest.get("direct") else "false",
        "sorting": "price", "currency": "eur", "limit": "200", "page": "1", "unique": "false",
    }
    url = TP_URL + "?" + urllib.parse.urlencode(q)
    data = http.get_json(url, {"X-Access-Token": token})
    if not data or not data.get("success", True):
        return []
    return data.get("data") or []


def clean_offers(raw, dest):
    lowcost = set(CFG["lowcost_airlines"])
    out = []
    for r in raw:
        dep, ret = d10(r.get("departure_at")), d10(r.get("return_at"))
        price = r.get("price")
        n = nights_between(dep, ret)
        al = (r.get("airline") or "").upper()
        if not dep or not ret or not isinstance(price, (int, float)) or price <= 0:
            continue
        if n < CFG["nights_min"] or n > CFG["nights_max"]:
            continue
        if al in lowcost:
            continue
        stops = max(int(r.get("transfers") or 0), int(r.get("return_transfers") or 0))
        if dest.get("direct") and stops > 0:
            continue
        if stops > 1:
            continue
        out.append({"dep": dep, "ret": ret, "nights": n, "price": round(price), "airline": al,
                    "stops": stops, "link": r.get("link") or ""})
    return out


# ---------------------------------------------------------------- history
def load_history():
    try:
        return json.load(open(HISTORY_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record(history, key, offers, today):
    prices = sorted(o["price"] for o in offers)
    if not prices:
        return
    entry = {"d": today.isoformat(), "min": prices[0], "med": round(median(prices))}
    rows = [e for e in history.get(key, []) if e.get("d") != entry["d"]]
    rows.append(entry)
    history[key] = rows[-120:]


def baseline(history, key, offers):
    """What this route+month usually costs per person. History first, else today's spread."""
    rows = history.get(key, [])
    if len(rows) >= 5:
        return median([e["med"] for e in rows])
    # No history yet: use the cheapest fare of every departure day in the month and take the
    # median. That is "what a normal day costs", so a day far below it is a real catch.
    daily = {}
    for o in offers:
        daily[o["dep"]] = min(o["price"], daily.get(o["dep"], o["price"]))
    if len(daily) >= 4:
        return median(list(daily.values()))
    return None


def alternatives(offers, best, limit=3, within=0.15):
    """Other departure days in the same month at most 15% dearer: a fallback if the best fare goes."""
    seen, out = {best["dep"]}, []
    for o in sorted(offers, key=lambda x: x["price"]):
        if o["dep"] in seen or o["price"] > best["price"] * (1 + within):
            continue
        seen.add(o["dep"])
        out.append({"from": o["dep"], "to": o["ret"], "price": o["price"]})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- hotels
def fetch_hotel(http, key, dest, dep, ret):
    body = {
        "cityName": dest["city"], "countryCode": dest["cc"], "checkin": dep, "checkout": ret,
        "occupancies": [{"adults": CFG["adults"]}], "currency": "EUR", "guestNationality": "GR",
        "timeout": 8, "limit": 200,
    }
    res = http.post_json(LITE_URL, body, {"X-API-Key": key})
    return pick_hotel(res)


def _amount(room):
    for path in (("offerRetailRate", "amount"),):
        v = room.get(path[0], {}) or {}
        if isinstance(v, dict) and isinstance(v.get(path[1]), (int, float)):
            return v[path[1]]
    for rate in room.get("rates") or []:
        tot = rate.get("retailRate", {}).get("total") or []
        if tot and isinstance(tot[0].get("amount"), (int, float)):
            return tot[0]["amount"]
    return None


def pick_hotel(res):
    """Cheapest hotel with rating >= min and enough reviews, plus the median of those that qualify."""
    if not isinstance(res, dict):
        return None
    info = {h.get("id"): h for h in (res.get("hotels") or []) if isinstance(h, dict)}
    good = []
    for item in res.get("data") or []:
        h = info.get(item.get("hotelId"), {})
        rating, reviews = h.get("rating"), h.get("reviewCount") or h.get("reviewsCount") or 0
        if not isinstance(rating, (int, float)) or rating < CFG["hotel_min_rating"] or reviews < CFG["hotel_min_reviews"]:
            continue
        prices = [p for p in (_amount(r) for r in item.get("roomTypes") or []) if p]
        if prices:
            good.append({"id": item.get("hotelId"), "name": h.get("name") or "Ξενοδοχείο",
                         "rating": round(rating, 1), "reviews": reviews, "price": round(min(prices))})
    if not good:
        return None
    good.sort(key=lambda x: x["price"])
    best = dict(good[0])
    best["median"] = round(median([g["price"] for g in good]))
    best["count"] = len(good)
    return best


# ---------------------------------------------------------------- output
def gflights(dest, dep, ret, adults, direct):
    q = f"Flights to {dest['city']} from Athens on {dep} through {ret} for {adults} adults" + (" nonstop" if direct else "")
    return "https://www.google.com/travel/flights?hl=el&q=" + urllib.parse.quote(q)


def booking(dest, dep, ret, adults):
    q = {"ss": dest["city"], "checkin": dep, "checkout": ret, "group_adults": adults, "no_rooms": 1,
         "group_children": 0, "nflt": "review_score=80", "order": "price", "selected_currency": "EUR"}
    return "https://www.booking.com/searchresults.el.html?" + urllib.parse.urlencode(q)


def status_for(saving):
    if saving is None:
        return "check"
    if saving >= CFG["hit_saving"]:
        return "hit"
    if saving >= CFG["near_saving"]:
        return "near"
    return "normal"


def build(http, tp_token, lite_key, today=None, sleep=0.15):
    today = today or datetime.now(ATHENS).date()
    history = load_history()
    flights = []
    errors = 0
    for dest in CFG["destinations"]:
        for month in months_ahead(today, CFG["months_ahead"]):
            try:
                raw = fetch_flights(http, tp_token, dest, month)
            except Exception as e:  # network hiccup on one route must not kill the run
                errors += 1
                print(f"flights {dest['iata']} {month}: {e}", file=sys.stderr)
                continue
            offers = clean_offers(raw, dest)
            key = f"{CFG['origin']}-{dest['iata']}-{month}"
            base = baseline(history, key, offers)
            record(history, key, offers, today)
            if not offers:
                continue
            best = min(offers, key=lambda o: o["price"])
            saving = (1 - best["price"] / base) if base else None
            flights.append({"dest": dest, "month": month, "offer": best, "base": base, "saving": saving,
                            "alts": alternatives(offers, best)})
            if sleep:
                time.sleep(sleep)

    # one entry per destination: the trip with the biggest saving (then cheapest)
    by_dest = {}
    for f in flights:
        cur = by_dest.get(f["dest"]["iata"])
        rank = ((f["saving"] or -1), -f["offer"]["price"])
        if not cur or rank > ((cur["saving"] or -1), -cur["offer"]["price"]):
            by_dest[f["dest"]["iata"]] = f
    top = sorted(by_dest.values(), key=lambda f: (-(f["saving"] or -1), f["offer"]["price"]))

    stamp = datetime.now(ATHENS).isoformat(timespec="minutes")
    adults = CFG["adults"]
    deals, flight_list = [], []
    for f in top[: CFG["max_packages"]]:
        d, o = f["dest"], f["offer"]
        flight_txt = (f"{airline_name(o['airline'])} " + ("απευθείας" if o["stops"] == 0 else "με 1 στάση")
                      + f", {o['price'] * adults} € για {adults} άτομα μετ' επιστροφής")
        common = {
            "destination": d["el"], "city": d["el"], "from": o["dep"], "to": o["ret"], "exact": True,
            "people": adults, "lowcost": False, "checked": stamp[:10], "checkedAt": stamp, "auto": True,
        }
        flight_list.append(dict(common, id=f"auto-f-{d['iata']}-{o['dep']}", kind="flight",
                                flight=flight_txt, hotel=None, total=o["price"] * adults,
                                typical=round(f["base"] * adults) if f["base"] else None,
                                savings=round(f["saving"] * 100) if f["saving"] and f["saving"] > 0 else None,
                                status=status_for(f["saving"]),
                                alts=[dict(a, price=a["price"] * adults) for a in f["alts"]],
                                source="Αυτόματος έλεγχος · τιμές που βρέθηκαν τις τελευταίες 48 ώρες",
                                links=[{"label": "Πτήση (Google Flights)", "url": gflights(d, o["dep"], o["ret"], adults, o["stops"] == 0)}]))
        if not lite_key:
            continue
        try:
            h = fetch_hotel(http, lite_key, d, o["dep"], o["ret"])
        except Exception as e:
            errors += 1
            print(f"hotel {d['iata']}: {e}", file=sys.stderr)
            continue
        if not h:
            continue
        total = o["price"] * adults + h["price"]
        typical = f["base"] * adults + h["median"] if f["base"] else None
        saving = 1 - total / typical if typical else None
        deals.append(dict(common, id=f"auto-p-{d['iata']}-{o['dep']}", kind="package",
                          flight=flight_txt,
                          hotel=f"{h['name']}, βαθμολογία {str(h['rating']).replace('.', ',')}/10 ({h['reviews']} κριτικές), {h['price']} € για {o['nights']} βράδια για {adults}. Η καθαριότητα δεν έχει ελεγχθεί ακόμα.",
                          total=round(total), typical=round(typical) if typical else None,
                          savings=round(saving * 100) if saving and saving > 0 else None,
                          status=status_for(saving),
                          source="Αυτόματος έλεγχος · πτήση από Aviasales, ξενοδοχείο από LiteAPI",
                          links=[{"label": "Πτήση (Google Flights)", "url": gflights(d, o["dep"], o["ret"], adults, o["stops"] == 0)},
                                 {"label": "Ξενοδοχεία 8+ (Booking)", "url": booking(d, o["dep"], o["ret"], adults)}]))

    out = {"updated": stamp, "deals": deals, "flights": flight_list,
           "stats": {"routes": len(CFG["destinations"]), "months": CFG["months_ahead"],
                     "priced_trips": len(flights), "errors": errors, "hotels": bool(lite_key)}}
    return out, history


def main():
    tp = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
    lite = os.environ.get("LITEAPI_KEY", "").strip()
    if not tp:
        print("TRAVELPAYOUTS_TOKEN is not set: nothing to do. Add it in GitHub → Settings → Secrets → Actions.")
        return 0
    out, history = build(Http(), tp, lite)
    if out["stats"]["priced_trips"] == 0 and out["stats"]["errors"]:
        print("Every request failed; keeping the previous auto.json.", file=sys.stderr)
        return 1
    json.dump(history, open(HISTORY_PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out["stats"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
