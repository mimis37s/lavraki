"""Offline tests for the price engine: canned API answers, no network.

Run:  python3 -m unittest discover -s engine/tests
"""
import os, sys, tempfile, unittest
from datetime import date
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import run  # noqa: E402


def fare(dep, ret, price, airline="A3", t=0, rt=0):
    return {"departure_at": dep + "T10:00:00+02:00", "return_at": ret + "T18:00:00+01:00", "price": price,
            "airline": airline, "transfers": t, "return_transfers": rt, "link": "/search/ATH0102VIE04021"}


class FakeHttp:
    def __init__(self):
        self.gets, self.posts = [], []

    def get_json(self, url, headers=None):
        self.gets.append(url)
        q = parse_qs(urlparse(url).query)
        dest, month = q["destination"][0], q["departure_at"][0]
        if dest == "VIE" and month == "2027-02":
            return {"success": True, "data": [
                fare("2027-02-19", "2027-02-21", 95),            # cheap Aegean, 2 nights: the catch
                fare("2027-02-12", "2027-02-14", 150),
                fare("2027-02-05", "2027-02-08", 160),
                fare("2027-02-20", "2027-02-23", 170),
                fare("2027-02-01", "2027-02-03", 40, "FR"),      # Ryanair: dropped
                fare("2027-02-01", "2027-02-02", 60),            # 1 night: dropped
                fare("2027-02-10", "2027-02-13", 80, "OS", 1, 0)  # stop on direct route: dropped
            ]}
        if dest == "NYC" and month == "2027-07":
            return {"success": True, "data": [fare("2027-07-19", "2027-07-25", 806, "UA")]}
        return {"success": True, "data": []}

    def post_json(self, url, body, headers=None):
        self.posts.append(body)
        return {"data": [
            {"hotelId": "h1", "roomTypes": [{"offerRetailRate": {"amount": 110.4}}]},
            {"hotelId": "h2", "roomTypes": [{"offerRetailRate": {"amount": 90}}]},   # rating too low
            {"hotelId": "h3", "roomTypes": [{"rates": [{"retailRate": {"total": [{"amount": 180}]}}]}]},
            {"hotelId": "h4", "roomTypes": [{"offerRetailRate": {"amount": 70}}]},   # too few reviews
        ], "hotels": [
            {"id": "h1", "name": "lyf Schönbrunn", "rating": 8.9, "reviewCount": 4005},
            {"id": "h2", "name": "Cheap Inn", "rating": 7.1, "reviewCount": 900},
            {"id": "h3", "name": "Grand", "rating": 9.0, "reviewCount": 2000},
            {"id": "h4", "name": "New Place", "rating": 9.5, "reviewCount": 12},
        ]}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        run.HISTORY_PATH = os.path.join(self.tmp, "history.json")
        run.CFG["destinations"] = [d for d in run.CFG["destinations"] if d["iata"] in ("VIE", "NYC")]

    def test_filters(self):
        dest = {"iata": "VIE", "direct": True}
        raw = FakeHttp().get_json("https://x/?destination=VIE&departure_at=2027-02")["data"]
        got = run.clean_offers(raw, dest)
        self.assertEqual(sorted(o["price"] for o in got), [95, 150, 160, 170])

    def test_months(self):
        self.assertEqual(run.months_ahead(date(2026, 11, 22), 3), ["2026-12", "2027-01", "2027-02"])

    def test_pick_hotel(self):
        h = run.pick_hotel(FakeHttp().post_json("", {}))
        self.assertEqual((h["name"], h["price"], h["median"], h["count"]), ("lyf Schönbrunn", 110, 145, 2))

    def test_build_without_hotels(self):
        http = FakeHttp()
        out, hist = run.build(http, "tok", "", today=date(2026, 11, 22), sleep=0)
        self.assertEqual(out["deals"], [])
        vie = [f for f in out["flights"] if f["id"].startswith("auto-f-VIE")][0]
        self.assertEqual(vie["total"], 190)            # 95 € x 2 people
        self.assertEqual(vie["typical"], 310)          # median(95,150,160,170)=155 x 2
        self.assertEqual(vie["savings"], 39)
        self.assertEqual(vie["status"], "hit")
        self.assertIn("ATH-VIE-2027-02", hist)
        self.assertTrue(all("X-Access-Token" for _ in http.gets))

    def test_build_with_hotels(self):
        out, _ = run.build(FakeHttp(), "tok", "key", today=date(2026, 11, 22), sleep=0)
        vie = [d for d in out["deals"] if d["id"].startswith("auto-p-VIE")][0]
        self.assertEqual(vie["total"], 190 + 110)
        self.assertEqual(vie["typical"], 310 + 145)
        self.assertEqual(vie["status"], "hit")         # 1 - 300/455 = 34 %
        self.assertIn("lyf Schönbrunn", vie["hotel"])
        nyc = [d for d in out["deals"] if d["id"].startswith("auto-p-NYC")][0]
        self.assertEqual(nyc["status"], "check")       # one fare only: no baseline yet

    def test_history_baseline(self):
        hist = {"K": [{"d": f"2026-11-{i:02d}", "min": 100, "med": 200} for i in range(1, 7)]}
        self.assertEqual(run.baseline(hist, "K", []), 200)


if __name__ == "__main__":
    unittest.main()
