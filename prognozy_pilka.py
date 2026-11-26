#!/usr/bin/env python3
"""
Prognozy piłkarskie: bramki, kartki, rożne -> najbardziej prawdopodobne typy.
Dane: football-data.co.uk (darmowe, BEZ klucza).

Użycie:
  python prognozy_pilka.py                      # top 10 lig, najbliższe mecze
  python prognozy_pilka.py --ligi E0,SP1,D1     # wybrane ligi
  python prognozy_pilka.py --min-proc 70        # tylko typy >= 70%
  python prognozy_pilka.py --csv prognozy.csv   # zapis do Excela
  python prognozy_pilka.py --html prognozy.html # strona z typami + forma + H2H
  python prognozy_pilka.py --mecz E0 "Arsenal" "Chelsea"   # jeden mecz

Kody lig: E0 Premier League, E1 Championship, SP1 LaLiga, I1 Serie A,
D1 Bundesliga, F1 Ligue 1, N1 Eredivisie, P1 Portugalia, T1 Turcja,
B1 Belgia, SC0 Szkocja, G1 Grecja.

Jak liczy:
  * Bramki: model Poissona (siła ataku i obrony u siebie/na wyjeździe).
  * Kartki i rożne: średnie drużyn (u siebie/na wyjeździe) + rozkład Poissona.
  * Bieżący sezon waży 1.0, poprzedni 0.4 (mniej losowości na starcie sezonu).
  * H2H: bezpośrednie mecze z 5 ostatnich sezonów korygują gole/kartki/rożne
    (maks. 30% wpływu, im więcej meczów H2H, tym większy).
  * Jeśli plik terminarza ma kursy bukmacherów, pokazuje VALUE (nasze % x kurs > 1.05).

To statystyka, nie pewniak: kontuzje, składy i motywacja nie są w modelu.
"""
import argparse
import json
import csv
import os
import io
import math
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = "https://www.football-data.co.uk"
STAN_DANYCH = {}   # co się pobrało - pokazywane w zakładce "Stan danych"
LOG = []


def print(*args, **kw):       # zapisuje komunikaty, żeby pokazać problemy na stronie
    import builtins
    LOG.append(" ".join(str(x) for x in args))
    builtins.print(*args, **kw)
DOMYSLNE_LIGI = "E0,E1,SP1,I1,D1,F1,N1,P1,B1,T1,SC0,POL,ARG,BRA,LM,LE,LK"
NOWE_LIGI = {"ARG": "Argentina", "BRA": "Brazil", "POL": "Poland", "AUT": "Austria", "DNK": "Denmark",
             "SWZ": "Switzerland", "NOR": "Norway", "SWE": "Sweden", "ROU": "Romania"}           # pliki /new/ (bez kartek i rożnych)
PUCHARY = {"LM": "champions-league", "LE": "europa-league", "LK": "conference-league"}
# zapasowy terminarz lig z fixturedownload.com (cały sezon rozpisany z góry)
TERMINARZ_FD = {"E0": "epl", "E1": "championship", "SP1": "la-liga", "I1": "serie-a", "D1": "bundesliga",
                "F1": "ligue-1", "N1": "eredivisie", "P1": "primeira-liga", "T1": "super-lig",
                "SC0": "scottish-premiership"}
LIGI_DLA_PUCHAROW = ["E0", "E1", "SP1", "I1", "D1", "F1", "N1", "P1", "B1", "T1", "SC0", "G1", "POL",
                     "AUT", "DNK", "SWZ", "NOR", "SWE", "ROU"]
# przybliżona siła lig (do meczów pucharowych między ligami)
SILA = {"E0": 1.0, "SP1": 0.93, "I1": 0.92, "D1": 0.92, "F1": 0.88, "P1": 0.80, "N1": 0.80,
        "E1": 0.74, "B1": 0.76, "T1": 0.76, "SC0": 0.68, "G1": 0.70, "ARG": 0.72, "BRA": 0.76, "POL": 0.66,
        "AUT": 0.70, "DNK": 0.68, "SWZ": 0.68, "NOR": 0.64, "SWE": 0.62, "ROU": 0.60}
SILA_POTEGA = 2.0
# Ustawienia modelu (nadpisywane parametrami). "stary" model = bez ulepszeń, do porównania w backteście.
MODEL = {"polowa": 120, "rho": -0.08, "waga_modelu": 0.5, "sedzia": True, "kalibracja": True}
MODEL_STARY = {"polowa": None, "rho": 0.0, "waga_modelu": 1.0, "sedzia": False, "kalibracja": False}
KALIBRACJA = {}   # kategoria rynku -> [(średnia prognoza, faktycznie weszło), ...]   # jak mocno różnica siły lig wpływa na gole w pucharach
NAZWY = {"E0": "Premier League", "E1": "Championship", "SP1": "LaLiga", "I1": "Serie A",
         "D1": "Bundesliga", "F1": "Ligue 1", "N1": "Eredivisie", "P1": "Portugalia",
         "T1": "Turcja", "B1": "Belgia", "SC0": "Szkocja", "G1": "Grecja",
         "ARG": "Argentyna", "BRA": "Brazylia", "POL": "Ekstraklasa", "AUT": "Austria", "DNK": "Dania", "SWZ": "Szwajcaria",
         "NOR": "Norwegia", "SWE": "Szwecja", "ROU": "Rumunia", "LM": "Liga Mistrzów", "LE": "Liga Europy",
         "LK": "Liga Konferencji"}


# ---------- pobieranie ----------
def pobierz_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tekst = r.read().decode("utf-8-sig", errors="replace")
    return [w for w in csv.DictReader(io.StringIO(tekst)) if w.get("HomeTeam")]


def kody_sezonow(ile=5, dzis=None):
    """Kody sezonów od bieżącego wstecz, np. ['2627','2526',...]."""
    d = dzis or date.today()
    start = d.year if d.month >= 7 else d.year - 1
    return [f"{(start - i) % 100:02d}{(start - i + 1) % 100:02d}" for i in range(ile)]


def data_meczu(w):
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(w.get("Date", ""), fmt)
        except ValueError:
            pass
    return datetime(1900, 1, 1)


KOLUMNY = {"rozne": ("HC", "AC"), "strzaly": ("HS", "AS"), "celne": ("HST", "AST"),
           "kartki": (("HY", "HR"), ("AY", "AR")), "gole": ("FTHG", "FTAG")}


def wart_strona(w, co, gosp):
    kol = KOLUMNY[co][0 if gosp else 1]
    if co == "kartki":
        y, r = num(w, kol[0]), num(w, kol[1])
        return None if y is None else y + (r or 0)
    return num(w, kol)


def wart_druz(w, druz, co):
    """Statystyka danej drużyny w meczu w (niezależnie, czy grała u siebie)."""
    return wart_strona(w, co, w["HomeTeam"].strip() == druz)


def wart_mecz(w, co):
    a, b = wart_strona(w, co, True), wart_strona(w, co, False)
    return None if a is None or b is None else a + b


def num(w, k):
    try:
        return float(w[k])
    except (KeyError, TypeError, ValueError):
        return None


# ---------- formaty danych ----------
def z_nowego_formatu(w, fixtures=False):
    """Pliki /new/ (Argentyna, Brazylia): Home/Away/HG/AG -> format główny."""
    h, g = (w.get("Home") or "").strip(), (w.get("Away") or "").strip()
    if not h or not g:
        return None
    n = dict(w)
    n.update({"HomeTeam": h, "AwayTeam": g, "FTHG": w.get("HG", ""), "FTAG": w.get("AG", "")})
    if not fixtures and (num(n, "FTHG") is None or num(n, "FTAG") is None):
        return None
    return n


def godzina_uk_na_pl(w):
    """Godziny football-data są czasu brytyjskiego; PL = UK + 1 h."""
    w = dict(w)
    try:
        d = datetime.strptime(f'{w["Date"]} {w["Time"]}', "%d/%m/%Y %H:%M") + timedelta(hours=1)
        w["Date"], w["Time"] = d.strftime("%d/%m/%Y"), d.strftime("%H:%M")
    except (KeyError, ValueError):
        try:
            w["Date"] = datetime.strptime(w.get("Date", ""), "%d/%m/%y").strftime("%d/%m/%Y")
        except ValueError:
            pass
    return w


def terminarz_pucharu(laduj_tekst, slug, kod):
    """Terminarz z fixturedownload.com (czas UTC -> lokalny)."""
    wynik = []
    t = laduj_tekst(f"https://fixturedownload.com/feed/json/{slug}", f"{slug}.json")
    if t and t.lstrip().startswith("["):
        import json
        for m in json.loads(t):
            if m.get("HomeTeamScore") is not None:
                continue
            try:
                d = datetime.strptime(m["DateUtc"].replace("Z", "").strip()[:16], "%Y-%m-%d %H:%M")
            except (KeyError, ValueError):
                continue
            wynik.append((d, m.get("HomeTeam", ""), m.get("AwayTeam", "")))
    else:
        t = laduj_tekst(f"https://fixturedownload.com/download/csv/{slug}", f"{slug}.csv")
        for m in csv.DictReader(io.StringIO(t or "")):
            if (m.get("Result") or "").strip():
                continue
            try:
                d = datetime.strptime(m["Date"].strip(), "%d/%m/%Y %H:%M")
            except (KeyError, ValueError):
                continue
            wynik.append((d, m.get("Home Team", ""), m.get("Away Team", "")))
    out = []
    for d, h, g in wynik:
        if not h or not g or "TBD" in h or "TBD" in g or "Winner" in h:
            continue
        lok = d.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        out.append({"Div": kod, "Date": lok.strftime("%d/%m/%Y"), "Time": lok.strftime("%H:%M"),
                    "HomeTeam": h, "AwayTeam": g})
    return out


# ---------- dopasowanie nazw drużyn (puchary) ----------
ALIASY = {
    "manchester city": "man city", "manchester united": "man united", "manchester utd": "man united",
    "paris saint germain": "paris sg", "psg": "paris sg",
    "bayern munchen": "bayern munich", "bayern": "bayern munich",
    "borussia dortmund": "dortmund", "bayer 04 leverkusen": "leverkusen", "bayer leverkusen": "leverkusen",
    "internazionale": "inter", "inter milan": "inter", "atletico de madrid": "ath madrid",
    "atletico madrid": "ath madrid", "athletic": "ath bilbao", "athletic bilbao": "ath bilbao",
    "real betis": "betis", "real sociedad": "sociedad", "sporting": "sp lisbon",
    "sporting lisbon": "sp lisbon", "braga": "sp braga", "psv": "psv eindhoven",
    "eintracht frankfurt": "ein frankfurt", "frankfurt": "ein frankfurt", "leipzig": "rb leipzig",
    "nottingham forest": "nott m forest", "tottenham hotspur": "tottenham", "spurs": "tottenham",
    "newcastle united": "newcastle", "west ham united": "west ham", "wolverhampton": "wolves",
    "olympique de marseille": "marseille", "olympique lyonnais": "lyon", "stade rennais": "rennes",
    "union saint gilloise": "st gilloise", "union sg": "st gilloise", "fenerbahce": "fenerbahce",
    "olympiakos": "olympiakos", "olympiacos": "olympiakos", "paok": "paok", "celta vigo": "celta",
    "gladbach": "m gladbach", "borussia monchengladbach": "m gladbach", "mainz 05": "mainz",
    "red bull salzburg": "salzburg", "rb salzburg": "salzburg", "rapid wien": "rapid vienna",
    "kobenhavn": "copenhagen", "fc copenhagen": "copenhagen", "bodo glimt": "bodo glimt",
    "wolverhampton wanderers": "wolves", "queens park rangers": "qpr", "sheffield wednesday": "sheffield weds",
    "west bromwich albion": "west brom", "brighton and hove albion": "brighton", "brighton hove albion": "brighton",
    "espanyol": "espanol", "athletic club": "ath bilbao", "deportivo alaves": "alaves",
    "legia warsaw": "legia", "legia warszawa": "legia", "lech poznan": "lech", "rakow czestochowa": "rakow",
    "jagiellonia bialystok": "jagiellonia", "pogon szczecin": "pogon", "gornik zabrze": "gornik",
    "az": "az alkmaar", "istanbul basaksehir": "buyuksehyr", "basaksehir": "buyuksehyr",
    "fatih karagumruk": "karagumruk", "kasimpasa": "kasimpasa", "caykur rizespor": "rizespor",
    "gaziantep": "gaziantep", "goztepe": "goztepe", "corum": "corum", "eyupspor": "eyupspor",
    "samsunspor": "samsunspor", "kocaelispor": "kocaelispor", "genclerbirligi": "genclerbirligi", "twente": "twente", "utrecht": "utrecht", "genk": "genk",
}
_SMIECI = {"fc", "cf", "ac", "as", "sc", "sl", "ssc", "ss", "afc", "rc", "losc", "ogc", "krc", "kaa",
           "rsc", "vfb", "vfl", "tsg", "sv", "1907", "1909", "1899", "cp", "sk", "fk", "nk", "gnk", "rcd", "cd",
           "ud", "sd", "acf", "bk", "if", "aik", "ca", "us", "osc", "kv", "kvc", "kas", "royal", "club"}


def normalizuj(nazwa):
    import unicodedata
    t = unicodedata.normalize("NFKD", nazwa).encode("ascii", "ignore").decode().lower()
    t = "".join(c if c.isalnum() else " " for c in t)
    slowa = [x for x in t.split() if x not in _SMIECI]
    t = " ".join(slowa) or t.strip()
    return ALIASY.get(t, t)


def znajdz(nazwa, indeks):
    import difflib
    n = normalizuj(nazwa)
    if n in indeks:
        return n
    for k in indeks:                      # jedna nazwa zawiera drugą
        if len(k) >= 4 and (k in n.split() or n in k.split() or k == n.replace(" ", "")):
            return k
    m = difflib.get_close_matches(n, list(indeks), n=1, cutoff=0.78)
    return m[0] if m else None


# ---------- statystyki ----------
class Liga:
    def __init__(self, mecze_wagi, ref=None, polowa=None):
        # mecze_wagi: lista (wiersz, waga); ref+polowa: starsze mecze ważą mniej (waga /2 co `polowa` dni)
        self.dom, self.wyj = {}, {}  # druzyna -> dict sum
        self.suma = {"w": 0}
        self.sedz = {}       # sędzia -> [suma kartek, waga]
        self.wszystkie = []  # do H2H
        for w, waga in mecze_wagi:
            fthg, ftag = num(w, "FTHG"), num(w, "FTAG")
            if fthg is None or ftag is None:
                continue
            h, a = w["HomeTeam"].strip(), w["AwayTeam"].strip()
            self.wszystkie.append(w)
            if waga <= 0:
                continue
            if polowa and ref:
                wiek = max(0, (ref - data_meczu(w)).days)
                waga *= 0.5 ** (wiek / polowa)
            kart_h = sum(x for x in (num(w, "HY"), num(w, "HR")) if x is not None)
            kart_a = sum(x for x in (num(w, "AY"), num(w, "AR")) if x is not None)
            ma_kartki = num(w, "HY") is not None
            hc, ac = num(w, "HC"), num(w, "AC")
            sedzia = (w.get("Referee") or "").strip()
            if sedzia and ma_kartki:
                sd = self.sedz.setdefault(sedzia, [0.0, 0.0])
                sd[0] += (kart_h + kart_a) * waga
                sd[1] += waga
            rek = {"gz_h": fthg, "gs_h": ftag, "k_h": kart_h if ma_kartki else None,
                   "k_a": kart_a if ma_kartki else None, "r_h": hc, "r_a": ac,
                   "s_h": num(w, "HS"), "s_a": num(w, "AS"), "c_h": num(w, "HST"), "c_a": num(w, "AST"),
                   "ma_k": ma_kartki}
            for slownik, druz in ((self.dom, h), (self.wyj, a)):
                s = slownik.setdefault(druz, {})
                s["w"] = s.get("w", 0) + waga
                s["mecze"] = s.get("mecze", 0) + 1
                for k, v in rek.items():
                    if k == "ma_k" or v is None:
                        continue
                    s[k] = s.get(k, 0) + v * waga
                    s["n_" + k] = s.get("n_" + k, 0) + waga
            for k, v in rek.items():
                if k == "ma_k" or v is None:
                    continue
                self.suma[k] = self.suma.get(k, 0) + v * waga
                self.suma["n_" + k] = self.suma.get("n_" + k, 0) + waga
            self.suma["w"] += waga

    def sr_ligi(self, k):
        n = self.suma.get("n_" + k, 0)
        return self.suma[k] / n if n else None

    @staticmethod
    def sr(s, k):
        n = s.get("n_" + k, 0) if s else 0
        return s[k] / n if n else None

    def prognoza(self, h, a, liga_g=None, sila_h=1.0, sila_g=1.0, sedzia=None):
        """liga_g: liga gościa (mecze pucharowe); sila_*: siła lig."""
        if liga_g is self:
            liga_g = None          # puchar, ale obie drużyny z tej samej ligi -> zwykły model + H2H
        lg = liga_g or self
        sh, sa = self.dom.get(h), lg.wyj.get(a)
        if not sh or not sa or sh.get("mecze", 0) < 3 or sa.get("mecze", 0) < 3:
            return None
        L1h, L1a = self.sr_ligi("gz_h"), self.sr_ligi("gs_h")
        L2h, L2a = lg.sr_ligi("gz_h"), lg.sr_ligi("gs_h")
        K = 4.0   # "wirtualne" mecze ze średnią ligi: mniej przesady przy małej liczbie meczów (beniaminki)

        def srs(st, key, srednia):
            n = st.get("n_" + key, 0)
            return (st.get(key, 0) + srednia * K) / (n + K) if srednia is not None else self.sr(st, key)
        atk_h = srs(sh, "gz_h", L1h) / L1h
        obr_h = srs(sh, "gs_h", L1a) / L1a
        atk_a = srs(sa, "gs_h", L2a) / L2a   # gole strzelone przez gościa
        obr_a = srs(sa, "gz_h", L2h) / L2h   # gole stracone przez gościa
        ratio = (sila_h / sila_g) ** SILA_POTEGA
        lam_h = atk_h * obr_a * (L1h + L2h) / 2 * ratio
        lam_a = atk_a * obr_h * (L1a + L2a) / 2 / ratio
        wynik = {"lam_h": lam_h, "lam_a": lam_a}

        # kartki/rożne każdej drużyny: (jej średnia + ile pozwala rywal) / 2
        def ocz(k_dom, k_wyj):
            if None in (self.sr(sh, k_dom), self.sr(sa, k_dom), self.sr(sh, k_wyj), self.sr(sa, k_wyj)):
                return None, None
            a1, a2 = srs(sh, k_dom, self.sr_ligi(k_dom)), srs(sa, k_dom, lg.sr_ligi(k_dom))
            b1, b2 = srs(sh, k_wyj, self.sr_ligi(k_wyj)), srs(sa, k_wyj, lg.sr_ligi(k_wyj))
            return (a1 + a2) / 2, (b1 + b2) / 2
        wynik["kartki_h"], wynik["kartki_a"] = ocz("k_h", "k_a")
        wynik["rozne_h"], wynik["rozne_a"] = ocz("r_h", "r_a")
        wynik["strzaly_h"], wynik["strzaly_a"] = ocz("s_h", "s_a")
        wynik["celne_h"], wynik["celne_a"] = ocz("c_h", "c_a")
        wynik["kartki"] = wynik["kartki_h"] + wynik["kartki_a"] if wynik["kartki_h"] is not None else None
        wynik["rozne"] = wynik["rozne_h"] + wynik["rozne_a"] if wynik["rozne_h"] is not None else None
        if sedzia and MODEL["sedzia"] and sedzia in self.sedz and wynik["kartki"]:
            s_k, n_k = self.sedz[sedzia]
            Lk = (self.sr_ligi("k_h") or 0) + (self.sr_ligi("k_a") or 0)
            if Lk:
                f = min(1.5, max(0.67, ((s_k + Lk * 5) / (n_k + 5)) / Lk))
                for k in ("kartki_h", "kartki_a"):
                    wynik[k] *= f
                wynik["kartki"] *= f
                wynik["sedzia"] = (sedzia, f)
        if liga_g:
            # puchary: strzały i rożne też zależą od siły lig (łagodniej niż gole)
            # strzały i celne tak mocno jak gole (słabszy rywal z silniejszej ligi strzela dużo mniej),
            # rożne łagodniej
            for co, pot in (("strzaly", SILA_POTEGA), ("celne", SILA_POTEGA), ("rozne", SILA_POTEGA / 2)):
                r_s = (sila_h / sila_g) ** pot
                if wynik.get(co + "_h") is not None:
                    wynik[co + "_h"] *= r_s
                    wynik[co + "_a"] /= r_s
            if wynik.get("rozne_h") is not None:
                wynik["rozne"] = wynik["rozne_h"] + wynik["rozne_a"]
            wynik["h2h"] = []
            return wynik

        # ---- H2H ----
        h2h = [w for w in self.wszystkie
               if {w["HomeTeam"].strip(), w["AwayTeam"].strip()} == {h, a}]
        h2h.sort(key=data_meczu, reverse=True)
        wynik["h2h"] = h2h
        n = len(h2h)
        if n:
            wh = 0.3 * min(n, 6) / 6
            gole = [num(w, "FTHG") + num(w, "FTAG") for w in h2h]
            sr_g = sum(gole) / n
            model_g = lam_h + lam_a
            f = (1 - wh) + wh * (sr_g / model_g) if model_g else 1
            wynik["lam_h"], wynik["lam_a"] = lam_h * f, lam_a * f
            # statystyki każdej drużyny z H2H osobno (np. rożne Arsenalu w meczach z Liverpoolem)
            sr_h2h = {}
            for co in ("kartki", "rozne", "strzaly", "celne"):
                for druz, kl in ((h, co + "_h"), (a, co + "_a")):
                    v = [x for x in (wart_druz(w, druz, co) for w in h2h) if x is not None]
                    if v and wynik.get(kl) is not None:
                        sr_h2h[kl] = sum(v) / len(v)
                        wynik[kl] = (1 - wh) * wynik[kl] + wh * sr_h2h[kl]
            for co in ("kartki", "rozne"):
                if wynik.get(co + "_h") is not None:
                    wynik[co] = wynik[co + "_h"] + wynik[co + "_a"]
            kart = [x for x in (wart_mecz(w, "kartki") for w in h2h) if x is not None]
            rz = [x for x in (wart_mecz(w, "rozne") for w in h2h) if x is not None]
            wyg = sum(1 for w in h2h if (w["HomeTeam"].strip() == h and num(w,"FTHG") > num(w,"FTAG"))
                      or (w["AwayTeam"].strip() == h and num(w, "FTAG") > num(w, "FTHG")))
            rem = sum(1 for w in h2h if num(w, "FTHG") == num(w, "FTAG"))
            btts = sum(1 for w in h2h if num(w, "FTHG") > 0 and num(w, "FTAG") > 0)
            wynik["h2h_txt"] = (f"   H2H ({n} meczów): {h} {wyg}W-{rem}R-{n-wyg-rem}P | śr. goli {sr_g:.1f}"
                                f" | obie strzeliły {btts}/{n}"
                                + (f" | śr. kartek {sum(kart)/len(kart):.1f}" if kart else "")
                                + (f" | śr. rożnych {sum(rz)/len(rz):.1f}" if rz else "")
                                + "\n   Ostatnie: " + ", ".join(
                                    f'{w["HomeTeam"].strip()} {int(num(w,"FTHG"))}:{int(num(w,"FTAG"))} {w["AwayTeam"].strip()}'
                                    for w in h2h[:5]))
        return wynik


# ---------- prawdopodobieństwa ----------
def pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def p_powyzej(linia, lam):
    return 1 - sum(pois(k, lam) for k in range(int(math.floor(linia)) + 1))


def typy(pr):
    lh, la = pr["lam_h"], pr["lam_a"]
    M = 10
    macierz = [[pois(i, lh) * pois(j, la) for j in range(M)] for i in range(M)]
    rho = MODEL["rho"]
    if rho:   # poprawka Dixona-Colesa: 0:0, 1:0, 0:1, 1:1 (prosty Poisson zaniża remisy)
        macierz[0][0] *= 1 - lh * la * rho
        macierz[0][1] *= 1 + lh * rho
        macierz[1][0] *= 1 + la * rho
        macierz[1][1] *= 1 - rho
        suma = sum(map(sum, macierz))
        macierz = [[v / suma for v in r] for r in macierz]
    p1 = sum(macierz[i][j] for i in range(M) for j in range(M) if i > j)
    px = sum(macierz[i][i] for i in range(M))
    p2 = 1 - p1 - px
    btts = sum(macierz[i][j] for i in range(1, M) for j in range(1, M))
    lt = lh + la
    t = [
        ("1 (gospodarz)", p1, "H", ("res", "W")), ("X (remis)", px, "D", ("res", "D")),
        ("2 (gość)", p2, "A", ("res", "L")),
        ("1X", p1 + px, None, ("res", "WD")), ("X2", px + p2, None, ("res", "DL")),
        ("12", p1 + p2, None, ("res", "WL")),
        ("Obie strzelą: TAK", btts, None, ("btts", True)), ("Obie strzelą: NIE", 1 - btts, None, ("btts", False)),
    ]
    for linia in (0.5, 1.5, 2.5, 3.5):
        po = p_powyzej(linia, lt)
        t += [(f"Gole powyżej {linia}", po, f">{linia:g}", ("gole", ">", linia)),
              (f"Gole poniżej {linia}", 1 - po, f"<{linia:g}", ("gole", "<", linia))]
    if pr.get("kartki"):
        for linia in (2.5, 3.5, 4.5, 5.5):
            po = p_powyzej(linia, pr["kartki"])
            t += [(f"Kartki powyżej {linia}", po, None, ("kartki", ">", linia)),
                  (f"Kartki poniżej {linia}", 1 - po, None, ("kartki", "<", linia))]
    if pr.get("rozne"):
        for linia in (7.5, 8.5, 9.5, 10.5, 11.5):
            po = p_powyzej(linia, pr["rozne"])
            t += [(f"Rożne powyżej {linia}", po, None, ("rozne", ">", linia)),
                  (f"Rożne poniżej {linia}", 1 - po, None, ("rozne", "<", linia))]
    if pr.get("celne_h") is not None:
        lc = pr["celne_h"] + pr["celne_a"]
        for linia in (6.5, 7.5, 8.5, 9.5, 10.5):
            po = p_powyzej(linia, lc)
            t += [(f"Strzały celne powyżej {linia}", po, None, ("celne", ">", linia)),
                  (f"Strzały celne poniżej {linia}", 1 - po, None, ("celne", "<", linia))]
    # statystyki pojedynczej drużyny
    for strona, kto in (("H", "Gospodarz"), ("A", "Gość")):
        lam = lh if strona == "H" else la
        for linia in (0.5, 1.5, 2.5):
            po = p_powyzej(linia, lam)
            t += [(f"{kto}: gole powyżej {linia}", po, None, ("dr", "gole", ">", linia, strona)),
                  (f"{kto}: gole poniżej {linia}", 1 - po, None, ("dr", "gole", "<", linia, strona))]
        rz = pr.get("rozne_h" if strona == "H" else "rozne_a")
        if rz:
            for linia in (2.5, 3.5, 4.5, 5.5, 6.5):
                po = p_powyzej(linia, rz)
                t += [(f"{kto}: rożne powyżej {linia}", po, None, ("dr", "rozne", ">", linia, strona)),
                      (f"{kto}: rożne poniżej {linia}", 1 - po, None, ("dr", "rozne", "<", linia, strona))]
        for co, nazwa_co, linie in (("strzaly", "strzały", (7.5, 9.5, 11.5, 13.5, 15.5)),
                                    ("celne", "strzały celne", (1.5, 2.5, 3.5, 4.5, 5.5, 6.5))):
            lam_s = pr.get(f"{co}_h" if strona == "H" else f"{co}_a")
            if lam_s:
                for linia in linie:
                    po = p_powyzej(linia, lam_s)
                    t += [(f"{kto}: {nazwa_co} powyżej {linia}", po, None, ("dr", co, ">", linia, strona)),
                          (f"{kto}: {nazwa_co} poniżej {linia}", 1 - po, None, ("dr", co, "<", linia, strona))]
        kt = pr.get("kartki_h" if strona == "H" else "kartki_a")
        if kt:
            for linia in (0.5, 1.5, 2.5, 3.5):
                po = p_powyzej(linia, kt)
                t += [(f"{kto}: kartki powyżej {linia}", po, None, ("dr", "kartki", ">", linia, strona)),
                      (f"{kto}: kartki poniżej {linia}", 1 - po, None, ("dr", "kartki", "<", linia, strona))]
    wyniki = sorted(((macierz[i][j], f"{i}:{j}") for i in range(6) for j in range(6)), reverse=True)[:3]
    return t, wyniki


# ---------- skuteczność typu w historii ----------
def wartosc(w, co):
    return wart_mecz(w, co)


def trafia(w, druzyna, spec, odwroc=False):
    """Czy typ 'wszedłby' w meczu w? Wynik 1X2 z perspektywy 'druzyna'."""
    gh, ga = num(w, "FTHG"), num(w, "FTAG")
    if gh is None or ga is None:
        return None
    if spec[0] == "res":
        moje, ich = (gh, ga) if w["HomeTeam"].strip() == druzyna else (ga, gh)
        r = "W" if moje > ich else "D" if moje == ich else "L"
        cel = spec[1]
        if odwroc:  # forma gościa: "1" = gość przegrywa
            cel = cel.translate(str.maketrans("WL", "LW"))
        return r in cel
    if spec[0] == "btts":
        return (gh > 0 and ga > 0) == spec[1]
    if spec[0] == "dr":
        _, co, op, linia, strona = spec
        wlasne = (strona == "H") != odwroc        # statystyka tej drużyny czy jej rywala
        jest_gosp = w["HomeTeam"].strip() == druzyna
        bierz_gosp = jest_gosp == wlasne
        v = wart_strona(w, co, bierz_gosp)
        if v is None:
            return None
        return v > linia if op == ">" else v < linia
    v = wartosc(w, spec[0])
    if v is None:
        return None
    return v > spec[2] if spec[1] == ">" else v < spec[2]


def skutecznosc(mecze, druzyna, spec, odwroc=False):
    wyn = [trafia(w, druzyna, spec, odwroc) for w in mecze]
    wyn = [x for x in wyn if x is not None]
    return (sum(wyn), len(wyn)) if wyn else (0, 0)


def wiersz_lab(w, druz):
    """Jeden mecz do „Sprawdź własną linię”: [data, rywal, u siebie, gole swoje/rywala, rożne, kartki, strzały, celne]."""
    gosp = w["HomeTeam"].strip() == druz
    rywal = (w["AwayTeam"] if gosp else w["HomeTeam"]).strip()
    r = [data_meczu(w).strftime("%d.%m.%y"), rywal, 1 if gosp else 0]
    for co in ("gole", "rozne", "kartki", "strzaly", "celne"):
        for strona in (gosp, not gosp):
            v = wart_strona(w, co, strona)
            r.append(None if v is None else int(v))
    return r


def ostatnie_mecze(liga, druzyna, n):
    if liga is None:
        return []
    m = [w for w in liga.wszystkie if druzyna in (w["HomeTeam"].strip(), w["AwayTeam"].strip())]
    m.sort(key=data_meczu, reverse=True)
    return m[:n]


def rynek_kat(spec):
    co = spec[1] if spec[0] == "dr" else spec[0]
    return {"res": "wynik", "btts": "bramki", "gole": "bramki", "rozne": "rozne", "kartki": "kartki",
            "strzaly": "strzaly", "celne": "strzaly"}.get(co, "inne")


_PMF = {}


def _pmf(lam):
    k = round(lam, 2)
    if k not in _PMF:
        _PMF[k] = [pois(i, k) for i in range(11)]
    return _PMF[k]


def lambdy_rynku(kursy):
    """Gole oczekiwane, które najlepiej pasują do kursów bukmacherów (1X2 + gole 2.5, bez marży)."""
    if not all(k in kursy for k in "HDA"):
        return None
    inv = [1 / kursy[k][0] for k in "HDA"]
    cel = [x / sum(inv) for x in inv]
    cel_o = None
    if ">2.5" in kursy and "<2.5" in kursy:
        o, u = 1 / kursy[">2.5"][0], 1 / kursy["<2.5"][0]
        cel_o = o / (o + u)

    def blad(lh, la):
        ph, pa = _pmf(lh), _pmf(la)
        cum, c = [], 0.0
        for x in pa:
            c += x
            cum.append(c)
        p1 = sum(ph[i] * cum[i - 1] for i in range(1, 11))
        px = sum(ph[i] * pa[i] for i in range(11))
        e = (p1 - cel[0]) ** 2 + (px - cel[1]) ** 2 + (1 - p1 - px - cel[2]) ** 2
        if cel_o is not None:
            t = lh + la
            e += (1 - math.exp(-t) * (1 + t + t * t / 2) - cel_o) ** 2
        return e
    best = min(((blad(x / 10, y / 10), x / 10, y / 10) for x in range(2, 39) for y in range(2, 39)))
    _, bx, by = best
    best = min(((blad(bx + i / 50, by + j / 50), bx + i / 50, by + j / 50)
                for i in range(-5, 6) for j in range(-5, 6) if bx + i / 50 > 0.05 and by + j / 50 > 0.05))
    return best[1], best[2]


def mieszaj_z_rynkiem(pr, kursy):
    """Łączy gole oczekiwane modelu z tymi, które wynikają z kursów (rynek wie o składach/kontuzjach)."""
    w = MODEL["waga_modelu"]
    if w >= 1 or not kursy:
        return
    m = lambdy_rynku(kursy)
    if not m:
        return
    pr["lam_model"] = (pr["lam_h"], pr["lam_a"])
    pr["lam_h"] = w * pr["lam_h"] + (1 - w) * m[0]
    pr["lam_a"] = w * pr["lam_a"] + (1 - w) * m[1]


def kalibruj(p, rk):
    """Poprawia prawdopodobieństwo według tego, jak takie typy wchodziły w backteście."""
    if not MODEL["kalibracja"]:
        return p
    pkt = KALIBRACJA.get(rk) or KALIBRACJA.get("wszystkie")
    if not pkt:
        return p
    if p <= pkt[0][0]:
        return min(0.99, max(0.01, p + (pkt[0][1] - pkt[0][0])))
    if p >= pkt[-1][0]:
        return min(0.99, max(0.01, p + (pkt[-1][1] - pkt[-1][0])))
    for (x1, y1), (x2, y2) in zip(pkt, pkt[1:]):
        if x1 <= p <= x2:
            return min(0.99, max(0.01, y1 + (y2 - y1) * (p - x1) / ((x2 - x1) or 1)))
    return p


def licz_kalibracje(pary):
    """pary: (kategoria, prognoza, wszedł). Zwraca słownik punktów kalibracji (wygładzonych)."""
    wynik = {}
    for kat in {k for k, _, _ in pary} | {"wszystkie"}:
        xs = [(p, t) for k, p, t in pary if kat == "wszystkie" or k == kat]
        if len(xs) < 60:
            continue
        pkt = []
        for od in range(0, 100, 10):
            b = [(p, t) for p, t in xs if od / 100 <= p < (od + 10) / 100]
            if len(b) < 15:
                continue
            sr = sum(p for p, _ in b) / len(b)
            fakt = sum(t for _, t in b) / len(b)
            pkt.append((sr, (fakt * len(b) + sr * 30) / (len(b) + 30)))   # wygładzenie małych próbek
        if len(pkt) >= 2:
            wynik[kat] = pkt
    return wynik


def kursy_z_terminarza(w):
    """Średnie kursy rynku z pliku fixtures (jeśli są)."""
    def k(*klucze):
        for kl in klucze:
            v = num(w, kl)
            if v:
                return v
        return None
    return {"H": k("AvgH", "B365H", "AvgCH", "PSH"), "D": k("AvgD", "B365D", "AvgCD", "PSD"),
            "A": k("AvgA", "B365A", "AvgCA", "PSA"),
            ">2.5": k("Avg>2.5", "B365>2.5"), "<2.5": k("Avg<2.5", "B365<2.5")}


# ---------- pamięć podręczna (oszczędza limity API) ----------
class Cache:
    def __init__(self, sciezka):
        import json
        import os
        self.sciezka, self.d = sciezka, {}
        if sciezka and os.path.exists(sciezka):
            try:
                with open(sciezka, encoding="utf-8") as f:
                    self.d = json.load(f)
            except Exception:
                self.d = {}

    def get(self, klucz, ttl_h=None):
        import time
        w = self.d.get(klucz)
        if not w:
            return None
        if ttl_h is not None and time.time() - w["t"] > ttl_h * 3600:
            return None
        return w["v"]

    def set(self, klucz, v):
        import time
        self.d[klucz] = {"t": time.time(), "v": v}

    def zapisz(self):
        import json
        import time
        if not self.sciezka:
            return
        stare = time.time() - 40 * 86400          # sprzątanie po 40 dniach
        self.d = {k: v for k, v in self.d.items() if v["t"] > stare}
        with open(self.sciezka, "w", encoding="utf-8") as f:
            json.dump(self.d, f)


def pobierz_json(url, naglowki=None):
    import json
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(naglowki or {})})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode()), r.headers


# ---------- kursy bukmacherów: The Odds API ----------
ODDS_KLUCZE = {
    "E0": "soccer_epl", "E1": "soccer_efl_champ", "SP1": "soccer_spain_la_liga",
    "I1": "soccer_italy_serie_a", "D1": "soccer_germany_bundesliga", "F1": "soccer_france_ligue_one",
    "N1": "soccer_netherlands_eredivisie", "P1": "soccer_portugal_primeira_liga",
    "B1": "soccer_belgium_first_div", "T1": "soccer_turkey_super_league", "SC0": "soccer_spl",
    "G1": "soccer_greece_super_league", "ARG": "soccer_argentina_primera_division",
    "BRA": "soccer_brazil_campeonato", "POL": "soccer_poland_ekstraklasa", "LM": "soccer_uefa_champs_league",
    "LE": "soccer_uefa_europa_league", "LK": "soccer_uefa_europa_conference_league",
}


def kursy_odds_api(klucz, kody, cache, ttl_h, regiony="eu"):
    """Zwraca {kod_ligi: [ {h, g, data, rynki:{'H':(kurs,buk),...}} ]}. Najlepszy kurs z wielu bukmacherów."""
    wynik = {}
    for kod in kody:
        sk = ODDS_KLUCZE.get(kod)
        if not sk:
            continue
        ck = f"odds|{sk}|{regiony}"
        dane = cache.get(ck, ttl_h)
        if dane is None:
            try:
                url = (f"https://api.the-odds-api.com/v4/sports/{sk}/odds?apiKey={klucz}"
                       f"&regions={regiony}&markets=h2h,totals&oddsFormat=decimal")
                dane, nag = pobierz_json(url)
                cache.set(ck, dane)
                print(f"  Kursy {NAZWY.get(kod, kod)}: {len(dane)} meczów (zostało zapytań: {nag.get('x-requests-remaining')})")
            except Exception as e:
                dane = cache.get(ck)              # limit wyczerpany / błąd -> ostatnie zapamiętane kursy
                print(f"  Kursy {NAZWY.get(kod, kod)}: nie pobrano ({e})"
                      + (" - używam ostatnich zapamiętanych" if dane else ""))
                if not dane:
                    continue
        lista = []
        for ev in dane:
            rynki = {}
            for bm in ev.get("bookmakers", []):
                for m in bm.get("markets", []):
                    for o in m.get("outcomes", []):
                        if m["key"] == "h2h":
                            k = "H" if o["name"] == ev["home_team"] else "A" if o["name"] == ev["away_team"] else "D"
                        elif m["key"] == "totals" and o.get("point") is not None:
                            k = (">" if o["name"] == "Over" else "<") + f'{float(o["point"]):g}'
                        else:
                            continue
                        if k not in rynki or o["price"] > rynki[k][0]:
                            rynki[k] = (o["price"], bm["title"])
            try:
                d = datetime.strptime(ev["commence_time"][:16], "%Y-%m-%dT%H:%M")
                d = d.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
            except Exception:
                d = None
            lista.append({"h": ev["home_team"], "g": ev["away_team"], "data": d, "rynki": rynki})
        wynik[kod] = lista
    return wynik


def dopasuj_kursy(kursy_api, kod, h, g, dzien):
    lista = kursy_api.get(kod) or []
    if not lista:
        return {}
    idx = {}
    for i, e in enumerate(lista):
        idx.setdefault(normalizuj(e["h"]), []).append(i)
    k = znajdz(h, idx)
    for i in idx.get(k, []):
        e = lista[i]
        if znajdz(g, {normalizuj(e["g"]): 1}) and (not e["data"] or not dzien or abs((e["data"].date() - dzien).days) <= 1):
            return e["rynki"]
    return {}


# ---------- zawodnicy: API-Football ----------
AF = "https://v3.football.api-sports.io"


class ApiFootball:
    def __init__(self, klucz, cache):
        self.klucz, self.cache, self.uzyte = klucz, cache, 0
        self.wyczerpane = False
        d = datetime.now().date()
        # darmowy plan: tylko wczoraj / dziś / jutro (zapamiętujemy przesunięcia względem dziś, nie daty)
        przes = cache.get("af|okno_dni") or [-1, 1]
        self.okno = [str(d + timedelta(days=przes[0])), str(d + timedelta(days=przes[1]))]

    def get(self, sciezka, ttl_h=None, zapisz=True):
        dane = self.cache.get("af|" + sciezka, ttl_h) if zapisz else None   # zapisz=False: zawsze świeże dane
        if dane is not None:
            return dane
        if self.wyczerpane:
            return self.cache.get("af|" + sciezka)   # stare dane lepsze niż żadne
        import time
        try:
            j, nag = pobierz_json(AF + sciezka, {"x-apisports-key": self.klucz})
            self.uzyte += 1
            self.pozostalo = nag.get("x-ratelimit-requests-remaining", getattr(self, "pozostalo", None))
            time.sleep(6.5)                       # darmowy plan: max 10 zapytań na minutę
            if j.get("errors"):
                import re
                m = re.search(r"from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", str(j["errors"]))
                if m:
                    self.okno = [m.group(1), m.group(2)]
                    d0 = datetime.now().date()
                    self.cache.set("af|okno_dni", [(date.fromisoformat(m.group(1)) - d0).days,
                                                   (date.fromisoformat(m.group(2)) - d0).days])
                print(f"  API-Football: {j['errors']}")
                if "requests" in str(j["errors"]).lower() or "limit" in str(j["errors"]).lower():
                    self.wyczerpane = True
                    print("  API-Football: dzienny limit wyczerpany - dalej używam zapamiętanych danych")
                return self.cache.get("af|" + sciezka)
            if nag.get("x-ratelimit-requests-remaining") == "0":
                self.wyczerpane = True
            if zapisz:
                self.cache.set("af|" + sciezka, j["response"])
            return j["response"]
        except Exception as e:
            print(f"  API-Football: błąd {e}")
            return self.cache.get("af|" + sciezka)


def w_oknie(af, dzien):
    return af.okno[0] <= dzien <= af.okno[1]


def lista_dnia(af, dzien, ttl_h):
    """Mecze z danego dnia (YYYY-MM-DD) w skróconej postaci - zapamiętywane, żeby nie zużywać limitu.
    Uczy się też nazw drużyn -> identyfikatorów API-Football."""
    k = f"afd|{dzien}"
    dane = af.cache.get(k, ttl_h)
    if dane is not None:
        return dane
    if not w_oknie(af, dzien):          # poza oknem darmowego planu - nie pytamy (każde pytanie zużywa limit)
        return af.cache.get(k) or []
    pelne = af.get(f"/fixtures?date={dzien}&timezone=Europe/Warsaw", zapisz=False)
    if pelne is None:
        return af.cache.get(k) or []
    dane = [{"fixture": {"id": f["fixture"]["id"], "status": {"short": (f["fixture"].get("status") or {}).get("short")}},
             "teams": {"home": {"id": f["teams"]["home"]["id"], "name": f["teams"]["home"]["name"]},
                       "away": {"id": f["teams"]["away"]["id"], "name": f["teams"]["away"]["name"]}},
             "goals": f.get("goals")} for f in pelne]
    af.cache.set(k, dane)
    nazwy = af.cache.get("af|nazwy") or {}
    import re as _re
    mlodziez = _re.compile(r"\b(u1[5-9]|u2[0-3]|ii|b|w|women|reserves?|youth)\b", _re.I)
    for f in dane:
        for s in ("home", "away"):
            nz = f["teams"][s]["name"]
            if mlodziez.search(nz):          # juniorzy, rezerwy, kobiety - pomijamy
                continue
            nazwy[normalizuj(nz)] = [f["teams"][s]["id"], nz]
    af.cache.set("af|nazwy", nazwy)
    return dane


def _zapisz_fx(af, fid, info, gracze_bloki):
    af.cache.set(f"af|fx|{fid}", {
        "gole": info.get("gole"), "teams": info.get("teams"),
        "players": [{"team": p["team"]["id"], "players": [
            {"name": x["player"]["name"], "s": x["statistics"][0] if x.get("statistics") else {}}
            for x in p.get("players", [])]} for p in gracze_bloki]})


def _ma_dane(af, fid):
    c = af.cache.get(f"af|fx|{fid}")
    return bool(c and c.get("players"))


def id_druzyny(af, nazwa):
    import re as _re
    nazwy = {k: v for k, v in (af.cache.get("af|nazwy") or {}).items()
             if not _re.search(r"\b(u1[5-9]|u2[0-3]|ii|b|w|women|reserves?|youth)\b", v[1], _re.I)}
    k = znajdz(nazwa, nazwy)
    return tuple(nazwy[k]) if k else None


def zbieraj_zawodnikow(af, terminarz, wszystkie_druzyny=(), limit=60):
    """Codziennie dopisuje statystyki zawodników z meczów z wczoraj/dziś drużyn, które niedługo grają.
    Darmowy plan widzi tylko wczoraj-jutro, więc baza rośnie z dnia na dzień."""
    dzis = datetime.now().date()
    for n in (1, 0, -1):                                  # wczoraj, dziś, jutro (nazwy drużyn + wyniki)
        lista_dnia(af, str(dzis - timedelta(days=n)), 2 if n >= 0 else 6)
    interes, pilne = set(), set()        # pilne = grają w najbliższych dniach (pobierane najpierw)
    for w in terminarz:
        for nazwa in (w.get("HomeTeam", ""), w.get("AwayTeam", "")):
            t = id_druzyny(af, nazwa)
            if t:
                pilne.add(t[0])
    for nazwa in wszystkie_druzyny:
        t = id_druzyny(af, nazwa)
        if t:
            interes.add(t[0])
    interes |= pilne
    pobrane = 0
    kolejka = []
    for n in (1, 0):
        dzien = str(dzis - timedelta(days=n))
        for f in lista_dnia(af, dzien, 2):
            ids = (f["teams"]["home"]["id"], f["teams"]["away"]["id"])
            if ids[0] in interes or ids[1] in interes:
                kolejka.append((0 if (ids[0] in pilne or ids[1] in pilne) else 1, dzien, f))
    kolejka.sort(key=lambda x: x[0])
    for _, dzien, f in kolejka:
        if True:
            if (f["fixture"]["status"] or {}).get("short") not in ("FT", "AET", "PEN"):
                continue
            fid = f["fixture"]["id"]
            for s in ("home", "away"):
                tid = f["teams"][s]["id"]
                if tid not in interes:
                    continue
                idx = af.cache.get(f"aft|{tid}") or []
                if fid not in [x[1] for x in idx]:
                    idx.append([dzien, fid])
                    af.cache.set(f"aft|{tid}", sorted(idx, reverse=True)[:12])
                if not _ma_dane(af, fid) and pobrane < limit and not af.wyczerpane:
                    r = af.get(f"/fixtures/players?fixture={fid}", zapisz=False)
                    pobrane += 1
                    if r:
                        _zapisz_fx(af, fid, {"gole": f.get("goals"), "teams": f.get("teams")}, r)
    ile = sum(1 for t in pilne if len([1 for _, i in (af.cache.get(f"aft|{t}") or []) if _ma_dane(af, i)]) >= 3)
    print(f"  [zawodnicy] zbieranie: pobrano statystyki {pobrane} meczów; drużyn grających wkrótce z min. 3 meczami "
          f"w bazie: {ile}/{len(pilne)} (baza rośnie codziennie)")


def zawodnicy_meczu(af, data, h, g, lam_h, lam_a, ile=6):
    """Typy na zawodników z serią albo dużą szansą. Zwraca listę typów (jak dla meczu)."""
    import math
    th, tg = id_druzyny(af, h), id_druzyny(af, g)
    if not th or not tg:
        return []
    druzyny = [({"id": th[0], "name": th[1]}, lam_h, "H"), ({"id": tg[0], "name": tg[1]}, lam_a, "A")]
    ostatnie, info = {}, {}
    for t, _, _ in druzyny:
        ostatnie[t["id"]] = [i for _, i in (af.cache.get(f"aft|{t['id']}") or [])][:ile]
    ma_dane = lambda fid: _ma_dane(af, fid)
    for t, _, _ in druzyny:
        n_ok = sum(1 for i in ostatnie.get(t["id"], []) if ma_dane(i))
        if n_ok < 3:
            print(f"   [zawodnicy] {t['name']}: w bazie {n_ok} mecz(e) ze statystykami - potrzeba 3 (zbieram codziennie)")
    dane = []
    for t, lam, strona in druzyny:
        mecze = [af.cache.get(f"af|fx|{i}") for i in ostatnie.get(t["id"], [])]
        mecze = [m for m in mecze if m and m.get("players")]       # od najnowszego
        if len(mecze) < 3:
            continue
        gole_druz = []
        for m in mecze:
            if not m.get("teams") or not m.get("gole"):
                continue
            dom = m["teams"]["home"]["id"] == t["id"]
            gole_druz.append(m["gole"].get("home" if dom else "away") or 0)
        gracze = {}
        for m in mecze:
            for blok in m["players"]:
                if blok["team"] != t["id"]:
                    continue
                for x in blok["players"]:
                    s = x["s"] or {}
                    mins = (s.get("games") or {}).get("minutes") or 0
                    if not mins:
                        continue
                    g_ = (s.get("goals") or {})
                    sh = (s.get("shots") or {})
                    c = (s.get("cards") or {})
                    gracze.setdefault(x["name"], []).append({
                        "min": mins, "gol": g_.get("total") or 0, "as": g_.get("assists") or 0,
                        "sot": sh.get("on") or 0, "sh": sh.get("total") or 0,
                        "kar": (c.get("yellow") or 0) + (c.get("red") or 0),
                        "fc": (s.get("fouls") or {}).get("committed") or 0,
                        "fs": (s.get("fouls") or {}).get("drawn") or 0,
                        "tk": (s.get("tackles") or {}).get("total") or 0})
        dane.append((t["name"], lam, strona, gole_druz, gracze))
    return typy_z_wystepow(h, g, dane)


def typy_z_wystepow(h, g, dane):
    """dane: [(nazwa drużyny, oczekiwane gole, 'H'/'A', gole drużyny w ost. meczach, {zawodnik: [występy od najnowszego]})]"""
    import math
    typy_zaw = []
    for nazwa_druz, lam, strona, gole_druz, gracze in dane:
        t = {"name": nazwa_druz}
        sr_gole = max(0.5, sum(gole_druz) / len(gole_druz)) if gole_druz else max(0.5, lam)
        f_atak = min(1.6, max(0.6, lam / sr_gole))
        for imie, wyst in gracze.items():
            if len(wyst) < 3 or sum(1 for w in wyst[:5] if w["min"] >= 45) < 3:
                continue                       # tylko regularnie grający
            mins = sum(w["min"] for w in wyst)
            oczek_min = min(90, sum(w["min"] for w in wyst[:5]) / len(wyst[:5]))
            rynki = [
                ("strzeli gola", "gol", 1, f_atak, 0.45, 3),
                ("asysta", "as", 1, f_atak, 0.35, 3),
                ("celny strzał 1+", "sot", 1, f_atak ** 0.5, 0.75, 5),
                ("strzały 2+", "sh", 2, f_atak ** 0.5, 0.70, 5),
                ("kartka", "kar", 1, 1.0, 0.40, 3),
                ("faule 1+", "fc", 1, 1.0, 0.80, 5),
                ("faule 2+", "fc", 2, 1.0, 0.60, 4),
                ("faulowany 1+", "fs", 1, 1.0, 0.80, 5),
                ("faulowany 2+", "fs", 2, 1.0, 0.60, 4),
                ("odbiory 2+", "tk", 2, 1.0, 0.65, 4),
            ]
            for nazwa, pole, prog, f_adj, p_min, seria_min in rynki:
                if any(w.get(pole) is None for w in wyst):
                    continue                   # tej statystyki nie ma w źródle (np. odbiory w ESPN)
                suma = sum(w[pole] for w in wyst)
                if pole == "kar":
                    suma, mins_ = suma + 0.18, mins + 90  # wygładzenie małej próbki
                else:
                    mins_ = mins
                lam_g = suma / mins_ * oczek_min * f_adj
                p = 1 - sum(math.exp(-lam_g) * lam_g ** k / math.factorial(k) for k in range(prog))
                traf = [w[pole] >= prog for w in wyst]
                emp = sum(traf) / len(traf)
                p = 0.6 * p + 0.4 * emp
                seria = 0
                for x in traf:
                    if not x:
                        break
                    seria += 1
                if p >= p_min or seria >= seria_min:
                    p = min(p, 0.97)
                    typy_zaw.append({
                        "kat": "zaw", "rk": "zaw", "typ": f"{imie} ({t['name']}): {nazwa}", "p": round(p * 100, 1),
                        "kurs": round(1 / p, 2), "rynek": None, "value": False,
                        "fh": [sum(traf), len(traf)] if strona == "H" else [0, 0],
                        "fg": [sum(traf), len(traf)] if strona == "A" else [0, 0],
                        "h2h": [0, 0], "seria": seria, "spec": ["zaw", imie, pole, prog, strona],
                        "ost": "".join("✓" if x else "✗" for x in traf[:5])})
    typy_zaw.sort(key=lambda x: (-x["seria"], -x["p"]))
    ile_r, wybrane = {}, []
    for x in typy_zaw:                         # max 4 na rynek, żeby np. faule nie zasłoniły strzałów i goli
        r = x["typ"].rsplit(": ", 1)[-1]
        if ile_r.get(r, 0) < 4:
            ile_r[r] = ile_r.get(r, 0) + 1
            wybrane.append(x)
    print(f"   [zawodnicy] {h} - {g}: typów na zawodników {len(wybrane)}")
    return wybrane[:16]


# ---------- BACKTEST ----------
def backtest(a, surowe, sciezka_html):
    """Prognozy dla N ostatnich rozegranych meczów, liczone tylko z danych sprzed meczu.
    Porównuje stary model (bez ulepszeń) z nowym."""
    stawka, podatek = 100.0, 0.88
    kandydaci = []
    for kod, mecze in surowe.items():
        if kod in PUCHARY:
            continue
        for w, waga in mecze:
            if waga >= 1.0 and num(w, "FTHG") is not None and num(w, "FTAG") is not None:
                kandydaci.append((data_meczu(w), kod, w))
    kandydaci.sort(key=lambda x: x[0], reverse=True)

    def symuluj(cfg, limit, tylko=None):
        zapas = dict(MODEL)
        MODEL.update(cfg)
        MODEL["kalibracja"] = False            # w backteście nigdy nie podglądamy przyszłości
        wyniki, cache_lig = [], {}
        try:
            for d, kod, w in kandydaci:
                if len(wyniki) >= limit:
                    break
                if tylko is not None and (kod, d, w["HomeTeam"].strip()) not in tylko:
                    continue
                klucz = (kod, d.date())
                if klucz not in cache_lig:
                    cache_lig[klucz] = Liga([(x, wg) for x, wg in surowe[kod] if data_meczu(x) < d],
                                            d, MODEL["polowa"])
                h, g = w["HomeTeam"].strip(), w["AwayTeam"].strip()
                pr = cache_lig[klucz].prognoza(h, g, sedzia=(w.get("Referee") or "").strip() or None)
                if not pr:
                    continue
                kursy = {k: (v, "") for k, v in kursy_z_terminarza(w).items() if v}
                mieszaj_z_rynkiem(pr, kursy)
                t, _ = typy(pr)
                typy_m = []
                for nazwa, p, kl, spec in t:
                    traf = trafia(w, h, spec)
                    if traf is None:
                        continue
                    nazwa = nazwa.replace("Gospodarz:", f"{h}:", 1).replace("Gość:", f"{g}:", 1)
                    typy_m.append({"typ": nazwa, "p": p, "rk": rynek_kat(spec),
                                   "kurs": kursy[kl][0] if kl and kl in kursy else None, "traf": traf})
                wyniki.append({"data": d, "kod": kod, "liga": NAZWY.get(kod, kod), "h": h, "g": g,
                               "wynik": f'{int(num(w, "FTHG"))}:{int(num(w, "FTAG"))}', "typy": typy_m})
        finally:
            MODEL.clear()
            MODEL.update(zapas)
        wyniki.sort(key=lambda x: x["data"])
        return wyniki

    mn, mx = a.min_proc / 100, a.max_proc / 100

    def rozlicz(zaklady):
        n = len(zaklady)
        tr = sum(1 for _, ok in zaklady if ok)
        zwrot = sum(stawka * k for k, ok in zaklady if ok)
        zwrot_pod = sum(stawka * podatek * k for k, ok in zaklady if ok)
        bil, krzywa = 0.0, []
        for k, ok in zaklady:
            bil += (stawka * podatek * k if ok else 0) - stawka
            krzywa.append(round(bil, 2))
        return {"n": n, "traf": tr, "proc": tr / n * 100 if n else 0, "postawiono": n * stawka,
                "zwrot": zwrot, "zysk": zwrot - n * stawka, "zysk_pod": zwrot_pod - n * stawka,
                "roi": (zwrot - n * stawka) / (n * stawka) * 100 if n else 0,
                "sr_kurs": sum(k for k, _ in zaklady) / n if n else 0, "krzywa": krzywa}

    def ocen(wyniki):
        pojedyncze, wybrane, value, kupony, ogolem = [], [], [], [], []
        for m in wyniki:
            kand = [x for x in m["typy"] if x["kurs"] and mn <= x["p"] <= mx]
            if kand:
                x = max(kand, key=lambda x: x["p"])
                pojedyncze.append((x["kurs"], x["traf"]))
                wybrane.append((m, x))
            for x in m["typy"]:
                if x["kurs"] and a.value_prog < x["p"] * x["kurs"] <= 1.4:
                    value.append((x["kurs"], x["traf"]))
            kand = [x for x in m["typy"] if mn <= x["p"] <= mx]
            if kand:
                ogolem.append(max(kand, key=lambda x: x["p"]))
        for i in range(0, len(wybrane) - 1, 2):
            (_, x1), (_, x2) = wybrane[i], wybrane[i + 1]
            kupony.append((x1["kurs"] * x2["kurs"], x1["traf"] and x2["traf"]))
        wsz = [x for m in wyniki for x in m["typy"]]
        w12 = [x for x in wsz if x["rk"] == "wynik" and x["typ"][:1] in "1X2" and len(x["typ"].split()[0]) == 1]
        brier = lambda xs: sum((x["p"] - x["traf"]) ** 2 for x in xs) / len(xs) if xs else 0
        return {"strategie": [("Najpewniejszy typ z kursem (1 mecz = 1 kupon)", rozlicz(pojedyncze)),
                              ("Value bety (pojedynczo)", rozlicz(value)),
                              ("Kupony po 2 zdarzenia", rozlicz(kupony))],
                "wybrane": wybrane, "ogolem": ogolem, "brier": brier(wsz), "brier12": brier(w12),
                "pary": [(x["rk"], x["p"], x["traf"]) for x in wsz]}

    nowe = symuluj(MODEL, a.backtest)
    if not nowe:
        print("Backtest: brak meczów do sprawdzenia.")
        return None
    stare = symuluj(MODEL_STARY, a.backtest, {(m["kod"], m["data"], m["h"]) for m in nowe})
    on, os_ = ocen(nowe), ocen(stare)

    # kalibracja: uczymy się na starszej połowie, sprawdzamy na nowszej (uczciwie)
    pol = len(nowe) // 2
    pary_stare = [(x["rk"], x["p"], x["traf"]) for m in nowe[:pol] for x in m["typy"]]
    pary_nowe = [(x["rk"], x["p"], x["traf"]) for m in nowe[pol:] for x in m["typy"]]
    kal_test = licz_kalibracje(pary_stare)
    zapas_k = dict(KALIBRACJA)
    KALIBRACJA.clear()
    KALIBRACJA.update(kal_test)
    zapas_m = MODEL["kalibracja"]
    MODEL["kalibracja"] = True
    b_przed = sum((p - t) ** 2 for _, p, t in pary_nowe) / max(1, len(pary_nowe))
    b_po = sum((kalibruj(p, k) - t) ** 2 for k, p, t in pary_nowe) / max(1, len(pary_nowe)) if kal_test else b_przed
    MODEL["kalibracja"] = zapas_m
    # docelowa kalibracja do dzisiejszych prognoz: ze wszystkich meczów backtestu
    KALIBRACJA.clear()
    kal_wlaczona = bool(kal_test) and b_po < b_przed
    if kal_wlaczona:          # używamy kalibracji tylko, jeśli naprawdę pomaga
        KALIBRACJA.update(licz_kalibracje(on["pary"]))
    try:
        with open(os.path.join(os.path.dirname(sciezka_html) or ".", "kalibracja.json"), "w", encoding="utf-8") as f:
            json.dump(KALIBRACJA, f)
    except Exception:
        pass

    od, do = nowe[0]["data"].strftime("%d.%m.%Y"), nowe[-1]["data"].strftime("%d.%m.%Y")
    print(f"\n===== BACKTEST: {len(nowe)} meczów ({od} - {do}), stawka 100 zł =====")
    print(f"Błąd prognoz (Brier, im mniej tym lepiej): stary {os_['brier']:.4f} -> nowy {on['brier']:.4f}")
    for (nazwa, s), (_, s0) in zip(on["strategie"], os_["strategie"]):
        print(f"{nazwa}: {s['n']} kuponów, wygrane {s['proc']:.1f}%, z podatkiem {s['zysk_pod']:+.0f} zł "
              f"(stary model: {s0['proc']:.1f}%, {s0['zysk_pod']:+.0f} zł)")
    print(f"Kalibracja na nowszej połowie: błąd {b_przed:.4f} -> {b_po:.4f}"
          + (" (włączona)" if b_po < b_przed and kal_test else " (wyłączona - nie pomaga)"))

    # ---- raport HTML ----
    def wykres(kr):
        if len(kr) < 2:
            return ""
        lo, hi = min(kr + [0]), max(kr + [0])
        rng = (hi - lo) or 1
        W, H = 600, 140
        pts = " ".join(f"{i / (len(kr) - 1) * W:.1f},{H - (v - lo) / rng * H:.1f}" for i, v in enumerate(kr))
        y0 = H - (0 - lo) / rng * H
        kol = "#00b050" if kr[-1] >= 0 else "#e0533d"
        return (f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" style="width:100%;height:140px"><line x1="0" x2="{W}" '
                f'y1="{y0:.1f}" y2="{y0:.1f}" stroke="#888" stroke-dasharray="4 4"/><polyline fill="none" stroke="{kol}" '
                f'stroke-width="2" vector-effect="non-scaling-stroke" points="{pts}"/></svg>')

    def lepiej(nowa, stara, mniej_lepiej=False):
        ok = nowa < stara if mniej_lepiej else nowa > stara
        return "#00b050" if ok else "#e0533d" if nowa != stara else "inherit"
    por = f"""<tr><td>Błąd prognoz (Brier) &mdash; wszystkie typy</td><td>{os_['brier']:.4f}</td>
<td style="color:{lepiej(on['brier'], os_['brier'], True)}"><b>{on['brier']:.4f}</b></td></tr>
<tr><td>Błąd prognoz (Brier) &mdash; 1X2</td><td>{os_['brier12']:.4f}</td>
<td style="color:{lepiej(on['brier12'], os_['brier12'], True)}"><b>{on['brier12']:.4f}</b></td></tr>"""
    for (nazwa, s), (_, s0) in zip(on["strategie"], os_["strategie"]):
        por += (f"<tr><td>{nazwa}: wygrane</td><td>{s0['proc']:.1f}% ({s0['n']})</td>"
                f"<td style='color:{lepiej(s['proc'], s0['proc'])}'><b>{s['proc']:.1f}% ({s['n']})</b></td></tr>"
                f"<tr><td>{nazwa}: wynik z podatkiem</td><td>{s0['zysk_pod']:+.0f} zł</td>"
                f"<td style='color:{lepiej(s['zysk_pod'], s0['zysk_pod'])}'><b>{s['zysk_pod']:+.0f} zł</b></td></tr>")
    karty = ""
    for nazwa, s in on["strategie"]:
        kol = "#00b050" if s["zysk_pod"] >= 0 else "#e0533d"
        karty += f"""<div class="k"><h3>{nazwa}</h3><div class="g">
<span>Kuponów</span><b>{s['n']}</b><span>Wygrane</span><b>{s['traf']} ({s['proc']:.1f}%)</b>
<span>Średni kurs</span><b>{s['sr_kurs']:.2f}</b><span>Postawiono</span><b>{s['postawiono']:.0f} zł</b>
<span>Wypłaty</span><b>{s['zwrot']:.0f} zł</b><span>Wynik bez podatku</span><b>{s['zysk']:+.0f} zł (ROI {s['roi']:+.1f}%)</b>
<span>Wynik z podatkiem 12%</span><b style="color:{kol}">{s['zysk_pod']:+.0f} zł</b></div>
<p class="m">Bilans z podatkiem, kupon po kuponie:</p>{wykres(s['krzywa'])}</div>"""
    kal_rows = ""
    for od_ in range(50, 100, 10):
        xs = [(p, t) for _, p, t in on["pary"] if od_ / 100 <= p < (od_ + 10) / 100]
        if xs:
            kal_rows += (f"<tr><td>{od_}-{od_ + 10}%</td><td>{len(xs)}</td><td>{sum(p for p, _ in xs) / len(xs) * 100:.1f}%</td>"
                         f"<td><b>{sum(t for _, t in xs) / len(xs) * 100:.1f}%</b></td></tr>")
    wiersze_typ = "".join(
        f"<tr><td>{m['data'].strftime('%d.%m')}</td><td>{m['h']} &ndash; {m['g']} <span class='m'>{m['liga']}</span></td>"
        f"<td>{m['wynik']}</td><td>{x['typ']}</td><td>{x['p'] * 100:.0f}%</td><td>{x['kurs']:.2f}</td>"
        f"<td>{'&#x2705;' if x['traf'] else '&#x274C;'}</td></tr>" for m, x in reversed(on["wybrane"]))
    og = on["ogolem"]
    og_txt = f"{sum(x['traf'] for x in og)}/{len(og)} ({sum(x['traf'] for x in og) / len(og) * 100:.1f}%)" if og else "-"
    zm_kal = (b_przed - b_po) / b_przed * 100 if b_przed else 0
    html = f"""<!DOCTYPE html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest</title><style>
:root{{--bg:#f4f6f9;--card:#fff;--txt:#1d2330;--mut:#6b7385;--lin:#e3e7ee}}
@media(prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1b2029;--txt:#e8ebf1;--mut:#97a0b3;--lin:#2b313d}}}}
body{{margin:0;font:14px/1.45 system-ui,Segoe UI,Arial;background:var(--bg);color:var(--txt)}}main{{max-width:1000px;margin:0 auto;padding:16px}}
.k{{background:var(--card);border:1px solid var(--lin);border-radius:12px;padding:14px 16px;margin-bottom:16px}}
.k h3{{margin:0 0 8px;font-size:16px}}.g{{display:grid;grid-template-columns:1fr auto;gap:4px 16px;max-width:460px}}.g b{{text-align:right}}
.m{{color:var(--mut);font-size:12px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:6px 8px;border-bottom:1px solid var(--lin);text-align:left}}
th{{font-size:11px;color:var(--mut);text-transform:uppercase}}a{{color:#4a7bd0}}</style></head><body><main>
<p><a href="index.html">&larr; Wróć do prognoz</a></p>
<h1 style="font-size:20px">Backtest: {len(nowe)} meczów ({od} &ndash; {do})</h1>
<p class="m">Każda prognoza policzona tylko z danych sprzed meczu. Stawka 100 zł na kupon. Kursy: średnie kursy rynku z football-data
(tylko 1X2 i gole 2.5). Próg prawdopodobieństwa {a.min_proc:.0f}&ndash;{a.max_proc:.0f}%. Wygenerowano {datetime.now():%d.%m.%Y %H:%M}.</p>
<div class="k"><h3>Stary model vs nowy model</h3>
<p class="m">Nowy = mieszanie z kursami rynku (waga modelu {MODEL['waga_modelu']:.0%}), świeższe mecze ważniejsze
(połowa wagi po {MODEL['polowa'] or '-'} dniach), poprawka na remisy (rho {MODEL['rho']}), sędzia przy kartkach.
Zielone = nowy lepszy.</p>
<table><tr><th></th><th>Stary</th><th>Nowy</th></tr>{por}</table>
<p class="m">Błąd prognoz (Brier) to najuczciwsza miara: im mniejszy, tym trafniejsze procenty. Wynik w zł na 200 meczach
w dużej mierze zależy od szczęścia.</p></div>
<div class="k"><h3>Kalibracja procentów</h3>
<p>Sprawdzona uczciwie: nauczona na starszej połowie meczów, sprawdzona na nowszej. Błąd: {b_przed:.4f} &rarr; <b>{b_po:.4f}</b>
({'poprawa' if zm_kal > 0 else 'pogorszenie'} o {abs(zm_kal):.1f}%).</p>
<p class="m">{'Kalibracja pomaga, więc dzisiejsze prognozy na stronie są nią poprawione.' if kal_wlaczona else
  'Kalibracja nie pomogła, więc jest wyłączona: procenty na stronie pochodzą prosto z modelu.'}</p>
<table><tr><th>Przedział</th><th>Typów</th><th>Model mówił średnio</th><th>Weszło naprawdę</th></tr>{kal_rows}</table></div>
<h2 style="font-size:17px">Nowy model &mdash; szczegóły</h2>
{karty}
<div class="k"><h3>Najpewniejszy typ meczu ogółem (też rożne, kartki, strzały)</h3><p>Trafione: <b>{og_txt}</b> <span class="m">(bez kursów, więc bez wyniku w zł)</span></p></div>
<div class="k"><h3>Typy z pierwszej strategii</h3><div style="overflow-x:auto"><table><tr><th>Data</th><th>Mecz</th><th>Wynik</th><th>Typ</th><th>Szansa</th><th>Kurs</th><th></th></tr>{wiersze_typ}</table></div></div>
</main></body></html>"""
    with open(sciezka_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Zapisano raport backtestu: {sciezka_html}")
    return sciezka_html


# ---------- WYNIKI Z ESPN (bez klucza, bez limitów) ----------
ESPN_LIGI = {"E0": "eng.1", "E1": "eng.2", "SP1": "esp.1", "I1": "ita.1", "D1": "ger.1", "F1": "fra.1",
             "N1": "ned.1", "P1": "por.1", "B1": "bel.1", "T1": "tur.1", "SC0": "sco.1", "G1": "gre.1",
             "POL": "pol.1", "ARG": "arg.1", "BRA": "bra.1", "AUT": "aut.1", "DNK": "den.1", "SWZ": "sui.1",
             "NOR": "nor.1", "SWE": "swe.1", "ROU": "rou.1", "LM": "uefa.champions", "LE": "uefa.europa",
             "LK": "uefa.europa.conf"}
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"


def espn_mecze(cache, kod, dzien):
    """Rozegrane mecze ligi z danego dnia (dzień lokalny; pytamy też o dzień wcześniej - ESPN liczy w UTC)."""
    liga = ESPN_LIGI.get(kod)
    if not liga:
        return []
    d = datetime.strptime(dzien, "%d/%m/%Y")
    k = f"espn|{liga}|{d:%Y%m%d}"
    dane = cache.get(k, 1)
    if dane is None:
        zdarzenia = []
        for dd in (d, d - timedelta(days=1)):          # ESPN liczy dni w UTC - sprawdzamy też dzień wcześniej
            try:
                j, _ = pobierz_json(f"{ESPN}/{liga}/scoreboard?dates={dd:%Y%m%d}")
                zdarzenia += j.get("events", [])
            except Exception as e:
                print(f"  ESPN {liga} {dd:%d.%m}: nie pobrano ({e})")
        if not zdarzenia:
            return cache.get(k) or []
        dane = []
        for ev in zdarzenia:
            for c in ev.get("competitions", [])[:1]:
                st = ((c.get("status") or ev.get("status") or {}).get("type") or {})
                druz = {x.get("homeAway"): x for x in c.get("competitors", [])}
                if "home" not in druz or "away" not in druz:
                    continue
                dane.append({"id": ev.get("id"), "h": druz["home"]["team"].get("displayName", ""),
                             "g": druz["away"]["team"].get("displayName", ""),
                             "hg": druz["home"].get("score"), "ag": druz["away"].get("score"),
                             "koniec": st.get("state") == "post" or bool(st.get("completed"))})
        cache.set(k, dane)
    return dane


def espn_statystyki(cache, kod, ev_id):
    k = f"espn|st|{ev_id}"
    st = cache.get(k)
    if st:
        return st
    try:
        j, _ = pobierz_json(f"{ESPN}/{ESPN_LIGI[kod]}/summary?event={ev_id}")
    except Exception:
        return {}
    st = {}
    for t in (j.get("boxscore") or {}).get("teams", []):
        strona = "H" if t.get("homeAway") == "home" else "A"
        for s_ in t.get("statistics", []):
            n = f'{s_.get("name", "")} {s_.get("label", "")}'.lower()
            try:
                v = float(str(s_.get("displayValue", "")).split()[0].replace("%", ""))
            except (ValueError, IndexError):
                continue
            if "corner" in n:
                kl = "C"
            elif "yellow" in n:
                kl = "Y"
            elif "red" in n and "card" in n:
                kl = "R"
            elif "ontarget" in n.replace(" ", "") or "on goal" in n or "on target" in n:
                kl = "ST"
            elif ("shots" in n or "totalshots" in n) and "blocked" not in n and "off" not in n:
                kl = "S"
            else:
                continue
            st[("H" if strona == "H" else "A") + kl] = int(v)
    if st:
        cache.set(k, st)
    return st


def espn_id_rozegranego(cache, e):
    gotowe = [m for m in espn_mecze(cache, e["kod"], e["data"]) if m["koniec"]]
    pasuje = lambda a, b: bool(znajdz(a, {normalizuj(b): 1}))
    pelne = [m for m in gotowe if pasuje(e["hp"], m["h"]) and pasuje(e["gp"], m["g"])] or \
        [m for m in gotowe if pasuje(e["hp"], m["h"]) or pasuje(e["gp"], m["g"])]
    return pelne[0]["id"] if len(pelne) == 1 else None


def rozlicz_zawodnika(cache, e, ev_id, spec):
    """True/False = typ na zawodnika wszedł/nie wszedł; 'zwrot' = nie zagrał; None = brak danych."""
    _, imie, pole, prog, strona = spec
    m = espn_gracze(cache, ESPN_LIGI[e["kod"]], ev_id)
    if not m or len(m) < 2:
        return None
    tid = ((m.get("_info") or {}).get("strony") or {}).get(strona)
    druz = m.get(tid) if tid else None
    kandydaci = [druz] if druz else [v for k, v in m.items() if k != "_info"]
    for d_ in kandydaci:
        w = (d_.get("gracze") or {}).get(imie)
        if w is not None:
            if w.get(pole) is None:
                return None
            return w[pole] >= prog
    return "zwrot"


def wynik_espn(cache, e):
    gotowe = [m for m in espn_mecze(cache, e["kod"], e["data"])
              if m["koniec"] and m["hg"] not in (None, "") and m["ag"] not in (None, "")]
    pasuje = lambda a, b: bool(znajdz(a, {normalizuj(b): 1}))
    pelne = [m for m in gotowe if pasuje(e["hp"], m["h"]) and pasuje(e["gp"], m["g"])]
    if not pelne:      # nazwa jednej drużyny inna - wystarczy druga, jeśli tego dnia grała tylko raz
        pelne = [m for m in gotowe if pasuje(e["hp"], m["h"]) or pasuje(e["gp"], m["g"])]
        pelne = pelne if len(pelne) == 1 else []
    for m in pelne[:1]:
        if True:
            w = {"FTHG": m["hg"], "FTAG": m["ag"], "_espn": m["id"]}
            st = espn_statystyki(cache, e["kod"], m["id"])
            for kl, col in (("C", "C"), ("Y", "Y"), ("R", "R"), ("S", "S"), ("ST", "ST")):
                for strona in ("H", "A"):
                    if strona + kl in st:
                        w[strona + col] = st[strona + kl]
            if "HY" in w and "HR" not in w:
                w["HR"], w["AR"] = 0, 0
            return w
    return None


# ---------- ZAWODNICY Z ESPN (bez klucza, bez limitu dziennego) ----------
ESPN_ZAW = {"licznik": 0, "limit": 1500}


def _espn(url):
    ESPN_ZAW["licznik"] += 1
    j, _ = pobierz_json(url)
    return j


def espn_ids_meczu(cache, kod, data, h, g):
    """(slug ligi, id gospodarza, id gościa) w ESPN dla meczu z terminarza."""
    liga = ESPN_LIGI.get(kod)
    if not liga or not data:
        return None
    d = datetime.strptime(data, "%d/%m/%Y")
    pasuje = lambda a, b: bool(znajdz(a, {normalizuj(b): 1}))
    for dd in (d, d - timedelta(days=1), d + timedelta(days=1)):
        k = f"espn|sb|{liga}|{dd:%Y%m%d}"
        ev = cache.get(k, 12)
        if ev is None:
            if ESPN_ZAW["licznik"] >= ESPN_ZAW["limit"]:
                return None
            try:
                j = _espn(f"{ESPN}/{liga}/scoreboard?dates={dd:%Y%m%d}")
            except Exception:
                continue
            ev = []
            for e in j.get("events", []):
                c = {x.get("homeAway"): x["team"] for x in (e.get("competitions") or [{}])[0].get("competitors", [])}
                if "home" in c and "away" in c:
                    ev.append([c["home"]["id"], c["home"].get("displayName", ""), c["away"]["id"], c["away"].get("displayName", "")])
            cache.set(k, ev)
        traf = [e for e in ev if pasuje(h, e[1]) and pasuje(g, e[3])] or \
               [e for e in ev if pasuje(h, e[1]) or pasuje(g, e[3])]
        if len(traf) == 1:
            return liga, traf[0][0], traf[0][2]
    return None


def espn_ostatnie(cache, ligi, tid, n=6):
    """Ostatnie rozegrane mecze drużyny (w podanych rozgrywkach): [(data, id meczu, slug)] od najnowszego."""
    wszystkie = {}
    for liga in ligi:
        k = f"espn|sch|{liga}|{tid}"
        lst = cache.get(k, 12)
        if lst is None:
            if ESPN_ZAW["licznik"] >= ESPN_ZAW["limit"]:
                lst = cache.get(k) or []
            else:
                try:
                    j = _espn(f"{ESPN}/{liga}/teams/{tid}/schedule")
                    lst = [[e["date"][:10], e["id"], liga] for e in j.get("events", [])
                           if ((e.get("competitions") or [{}])[0].get("status") or {}).get("type", {}).get("completed")]
                    cache.set(k, lst)
                except Exception:
                    lst = cache.get(k) or []
        for x in lst:
            wszystkie[x[1]] = x
    return sorted(wszystkie.values(), reverse=True)[:n]


def espn_gracze(cache, liga, ev_id):
    """{id drużyny: {"gole": n, "gracze": {nazwisko: występ}}} z jednego meczu."""
    k = f"espn|pl2|{ev_id}"
    d = cache.get(k)
    if d is not None:
        return d
    if ESPN_ZAW["licznik"] >= ESPN_ZAW["limit"]:
        return None
    try:
        j = _espn(f"{ESPN}/{liga}/summary?event={ev_id}")
    except Exception:
        return None
    gole, info = {}, {"strony": {}, "nazwy": {}, "data": ""}
    hc = ((j.get("header") or {}).get("competitions") or [{}])[0]
    info["data"] = (hc.get("date") or "")[:10]
    for c in hc.get("competitors", []):
        tid_ = str(c.get("id") or (c.get("team") or {}).get("id"))
        info["strony"]["H" if c.get("homeAway") == "home" else "A"] = tid_
        info["nazwy"][tid_] = (c.get("team") or {}).get("displayName", "")
        try:
            gole[tid_] = int(c.get("score"))
        except (TypeError, ValueError):
            pass
    d = {"_info": info}
    for t in j.get("rosters", []):
        tid = str((t.get("team") or {}).get("id"))
        gracze = {}
        for p in t.get("roster", []):
            st = {s_.get("name"): s_.get("value") or 0 for s_ in p.get("stats", [])}
            if p.get("starter"):
                mins = 65 if p.get("subbedOut") else 90
            elif p.get("subbedIn"):
                mins = 15 if p.get("subbedOut") else 25
            else:
                continue                     # nie grał
            gracze[(p.get("athlete") or {}).get("displayName", "?")] = {
                "min": mins, "gol": int(st.get("totalGoals", 0)), "as": int(st.get("goalAssists", 0)),
                "sot": int(st.get("shotsOnTarget", 0)), "sh": int(st.get("totalShots", 0)),
                "kar": int(st.get("yellowCards", 0)) + int(st.get("redCards", 0)),
                "fc": int(st.get("foulsCommitted", 0)), "fs": int(st.get("foulsSuffered", 0))}
        if gracze:
            d[tid] = {"gole": gole.get(tid), "gracze": gracze}
    if len(d) > 1:
        cache.set(k, d)
    return d


def zawodnicy_espn(cache, kod, data, h, g, lam_h, lam_a, ligi_dom=None, ile=6):
    """Typy na zawodników z danych ESPN. None = ESPN nie zna tego meczu (wtedy próbujemy API-Football)."""
    ids = espn_ids_meczu(cache, kod, data, h, g)
    if not ids:
        return None, None
    liga, hid, gid = ids
    ligi_dom = ligi_dom or {}
    dane, lab = [], {}
    for nazwa, tid, lam, strona in ((h, hid, lam_h, "H"), (g, gid, lam_a, "A")):
        wiersze = {}
        ligi = [liga] + [ESPN_LIGI[k] for k in ligi_dom.get(nazwa, []) if k in ESPN_LIGI and ESPN_LIGI[k] != liga]
        gole_druz, gracze, n = [], {}, 0
        for dt, ev_id, sl in espn_ostatnie(cache, ligi, tid, ile):
            m = espn_gracze(cache, sl, ev_id)
            if not m or str(tid) not in m:
                continue
            n += 1
            inf = m.get("_info") or {}
            dom = (inf.get("strony") or {}).get("H") == str(tid)
            rywal = next((v for k_, v in (inf.get("nazwy") or {}).items() if k_ != str(tid)), "?")
            if m[str(tid)].get("gole") is not None:
                gole_druz.append(m[str(tid)]["gole"])
            for imie, w in m[str(tid)]["gracze"].items():
                gracze.setdefault(imie, []).append(w)
                wiersze.setdefault(imie, []).append(
                    [f"{dt[8:10]}.{dt[5:7]}.{dt[2:4]}", rywal, dom, w["gol"], w["as"], w["sh"], w["sot"], w["kar"],
                     w["fc"], w["fs"], w["min"]])
        regularni = sorted(((imie, r) for imie, r in wiersze.items()
                            if len(r) >= 3 and sum(1 for x in r[:5] if x[10] >= 45) >= 2),
                           key=lambda z: -sum(x[10] for x in z[1]))[:16]
        if regularni:
            lab[strona] = regularni
        if n < 3:
            pow_ = "limit zapytań na to odświeżenie - dobierze następnym razem" \
                if ESPN_ZAW["licznik"] >= ESPN_ZAW["limit"] else "za mało"
            print(f"   [zawodnicy] {nazwa}: w ESPN {n} mecz(e) ze statystykami - {pow_}")
            continue
        dane.append((nazwa, lam, strona, gole_druz, gracze))
    return typy_z_wystepow(h, g, dane), lab


# ---------- DZIENNIK TYPÓW I ROZLICZENIE ----------
def wczytaj_historie(sciezka):
    try:
        with open(sciezka, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def zapisz_historie(historia, sciezka):
    granica = datetime.now() - timedelta(days=365)
    historia = {k: v for k, v in historia.items()
                if datetime.strptime(v["data"], "%d/%m/%Y") >= granica}
    for v in historia.values():          # rozliczone mecze: tylko to, co potrzebne do statystyk (mniejszy plik)
        if v.get("koniec"):
            v["typy"] = [{k: t[k] for k in ("typ", "p", "kurs", "rk", "traf", "k0", "v0") if k in t}
                         for t in v["typy"] if t.get("traf") is not None]
    with open(sciezka, "w", encoding="utf-8") as f:
        json.dump(historia, f, ensure_ascii=False)
    return historia


def start_meczu(e):
    try:
        return datetime.strptime(f'{e["data"]} {e.get("godz") or "12:00"}', "%d/%m/%Y %H:%M")
    except ValueError:
        return None


def zapamietaj_typy(historia, kod, w, h, g, h_pokaz, g_pokaz, typy_do_zapisu):
    """Zapisuje typy pokazane przed meczem (nadpisuje do momentu rozpoczęcia meczu)."""
    e = {"kod": kod, "liga": NAZWY.get(kod, kod), "data": w.get("Date", ""), "godz": w.get("Time", ""),
         "h": h, "g": g, "hp": h_pokaz, "gp": g_pokaz, "typy": typy_do_zapisu}
    st = start_meczu(e)
    if not st or st <= datetime.now():
        return
    klucz = f'{kod}|{e["data"]}|{h_pokaz}|{g_pokaz}'
    stare = {t["typ"]: t for t in historia.get(klucz, {}).get("typy", [])}
    for t in e["typy"]:                 # pierwszy zauważony kurs (do sprawdzenia ruchu kursów - CLV)
        s = stare.get(t["typ"])
        t["k0"] = (s.get("k0") or s.get("kurs")) if s else t.get("kurs")
        t["v0"] = s.get("v0", s.get("v")) if s else t.get("v")
        if not t["k0"]:
            t["k0"] = t.get("kurs")
    historia[klucz] = e


def wyniki_fixturedownload(laduj_tekst, slug):
    """Rozegrane mecze z fixturedownload.com: tylko gole."""
    out = []
    try:
        t = laduj_tekst(f"https://fixturedownload.com/feed/json/{slug}", f"{slug}.json")
    except Exception:
        return out
    if not t or not t.lstrip().startswith("["):
        return out
    for m in json.loads(t):
        if m.get("HomeTeamScore") is None or m.get("AwayTeamScore") is None:
            continue
        try:
            d = datetime.strptime(m["DateUtc"].replace("Z", "").strip()[:16], "%Y-%m-%d %H:%M")
            d = d.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        out.append({"data": d.strftime("%d/%m/%Y"), "h": m.get("HomeTeam", ""), "g": m.get("AwayTeam", ""),
                    "hg": m["HomeTeamScore"], "ag": m["AwayTeamScore"]})
    return out


def statystyki_af(af, e):
    """Wynik i statystyki meczu z API-Football (gdy football-data jeszcze ich nie ma)."""
    dzien = datetime.strptime(e["data"], "%d/%m/%Y").strftime("%Y-%m-%d")
    lista = lista_dnia(af, dzien, 2)
    idx = {}
    for f in lista:
        idx.setdefault(normalizuj(f["teams"]["home"]["name"]), []).append(f)
    mecz = None
    for f in idx.get(znajdz(e["hp"], idx), []):
        if znajdz(e["gp"], {normalizuj(f["teams"]["away"]["name"]): 1}):
            mecz = f
    if not mecz:
        print(f"   [rozliczenie] API-Football: nie znaleziono {e['hp']} - {e['gp']} ({len(lista)} meczów {dzien})")
        return None
    stat = (mecz.get("fixture", {}).get("status", {}) or {}).get("short")
    if stat not in ("FT", "AET", "PEN"):
        print(f"   [rozliczenie] API-Football: {e['hp']} - {e['gp']} ma status {stat}")
        return None
    w = {"FTHG": (mecz.get("goals") or {}).get("home"), "FTAG": (mecz.get("goals") or {}).get("away")}
    ks = f"af|st|{mecz['fixture']['id']}"
    st = af.cache.get(ks)
    if not st:
        st = af.get(f"/fixtures/statistics?fixture={mecz['fixture']['id']}", zapisz=False) or []
        if st:                                   # pusta odpowiedź (statystyki jeszcze niegotowe) - nie zapamiętujemy
            af.cache.set(ks, st)
    mapa = {"Corner Kicks": ("HC", "AC"), "Yellow Cards": ("HY", "AY"), "Red Cards": ("HR", "AR"),
            "Total Shots": ("HS", "AS"), "Shots on Goal": ("HST", "AST")}
    for blok in st:
        dom = blok.get("team", {}).get("id") == mecz["teams"]["home"]["id"]
        for s in blok.get("statistics", []):
            if s.get("type") in mapa:
                w[mapa[s["type"]][0 if dom else 1]] = s.get("value") or 0
    return w


def rozlicz_historie(historia, surowe, wyniki_fd, af, cache=None):
    """Uzupełnia wynik i trafienia typów w zapisanych meczach."""
    teraz = datetime.now()
    ile_af = 0
    diag = {"do_rozliczenia": 0, "football-data": 0, "ESPN": 0, "API-Football": 0, "fixturedownload": 0, "brak": 0}
    brakujace = []
    for e in historia.values():
        if e.get("koniec"):
            continue
        st = start_meczu(e)
        if not st or teraz < st + timedelta(hours=2, minutes=15):
            continue
        diag["do_rozliczenia"] += 1
        wiersz, zrodlo = None, ""
        # 1) football-data: pełne statystyki
        for x, _ in surowe.get(e["kod"], []):
            if (x.get("Date") and data_meczu(x).strftime("%d/%m/%Y") == e["data"]
                    and normalizuj(x["HomeTeam"]) == normalizuj(e["h"])
                    and normalizuj(x["AwayTeam"]) == normalizuj(e["g"])):
                wiersz, zrodlo = dict(x), "football-data"
                break
        # 2) ESPN: wynik + rożne/kartki/strzały (bez klucza)
        if not wiersz and cache is not None:
            try:
                s_ = wynik_espn(cache, e)
            except Exception as ex:
                s_ = None
                print(f"  ESPN: błąd {ex}")
            if s_:
                wiersz, zrodlo = s_, "ESPN"
        # 3) API-Football: wynik + rożne/kartki/strzały
        if not wiersz and af and not af.wyczerpane and ile_af < 25:
            ile_af += 1
            s = statystyki_af(af, e)
            if s and s.get("FTHG") is not None:
                wiersz, zrodlo = s, "API-Football"
        # 3) fixturedownload: same gole
        if not wiersz:
            for m in wyniki_fd.get(e["kod"], []):
                if m["data"] == e["data"] and znajdz(e["hp"], {normalizuj(m["h"]): 1}) \
                        and znajdz(e["gp"], {normalizuj(m["g"]): 1}):
                    wiersz, zrodlo = {"FTHG": m["hg"], "FTAG": m["ag"]}, "fixturedownload (tylko gole)"
                    break
        if not wiersz:
            diag["brak"] += 1
            if len(brakujace) < 5:
                brakujace.append(f'{e["liga"]} {e["data"][:5]} {e["hp"]} - {e["gp"]}')
            if teraz > st + timedelta(days=4):
                e["koniec"] = True       # nie znaleziono wyniku - dajemy spokój
            continue
        diag[zrodlo.split(" ")[0]] = diag.get(zrodlo.split(" ")[0], 0) + 1
        wiersz["HomeTeam"], wiersz["AwayTeam"] = e["h"], e["g"]
        e["wynik"] = f'{int(float(wiersz["FTHG"]))}:{int(float(wiersz["FTAG"]))}'
        e["zrodlo"] = zrodlo
        brak, brak_zaw = 0, 0
        ev_espn = wiersz.get("_espn")
        for t in e["typy"]:
            if t.get("traf") is not None or t.get("zwrot"):
                continue
            if t["spec"][0] == "zaw":
                r = None
                if cache is not None and e["kod"] in ESPN_LIGI:
                    try:
                        if not ev_espn:
                            ev_espn = espn_id_rozegranego(cache, e)
                        r = rozlicz_zawodnika(cache, e, ev_espn, t["spec"]) if ev_espn else None
                    except Exception as ex:
                        print(f"  ESPN zawodnicy: błąd {ex}")
                if r == "zwrot":
                    t["zwrot"] = True              # zawodnik nie zagrał - zakład zwykle zwracany
                    continue
                if r is None:
                    brak += 1
                    brak_zaw += 1
                else:
                    t["traf"] = bool(r)
                continue
            r = trafia(wiersz, e["h"], tuple(t["spec"]))
            if r is None:
                brak += 1
            else:
                t["traf"] = bool(r)
        # pełne dane albo minęły 4 dni -> zamykamy
        if not brak or teraz > st + timedelta(days=4) or (zrodlo == "football-data" and not brak_zaw):
            e["koniec"] = True
    zapisane = sum(1 for e in historia.values() if start_meczu(e) and start_meczu(e) < teraz)
    print(f"Rozliczanie: zapisanych rozegranych meczów {zapisane}, do rozliczenia teraz {diag['do_rozliczenia']}, "
          f"wyniki z: football-data {diag['football-data']}, ESPN {diag['ESPN']}, API-Football {diag['API-Football']}, "
          f"fixturedownload {diag['fixturedownload']}; bez wyniku {diag['brak']}"
          + (f" (np. {'; '.join(brakujace)})" if brakujace else "")
          + ("" if af else " | brak klucza API-Football") + (" | limit API-Football wyczerpany" if af and af.wyczerpane else ""))


def zapisz_rozliczenie(historia, sciezka):
    dane = []
    for e in historia.values():
        if "wynik" not in e:
            continue
        for t in e["typy"]:
            if t.get("traf") is None:
                continue
            dane.append({"id": f'{e["data"]}|{e["hp"]}|{e["gp"]}', "d": e["data"], "g": e.get("godz", ""),
                         "m": f'{e["hp"]} – {e["gp"]}', "l": e["liga"],
                         "w": e["wynik"], "t": t["typ"], "p": t["p"], "k": t.get("kurs"),
                         "r": t.get("rk", ""), "ok": t["traf"], "k0": t.get("k0"), "v0": t.get("v0")})
    oczekuje = sum(1 for e in historia.values() if "wynik" not in e)
    html = ROZLICZENIE.replace("__DANE__", json.dumps(dane, ensure_ascii=False)).replace("__PWA__", PWA_HEAD) \
        .replace("__CZAS__", datetime.now().strftime("%d.%m.%Y %H:%M")).replace("__CZEKA__", str(oczekuje))
    with open(sciezka, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Rozliczenie: {len(dane)} rozliczonych typów, {oczekuje} meczów czeka na wynik -> {sciezka}")


ROZLICZENIE = """<!DOCTYPE html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rozliczenie typów</title>__PWA__<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet"><style>
:root{--bg:#EEF2F6;--card:#fff;--txt:#12233A;--mut:#5B6B80;--lin:#D6DEE8;--amber:#C98A12;--amber-t:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#0E1A2B;--card:#15243A;--txt:#E9EEF5;--mut:#8FA3BC;--lin:#23364F;--amber:#F2B33D;--amber-t:#1B1405}}
h1,h3{font-family:"Barlow Condensed","Arial Narrow",sans-serif}a{color:var(--amber)}
body{margin:0;font:15px/1.5 Barlow,system-ui,sans-serif;background:var(--bg);color:var(--txt)}main{max-width:1000px;margin:0 auto;padding:16px}
.k{background:var(--card);border:1px solid var(--lin);border-radius:12px;padding:14px 16px;margin-bottom:16px;overflow-x:auto}
.m{color:var(--mut);font-size:12px}table{width:100%;border-collapse:collapse}td,th{padding:6px 8px;border-bottom:1px solid var(--lin);text-align:left;white-space:nowrap}
th{font-size:11px;color:var(--mut);text-transform:uppercase}a{color:#4a7bd0}
.chipy{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.chip{padding:6px 14px;border-radius:999px;border:1px solid var(--lin);background:var(--card);color:var(--txt);font-weight:600;cursor:pointer}
.chip.on{background:var(--amber);color:var(--amber-t);border-color:var(--amber)}.plus{color:#00b050;font-weight:700}.k:empty{display:none}#dni{flex-wrap:nowrap;overflow-x:auto;padding-bottom:4px}#dni .chip{flex:0 0 auto}.minus{color:#e0533d;font-weight:700}
</style></head><body><main>
<p><a href="index.html">&larr; Wróć do prognoz</a></p>
<h1 style="font-size:20px">Rozliczenie typów (stawka 100 zł na zdarzenie)</h1>
<p class="m">Typy zapisane przed meczem tak, jak pokazywała je strona, rozliczone z prawdziwymi wynikami. Aktualizacja: __CZAS__.
Mecze czekające na wynik: __CZEKA__.</p>
<div class="chipy" id="dni"></div>
<div class="chipy" id="rynki"></div>
<label class="m">Liga <select id="liga" style="padding:4px 8px;border-radius:6px"><option value="">wszystkie</option></select></label>
&nbsp; <label class="m"><input type="checkbox" id="pod"> uwzględnij podatek 12%</label>
<p class="m" id="opisKat"></p>
<div class="k" id="tab"></div>
<div class="k" id="strat"></div>
<div class="k" id="bilans"></div>
<div class="k" id="rynkiTab"></div>
<div class="k" id="ligiTab"></div>
<div class="k"><p class="m"><b>Jak czytać:</b> „Z kursem bukmachera” to prawdziwy wynik w zł (tylko typy, dla których był kurs:
1X2 i gole). „Szacunek” liczy wszystkie typy po kursie uczciwym pomniejszonym o typową marżę 7% &mdash; bukmacherzy dają podobne
kursy na rożne i kartki, ale to tylko przybliżenie. Najważniejsze: czy „weszło” zgadza się z „model mówił”.</p></div>
<div class="k" id="clv"></div>
<div class="k" id="lista"></div>
</main><script>
const D=__DANE__;
let DZ="",RY="";
const ts=d=>{const [a,b,c]=d.split("/").map(Number);return new Date(c,b-1,a).getTime()};
const daty=[...new Set(D.map(x=>x.d))].sort((a,b)=>ts(b)-ts(a));
dni.innerHTML='<button class="chip on" data-d="">Wszystkie dni</button>'+daty.map(d=>`<button class="chip" data-d="${d}">${d.slice(0,5).replace("/",".")} (${D.filter(x=>x.d===d).length})</button>`).join("");
const RN={"":"Wszystkie rynki",wynik:"Wynik",bramki:"Bramki",rozne:"Rożne",kartki:"Kartki",strzaly:"Strzały",zaw:"Zawodnicy",value:"&#x1F4B0; Value bety"};
rynki.innerHTML=Object.entries(RN).map(([k,v])=>`<button class="chip${k?"":" on"}" data-r="${k}">${v}</button>`).join("");
dni.onclick=e=>{const c=e.target.closest("[data-d]");if(!c)return;DZ=c.dataset.d;dni.querySelectorAll(".chip").forEach(b=>b.classList.toggle("on",b===c));rys()};
rynki.onclick=e=>{const c=e.target.closest("[data-r]");if(!c)return;RY=c.dataset.r;rynki.querySelectorAll(".chip").forEach(b=>b.classList.toggle("on",b===c));rys()};
pod.onchange=rys;liga.onchange=rys;
liga.innerHTML+=[...new Set(D.map(x=>x.l))].sort().map(l=>`<option value="${l}">${l}</option>`).join("");
const zl=v=>`<span class="${v>=0?"plus":"minus"}">${v>=0?"+":""}${v.toFixed(0)} zł</span>`;
// value bety to osobna kategoria (niższe szanse, prawdziwe kursy) - nie mieszamy ich ze zwykłymi typami
function rys(){const P=pod.checked?0.88:1;const baza=D.filter(x=>(!DZ||x.d===DZ)&&(!liga.value||x.l===liga.value));
const xs=baza.filter(x=>RY==="value"?x.v0:(!x.v0&&(!RY||x.r===RY)));
opisKat.innerHTML=RY==="value"?"Pokazuję tylko value bety: typy, w których model widział przewagę nad kursem bukmachera. Liczone po prawdziwych kursach.":
`Value bety są liczone osobno (${baza.filter(x=>x.v0).length} rozliczonych) &mdash; zobacz kategorię „Value bety”.`;
const prz=[[0,50],[50,60],[60,70],[70,80],[80,90],[90,101]];let wiersze="",S={n:0,t:0,kn:0,kz:0,sz:0,sp:0};
for(const [a,b] of prz){const g=xs.filter(x=>x.p*100>=a&&x.p*100<b);if(!g.length)continue;
const t=g.filter(x=>x.ok).length,sp=g.reduce((s,x)=>s+x.p,0)/g.length*100;
const zk=g.filter(x=>x.k),kz=zk.reduce((s,x)=>s+(x.ok?100*P*x.k:0)-100,0);
const sz=g.reduce((s,x)=>s+(x.ok?100*P*(1/x.p)*0.93:0)-100,0);
S.n+=g.length;S.t+=t;S.kn+=zk.length;S.kz+=kz;S.sz+=sz;S.sp+=sp*g.length;
wiersze+=`<tr><td><b>${a===0?"poniżej 50":a+"–"+Math.min(b,100)}%</b></td><td>${g.length}</td><td>${t}</td><td>${sp.toFixed(1)}%</td>
<td><b>${(t/g.length*100).toFixed(1)}%</b></td><td>${zk.length?zk.length+" &middot; "+zl(kz):"-"}</td><td>${zl(sz)}</td></tr>`}
const cl=baza.filter(x=>x.k0&&x.k),clv=a=>a.length?a.reduce((s,x)=>s+(x.k0/x.k-1),0)/a.length*100:0,
plus=a=>a.length?a.filter(x=>x.k0>x.k+0.001).length/a.length*100:0,vb=cl.filter(x=>x.v0);
clv.innerHTML=cl.length?`<h3 style="margin:0 0 6px;font-size:16px">Ruch kursów (CLV)</h3>
<p class="m">Porównanie kursu z chwili, gdy typ pierwszy raz pojawił się na stronie, z ostatnim kursem przed meczem. Jeśli kurs potem
spadł, rynek „przyznał rację” modelowi &mdash; to najlepszy wczesny znak, że model ma przewagę (szybszy niż czekanie na wyniki).</p>
<table><tr><th></th><th>Typów</th><th>Kurs spadł</th><th>Średni CLV</th></tr>
<tr><td>Value bety</td><td>${vb.length}</td><td>${plus(vb).toFixed(0)}%</td><td><b class="${clv(vb)>=0?"plus":"minus"}">${clv(vb)>=0?"+":""}${clv(vb).toFixed(1)}%</b></td></tr>
<tr><td>Wszystkie typy z kursem</td><td>${cl.length}</td><td>${plus(cl).toFixed(0)}%</td><td><b class="${clv(cl)>=0?"plus":"minus"}">${clv(cl)>=0?"+":""}${clv(cl).toFixed(1)}%</b></td></tr></table>
<p class="m">CLV dodatni (na plus) przez dłuższy czas = model wyprzedza bukmachera. Ujemny = rynek wie więcej.</p>`:"";
tab.innerHTML=xs.length?`<table><tr><th>Prawdopodobieństwo</th><th>Typów</th><th>Weszło</th><th>Model mówił</th><th>Weszło %</th>
<th>Z kursem bukmachera</th><th>Szacunek (wszystkie)</th></tr>${wiersze}
<tr><td><b>RAZEM</b></td><td><b>${S.n}</b></td><td><b>${S.t}</b></td><td>${(S.sp/S.n).toFixed(1)}%</td><td><b>${(S.t/S.n*100).toFixed(1)}%</b></td>
<td>${S.kn?S.kn+" &middot; "+zl(S.kz):"-"}</td><td>${zl(S.sz)}</td></tr></table>
<p class="m">Postawiono łącznie ${S.n*100} zł (100 zł na każde zdarzenie).</p>`:'<p>Brak rozliczonych typów. Typy zapisują się przed meczem, a rozliczają kilka godzin po jego końcu.</p>';
// ---- baza: strategie, bilans dzień po dniu, rynki, ligi ----
const zK=x=>x.k?(x.ok?100*P*x.k:0)-100:0, zS=x=>(x.ok?100*P*(1/x.p)*0.93:0)-100;
const podsum=g=>({n:g.length,t:g.filter(x=>x.ok).length,sp:g.reduce((s,x)=>s+x.p,0)/(g.length||1)*100,
 kn:g.filter(x=>x.k).length,kz:g.reduce((s,x)=>s+zK(x),0),sz:g.reduce((s,x)=>s+zS(x),0)});
const wiersz=(nazwa,q)=>`<tr><td><b>${nazwa}</b></td><td>${q.n}</td><td>${(q.t/(q.n||1)*100).toFixed(1)}%</td><td>${q.sp.toFixed(1)}%</td>
<td>${q.kn?q.kn+" &middot; "+zl(q.kz):"-"}</td><td>${zl(q.sz)}</td><td>${(q.sz/(q.n*100||1)*100).toFixed(1)}%</td></tr>`;
const nag=`<tr><th></th><th>Typów</th><th>Weszło</th><th>Model mówił</th><th>Z kursem bukm.</th><th>Szacunek</th><th>ROI (szac.)</th></tr>`;
const grupuj=f=>{const G={};xs.forEach(x=>{(G[f(x)]=G[f(x)]||[]).push(x)});return Object.entries(G).map(([k,g])=>[k,podsum(g)]).sort((a,b)=>b[1].n-a[1].n)};
if(xs.length){
const ids={};xs.forEach(x=>{if(!ids[x.id]||x.p>ids[x.id].p)ids[x.id]=x});
const S1=Object.values(ids),S2=baza.filter(x=>x.v0&&x.k),S3=xs.filter(x=>x.p>=0.7&&x.p<0.85),S4=xs.filter(x=>x.p>=0.85);
strat.innerHTML=`<h3 style="margin:0 0 6px;font-size:16px">Strategie (100 zł na każde zdarzenie)</h3><div style="overflow-x:auto"><table>${nag}
${wiersz("Najpewniejszy typ każdego meczu",podsum(S1))}${wiersz("Value bety (kurs bukmachera)",podsum(S2))}
${wiersz("Wszystkie typy 70–85%",podsum(S3))}${wiersz("Wszystkie typy 85% i więcej",podsum(S4))}${wiersz(RY==="value"?"Wszystkie value bety":"Wszystkie typy (bez value betów)",podsum(xs))}</table></div>
<p class="m">Szacunek: typy bez kursu bukmachera liczone po kursie uczciwym minus 7% marży. Value bety: prawdziwe kursy.</p>`;
const dni=[...new Set(xs.map(x=>x.d))].sort((a,b)=>ts(a)-ts(b));let cs=0,ck=0;const ps=[],pk=[],rows=[];
dni.forEach(d=>{const q=podsum(xs.filter(x=>x.d===d));cs+=q.sz;ck+=q.kz;ps.push(cs);pk.push(ck);
rows.push(`<tr><td>${d.slice(0,5).replace("/",".")}</td><td>${q.n}</td><td>${(q.t/q.n*100).toFixed(0)}%</td><td>${zl(q.sz)}</td><td>${zl(cs)}</td></tr>`)});
const W=600,H=150,all=[...ps,...pk,0],lo=Math.min(...all),hi=Math.max(...all),rg=(hi-lo)||1,
pt=a=>a.map((v,i)=>`${a.length>1?i/(a.length-1)*W:W/2},${H-(v-lo)/rg*H}`).join(" "),y0=H-(0-lo)/rg*H;
bilans.innerHTML=`<h3 style="margin:0 0 6px;font-size:16px">Bilans dzień po dniu</h3>
${dni.length>1?`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:150px"><line x1="0" x2="${W}" y1="${y0}" y2="${y0}" stroke="#888" stroke-dasharray="4 4"/>
<polyline fill="none" stroke="#4a7bd0" stroke-width="2" vector-effect="non-scaling-stroke" points="${pt(ps)}"/>
<polyline fill="none" stroke="#00b050" stroke-width="2" vector-effect="non-scaling-stroke" points="${pt(pk)}"/></svg>
<p class="m"><span style="color:#4a7bd0">&#9644;</span> szacunek (wszystkie typy) &nbsp; <span style="color:#00b050">&#9644;</span> z kursem bukmachera</p>`:""}
<div style="overflow-x:auto"><table><tr><th>Dzień</th><th>Typów</th><th>Weszło</th><th>Wynik dnia (szac.)</th><th>Razem (szac.)</th></tr>${rows.reverse().join("")}</table></div>`;
rynkiTab.innerHTML=`<h3 style="margin:0 0 6px;font-size:16px">Według rynku</h3><div style="overflow-x:auto"><table>${nag}${grupuj(x=>RN[x.r]||x.r||"inne").map(([k,q])=>wiersz(k,q)).join("")}</table></div>`;
ligiTab.innerHTML=`<h3 style="margin:0 0 6px;font-size:16px">Według ligi</h3><div style="overflow-x:auto"><table>${nag}${grupuj(x=>x.l).map(([k,q])=>wiersz(k,q)).join("")}</table></div>`;
}else{strat.innerHTML=bilans.innerHTML=rynkiTab.innerHTML=ligiTab.innerHTML=""}
lista.innerHTML=xs.length?`<table><tr><th>Data</th><th>Mecz</th><th>Wynik</th><th>Typ</th><th>Szansa</th><th>Kurs</th><th></th></tr>
${xs.slice().sort((a,b)=>ts(b.d)-ts(a.d)||b.p-a.p).map(x=>`<tr><td>${x.d.slice(0,5)}</td><td>${x.m} <span class="m">${x.l}</span></td><td>${x.w}</td>
<td>${x.t}</td><td>${(x.p*100).toFixed(0)}%</td><td>${x.k?x.k.toFixed(2):"-"}</td><td>${x.ok?"&#x2705;":"&#x274C;"}</td></tr>`).join("")}</table>`:""}
rys();
</script></body></html>"""


# ---------- POWIADOMIENIA NA TELEFON (Web Push) ----------
def vapid_klucze(cache):
    """Para kluczy do powiadomień - tworzona raz i zapamiętywana."""
    k = cache.get("push|vapid")
    if k:
        cache.set("push|vapid", k)            # odświeżamy datę, żeby sprzątanie po 40 dniach go nie usunęło
        return k
    try:
        import base64
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return None
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    k = {"pub": base64.urlsafe_b64encode(pub).rstrip(b"=").decode(), "priv": pem}
    cache.set("push|vapid", k)
    return k


def raport_powiadomienie(mecze_html, historia, url):
    dzis = datetime.now().strftime("%d/%m/%Y")
    wczoraj = (datetime.now() - timedelta(days=1)).strftime("%d/%m/%Y")
    dzisiejsze = [m for m in mecze_html if m["data"] == dzis]
    pewne = []
    for m in dzisiejsze:
        kand = [x for x in m["typy"] if x.get("kat") != "zaw" and 65 <= x["p"] <= 90]
        if kand:
            x = max(kand, key=lambda x: x["p"])
            pewne.append((x["p"], m, x))
    pewne.sort(key=lambda z: -z[0])
    value = sum(1 for m in dzisiejsze for x in m["typy"]
                if x.get("value") and x.get("rynek") and x["p"] / 100 * x["rynek"] - 1 <= 0.4)
    linie = [f"{m['godz']} {m['h']}–{m['g']}: {x['typ']} ({p:.0f}%)" for p, m, x in pewne[:3]]
    if value:
        linie.append(f"💎 Value betów dziś: {value}")
    roz = [tp for e in historia.values() if e["data"] == wczoraj and "wynik" in e
           for tp in e["typy"] if tp.get("traf") is not None]
    if roz:
        tr = sum(1 for tp in roz if tp["traf"])
        linie.append(f"🧾 Wczoraj weszło {tr}/{len(roz)} typów")
    if not linie:
        linie = ["Dziś brak typów spełniających kryteria."]
    return {"title": f"⚽ Prognozy na {dzis[:5].replace('/', '.')} (meczów: {len(dzisiejsze)})",
            "body": "\n".join(linie), "url": url or "./index.html"}


def wyslij_powiadomienia(cache, dane):
    """Wysyła powiadomienie na każde urządzenie zapisane w sekrecie PUSH_SUB."""
    surowe = (os.environ.get("PUSH_SUB") or "").strip()
    if not surowe:
        return False
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("Powiadomienia: brak biblioteki pywebpush (pip install pywebpush)")
        return False
    k = vapid_klucze(cache)
    if not k:
        return False
    import tempfile
    try:
        subs = json.loads(surowe)
    except ValueError:
        subs = [json.loads(x) for x in surowe.splitlines() if x.strip().startswith("{")]
    if isinstance(subs, dict):
        subs = [subs]
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(k["priv"])
        plik = f.name
    ok = 0
    for sub in subs:
        try:
            webpush(subscription_info=sub, data=json.dumps(dane, ensure_ascii=False),
                    vapid_private_key=plik, vapid_claims={"sub": "mailto:prognozy@example.com"})
            ok += 1
        except Exception as e:
            print(f"Powiadomienia: nie wysłano ({e}). Jeśli to się powtarza, włącz powiadomienia na stronie "
                  f"jeszcze raz i podmień sekret PUSH_SUB.")
    os.remove(plik)
    print(f"Powiadomienia: wysłano na {ok}/{len(subs)} urządzeń")
    return ok > 0


# ---------- APLIKACJA NA TELEFON (PWA) ----------
PWA_HEAD = """<link rel="manifest" href="manifest.webmanifest"><meta name="theme-color" content="#305496">
<link rel="icon" href="icon-192.png"><link rel="apple-touch-icon" href="icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Prognozy"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">"""


def zapisz_pwa(katalog):
    import base64
    with open(os.path.join(katalog, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump({"name": "Prognozy piłkarskie", "short_name": "Prognozy", "start_url": "./index.html",
                   "scope": "./", "display": "standalone", "background_color": "#12151b", "theme_color": "#305496",
                   "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                             {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]},
                  f, ensure_ascii=False)
    for n, b in ((192, IKONA_192), (512, IKONA_512)):
        with open(os.path.join(katalog, f"icon-{n}.png"), "wb") as f:
            f.write(base64.b64decode(b))
    with open(os.path.join(katalog, "sw.js"), "w", encoding="utf-8") as f:
        f.write("self.addEventListener('install',e=>self.skipWaiting());"
                "self.addEventListener('activate',e=>self.clients.claim());"
                "self.addEventListener('fetch',e=>{e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)))});"
                "self.addEventListener('push',e=>{let d={};try{d=e.data.json()}catch(x){d={title:'Prognozy',body:e.data?e.data.text():''}}"
                "e.waitUntil(self.registration.showNotification(d.title||'Prognozy',{body:d.body||'',icon:'icon-192.png',"
                "badge:'icon-192.png',data:{url:d.url||'./index.html'}}))});"
                "self.addEventListener('notificationclick',e=>{e.notification.close();"
                "e.waitUntil(clients.openWindow(e.notification.data&&e.notification.data.url||'./index.html'))});")


IKONA_192 = "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"
IKONA_512 = "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"


# ---------- strona HTML ----------
STRONA = """<!DOCTYPE html>
<html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prognozy __DATA__</title>
__PWA__
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#f4f6f9;--card:#fff;--txt:#1d2330;--mut:#6b7385;--lin:#e3e7ee}
@media(prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--txt:#e8ebf1;--mut:#97a0b3;--lin:#2b313d}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Arial;background:var(--bg);color:var(--txt)}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--lin);padding:12px 16px}
h1{font-size:18px;margin:0 0 8px}.filtry{display:flex;flex-wrap:wrap;gap:12px;align-items:center;color:var(--mut);font-size:13px}
.filtry select{padding:5px 8px;border:1px solid var(--lin);border-radius:8px;background:var(--bg);color:var(--txt)}
.filtry input[type=search]{padding:6px 10px;border:1px solid var(--lin);border-radius:8px;background:var(--bg);color:var(--txt);min-width:180px}
main{max-width:1000px;margin:0 auto;padding:16px}
.mecz{background:var(--card);border:1px solid var(--lin);border-radius:12px;margin-bottom:16px;overflow:hidden}
.top{padding:12px 16px;border-bottom:1px solid var(--lin)}.top b{font-size:16px}.top .m{color:var(--mut);font-size:12px}
.info{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:6px;font-size:12px;color:var(--mut)}
table{width:100%;border-collapse:collapse}th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--lin);white-space:nowrap}
th{font-size:11px;text-transform:uppercase;color:var(--mut);font-weight:600}
.wrap{overflow-x:auto}.p{display:inline-block;min-width:54px;text-align:center;padding:2px 8px;border-radius:999px;font-weight:700}
.z1{background:#00b050;color:#fff}.z2{background:#92d050;color:#000}.z3{background:#ffeb84;color:#000}.z4{background:#f8cbad;color:#000}
.st{display:inline-flex;align-items:center;gap:6px}.bar{width:46px;height:6px;border-radius:3px;background:var(--lin);overflow:hidden}
.bar i{display:block;height:100%}.val{background:#ffd700;color:#000;font-weight:700;padding:1px 6px;border-radius:6px;font-size:11px}
.leg{font-size:12px;color:var(--mut);margin:0 0 12px}.ukryj{display:none}
@media(max-width:640px){.leg{display:none}}
.c-br{display:none}.top{cursor:pointer;position:relative;padding-right:34px!important}
.strz{position:absolute;right:14px;top:12px;font-size:16px;color:var(--mut)}
.skrot{display:none;margin-top:6px;font-size:13px}.mecz.zw .skrot{display:block}.mecz.zw .wrap,.mecz.zw .info{display:none}
.zwbar{display:flex;gap:6px;margin:0 0 10px}.fsum{cursor:pointer;font-weight:600;font-size:13px;color:var(--mut);margin-bottom:6px;list-style:none}
.fsum::-webkit-details-marker{display:none}
@media(max-width:640px){
header{position:static;padding:10px 12px}h1{font-size:16px;line-height:1.4}
.filtry{gap:8px}.filtry label{flex:1 1 100%}.filtry input[type=range]{width:55%}.filtry select,.filtry input[type=search]{flex:1;min-width:0;max-width:100%}
.chipy{flex-wrap:nowrap!important;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px;scrollbar-width:none}
.chipy::-webkit-scrollbar{display:none}.chip{flex:0 0 auto;padding:6px 12px}
main{padding:10px 8px}.top{padding:10px 12px}.top b{font-size:15px}
.wrap{overflow:visible}.wrap table,.wrap tbody{display:block;width:100%}
.wrap tr:has(th){display:none}
.wrap tr{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding:10px 12px;border-bottom:1px solid var(--lin)}
.wrap td{border:0;padding:0;white-space:normal}
.c-typ{flex:1 1 0;min-width:0;font-weight:600;font-size:14px}.c-m{flex:1 1 100%;order:-1;font-size:13px}
.c-br{display:block;flex-basis:100%;height:0}
.c-x{font-size:12px;color:var(--mut)}.c-x::before{content:attr(data-l) ": ";color:var(--mut)}
.c-x .st{color:var(--txt)}.bar{width:32px}
#gen,#vb{padding:0 12px}.gprop{grid-template-columns:1fr}
#kpanel{right:8px;left:8px;width:auto;max-width:none;bottom:70px;max-height:70vh}
#kbtn{right:12px;bottom:12px}
}
.chipy{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{padding:6px 14px;border-radius:999px;border:1px solid var(--lin);background:var(--bg);color:var(--txt);font-weight:600;font-size:13px;cursor:pointer}
.chip.on{background:#305496;border-color:#305496;color:#fff}
.dod{width:28px;height:28px;border-radius:8px;border:1px solid var(--lin);background:var(--bg);color:var(--txt);font-weight:700;cursor:pointer}
.dod.on{background:#305496;color:#fff;border-color:#305496}
#kbtn{position:fixed;right:16px;bottom:16px;z-index:20;padding:12px 18px;border:0;border-radius:999px;background:#305496;color:#fff;font-weight:700;box-shadow:0 4px 14px rgba(0,0,0,.25);cursor:pointer}
#kpanel{position:fixed;right:16px;bottom:72px;z-index:20;width:380px;max-width:calc(100vw - 32px);max-height:75vh;overflow:auto;background:var(--card);border:1px solid var(--lin);border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.25);padding:14px;display:none}
#kpanel.otw{display:block}#kpanel h2{font-size:16px;margin:0 0 8px}
.kp{border-bottom:1px solid var(--lin);padding:8px 0;font-size:13px}.kp .mm{color:var(--mut);font-size:11px}
.kp input,.kform input{width:70px;padding:4px 6px;border:1px solid var(--lin);border-radius:6px;background:var(--bg);color:var(--txt)}
.kp .x{float:right;border:0;background:none;color:var(--mut);font-size:16px;cursor:pointer}
.ksum{display:grid;grid-template-columns:1fr auto;gap:4px 12px;font-size:13px;margin-top:10px}.ksum b{text-align:right}
.kform input[type=checkbox]{width:auto}.kform{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:var(--mut);margin:8px 0;align-items:center}
.kbt{padding:7px 12px;border:0;border-radius:8px;background:#305496;color:#fff;font-weight:600;cursor:pointer;margin:4px 4px 0 0}
.kbt.sz{background:var(--lin);color:var(--txt)}
#gen{background:var(--card);border:1px solid var(--lin);border-radius:12px;margin-bottom:16px;padding:0 16px}
#gen summary{padding:14px 0;font-weight:700;font-size:15px;cursor:pointer}
.gprop{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding-bottom:16px}
.gk{border:1px solid var(--lin);border-radius:10px;padding:10px 12px;font-size:13px}
.gk h3{margin:0 0 6px;font-size:14px;display:flex;justify-content:space-between}
.gk ul{margin:6px 0;padding-left:18px}.mm{color:var(--mut);font-size:11px}
#vb{background:var(--card);border:1px solid var(--lin);border-radius:12px;margin-bottom:16px;padding:0 16px}
#vb summary{padding:14px 0;font-weight:700;font-size:15px;cursor:pointer}.plus{color:#00b050;font-weight:700}.gk li{margin:3px 0}.gk .mm{color:var(--mut);font-size:11px}.knot{font-size:11px;color:var(--mut);margin-top:8px}

/* ===== Nocny stadion: granat, kreda, światło reflektorów ===== */
:root{--bg:#EEF2F6;--card:#FFFFFF;--txt:#12233A;--mut:#5B6B80;--lin:#D6DEE8;--amber:#C98A12;--amber-t:#fff;
--win:#1F9A5E;--loss:#D2492F;--home:#3F7FC4;--away:#C98A12;--cond:"Barlow Condensed","Arial Narrow",sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#0E1A2B;--card:#15243A;--txt:#E9EEF5;--mut:#8FA3BC;--lin:#23364F;
--amber:#F2B33D;--amber-t:#1B1405;--win:#2FA86B;--loss:#E05A47;--home:#7FB3E8;--away:#F2B33D}}
body{font:15px/1.5 Barlow,system-ui,sans-serif;background:var(--bg)}
a{color:var(--amber)}:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
header{background:var(--card);padding:0 16px 10px;border-bottom:1px solid var(--lin)}
.bar1{display:flex;align-items:center;gap:10px;padding:12px 0 8px}
.bar1 img{width:30px;height:30px;border-radius:7px}
.brand{font-family:var(--cond);font-weight:700;font-size:24px;letter-spacing:.2px;line-height:1}
.czas{color:var(--mut);font-size:13px;margin-left:auto;text-align:right}
.taby{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--lin);margin:0 -16px 10px;padding:0 12px}
.taby::-webkit-scrollbar{display:none}
.tab{flex:0 0 auto;font-family:var(--cond);font-weight:600;font-size:17px;padding:8px 12px;border:0;background:none;color:var(--mut);
border-bottom:3px solid transparent;cursor:pointer;text-decoration:none}
.tab.on{color:var(--txt);border-bottom-color:var(--amber)}
.kpi{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px;color:var(--mut);margin:2px 0 10px}
.kpi b{font-family:var(--cond);font-size:19px;color:var(--txt);font-weight:700;margin-right:3px}
.chip{background:var(--card);border-color:var(--lin);font-family:var(--cond);font-size:15px;font-weight:600}
.chip.on{background:var(--amber);border-color:var(--amber);color:var(--amber-t)}
#kbtn{background:var(--amber);color:var(--amber-t);font-family:var(--cond);font-size:17px}
.kbt{background:var(--amber);color:var(--amber-t)}.kbt.sz{background:var(--lin);color:var(--txt)}
.mecz{border-radius:14px;border-color:var(--lin);box-shadow:none}
.top{padding:12px 16px 14px;border-bottom:1px solid var(--lin)}
.meta{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--mut)}
.liga{font-weight:600;color:var(--txt)}.godz{font-family:var(--cond);font-weight:600;font-size:16px;color:var(--txt);
background:var(--bg);border-radius:6px;padding:0 7px}
.sb{display:grid;grid-template-columns:1fr minmax(150px,240px) 1fr;align-items:center;gap:12px;margin-top:8px}
.sb .t{font-family:var(--cond);font-weight:700;font-size:24px;line-height:1.1}.sb .t2{text-align:right}
.xg{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center;font-family:var(--cond);font-weight:700;font-size:18px}
.xgbar{height:8px;border-radius:4px;background:var(--away);overflow:hidden}.xgbar i{display:block;height:100%;background:var(--home)}
.xg .h{color:var(--home)}.xg .a{color:var(--away)}
.wdl{margin-top:10px}.wdlbar{display:flex;height:6px;border-radius:3px;overflow:hidden;gap:2px}
.wdlbar i{display:block;height:100%}.w1{background:var(--home)}.wx{background:var(--mut)}.w2{background:var(--away)}
.wdlt{display:flex;justify-content:space-between;gap:8px;margin-top:4px;font-size:13px;color:var(--mut)}
.wdlt b{font-family:var(--cond);font-size:18px;color:var(--txt);margin-right:3px}.wdlt b.h{color:var(--home)}.wdlt b.a{color:var(--away)}
.wdlt span:nth-child(2){text-align:center}.wdlt span:last-child{text-align:right}
@media(max-width:640px){.wdlt span{font-size:11px}.wdlt b{display:block;font-size:17px}}.xgl{grid-column:1/-1;text-align:center;font:500 11px Barlow,sans-serif;color:var(--mut);margin-top:-4px}
.info{gap:6px 8px;margin-top:10px}.info span{background:var(--bg);border-radius:6px;padding:2px 8px}
.p{font-family:var(--cond);font-size:16px;border-radius:7px;padding:1px 8px}
.z1{background:var(--win);color:#fff}.z2{background:#7CC36B;color:#10230f}.z3{background:#F2B33D;color:#2a1c00}.z4{background:#E58A5C;color:#2b1205}
th{text-transform:none;font-size:12px;letter-spacing:0}
.dod{border-radius:8px}.dod.on{background:var(--amber);border-color:var(--amber);color:var(--amber-t)}
.val{background:var(--amber);color:var(--amber-t)}
.panel-ukryty{display:none!important}
.labwrap{padding:10px 14px 14px;border-top:1px solid var(--lin)}.mecz.zw .labwrap{display:none}
.labbtn{width:100%;padding:10px;border-radius:10px;border:1px dashed var(--lin);background:var(--bg);color:var(--txt);font:600 17px var(--cond);cursor:pointer}
.lab{margin-top:12px}.lab .seg{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.lab .seg button{flex:0 0 auto;font:600 15px var(--cond);padding:5px 12px;border-radius:999px;border:1px solid var(--lin);background:var(--bg);color:var(--txt);cursor:pointer}
.lab .seg button.on{background:var(--amber);color:var(--amber-t);border-color:var(--amber)}
.lab .linia{display:flex;align-items:center;justify-content:center;gap:14px;margin:12px 0 4px}
.lab .linia button{width:44px;height:44px;border-radius:12px;border:1px solid var(--lin);background:var(--bg);color:var(--txt);font-size:24px;cursor:pointer}
.lab .linia span{font:700 34px var(--cond);min-width:100px;text-align:center}
.lab .wyn{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}
.lab .wyn div{background:var(--bg);border-radius:12px;padding:10px;text-align:center;font-size:14px}
.lab .wyn b{display:block;font:700 30px var(--cond)}.lab .wyn .pow b{color:var(--win)}.lab .wyn small{color:var(--mut);font-size:12px}
.kursl{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px;color:var(--mut);margin:4px 0 12px}
.kursl input{width:80px;padding:6px 8px;border-radius:8px;border:1px solid var(--lin);background:var(--bg);color:var(--txt);font:600 16px var(--cond)}
.valb{background:var(--amber);color:var(--amber-t);font-weight:700;padding:2px 8px;border-radius:6px}
.hist .r{display:grid;grid-template-columns:96px 1fr 44px;align-items:center;gap:8px;margin:8px 0;font-size:13px}
.hist .n{color:var(--mut);line-height:1.2}.hist .n small{font-size:11px}
.kropki{display:flex;gap:4px;align-items:flex-end;height:40px;position:relative}
.kropki i{flex:1;max-width:28px;border-radius:3px 3px 0 0;background:#3a4f6d;cursor:pointer}.kropki i.t{background:var(--win)}
.kropki i.wyb{outline:2px solid var(--amber);outline-offset:1px}
.zsel{width:100%;margin:0 0 8px;font:600 15px var(--cond);padding:8px 10px;border-radius:10px;border:1px solid var(--lin);background:var(--bg);color:var(--txt)}
.gw{background:none;border:0;font-size:20px;line-height:1;color:var(--mut);cursor:pointer;padding:2px 6px;margin-left:auto;margin-right:22px}.gw.on{color:#F2B33D}
.kropki .lin{position:absolute;left:0;right:0;border-top:2px dashed var(--amber);pointer-events:none}
.hist .r b{font:700 16px var(--cond);text-align:right}
.barinfo{min-height:38px;background:var(--bg);border-radius:8px;padding:8px 10px;font-size:13px;color:var(--mut)}.barinfo b{color:var(--txt)}
#stan .k{background:var(--card);border:1px solid var(--lin);border-radius:14px;padding:14px 16px}
#stan .m{color:var(--mut);font-size:13px}#stan ul{padding-left:18px}
.stan-g{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
.stan-g div{background:var(--bg);border-radius:10px;padding:10px 12px}.stan-g b{font-family:var(--cond);font-size:20px;display:block}
.ok{color:var(--win)}.zle{color:var(--loss)}
@media(max-width:640px){
.sb{grid-template-columns:1fr 1fr;gap:6px 10px}.sb .t{font-size:20px}.xg{grid-column:1/-1;grid-row:2}
.brand{font-size:21px}.czas{font-size:12px}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head><body>
<header><div class="bar1"><img src="icon-192.png" alt=""><span class="brand">Prognozy</span>
<span class="czas">Aktualizacja<br>__DATA__</span></div>
<nav class="taby" id="taby"><button class="tab on" data-tab="mecze">Mecze</button><button class="tab" data-tab="vb">Value bety</button>
<button class="tab" data-tab="gen">Generator</button><a class="tab" href="rozliczenie.html">Rozliczenie</a>__BT__
<button class="tab" data-tab="stan">Stan danych</button><a class="tab" href="#" id="dzwonek">&#x1F514;</a></nav>
<div class="kpi" id="kpi"></div>
<div id="pushpanel" class="k" style="display:none;margin:8px 0;padding:12px;border:1px solid var(--lin);border-radius:10px;background:var(--bg);font-size:13px"></div>
<details id="filtrDet" open><summary class="fsum">&#x2699;&#xFE0F; Filtry i sortowanie</summary>
<div class="filtry">
<label>Min. prawd. <input type="range" id="min" min="50" max="95" value="60"> <b id="minv">60</b>%</label>
<label>Min. forma <input type="range" id="minf" min="0" max="100" step="10" value="0"> <b id="minfv">0</b>%</label>
<label><input type="checkbox" id="val"> tylko VALUE</label>
<label>Sortuj <select id="sort">
<option value="p">prawdopodobieństwo</option>
<option value="t">data i godzina meczu</option>
<option value="kd">kurs: od najwyższego</option>
<option value="kr">kurs: od najniższego</option>
<option value="rd">kurs rynku: od najwyższego</option>
<option value="f">forma (średnia obu drużyn)</option>
<option value="h">H2H</option></select></label>
<label>Liga <select id="liga"><option value="">wszystkie</option></select></label>
<label>Typy <select id="kat"><option value="">wszystkie</option><option value="mecz">cały mecz</option>
<option value="druz">statystyki drużyn</option><option value="gosp">tylko gospodarz</option><option value="gosc">tylko gość</option><option value="zaw">zawodnicy</option></select></label>
<label><input type="checkbox" id="razem"> wszystkie typy w jednej liście</label>
<input type="search" id="szukaj" placeholder="Szukaj drużyny lub ligi">

</div></details>
<div class="chipy" id="chipy">
<button class="chip on" data-rk="">Wszystkie</button><button class="chip" data-rk="wynik">&#x1F3C6; Wynik</button>
<button class="chip" data-rk="bramki">&#x26BD; Bramki</button><button class="chip" data-rk="rozne">&#x1F6A9; Rożne</button>
<button class="chip" data-rk="kartki">&#x1F7E8; Kartki</button><button class="chip" data-rk="strzaly">&#x1F3AF; Strzały</button>
<button class="chip" data-rk="zaw">&#x1F464; Zawodnicy</button></div>
<div class="chipy" id="dni"></div></header>
<main><p class="leg panel-ukryty">Forma = ile razy dany typ wszedł w ostatnich __N__ meczach drużyny (dla gościa z jego perspektywy). H2H = w bezpośrednich meczach z 5 sezonów. Kolory: zielony = częściej, pomarańczowy = rzadziej.</p>
<details id="vb" open><summary>&#x1F48E; Value bety</summary>
<div class="kform"><label><input type="checkbox" id="vpod"> uwzględnij podatek 12%</label>
<label>min. przewaga <input type="number" id="vmin" value="5" min="0" max="100" style="width:60px">%</label>
<label>min. szansa <input type="number" id="vpmin" value="30" min="1" max="99" style="width:60px">%</label>
<label>Sortuj <select id="vsort"><option value="ev">przewaga</option>
<option value="kd">kurs: od najwyższego</option><option value="kr">kurs: od najniższego</option>
<option value="p">szansa</option><option value="t">data meczu</option></select></label>
<label>maks. przewaga <input type="number" id="vmax" value="40" min="5" max="1000" style="width:60px">%</label></div>
<div class="wrap" id="vlista"></div>
<p class="knot">Value bet = kurs u bukmachera wyższy, niż wynika ze statystyk. Przewaga = szansa &times; kurs &minus; 1. Przy podatku 12% potrzeba ok. 14% przewagi, żeby wyjść na plus. Pojedynczy value bet i tak często przegrywa: zysk pojawia się dopiero przy wielu takich zakładach. Bardzo duża przewaga (np. +100%) to prawie zawsze błąd modelu, a nie okazja: bukmacher wie o składach, kontuzjach i formie więcej niż statystyka - dlatego domyślnie ukrywam przewagi powyżej 40%.</p></details>
<details id="gen" open><summary>&#x1F3B2; Generator kuponów</summary>
<div class="kform">
<label>min. prawd. typu <input type="number" id="gmin" value="70" min="50" max="99">%</label>
<label>maks. zdarzeń <input type="number" id="gmax" value="6" min="1" max="20"></label>
<label>kursy kuponów <input type="text" id="gcele" value="2, 4, 8, 20" style="width:110px"></label>
<button class="kbt" id="ggen">Generuj</button></div>
<p class="knot" style="margin-top:0">Generator bierze pod uwagę filtry u góry strony (rodzaj typów, wyszukiwarkę). Każdy kupon to inne zdarzenia, gdy tylko się da.</p>
<div class="gprop" id="gprop"></div></details>
<div id="lista"></div>
<section id="stan" class="panel-ukryty"></section></main>
<button id="kbtn">&#x1F3AB; Kupon (0)</button>
<div id="kpanel"><h2>Kreator kuponu</h2>
<div class="kform"><b style="color:var(--txt)">Zbuduj automatycznie:</b>
<label>kurs ok. <input type="number" id="acel" value="3" step="0.1" min="1.1"></label>
<label>min. prawd. typu <input type="number" id="amin" value="75" min="50" max="99">%</label>
<label>maks. zdarzeń <input type="number" id="amax" value="5" min="1" max="20"></label>
<button class="kbt" id="auto">Zbuduj</button></div>
<div class="knot" id="anot"></div>
<div id="kpoz"></div>
<div class="kform"><label>Stawka <input type="number" id="stawka" value="10" min="1" step="1"> zł</label></div>
<div class="ksum" id="ksum"></div>
<div><button class="kbt" id="kopiuj">Kopiuj kupon</button><button class="kbt sz" id="czysc">Wyczyść</button></div>
<p class="knot">Kurs w polu możesz zmienić na kurs ze swojego bukmachera. Podatek: 12% od stawki, a od wygranej powyżej 2280 zł dodatkowo 10%. Szansa kuponu to iloczyn szans typów (zakłada, że mecze są niezależne). Z jednego meczu można wziąć tylko jeden typ.</p></div>
<script>
const D=__JSON__;
const ID={};D.forEach((m,i)=>m.typy.forEach((x,j)=>{x.m=m;x.mi=i;x.id=i+"-"+j;x.kl=m.h+"|"+m.g+"|"+x.typ;ID[x.id]=x}));
const K=new Map(); // id -> kurs wpisany
const zapisz=()=>{try{localStorage.setItem("kupon",JSON.stringify([...K].map(([id,k])=>[ID[id].kl,k])));localStorage.setItem("stawka",stawka.value)}catch(e){}};
function wczytaj(){try{const z=JSON.parse(localStorage.getItem("kupon")||"[]");const po={};Object.values(ID).forEach(x=>po[x.kl]=x);
for(const [kl,k] of z)if(po[kl])K.set(po[kl].id,k);const s=localStorage.getItem("stawka");if(s)stawka.value=s}catch(e){}}
function dodaj(id){const x=ID[id];if(K.has(id)){K.delete(id)}else{for(const k of [...K.keys()])if(ID[k].mi===x.mi)K.delete(k);
K.set(id,x.rynek||x.kurs)}zapisz();rys();kupon()}
function kupon(){kbtn.innerHTML=`&#x1F3AB; Kupon (${K.size})`;
if(!K.size){kpoz.innerHTML='<p class="knot">Kliknij „+” przy typie albo zbuduj kupon automatycznie.</p>';ksum.innerHTML="";return}
let P=1,KU=1;kpoz.innerHTML=[...K].map(([id,k])=>{const x=ID[id];P*=x.p/100;KU*=+k||1;
return`<div class="kp"><button class="x" data-us="${id}">&times;</button><span class="mm">${x.m.liga} &middot; ${x.m.data} ${x.m.godz}</span><br>
<b>${x.m.h} &ndash; ${x.m.g}</b><br>${x.typ} &middot; <span class="p ${kl(x.p)}" style="min-width:0;padding:0 6px">${x.p}%</span>
&middot; kurs <input type="number" step="0.01" min="1.01" value="${(+k).toFixed(2)}" data-kurs="${id}"> <span class="mm">(uczciwy ${x.kurs.toFixed(2)})</span></div>`}).join("");
const st=+stawka.value||0,brutto=st*0.88*KU,pod=brutto>2280?brutto*0.1:0,netto=brutto-pod,ev=P*KU*0.88;
ksum.innerHTML=`<span>Zdarzeń</span><b>${K.size}</b><span>Łączny kurs</span><b>${KU.toFixed(2)}</b>
<span>Szansa kuponu (ze statystyk)</span><b>${(P*100).toFixed(1)}%</b><span>Uczciwy kurs kuponu</span><b>${(1/P).toFixed(2)}</b>
<span>Możliwa wygrana (po podatku 12%)</span><b>${brutto.toFixed(2)} zł</b>${pod?`<span>Podatek od wygranej 10%</span><b>-${pod.toFixed(2)} zł</b>`:""}
<span>Na rękę</span><b style="font-size:15px">${netto.toFixed(2)} zł</b>
<span>Opłacalność (oczekiwany zwrot)</span><b style="color:${ev>=1?"#00b050":"#e46c0a"}">${(ev*100).toFixed(0)}%</b>`}
kbtn.onclick=()=>kpanel.classList.toggle("otw");
document.addEventListener("click",e=>{const b=e.target.closest("[data-id]");if(b){dodaj(b.dataset.id);return}
const u=e.target.closest("[data-us]");if(u){K.delete(u.dataset.us);zapisz();rys();kupon()}});
document.addEventListener("change",e=>{const i=e.target.closest("[data-kurs]");if(i){K.set(i.dataset.kurs,+i.value||1);zapisz();kupon()}});
stawka.addEventListener("input",()=>{zapisz();kupon()});
czysc.onclick=()=>{K.clear();zapisz();rys();kupon();anot.textContent=""};
kopiuj.onclick=()=>{let KU=1;const t=[...K].map(([id,k])=>{const x=ID[id];KU*=+k;return`${x.m.data} ${x.m.godz} ${x.m.h} - ${x.m.g}: ${x.typ} @ ${(+k).toFixed(2)}`});
t.push(`Kurs łączny: ${KU.toFixed(2)}`);const txt=t.join("\\n");
(navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).then(()=>kopiuj.textContent="Skopiowano!",()=>{prompt("Skopiuj:",txt)});
setTimeout(()=>kopiuj.textContent="Kopiuj kupon",2000)};
function buduj(cel,mn,mx,wyklucz=new Set(),los=0){const q=szukaj.value.toLowerCase();
const r=x=>x.p*(1+(Math.random()-.5)*los);
const mecze=D.filter((m,i)=>!wyklucz.has(i)&&(!q||(m.h+" "+m.g+" "+m.liga).toLowerCase().includes(q)))
.map(m=>m.typy.filter(x=>x.p>=mn&&pasujeKat(x)).map(x=>({x,k:x.rynek||x.kurs,s:r(x)}))).filter(c=>c.length);
let best=null;
for(let n=1;n<=Math.min(mx,mecze.length);n++){const prog=Math.pow(cel,1/n)*0.97;
const wyb=mecze.map(c=>c.filter(z=>z.k>=prog).sort((a,b)=>b.s-a.s)[0]).filter(Boolean).sort((a,b)=>b.s-a.s).slice(0,n);
if(wyb.length<n)continue;const KU=wyb.reduce((s,z)=>s*z.k,1),P=wyb.reduce((s,z)=>s*z.x.p/100,1);
if(KU>=cel*0.9&&(!best||P>best.P))best={wyb,KU,P}}
if(!best){if(!mecze.length)return null;const wyb=mecze.map(c=>c.sort((a,b)=>b.k-a.k)[0]).sort((a,b)=>b.x.p-a.x.p).slice(0,mx);
best={wyb,KU:wyb.reduce((s,z)=>s*z.k,1),P:wyb.reduce((s,z)=>s*z.x.p/100,1),brak:true}}
return best}
auto.onclick=()=>{const cel=+acel.value||3,best=buduj(cel,+amin.value||75,+amax.value||5);
K.clear();if(best)for(const z of best.wyb)K.set(z.x.id,z.k);
anot.textContent=!best?"Brak typów spełniających warunki - obniż min. prawdopodobieństwo.":
best.brak?`Nie da się dojść do kursu ${cel} - wzięto najwyższe dostępne (kurs ${best.KU.toFixed(2)}). Obniż min. prawdopodobieństwo albo zwiększ liczbę zdarzeń.`:
`Dobrano ${K.size} zdarz. tak, żeby przy kursie ok. ${cel} szansa kuponu była najwyższa (${(best.P*100).toFixed(0)}%). Sprawdź kursy u bukmachera.`;
zapisz();rys();kupon()};
const NAZWY_POZ=c=>c<=2.5?"Pewny":c<=5?"Średni":c<=12?"Odważny":"Ryzykowny";
let PROP=[];
function karta(i){const g=PROP[i];if(!g.b)return`<div class="gk"><h3>${NAZWY_POZ(g.cel)} (kurs ~${g.cel})</h3><p class="mm">Za mało typów przy tych ustawieniach.</p></div>`;
const st=+stawka.value||10,br=st*.88*g.b.KU,netto=br>2280?br*.9:br;
return`<div class="gk"><h3><span>${NAZWY_POZ(g.cel)} (kurs ~${g.cel})</span><span class="p ${kl(g.b.P*100)}" style="min-width:0">${(g.b.P*100).toFixed(0)}%</span></h3>
<ul>${g.b.wyb.map(z=>`<li><b>${z.x.m.h} &ndash; ${z.x.m.g}</b>: ${z.x.typ} @ ${z.k.toFixed(2)} <span class="mm">(${z.x.p}%) &middot; ${z.x.m.data} ${z.x.m.godz}</span></li>`).join("")}</ul>
<div>Kurs <b>${g.b.KU.toFixed(2)}</b> &middot; ${st} zł &rarr; <b>${netto.toFixed(2)} zł</b>${g.b.brak?' <span class="mm">(nie doszło do celu)</span>':""}</div>
<button class="kbt" data-wez="${i}">Weź ten kupon</button><button class="kbt sz" data-losuj="${i}">&#x1F3B2; Losuj inny</button></div>`}
function generuj(){const mn=+gmin.value||70,mx=+gmax.value||6;
const cele=gcele.value.split(/[;,\\s]+/).map(Number).filter(x=>x>1).slice(0,8);
const uzyte=new Set();PROP=cele.map(cel=>{let b=buduj(cel,mn,mx,uzyte);if(!b||b.brak)b=buduj(cel,mn,mx)||b;
if(b)b.wyb.forEach(z=>uzyte.add(z.x.mi));return{cel,b}});
gprop.innerHTML=PROP.map((_,i)=>karta(i)).join("")}
ggen.onclick=generuj;
gprop.addEventListener("click",e=>{const w=e.target.closest("[data-wez]"),l=e.target.closest("[data-losuj]");
if(w){const g=PROP[+w.dataset.wez];K.clear();g.b.wyb.forEach(z=>K.set(z.x.id,z.k));zapisz();rys();kupon();kpanel.classList.add("otw")}
if(l){const i=+l.dataset.losuj,g=PROP[i],stare=g.b?new Set(g.b.wyb.map(z=>z.x.mi)):new Set();
let b=null;for(let t=0;t<12&&!b;t++){const c=buduj(g.cel,+gmin.value||70,+gmax.value||6,new Set(),0.5);
if(c&&!c.brak&&c.wyb.some(z=>!stare.has(z.x.mi)||!K.has(z.x.id)))b=c}
const c2=b||buduj(g.cel,+gmin.value||70,+gmax.value||6,stare);if(c2)g.b=c2;
gprop.children[i].outerHTML=karta(i)}});
const seria=x=>x.kat==="zaw"?`<br><span class="mm">${x.seria>=2?`&#x1F525; seria ${x.seria} &middot; `:""}ostatnie: ${x.ost}</span>`:"";
const wiersz=x=>`<td class="c-btn"><button class="dod${K.has(x.id)?" on":""}" data-id="${x.id}" title="Dodaj do kuponu">${K.has(x.id)?"&#10003;":"+"}</button></td><td class="c-typ">${x.typ}${seria(x)}</td><td class="c-p"><span class="p ${kl(x.p)}">${x.p}%</span></td><td class="c-br"></td><td class="c-x" data-l="uczciwy">${x.kurs.toFixed(2)}</td>
<td class="c-x" data-l="kurs">${x.rynek?x.rynek.toFixed(2):"-"} ${x.value?'<span class="val">VALUE</span>':""}${x.k0?`<br><span class="mm" style="color:${x.k0>x.rynek?"#00b050":"#e0533d"}">${x.k0>x.rynek?"&#x2193;":"&#x2191;"} było ${x.k0.toFixed(2)}</span>`:""}${x.buk?`<br><span class="mm">${x.buk}</span>`:""}</td><td class="c-x" data-l="${x.m.h}">${st(x.fh)}</td><td class="c-x" data-l="${x.m.g}">${st(x.fg)}</td><td class="c-x" data-l="H2H">${st(x.h2h)}</td>`;
const kl=p=>p>=80?"z1":p>=70?"z2":p>=60?"z3":"z4";
const kol=q=>q>=.8?"#00b050":q>=.6?"#92d050":q>=.4?"#ffd84d":"#f4a582";
function st(s){if(!s[1])return'<span style="color:var(--mut)">-</span>';const q=s[0]/s[1];
return`<span class="st">${s[0]}/${s[1]}<span class="bar"><i style="width:${q*100}%;background:${kol(q)}"></i></span></span>`}
const r=s=>s[1]?s[0]/s[1]:-1;
const KLUCZ={p:x=>-x.p,t:x=>x.m.ts,kd:x=>-x.kurs,kr:x=>x.kurs,rd:x=>-(x.rynek||0),f:x=>-((r(x.fh)+r(x.fg))/2),h:x=>-r(x.h2h)};
const sortuj=a=>a.sort((x,y)=>KLUCZ[sort.value](x)-KLUCZ[sort.value](y)||y.p-x.p);
let RK="",DZIEN="",TYLKO_UL=false,UL=new Set();
try{UL=new Set(JSON.parse(localStorage.getItem("ulubione")||"[]"))}catch(e){}
const mkey=m=>m.h+"|"+m.g+"|"+m.data;
const brakTxt=()=>TYLKO_UL&&![...UL].some(k=>D.some(m=>mkey(m)===k))?'<p>Nie masz jeszcze ulubionych meczów. Dotknij &#9734; przy meczu, żeby go dodać.</p>':'<p>Brak typów przy tych filtrach.</p>';
const zapiszUl=()=>{try{localStorage.setItem("ulubione",JSON.stringify([...UL].filter(k=>D.some(m=>mkey(m)===k))))}catch(e){}};
const pasujeKat=x=>(!TYLKO_UL||UL.has(mkey(x.m)))&&(!liga.value||x.m.liga===liga.value)&&(!DZIEN||x.m.data===DZIEN)&&(!RK||x.rk===RK)&&(!kat.value||x.kat===kat.value||(kat.value==="druz"&&(x.kat==="gosp"||x.kat==="gosc")));
// ligi: lista z liczbą meczów
(()=>{const L={};D.forEach(m=>L[m.liga]=(L[m.liga]||0)+1);
liga.innerHTML='<option value="">wszystkie</option>'+Object.keys(L).sort().map(l=>`<option value="${l}">${l} (${L[l]})</option>`).join("")})();
// dni: przyciski z datami meczów
const ts=(d,g)=>{const [dd,mm,rr]=(d||"").split("/").map(Number),[hh,mi]=(g||"00:00").split(":").map(Number);return rr?new Date(rr,mm-1,dd,hh||0,mi||0).getTime():0};
D.forEach(m=>m.ts=ts(m.data,m.godz));
(()=>{const DNI=["Nd","Pn","Wt","Śr","Cz","Pt","So"],dz=new Date();dz.setHours(0,0,0,0);
const daty=[...new Set(D.map(m=>m.data).filter(Boolean))].sort((a,b)=>ts(a)-ts(b));
const ety=d=>{const t=new Date(ts(d)),r=Math.round((t-dz)/864e5),dm=d.slice(0,5).replace("/",".");
return r===0?`Dziś ${dm}`:r===1?`Jutro ${dm}`:`${DNI[t.getDay()]} ${dm}`};
dni.innerHTML=`<button class="chip on" data-dz="">&#x1F4C5; Wszystkie dni</button>`+daty.map(d=>{
const n=D.filter(m=>m.data===d).length;return`<button class="chip" data-dz="${d}">${ety(d)} <span style="opacity:.7">(${n})</span></button>`}).join("")+
`<button class="chip" id="ulChip" aria-pressed="false">&#x2B50; Ulubione <span style="opacity:.7">(${[...UL].filter(k=>D.some(m=>mkey(m)===k)).length})</span></button>`;
ulChip.addEventListener("click",()=>{TYLKO_UL=!TYLKO_UL;ulChip.classList.toggle("on",TYLKO_UL);ulChip.setAttribute("aria-pressed",TYLKO_UL);rys()});
dni.addEventListener("click",e=>{const c=e.target.closest("[data-dz]");if(!c)return;DZIEN=c.dataset.dz;
dni.querySelectorAll("[data-dz]").forEach(b=>b.classList.toggle("on",b===c));rys()})})();
lista.addEventListener("click",e=>{const b=e.target.closest("[data-ul]");if(!b)return;const k=b.dataset.ul;
UL.has(k)?UL.delete(k):UL.add(k);zapiszUl();ulChip.querySelector("span").textContent=`(${[...UL].filter(k=>D.some(m=>mkey(m)===k)).length})`;
if(TYLKO_UL)rys();else{b.classList.toggle("on",UL.has(k));b.innerHTML=UL.has(k)?"&#9733;":"&#9734;";b.setAttribute("aria-label",UL.has(k)?"Usuń z ulubionych":"Dodaj do ulubionych")}});
chipy.addEventListener("click",e=>{const c=e.target.closest("[data-rk]");if(!c)return;RK=c.dataset.rk;
chipy.querySelectorAll(".chip").forEach(b=>b.classList.toggle("on",b===c));rys()});
function value(){const pod=vpod.checked?0.88:1,mn=(+vmin.value||0)/100,pm=+vpmin.value||0;
const t=D.flatMap(m=>m.typy).filter(x=>x.rynek&&x.p>=pm&&pasujeKat(x)).map(x=>({x,ev:x.p/100*x.rynek*pod-1})).filter(z=>z.ev>=mn&&z.ev<=(+vmax.value||40)/100);
const VS={ev:(a,b)=>b.ev-a.ev,kd:(a,b)=>b.x.rynek-a.x.rynek,kr:(a,b)=>a.x.rynek-b.x.rynek,p:(a,b)=>b.x.p-a.x.p,t:(a,b)=>a.x.m.ts-b.x.m.ts};
t.sort((a,b)=>VS[vsort.value](a,b)||b.ev-a.ev);
vlista.innerHTML=t.length?`<table><tr><th></th><th>Mecz</th><th>Typ</th><th>Szansa</th><th>Uczciwy kurs</th><th>Kurs</th><th>Przewaga</th></tr>
${t.map(({x,ev})=>`<tr><td class="c-btn"><button class="dod${K.has(x.id)?" on":""}" data-id="${x.id}">${K.has(x.id)?"&#10003;":"+"}</button></td>
<td class="c-m"><b>${x.m.h} &ndash; ${x.m.g}</b><br><span class="mm">${x.m.liga} &middot; ${x.m.data} ${x.m.godz}</span></td><td class="c-typ">${x.typ}</td>
<td class="c-p"><span class="p ${kl(x.p)}">${x.p}%</span></td><td class="c-br"></td><td class="c-x" data-l="uczciwy">${x.kurs.toFixed(2)}</td><td class="c-x" data-l="kurs"><b>${x.rynek.toFixed(2)}</b>${x.buk?` <span class="mm">${x.buk}</span>`:""}</td>
<td class="c-x plus" data-l="przewaga">+${(ev*100).toFixed(1)}%</td></tr>`).join("")}</table>`:'<p class="knot">Brak value betów przy tych ustawieniach. Kursy bukmacherów są tylko dla 1X2 i goli (więcej rynków z kluczem The Odds API).</p>'}
for(const e of [vpod,vmin,vpmin,vmax,vsort])e.addEventListener("input",value);
function rys(){value();const mn=+min.value,mf=+minf.value/100,v=val.checked,q=szukaj.value.toLowerCase();
minv.textContent=mn;minfv.textContent=minf.value;let html="";
const ok=x=>pasujeKat(x)&&x.p>=mn&&(!v||x.value)&&(!mf||[x.fh,x.fg].every(s=>s[1]&&s[0]/s[1]>=mf));
const pasuje=m=>!q||(m.h+" "+m.g+" "+m.liga).toLowerCase().includes(q);
if(razem.checked){const t=sortuj(D.filter(pasuje).flatMap(m=>m.typy.filter(ok)));
lista.innerHTML=t.length?`<div class="mecz"><div class="wrap"><table><tr><th></th><th>Mecz</th><th>Typ</th><th>Prawd.</th><th>Uczciwy kurs</th><th>Kurs rynku</th><th>Forma gosp.</th><th>Forma gościa</th><th>H2H</th></tr>
${t.map(x=>`<tr>${wiersz(x).replace("</td>",`</td><td class="c-m"><b>${x.m.h} &ndash; ${x.m.g}</b><br><span style="color:var(--mut);font-size:11px">${x.m.liga} &middot; ${x.m.data} ${x.m.godz}</span></td>`)}</tr>`).join("")}</table></div></div>`:brakTxt();return}
const mecze=D.filter(pasuje).map(m=>({m,t:sortuj(m.typy.filter(ok))})).filter(z=>z.t.length);
if(sort.value!=="p")mecze.sort((a,b)=>KLUCZ[sort.value](a.t[0])-KLUCZ[sort.value](b.t[0]));
for(const {m,t} of mecze){const mk=m.h+"|"+m.g+"|"+m.data,zw=ZWIN_DOM!==PRZEL.has(mk),naj=t[0];
const xs_=m.lh+m.la||1;
html+=`<div class="mecz${zw?" zw":""}" data-mk="${mk}"><div class="top"><span class="strz">${zw?"&#9656;":"&#9662;"}</span>
<div class="meta"><span class="godz">${m.godz||"--:--"}</span><span>${m.data.slice(0,5).replace("/",".")}</span><span class="liga">${m.liga}</span><button class="gw${UL.has(mk)?" on":""}" data-ul="${mk}" aria-label="${UL.has(mk)?"Usuń z ulubionych":"Dodaj do ulubionych"}">${UL.has(mk)?"&#9733;":"&#9734;"}</button></div>
<div class="sb"><div class="t">${m.h}</div><div class="xg"><span class="h">${m.lh.toFixed(2)}</span><div class="xgbar"><i style="width:${(m.lh/xs_*100).toFixed(0)}%"></i></div><span class="a">${m.la.toFixed(2)}</span><div class="xgl">oczekiwane gole</div></div><div class="t t2">${m.g}</div></div>${m.p1x2?`
<div class="wdl"><div class="wdlbar"><i class="w1" style="width:${m.p1x2[0]}%"></i><i class="wx" style="width:${m.p1x2[1]}%"></i><i class="w2" style="width:${m.p1x2[2]}%"></i></div>
<div class="wdlt"><span><b class="h">${m.p1x2[0]}%</b> wygrana ${m.h}</span><span><b>${m.p1x2[1]}%</b> remis</span><span><b class="a">${m.p1x2[2]}%</b> wygrana ${m.g}</span></div></div>`:""}
<div class="skrot">${naj.typ} <span class="p ${kl(naj.p)}" style="min-width:0;padding:0 8px">${naj.p}%</span> <span class="m">&middot; typów: ${t.length} &middot; dotknij, aby rozwinąć</span></div>
<div class="info">${m.kartki?`<span>Kartki ~<b>${m.kartki}</b></span>`:""}${m.rozne?`<span>Rożne ~<b>${m.rozne}</b></span>`:""}${m.sedzia?`<span>Sędzia: <b>${m.sedzia}</b></span>`:""}${m.z_kursow?`<span title="Jedna z drużyn gra w lidze spoza bazy danych">&#x2139;&#xFE0F; prognoza tylko z kursów bukmacherów</span>`:""}
<span>Najczęstsze wyniki: ${m.wyniki.join(", ")}</span></div>
<div class="info"><span>H2H: ${m.h2h.length?m.h2h.join(" &middot; "):"brak meczów"}</span></div></div>
<div class="wrap"><table><tr><th></th><th>Typ</th><th>Prawd.</th><th>Uczciwy kurs</th><th>Kurs rynku</th><th>Forma ${m.h}</th><th>Forma ${m.g}</th><th>H2H</th></tr>
${t.map(x=>`<tr>${wiersz(x)}</tr>`).join("")}
</table></div>${m.lab?`<div class="labwrap"><button class="labbtn" data-labopen="${mk}">&#x1F527; Sprawdź własną linię</button>
<div class="lab${LABOTW.has(mk)?"":" panel-ukryty"}" data-lab="${mk}">${LABOTW.has(mk)?labHTML(m):""}</div></div>`:""}</div>`}
lista.innerHTML=(html?`<div class="zwbar"><button class="kbt sz" id="zwAll">&#9656; Zwiń wszystkie</button><button class="kbt sz" id="rozAll">&#9662; Rozwiń wszystkie</button></div>`:"")+(html||brakTxt())}
const STAN=__STAN__;
// zakładki
const PANELE={mecze:["lista"],vb:["vb"],gen:["gen"],stan:["stan"]};
taby.addEventListener("click",e=>{const b=e.target.closest("[data-tab]");if(!b)return;
taby.querySelectorAll("[data-tab]").forEach(x=>x.classList.toggle("on",x===b));
for(const [t,ids] of Object.entries(PANELE))ids.forEach(id=>document.getElementById(id).classList.toggle("panel-ukryty",t!==b.dataset.tab));
if(b.dataset.tab==="vb")vb.open=true;if(b.dataset.tab==="gen")gen.open=true;scrollTo({top:0})});
["vb","gen"].forEach(id=>document.getElementById(id).classList.add("panel-ukryty"));
// liczby na górze
(()=>{const dz=new Date(),d=`${String(dz.getDate()).padStart(2,"0")}/${String(dz.getMonth()+1).padStart(2,"0")}/${dz.getFullYear()}`;
const dzis=D.filter(m=>m.data===d).length,pewne=D.reduce((s,m)=>s+m.typy.filter(x=>x.p>=80&&x.kat!=="zaw").length,0),
val=D.reduce((s,m)=>s+m.typy.filter(x=>x.value).length,0);
const odm=(n,a,b,c)=>n===1?a:(n%10>=2&&n%10<=4&&(n%100<10||n%100>=20)?b:c);
kpi.innerHTML=`<span><b>${D.length}</b>${odm(D.length,"mecz","mecze","meczów")} (dziś ${dzis})</span><span><b>${pewne}</b>${odm(pewne,"typ","typy","typów")} od 80%</span><span><b>${val}</b>${odm(val,"value bet","value bety","value betów")}</span>`+
(STAN.wczoraj?`<span><b>${STAN.wczoraj[0]}/${STAN.wczoraj[1]}</b>wczoraj weszło</span>`:"")})();
// stan danych
(()=>{const z=(ok,t)=>`<span class="${ok?"ok":"zle"}">${ok?"&#10003;":"&#10007;"}</span> ${t}`;
const L=Object.entries(STAN.ligi||{}).sort((a,b)=>b[1]-a[1]).map(([l,n])=>`${l}: ${n}`).join(", ")||"brak meczów";
stan.innerHTML=`<div class="k"><h3 style="margin:0 0 10px;font-family:var(--cond);font-size:22px">Stan danych</h3>
<p class="m">Co się pobrało przy ostatnim odświeżeniu (${STAN.czas}). Strona odświeża się sama co 3 godziny.</p>
<div class="stan-g">
<div><b>${D.length}</b>meczów w najbliższych dniach<br><span class="m">${L}</span></div>
<div><b>${STAN.baza??0}</b>rozliczonych meczów w bazie<br><span class="m">${STAN.czeka??0} czeka na wynik</span></div>
<div><b>${STAN.kal?"włączona":"wyłączona"}</b>kalibracja procentów<br><span class="m">${STAN.kal?"procenty poprawione według tego, jak typy naprawdę wchodziły w backteście":"poprawka nie pomagała, więc procenty są prosto z modelu"}</span></div>
</div>
</div>`})();
// ===== Sprawdź własną linię =====
const KAL=__KAL__;
const kalibruj=(p,rk)=>{const pk=KAL[rk]||KAL.wszystkie;if(!pk||!pk.length)return p;
const c=v=>Math.min(.99,Math.max(.01,v));if(p<=pk[0][0])return c(p+pk[0][1]-pk[0][0]);if(p>=pk[pk.length-1][0])return c(p+pk[pk.length-1][1]-pk[pk.length-1][0]);
for(let i=0;i<pk.length-1;i++){const [x1,y1]=pk[i],[x2,y2]=pk[i+1];if(p>=x1&&p<=x2)return c(y1+(y2-y1)*(p-x1)/((x2-x1)||1))}return p};
const pois=(k,l)=>{let r=Math.exp(-l);for(let i=1;i<=k;i++)r*=l/i;return r};
const powyzej=(lin,l)=>{let s=0;for(let k=0;k<=Math.floor(lin);k++)s+=pois(k,l);return 1-s};
const RYN={gole:["Gole",3,"bramki"],rozne:["Rożne",5,"rozne"],kartki:["Kartki",7,"kartki"],strzaly:["Strzały",9,"strzaly"],celne:["Celne",11,"strzaly"]};
const LABOTW=new Set(),LABST={};
const mByKey=k=>D.find(m=>m.h+"|"+m.g+"|"+m.data===k);
const sr=a=>a.length?a.reduce((s,x)=>s+x,0)/a.length:null;
// oczekiwane wartości: z modelu albo (gdy go brak) z historii znanej drużyny
function lamFor(m,rk){const L=m.lab.lam[rk];if(L)return{l:L,est:false};const i=RYN[rk][1];
const col=(rows,c)=>rows.map(r=>r[c]).filter(v=>v!=null);
const hOwn=sr(col(m.lab.hh,i)),hOpp=sr(col(m.lab.hh,i+1)),aOwn=sr(col(m.lab.ha,i)),aOpp=sr(col(m.lab.ha,i+1));
const lh=sr([hOwn,aOpp].filter(v=>v!=null)),la=sr([aOwn,hOpp].filter(v=>v!=null));
return lh==null||la==null?null:{l:[lh,la],est:true}}
const dostepne=m=>Object.keys(RYN).filter(r=>lamFor(m,r));
// ---- własna linia dla zawodnika (wiersz: data, rywal, dom, gole, asysty, strzały, celne, kartki, faule, faulowany, minuty)
const ZST={sh:["Strzały",5],sot:["Celne",6],gol:["Gole",3],as:["Asysty",4],kar:["Kartki",7],fc:["Faule",8],fs:["Faulowany",9]};
const zawLista=m=>{const z=(m.lab&&m.lab.zaw)||{};return[...(z.H||[]).map((p,i)=>["H",i,p]),...(z.A||[]).map((p,i)=>["A",i,p])]};
const zawGracz=(m,S)=>(m.lab.zaw[S.zp[0]]||[])[S.zp[1]];
const zawLinia=(m,S)=>{S.zlin=Math.floor(sr(zawGracz(m,S)[1].map(r=>r[ZST[S.zs][1]])))+.5};
function zawStart(m,S){const L=zawLista(m);if(!L.length)return false;if(!S.zp){
const b=L.slice().sort((x,y)=>sr(y[2][1].map(r=>r[5]))-sr(x[2][1].map(r=>r[5])))[0];S.zp=[b[0],b[1]];S.zs="sh";zawLinia(m,S)}return true}
const segRyn=(m,S)=>`<div class="seg">${Object.entries(RYN).filter(([r])=>lamFor(m,r)).map(([r,[n]])=>`<button class="${r===S.rk?"on":""}" data-lset="rk" data-v="${r}">${n}</button>`).join("")}${zawLista(m).length?`<button class="${S.rk==="zaw"?"on":""}" data-lset="rk" data-v="zaw">&#x1F464; Zawodnik</button>`:""}</div>`;
function zawBarInfo(m,S,j){const g=zawGracz(m,S);if(j==null)return"Najedź albo dotknij słupka, żeby zobaczyć mecz.";const r=g[1][j],x=r[ZST[S.zs][1]];
return`<b>${r[0]}</b> &middot; ${r[2]?"u siebie":"na wyjeździe"} z ${r[1]} &middot; ok. ${r[10]} min<br>${ZST[S.zs][0]}: <b>${x}</b> (${x>S.zlin?"powyżej":"poniżej"} ${S.zlin.toFixed(1)})`}
function zawHTML(m,S){const [imie,rows]=zawGracz(m,S),i=ZST[S.zs][1],v=rows.map(r=>r[i]),lam=sr(v),tr=v.filter(x=>x>S.zlin).length;
let p=.6*powyzej(S.zlin,lam)+.4*tr/v.length;p=Math.min(.98,Math.max(.02,p));const q=1-p,k=parseFloat(String(S.kurs).replace(",",".")),kq=parseFloat(String(S.kursq).replace(",","."));
const ev=k>1?p*k-1:null,evq=kq>1?q*kq-1:null;
const vb=e=>e==null?"":`<span class="${e>0?"valb":"m"}">${e>0?`VALUE +${(e*100).toFixed(0)}%`:`bez value (${(e*100).toFixed(0)}%)`}</span>`;
const opcje=["H","A"].map(st=>(m.lab.zaw[st]||[]).length?`<optgroup label="${st==="H"?m.h:m.g}">${m.lab.zaw[st].map((g,j)=>`<option value="${st}|${j}"${S.zp[0]===st&&S.zp[1]===j?" selected":""}>${g[0]}</option>`).join("")}</optgroup>`:"").join("");
const mx=Math.max(S.zlin+2,...v),druz=S.zp[0]==="H"?m.h:m.g;
return`${segRyn(m,S)}<select class="zsel" data-zsel aria-label="Zawodnik">${opcje}</select>
<div class="seg male">${Object.entries(ZST).map(([k_,[n]])=>`<button class="${k_===S.zs?"on":""}" data-zset="st" data-v="${k_}">${n}</button>`).join("")}</div>
<div class="linia"><button data-zset="minus" aria-label="Zmniejsz linię">&minus;</button><span>${S.zlin.toFixed(1)}</span><button data-zset="plus" aria-label="Zwiększ linię">+</button></div>
<p class="m" style="text-align:center;margin:0">średnio ${lam.toFixed(1)} na mecz &middot; ${imie} (${druz}) &middot; zakłada, że zagra</p>
<div class="wyn"><div class="${p>=.5?"pow":""}"><b>${(p*100).toFixed(0)}%</b>powyżej ${S.zlin.toFixed(1)}<br><small>uczciwy kurs ${(1/p).toFixed(2)}</small></div>
<div class="${q>.5?"pow":""}"><b>${(q*100).toFixed(0)}%</b>poniżej ${S.zlin.toFixed(1)}<br><small>uczciwy kurs ${(1/q).toFixed(2)}</small></div></div>
<div class="kursl">Kurs u bukmachera: powyżej <input inputmode="decimal" data-lset="kurs" data-pole="kurs" value="${S.kurs}" placeholder="1.85"> ${vb(ev)}</div>
<div class="kursl" style="margin-top:-6px">poniżej <input inputmode="decimal" data-lset="kurs" data-pole="kursq" value="${S.kursq}" placeholder="1.95"> ${vb(evq)}</div>
<div class="hist"><div class="r"><span class="n">${imie}<br><small>ost. ${v.length}</small></span><div class="kropki">${rows.map((r,j)=>{const x=r[i];
return`<i class="${x>S.zlin?"t":""}${S.zbar===j?" wyb":""}" title="${r[0]} ${r[2]?"u siebie":"wyjazd"} z ${r[1]} · ${ZST[S.zs][0].toLowerCase()}: ${x}" style="height:${Math.max(6,x/mx*100)}%" data-zbar="${j}"></i>`}).join("")}
<div class="lin" style="bottom:${S.zlin/mx*100}%"></div></div><b>${tr}/${v.length}</b></div></div>
<div class="barinfo m">${zawBarInfo(m,S,S.zbar)}</div>`}
function labStan(m){const k=m.h+"|"+m.g+"|"+m.data;if(!LABST[k]){const rk=lamFor(m,"rozne")?"rozne":"gole";
const L0=lamFor(m,rk).l,l=(L0[0]+L0[1]);LABST[k]={rk,cel:"mecz",lin:Math.floor(l)+.5,kurs:"",kursq:"",bar:null}}return LABST[k]}
function barInfo(m,S,wb){wb=wb||S.bar;if(!wb)return"Najedź albo dotknij słupka, żeby zobaczyć mecz.";const [idx,j]=wb,rows=[m.lab.hh,m.lab.ha,m.lab.h2h][idx];
const swoja=idx===1?(S.cel==="a"||S.cel==="mecz"):(S.cel!=="a");const v=rows.map(r=>[r,wartosc(r,S.rk,S.cel,swoja)]).filter(z=>z[1]!=null)[j];
if(!v)return"Dotknij słupka, żeby zobaczyć mecz.";const r=v[0],i=RYN[S.rk][1],druz=idx===1?m.g:m.h,gole=r[2]?`${r[3]}:${r[4]}`:`${r[4]}:${r[3]}`;
return`<b>${r[0]}</b> &middot; ${r[2]?`${druz} &ndash; ${r[1]}`:`${r[1]} &ndash; ${druz}`} <b>${gole}</b><br>
${RYN[S.rk][0]}: ${druz} ${r[i]}, ${r[1]} ${r[i+1]} &rarr; liczone: <b>${v[1]}</b> (${v[1]>S.lin?"powyżej":"poniżej"} ${S.lin.toFixed(1)})`}
function wartosc(r,rk,cel,swoja){const i=RYN[rk][1],own=r[i],opp=r[i+1];if(own==null||opp==null)return null;
if(cel==="mecz")return own+opp;return swoja?own:opp}
function labHTML(m){if(labStan(m).rk==="zaw"&&zawStart(m,labStan(m)))return zawHTML(m,labStan(m));const S=labStan(m),LL=lamFor(m,S.rk),L1=LL.l,lamv=S.cel==="mecz"?L1[0]+L1[1]:L1[S.cel==="h"?0:1];
let p=powyzej(S.lin,lamv);const kat=RYN[S.rk][2];p=kalibruj(p,kat);const q=1-p,k=parseFloat(String(S.kurs).replace(",",".")),kq=parseFloat(String(S.kursq).replace(",","."));
const ev=k>1?p*k-1:null,evq=kq>1?q*kq-1:null;
const vb=e=>e==null?"":`<span class="${e>0?"valb":"m"}">${e>0?`VALUE +${(e*100).toFixed(0)}%`:`bez value (${(e*100).toFixed(0)}%)`}</span>`;
const kto={mecz:"Cały mecz",h:m.h,a:m.g};
const wiersze=[[m.h,"ost. "+m.lab.hh.length,m.lab.hh,S.cel!=="a"],[m.g,"ost. "+m.lab.ha.length,m.lab.ha,S.cel==="a"||S.cel==="mecz"],[ "H2H","5 sezonów",m.lab.h2h,S.cel!=="a"]];
const opis=S.cel==="mecz"?"suma obu drużyn":S.cel==="h"?`${m.h}: jego mecze / ile pozwalają rywale ${m.g}`:`${m.g}: jego mecze / ile pozwalają rywale ${m.h}`;
const pasek=(nazwa,pod,rows,swoja,idx)=>{const v=rows.map(r=>[r,wartosc(r,S.rk,S.cel,idx===1?(S.cel==="a"||S.cel==="mecz"):(S.cel!=="a"))]).filter(z=>z[1]!=null);
if(!v.length)return`<div class="r"><span class="n">${nazwa}<br><small>${pod}</small></span><span class="m">brak danych</span><b>-</b></div>`;
const mx=Math.max(S.lin+2,...v.map(z=>z[1])),tr=v.filter(z=>z[1]>S.lin).length;
return`<div class="r"><span class="n">${nazwa}<br><small>${pod}</small></span><div class="kropki">${v.map(([r,x],j)=>
`<i class="${x>S.lin?"t":""}${S.bar&&S.bar[0]===idx&&S.bar[1]===j?" wyb":""}" title="${r[0]} ${r[2]?(idx===1?m.g:m.h)+" – "+r[1]:r[1]+" – "+(idx===1?m.g:m.h)} ${r[2]?r[3]+":"+r[4]:r[4]+":"+r[3]} · ${RYN[S.rk][0].toLowerCase()}: ${x}" style="height:${Math.max(6,x/mx*100)}%" data-bar="${idx}|${j}"></i>`).join("")}
<div class="lin" style="bottom:${S.lin/mx*100}%"></div></div><b>${tr}/${v.length}</b></div>`};
return`${segRyn(m,S)}
<div class="seg male">${Object.entries(kto).map(([c,n])=>`<button class="${c===S.cel?"on":""}" data-lset="cel" data-v="${c}">${n}</button>`).join("")}</div>
<div class="linia"><button data-lset="minus" aria-label="Zmniejsz linię">&minus;</button><span>${S.lin.toFixed(1)}</span><button data-lset="plus" aria-label="Zwiększ linię">+</button></div>
<p class="m" style="text-align:center;margin:0">oczekiwane: ${lamv.toFixed(1)} &middot; ${opis}</p>
${LL.est?`<p class="m" style="text-align:center;margin:4px 0 0">&#x2139;&#xFE0F; szacunek tylko z historii ${m.lab.hh.length?m.h:m.g} &mdash; drugiej drużyny nie ma w danych</p>`:""}
<div class="wyn"><div class="${p>=.5?"pow":""}"><b>${(p*100).toFixed(0)}%</b>powyżej ${S.lin.toFixed(1)}<br><small>uczciwy kurs ${(1/p).toFixed(2)}</small></div>
<div class="${q>.5?"pow":""}"><b>${(q*100).toFixed(0)}%</b>poniżej ${S.lin.toFixed(1)}<br><small>uczciwy kurs ${(1/q).toFixed(2)}</small></div></div>
<div class="kursl">Kurs u bukmachera: powyżej <input inputmode="decimal" data-lset="kurs" data-pole="kurs" value="${S.kurs}" placeholder="1.85"> ${vb(ev)}</div>
<div class="kursl" style="margin-top:-6px">poniżej <input inputmode="decimal" data-lset="kurs" data-pole="kursq" value="${S.kursq}" placeholder="1.95"> ${vb(evq)}</div>
<div class="hist">${pasek(m.h,"ost. "+m.lab.hh.length,m.lab.hh,true,0)}${pasek(m.g,"ost. "+m.lab.ha.length,m.lab.ha,true,1)}${pasek("H2H","5 sezonów",m.lab.h2h,true,2)}</div>
<div class="barinfo m">${barInfo(m,S)}</div>`}
lista.addEventListener("click",e=>{
const o=e.target.closest("[data-labopen]");if(o){const k=o.dataset.labopen,box=o.parentElement.querySelector(".lab");
if(LABOTW.has(k)){LABOTW.delete(k);box.classList.add("panel-ukryty")}else{LABOTW.add(k);box.innerHTML=labHTML(mByKey(k));box.classList.remove("panel-ukryty")}return}
const b=e.target.closest("[data-lset]");if(b&&b.dataset.lset!=="kurs"){const box=b.closest("[data-lab]"),m=mByKey(box.dataset.lab),S=labStan(m);
S.bar=null;S.zbar=null;if(b.dataset.lset==="rk"&&b.dataset.v==="zaw"){S.rk="zaw";zawStart(m,S)}
else if(b.dataset.lset==="rk"){S.rk=b.dataset.v;const L=lamFor(m,S.rk).l;S.lin=Math.floor(S.cel==="mecz"?L[0]+L[1]:L[S.cel==="h"?0:1])+.5}
else if(b.dataset.lset==="cel"){S.cel=b.dataset.v;const L=lamFor(m,S.rk).l;S.lin=Math.floor(S.cel==="mecz"?L[0]+L[1]:L[S.cel==="h"?0:1])+.5}
else if(b.dataset.lset==="minus")S.lin=Math.max(.5,S.lin-1);else if(b.dataset.lset==="plus")S.lin+=1;
box.innerHTML=labHTML(m);return}
const zb=e.target.closest("[data-zset],[data-zbar]");if(zb){const box=zb.closest("[data-lab]"),m=mByKey(box.dataset.lab),S=labStan(m);
if(zb.dataset.zbar!=null)S.zbar=+zb.dataset.zbar;else if(zb.dataset.zset==="st"){S.zs=zb.dataset.v;S.zbar=null;zawLinia(m,S)}
else if(zb.dataset.zset==="minus")S.zlin=Math.max(.5,S.zlin-1);else S.zlin+=1;box.innerHTML=labHTML(m);return}
const bar=e.target.closest("[data-bar]");if(bar){const box=bar.closest("[data-lab]"),m=mByKey(box.dataset.lab),S=labStan(m);
S.bar=bar.dataset.bar.split("|").map(Number);box.innerHTML=labHTML(m)}});
lista.addEventListener("change",e=>{const z=e.target.closest("[data-zsel]");if(!z)return;const box=z.closest("[data-lab]"),m=mByKey(box.dataset.lab),S=labStan(m);
const [st,j]=z.value.split("|");S.zp=[st,+j];S.zbar=null;zawLinia(m,S);box.innerHTML=labHTML(m)});
lista.addEventListener("mouseover",e=>{const zb=e.target.closest("[data-zbar]");if(zb){const box=zb.closest("[data-lab]"),m=mByKey(box.dataset.lab);
box.querySelector(".barinfo").innerHTML=zawBarInfo(m,labStan(m),+zb.dataset.zbar);return}});
lista.addEventListener("mouseout",e=>{const zb=e.target.closest("[data-zbar]");if(zb){const box=zb.closest("[data-lab]"),m=mByKey(box.dataset.lab),S=labStan(m);
box.querySelector(".barinfo").innerHTML=zawBarInfo(m,S,S.zbar)}});
lista.addEventListener("mouseover",e=>{const bar=e.target.closest("[data-bar]");if(!bar)return;
const box=bar.closest("[data-lab]"),m=mByKey(box.dataset.lab),S=labStan(m);
box.querySelector(".barinfo").innerHTML=barInfo(m,S,bar.dataset.bar.split("|").map(Number))});
lista.addEventListener("mouseout",e=>{const bar=e.target.closest("[data-bar]");if(!bar)return;
const box=bar.closest("[data-lab]"),m=mByKey(box.dataset.lab);box.querySelector(".barinfo").innerHTML=barInfo(m,labStan(m))});
lista.addEventListener("input",e=>{const i=e.target.closest('[data-lset="kurs"]');if(!i)return;const box=i.closest("[data-lab]"),m=mByKey(box.dataset.lab);
const pole=i.dataset.pole||"kurs";labStan(m)[pole]=i.value;const pos=i.selectionStart;box.innerHTML=labHTML(m);const n=box.querySelector(`[data-pole="${pole}"]`);n.focus();try{n.setSelectionRange(pos,pos)}catch(x){}});
let ZWIN_DOM=innerWidth<=640;const PRZEL=new Set();
lista.addEventListener("click",e=>{
if(e.target.closest("#zwAll")){ZWIN_DOM=true;PRZEL.clear();rys();return}
if(e.target.closest("#rozAll")){ZWIN_DOM=false;PRZEL.clear();rys();return}
const top=e.target.closest(".top");if(!top||e.target.closest("button,a,input"))return;
const k=top.parentElement.dataset.mk;if(!k)return;PRZEL.has(k)?PRZEL.delete(k):PRZEL.add(k);
const zw=ZWIN_DOM!==PRZEL.has(k);top.parentElement.classList.toggle("zw",zw);top.querySelector(".strz").innerHTML=zw?"&#9656;":"&#9662;"});
if(location.hostname==="localhost"||location.hostname==="127.0.0.1")setTimeout(()=>location.reload(),15*60*1000);
for(const e of [min,minf,val,szukaj,sort,razem,kat,liga])e.addEventListener("input",rys);wczytaj();rys();kupon();generuj();
if(innerWidth<=640){filtrDet.open=false;vb.open=false;gen.open=false}
if("serviceWorker" in navigator&&location.protocol.startsWith("http"))navigator.serviceWorker.register("sw.js").catch(()=>{});
const VAPID=__VAPID__;
const b64=s=>{const p="=".repeat((4-s.length%4)%4),r=atob((s+p).replace(/-/g,"+").replace(/_/g,"/"));return Uint8Array.from([...r].map(c=>c.charCodeAt(0)))};
dzwonek.onclick=async e=>{e.preventDefault();const P=pushpanel;P.style.display="block";
if(!VAPID){P.innerHTML="Powiadomienia nie są jeszcze gotowe - spróbuj po następnym odświeżeniu strony przez GitHuba.";return}
if(!("serviceWorker" in navigator)||!("PushManager" in window)){P.innerHTML="Ta przeglądarka nie obsługuje powiadomień. <b>iPhone:</b> najpierw dodaj stronę do ekranu początkowego (Udostępnij &rarr; Do ekranu początkowego) i otwórz ją z ikony. <b>Android:</b> użyj Chrome.";return}
try{P.innerHTML="Zezwól na powiadomienia w okienku przeglądarki...";
if(await Notification.requestPermission()!=="granted"){P.innerHTML="Powiadomienia zablokowane. Odblokuj je w ustawieniach strony w przeglądarce i spróbuj ponownie.";return}
const reg=await navigator.serviceWorker.register("sw.js");await navigator.serviceWorker.ready;
let sub=await reg.pushManager.getSubscription();
if(sub&&sub.options&&sub.options.applicationServerKey){const k=new Uint8Array(sub.options.applicationServerKey);const v=b64(VAPID);if(k.length!==v.length||k.some((x,i)=>x!==v[i])){await sub.unsubscribe();sub=null}}
if(!sub)sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64(VAPID)});
const kod=JSON.stringify(sub);
P.innerHTML=`<b>Ostatni krok:</b> skopiuj ten kod i dodaj go na GitHubie jako sekret <b>PUSH_SUB</b>
(Settings &rarr; Secrets and variables &rarr; Actions &rarr; New repository secret). Raz wystarczy.
<textarea id="pushkod" readonly style="width:100%;height:90px;margin:8px 0;font-size:11px">${kod}</textarea>
<button class="kbt" id="pushkopiuj">Kopiuj kod</button>`;
pushkopiuj.onclick=()=>{pushkod.select();(navigator.clipboard?navigator.clipboard.writeText(kod):Promise.reject()).then(()=>pushkopiuj.textContent="Skopiowano!",()=>document.execCommand("copy"))};
}catch(err){P.innerHTML="Nie udało się włączyć powiadomień: "+err}};
</script></body></html>"""


def zapisz_html(sciezka, mecze, n_forma, otworz=True, odswiez_url="", vapid_pub=""):
    import json
    import os
    import webbrowser
    strona = (STRONA.replace("__JSON__", json.dumps(mecze, ensure_ascii=False))
              .replace("__DATA__", datetime.now().strftime("%d.%m.%Y %H:%M"))
              .replace("__N__", str(n_forma)).replace("__PWA__", PWA_HEAD)
              .replace("__VAPID__", json.dumps(vapid_pub))
              .replace("__STAN__", json.dumps(STAN_DANYCH, ensure_ascii=False))
              .replace("__KAL__", json.dumps(KALIBRACJA))
              .replace("__BT__", '<a class="tab" href="backtest.html">Backtest</a>'
                       if os.path.exists(os.path.join(os.path.dirname(sciezka) or ".", "backtest.html")) else "")
              )
    with open(sciezka, "w", encoding="utf-8") as f:
        f.write(strona)
    try:
        zapisz_pwa(os.path.dirname(sciezka) or ".")
    except Exception as e:
        print(f"PWA: {e}")
    print(f"\nZapisano stronę: {sciezka}")
    if otworz:
        try:
            webbrowser.open("file://" + os.path.abspath(sciezka))
        except Exception:
            pass


# ---------- Excel z kolorami ----------
def zapisz_xlsx(sciezka, wiersze):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("\nBrak biblioteki openpyxl. Wpisz w cmd:  pip install openpyxl  i uruchom ponownie.")
        return
    def f(x):
        try:
            return float(str(x).replace(",", "."))
        except ValueError:
            return None
    # progi kolorów (prawdopodobieństwo %)
    kolory = [(80, "00B050", "FFFFFF"),   # ciemnozielony
              (70, "92D050", "000000"),   # jasnozielony
              (60, "FFEB84", "000000"),   # żółty
              (0, "F8CBAD", "000000")]    # pomarańczowy
    wb = Workbook()
    ws = wb.active
    ws.title = "Prognozy"
    nag = ["Liga", "Data", "Godzina", "Gospodarz", "Gość", "Typ", "Prawd. %",
           "Uczciwy kurs", "Kurs rynku", "Value", "Forma gosp.", "Forma gościa", "H2H"]
    ws.append(nag)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
        c.alignment = Alignment(horizontal="center")
    for w in wiersze:
        rz = w[:6] + [f(w[6]), f(w[7]), f(w[8]), w[9]] + w[10:13]
        ws.append(rz)
        r = ws.max_row
        for col in (11, 12, 13):
            v = ws.cell(r, col).value
            ws.cell(r, col).alignment = Alignment(horizontal="center")
            if v and "/" in v:
                tr, n = map(int, v.split("/"))
                if n:
                    q = tr / n
                    tlo = "00B050" if q >= .8 else "92D050" if q >= .6 else "FFEB84" if q >= .4 else "F8CBAD"
                    ws.cell(r, col).fill = PatternFill("solid", fgColor=tlo)
        p = rz[6] or 0
        for prog, tlo, czcionka in kolory:
            if p >= prog:
                for col in (6, 7, 8):
                    ws.cell(r, col).fill = PatternFill("solid", fgColor=tlo)
                    ws.cell(r, col).font = Font(color=czcionka, bold=(prog == 80))
                break
        if rz[9] == "TAK":
            for col in (9, 10):
                ws.cell(r, col).fill = PatternFill("solid", fgColor="FFD700")
                ws.cell(r, col).font = Font(bold=True)
        ws.cell(r, 7).number_format = "0.0"
        ws.cell(r, 8).number_format = "0.00"
        ws.cell(r, 9).number_format = "0.00"
    for i, szer in enumerate([16, 11, 8, 20, 20, 24, 9, 12, 11, 8, 12, 12, 9], 1):
        ws.column_dimensions[get_column_letter(i)].width = szer
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    # legenda
    ws2 = wb.create_sheet("Legenda")
    for opis, tlo in (("80% i więcej - bardzo mocny typ", "00B050"), ("70-80% - mocny typ", "92D050"),
                      ("60-70% - średni typ", "FFEB84"), ("poniżej 60% - ryzykowny", "F8CBAD"),
                      ("VALUE - kurs wyższy niż wynika ze statystyk", "FFD700"),
                      ("Forma/H2H: ile razy typ wszedł, np. 8/10", "FFFFFF")):
        ws2.append([opis])
        ws2.cell(ws2.max_row, 1).fill = PatternFill("solid", fgColor=tlo)
    ws2.column_dimensions["A"].width = 48
    wb.save(sciezka)
    print(f"\nZapisano kolorowy plik: {sciezka}")


# ---------- tryb serwera ----------
def serwer(a):
    import threading
    import time
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    a.html = a.html or "prognozy.html"
    blokada = threading.Lock()

    def odswiez():
        with blokada:
            print(f"\n[{datetime.now():%H:%M}] Pobieram dane...")
            try:
                uruchom(a, otworz=False)
                return True
            except Exception as e:
                print(f"Błąd: {e}")
                return False

    def petla():
        while True:
            time.sleep(a.co_ile * 3600)
            odswiez()

    class H(BaseHTTPRequestHandler):
        def _wyslij(self, kod, tresc, typ):
            self.send_response(kod)
            self.send_header("Content-Type", typ)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(tresc)

        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):
                try:
                    with open(a.html, "rb") as f:
                        self._wyslij(200, f.read(), "text/html; charset=utf-8")
                except FileNotFoundError:
                    self._wyslij(503, "Strona jeszcze się generuje, odśwież za chwilę.".encode(), "text/plain; charset=utf-8")
            else:
                self._wyslij(404, b"", "text/plain")

        def do_POST(self):
            if self.path == "/odswiez":
                ok = odswiez()
                self._wyslij(200 if ok else 500, b"ok" if ok else b"blad", "text/plain")
            else:
                self._wyslij(404, b"", "text/plain")

        def log_message(self, *args):
            pass

    odswiez()
    threading.Thread(target=petla, daemon=True).start()
    adres = f"http://localhost:{a.port}"
    print(f"\nStrona działa: {adres}  (dane co {a.co_ile:g} h, przycisk Odśwież na stronie)")
    print("Nie zamykaj tego okna. Zatrzymanie: Ctrl+C.")
    try:
        webbrowser.open(adres)
    except Exception:
        pass
    try:
        ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
    except KeyboardInterrupt:
        print("Zatrzymano.")


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Prognozy: bramki, kartki, rożne")
    ap.add_argument("--ligi", default=DOMYSLNE_LIGI)
    ap.add_argument("--min-proc", type=float, default=65, help="pokaż typy z prawd. >= X%%")
    ap.add_argument("--max-proc", type=float, default=95, help="ukryj typy oczywiste > X%%")
    ap.add_argument("--dni", type=int, default=7, help="mecze z najbliższych N dni")
    ap.add_argument("--csv")
    ap.add_argument("--xlsx", help="kolorowy plik Excela, np. prognozy.xlsx (wymaga: pip install openpyxl)")
    ap.add_argument("--html", help="strona z wynikami, np. prognozy.html (otwiera się w przeglądarce)")
    ap.add_argument("--z-rozegranymi", action="store_true",
                    help="pokazuj też dzisiejsze mecze, które już się zaczęły")
    ap.add_argument("--serwer", action="store_true",
                    help="tryb strony z przyciskiem Odśwież i automatycznym pobieraniem")
    ap.add_argument("--co-ile", type=float, default=3, help="co ile godzin pobierać dane (tryb --serwer)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--odswiez-url", default="", help="link przycisku Odśwież na stronie w internecie")
    ap.add_argument("--nie-otwieraj", action="store_true", help="nie otwieraj przeglądarki")
    ap.add_argument("--odds-key", default=os.environ.get("ODDS_API_KEY"),
                    help="klucz The Odds API: prawdziwe kursy bukmacherów do value betów")
    ap.add_argument("--kursy-co", type=float, default=24, help="co ile godzin pobierać kursy (oszczędza limit)")
    ap.add_argument("--af-key", default=os.environ.get("APIFOOTBALL_KEY"),
                    help="klucz API-Football: statystyki zawodników")
    ap.add_argument("--zaw-max", type=int, default=200, help="dla ilu meczów liczyć zawodników (limit 100 zapytań/dzień)")
    ap.add_argument("--value-prog", type=float, default=1.05, help="value bet gdy szansa x kurs > próg")
    ap.add_argument("--cache", default="cache_api.json", help="plik pamięci podręcznej API")
    ap.add_argument("--push-godzina", type=int, default=7, help="od której godziny wysłać poranne powiadomienie")
    ap.add_argument("--push-teraz", action="store_true", help="wyślij powiadomienie od razu (test)")
    ap.add_argument("--historia", default="historia.json", help="plik dziennika typów (do rozliczenia)")
    ap.add_argument("--backtest", type=int, default=0, help="sprawdź model na N ostatnich rozegranych meczach")
    ap.add_argument("--waga-modelu", type=float, default=0.5,
                    help="0-1: ile model, a ile kursy rynku przy golach (1 = tylko model)")
    ap.add_argument("--polowa", type=float, default=120, help="po ilu dniach mecz waży o połowę mniej (0 = wyłącz)")
    ap.add_argument("--rho", type=float, default=-0.08, help="poprawka na remisy/niskie wyniki (0 = wyłącz)")
    ap.add_argument("--bez-sedziego", action="store_true", help="nie uwzględniaj sędziego przy kartkach")
    ap.add_argument("--bez-kalibracji", action="store_true", help="nie poprawiaj procentów wynikami backtestu")
    ap.add_argument("--stary-model", action="store_true", help="model bez ulepszeń (do porównań)")
    ap.add_argument("--forma", type=int, default=10, help="ile ostatnich meczów do statystyk formy")
    ap.add_argument("--mecz", nargs=3, metavar=("LIGA", "GOSPODARZ", "GOŚĆ"))
    ap.add_argument("--dane", help="folder z lokalnymi plikami CSV (test/offline)")
    a = ap.parse_args()
    MODEL.update({"polowa": a.polowa or None, "rho": a.rho, "waga_modelu": a.waga_modelu,
                  "sedzia": not a.bez_sedziego, "kalibracja": not a.bez_kalibracji})
    if a.stary_model:
        MODEL.update(MODEL_STARY)
    if a.serwer:
        serwer(a)
        return
    uruchom(a)


def uruchom(a, otworz=True):

    import os
    sezony = kody_sezonow(5)
    wagi = [1.0, 0.4, 0.0, 0.0, 0.0]  # 0.0 = tylko do H2H
    ligi = [x.strip() for x in a.ligi.split(",") if x.strip()]
    if a.mecz:
        ligi = [a.mecz[0]]
    puchary = [k for k in ligi if k in PUCHARY]
    # ligi, z których potrzebujemy statystyk (puchary korzystają z lig krajowych)
    ligi_dane = [k for k in ligi if k not in PUCHARY]
    if puchary:
        ligi_dane += [k for k in LIGI_DLA_PUCHAROW if k not in ligi_dane]

    def laduj_tekst(url, lok):
        if a.dane:
            p = os.path.join(a.dane, lok)
            if not os.path.exists(p):
                return None
            with open(p, encoding="utf-8-sig") as f:
                return f.read()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8-sig", errors="replace")

    def laduj_csv(url, lok):
        t = laduj_tekst(url, lok)
        return list(csv.DictReader(io.StringIO(t))) if t else []

    modele, surowe = {}, {}
    dzis = date.today()
    for kod in ligi_dane:
        mecze = []
        if kod in NOWE_LIGI:
            try:
                for w in laduj_csv(f"{BASE}/new/{kod}.csv", f"{kod}.csv"):
                    w = z_nowego_formatu(w)
                    if not w:
                        continue
                    dni = (dzis - data_meczu(w).date()).days
                    if dni > 1830:
                        continue
                    mecze.append((w, 1.0 if dni <= 365 else 0.4 if dni <= 730 else 0.0))
            except Exception as e:
                print(f"  {kod}: nie pobrano ({e})")
        else:
            for sez, waga in zip(sezony, wagi):
                try:
                    wiersze = [w for w in laduj_csv(f"{BASE}/mmz4281/{sez}/{kod}.csv", f"{kod}_{sez}.csv")
                               if w.get("HomeTeam")]
                    mecze += [(w, waga) for w in wiersze]
                except Exception as e:
                    print(f"  {kod} {sez}: nie pobrano ({e})")
        modele[kod] = Liga(mecze, datetime.now(), MODEL["polowa"])
        surowe[kod] = mecze
        print(f"  {NAZWY.get(kod, kod)}: {len(mecze)} meczów w bazie")

    plik_kal = os.path.join(os.path.dirname(a.html or "") or ".", "kalibracja.json")
    if not a.backtest and os.path.exists(plik_kal):
        try:
            with open(plik_kal, encoding="utf-8") as f:
                KALIBRACJA.update({k: [tuple(x) for x in v] for k, v in json.load(f).items()})
        except Exception:
            pass
    if a.backtest:
        sciezka_bt = os.path.join(os.path.dirname(a.html or "") or ".", "backtest.html")
        try:
            backtest(a, surowe, sciezka_bt)
        except Exception as e:
            print(f"Backtest: błąd {e}")

    # indeks drużyn do pucharów: nazwa znormalizowana -> (kod ligi, nazwa w danych)
    indeks = {}
    for kod in LIGI_DLA_PUCHAROW:
        if kod in modele:
            for druz in modele[kod].dom:
                indeks.setdefault(normalizuj(druz), (kod, druz))

    # ---- terminarz ----
    terminarz = []
    if a.mecz:
        terminarz = [{"Div": a.mecz[0], "HomeTeam": a.mecz[1], "AwayTeam": a.mecz[2], "Date": "", "Time": ""}]
    else:
        try:
            for w in laduj_csv(f"{BASE}/fixtures.csv", "fixtures.csv"):
                if w.get("Div") in ligi:
                    terminarz.append(godzina_uk_na_pl(w))
        except Exception as e:
            print(f"  Terminarz lig: nie pobrano ({e})")
        if any(k in NOWE_LIGI for k in ligi):
            try:
                for w in laduj_csv(f"{BASE}/new_league_fixtures.csv", "new_league_fixtures.csv"):
                    kod = {v: k for k, v in NOWE_LIGI.items()}.get(w.get("Country", "").strip())
                    if kod in ligi:
                        w = z_nowego_formatu(w, fixtures=True)
                        w["Div"] = kod
                        terminarz.append(godzina_uk_na_pl(w))
            except Exception as e:
                print(f"  Terminarz Argentyna/Brazylia: nie pobrano ({e})")
        rok = int("20" + sezony[0][:2])
        # uzupełnienie terminarza lig (football-data publikuje go z opóźnieniem)
        juz = {(w["Div"], normalizuj(w["HomeTeam"]), normalizuj(w["AwayTeam"])) for w in terminarz}
        dodane = 0
        for kod in ligi:
            if kod not in TERMINARZ_FD or kod not in modele:
                continue
            idx = {normalizuj(d): d for d in modele[kod].dom}
            try:
                lista = terminarz_pucharu(laduj_tekst, f"{TERMINARZ_FD[kod]}-{rok}", kod)
            except Exception:
                continue
            for w in lista:
                kh, kg = znajdz(w["HomeTeam"], idx), znajdz(w["AwayTeam"], idx)
                if not kh or not kg:
                    continue
                w["HomeTeam"], w["AwayTeam"] = idx[kh], idx[kg]
                k = (kod, normalizuj(w["HomeTeam"]), normalizuj(w["AwayTeam"]))
                if k not in juz:
                    terminarz.append(w)
                    juz.add(k)
                    dodane += 1
        if dodane:
            print(f"  Zapasowy terminarz: +{dodane} meczów")
        for kod in puchary:
            slug = f"{PUCHARY[kod]}-{rok}"
            try:
                terminarz += terminarz_pucharu(laduj_tekst, slug, kod)
            except Exception as e:
                print(f"  {NAZWY[kod]}: nie pobrano terminarza ({e})")

        teraz = datetime.now()
        wybrane = []
        for w in terminarz:
            try:
                d = datetime.strptime(w["Date"], "%d/%m/%Y").date()
            except (KeyError, ValueError):
                continue
            if not (0 <= (d - dzis).days <= a.dni):
                continue
            if d == dzis and not a.z_rozegranymi and w.get("Time"):
                try:
                    start = datetime.combine(d, datetime.strptime(w["Time"], "%H:%M").time())
                    if start <= teraz:
                        continue
                except ValueError:
                    pass
            wybrane.append(w)
        terminarz = sorted(wybrane, key=lambda w: (datetime.strptime(w["Date"], "%d/%m/%Y"), w.get("Time", "")))

    cache = Cache(a.cache)
    kursy_api = {}
    if a.odds_key and terminarz:
        kursy_api = kursy_odds_api(a.odds_key, sorted({w["Div"] for w in terminarz}), cache, a.kursy_co)
    af = ApiFootball(a.af_key, cache) if a.af_key else None
    ile_zaw = 0
    ligi_dom = {}                                    # drużyna -> jej liga krajowa (do statystyk zawodników z pucharów)
    for kod_, mod_ in modele.items():
        if kod_ not in PUCHARY:
            for d_ in mod_.dom:
                ligi_dom.setdefault(d_, []).append(kod_)
    if af and terminarz:
        try:
            zbieraj_zawodnikow(af, terminarz, [d for kod in modele for d in modele[kod].dom])
        except Exception as e:
            print(f"  [zawodnicy] zbieranie: błąd {e}")
    # ---- dziennik typów: rozliczenie zakończonych meczów ----
    historia = wczytaj_historie(a.historia)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not historia and "/" in repo and not a.dane:
        # pamięć GitHuba wyczyszczona -> odtwarzamy bazę z kopii opublikowanej na stronie
        try:
            j, _ = pobierz_json(f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/historia.json")
            if isinstance(j, dict):
                historia = j
                print(f"Baza typów: odtworzona z kopii na stronie ({len(historia)} meczów)")
        except Exception as e:
            print(f"Baza typów: brak kopii na stronie ({e})")
    wyniki_fd = {}
    if historia:
        rok_fd = int("20" + sezony[0][:2])
        for kod in {e["kod"] for e in historia.values() if not e.get("koniec")}:
            slug = PUCHARY.get(kod) or TERMINARZ_FD.get(kod)
            if slug:
                wyniki_fd[kod] = wyniki_fixturedownload(laduj_tekst, f"{slug}-{rok_fd}")
        rozlicz_historie(historia, surowe, wyniki_fd, af, cache)
    wiersze_out, mecze_html = [], []
    for w in terminarz:
        kod = w["Div"]
        h_pokaz, g_pokaz = w["HomeTeam"].strip(), w["AwayTeam"].strip()
        if kod in PUCHARY:
            pop = lambda n: "Paris SG" if normalizuj(n) == "paris" else n
            zh, zg = indeks.get(znajdz(pop(h_pokaz), indeks)), indeks.get(znajdz(pop(g_pokaz), indeks))
            if not zh or not zg:
                # drużyna z ligi spoza bazy: gole i wynik liczone z kursów bukmacherów (bez rożnych/kartek)
                brak = h_pokaz if not zh else g_pokaz
                try:
                    d_k = datetime.strptime(w.get("Date", ""), "%d/%m/%Y").date()
                except ValueError:
                    d_k = None
                k_r = dict(dopasuj_kursy(kursy_api, kod, h_pokaz, g_pokaz, d_k))
                lam_r = lambdy_rynku(k_r)
                if not lam_r:
                    print(f"\n{NAZWY[kod]} {h_pokaz} - {g_pokaz}: brak danych o {brak} i brak kursów - pomijam")
                    continue
                print(f"   [puchar] {h_pokaz} - {g_pokaz}: brak danych o {brak} -> prognoza z kursów bukmacherów")
                h = zh[1] if zh else h_pokaz
                g = zg[1] if zg else g_pokaz
                liga_h = modele[zh[0]] if zh else None
                liga_g = modele[zg[0]] if zg else None
                pr = {"lam_h": lam_r[0], "lam_a": lam_r[1], "h2h": [], "z_kursow": True}
            else:
                print(f"   [puchar] {h_pokaz} -> {zh[1]} ({zh[0]}), {g_pokaz} -> {zg[1]} ({zg[0]})")
                (kod_h, h), (kod_g, g) = zh, zg
                liga_h, liga_g = modele[kod_h], modele[kod_g]
                pr = liga_h.prognoza(h, g, liga_g, SILA.get(kod_h, 0.7), SILA.get(kod_g, 0.7))
        else:
            h, g = h_pokaz, g_pokaz
            liga_h = liga_g = modele.get(kod)
            pr = liga_h.prognoza(h, g, sedzia=(w.get("Referee") or "").strip() or None) if liga_h else None
        if not pr:
            print(f"\n{h_pokaz} - {g_pokaz}: za mało danych (sprawdź pisownię nazw drużyn)")
            continue
        kursy = {k: (v, "średnia rynku") for k, v in kursy_z_terminarza(w).items() if v}
        try:
            dzien = datetime.strptime(w.get("Date", ""), "%d/%m/%Y").date()
        except ValueError:
            dzien = None
        for k, v in dopasuj_kursy(kursy_api, kod, h_pokaz, g_pokaz, dzien).items():
            if k not in kursy or v[0] > kursy[k][0]:
                kursy[k] = v
        mieszaj_z_rynkiem(pr, kursy)
        t, wyniki = typy(pr)
        forma_h = ostatnie_mecze(liga_h, h, a.forma)
        forma_g = ostatnie_mecze(liga_g, g, a.forma)
        h2h_m = pr.get("h2h", [])[:10]
        _p = {n: kalibruj(p, "wynik") for n, p, _, _ in t if n in ("1 (gospodarz)", "X (remis)", "2 (gość)")}
        _s = sum(_p.values()) or 1
        p1x2 = [round(_p.get(n, 0) / _s * 100) for n in ("1 (gospodarz)", "X (remis)", "2 (gość)")]
        lab = {"lam": {"gole": [round(pr["lam_h"], 3), round(pr["lam_a"], 3)]},
               "hh": [wiersz_lab(x, h) for x in forma_h], "ha": [wiersz_lab(x, g) for x in forma_g],
               "h2h": [wiersz_lab(x, h) for x in pr.get("h2h", [])[:10]]}
        for co in ("rozne", "kartki", "strzaly", "celne"):
            if pr.get(co + "_h") is not None and pr.get(co + "_a") is not None:
                lab["lam"][co] = [round(pr[co + "_h"], 3), round(pr[co + "_a"], 3)]
        mecz_html = {"liga": NAZWY.get(kod, kod), "data": w.get("Date", ""), "godz": w.get("Time", ""), "p1x2": p1x2,
                     "lab": lab,
                     "h": h_pokaz, "g": g_pokaz, "lh": round(pr["lam_h"], 2), "la": round(pr["lam_a"], 2),
                     "kartki": round(pr["kartki"], 1) if pr.get("kartki") else None,
                     "rozne": round(pr["rozne"], 1) if pr.get("rozne") else None,
                     "sedzia": f'{pr["sedzia"][0]} (kartki x{pr["sedzia"][1]:.2f})' if pr.get("sedzia") else None,
                     "z_kursow": bool(pr.get("z_kursow")),
                     "wyniki": [f"{s} ({p*100:.0f}%)" for p, s in wyniki],
                     "h2h": [f'{x["HomeTeam"].strip()} {int(num(x,"FTHG"))}:{int(num(x,"FTAG"))} {x["AwayTeam"].strip()}'
                             for x in h2h_m[:5]],
                     "typy": []}
        mecze_html.append(mecz_html)
        naglowek = f"{NAZWY.get(kod, kod)} | {w.get('Date', '')} {w.get('Time', '')} | {h_pokaz} - {g_pokaz}"
        print(f"\n=== {naglowek}")
        info = f"   Oczekiwane gole {pr['lam_h']:.2f} : {pr['lam_a']:.2f}"
        if pr.get("kartki"):
            info += f" | kartki ~{pr['kartki']:.1f}"
        if pr.get("rozne"):
            info += f" | rożne ~{pr['rozne']:.1f}"
        if pr.get("sedzia"):
            info += f" | sędzia {pr['sedzia'][0]} (kartki x{pr['sedzia'][1]:.2f})"
        if pr.get("lam_model"):
            info += f" | model sam: {pr['lam_model'][0]:.2f}:{pr['lam_model'][1]:.2f}"
        print(info)
        print(pr.get("h2h_txt", "   H2H: brak bezpośrednich meczów w ostatnich 5 sezonach"))
        print("   Najczęstsze wyniki: " + ", ".join(f"{s} ({p*100:.0f}%)" for p, s in wyniki))
        print(f"   {'':6}  {'':<24} {'':<20} forma {h[:10]} / {g[:10]} (ost. {a.forma}) | H2H")
        for nazwa, p, klucz, spec in sorted(t, key=lambda x: -x[1]):
            kat = "gosp" if nazwa.startswith("Gospodarz:") else "gosc" if nazwa.startswith("Gość:") else "mecz"
            # typy drużynowe podpisane nazwą drużyny, np. "Arsenal: rożne powyżej 5.5"
            nazwa = nazwa.replace("Gospodarz:", f"{h_pokaz}:", 1).replace("Gość:", f"{g_pokaz}:", 1)
            rk = rynek_kat(spec)
            p = kalibruj(p, rk)
            proc = p * 100
            kurs_rynku, buk = kursy.get(klucz, (None, None)) if klucz else (None, None)
            jest_value = bool(kurs_rynku and p * kurs_rynku > a.value_prog and not pr.get("z_kursow"))
            if not (a.min_proc <= proc <= a.max_proc) and not jest_value:
                continue
            value = ""
            if kurs_rynku:
                value = f" | kurs {kurs_rynku:.2f} ({buk})" + (" << VALUE" if jest_value else "")
            sh = skutecznosc(forma_h, h, spec)
            sg = skutecznosc(forma_g, g, spec, odwroc=True)
            s2 = skutecznosc(h2h_m, h, spec)
            txt = lambda s: f"{s[0]}/{s[1]}" if s[1] else "-"
            print(f"   {proc:5.1f}%  {nazwa:<24} uczciwy kurs {1/p:.2f}{value}"
                  f"   [{txt(sh)} | {txt(sg)} | H2H {txt(s2)}]")
            wiersze_out.append([NAZWY.get(kod, kod), w.get("Date", ""), w.get("Time", ""), h, g,
                                nazwa, f"{proc:.1f}".replace(".", ","), f"{1/p:.2f}".replace(".", ","),
                                f"{kurs_rynku:.2f}".replace(".", ",") if kurs_rynku else "",
                                "TAK" if jest_value else "", txt(sh), txt(sg), txt(s2)])
            mecz_html["typy"].append({"kat": kat, "rk": rk, "typ": nazwa, "p": round(proc, 1), "kurs": round(1 / p, 2),
                                      "rynek": kurs_rynku, "buk": buk, "value": jest_value, "spec": list(spec),
                                      "fh": list(sh), "fg": list(sg), "h2h": list(s2)})
        poprz = {t["typ"]: t for t in historia.get(f'{kod}|{w.get("Date", "")}|{h_pokaz}|{g_pokaz}', {}).get("typy", [])}
        for x in mecz_html["typy"]:
            k0 = (poprz.get(x["typ"]) or {}).get("k0") or (poprz.get(x["typ"]) or {}).get("kurs")
            if k0 and x.get("rynek") and abs(k0 - x["rynek"]) >= 0.01:
                x["k0"] = k0
        zaw = None
        if kod in ESPN_LIGI and not a.dane:
            try:
                zaw, zlab = zawodnicy_espn(cache, kod, w.get("Date"), h_pokaz, g_pokaz, pr["lam_h"], pr["lam_a"],
                                      {h_pokaz: ligi_dom.get(h, []), g_pokaz: ligi_dom.get(g, [])})
                if zlab and mecz_html.get("lab"):
                    mecz_html["lab"]["zaw"] = zlab
            except Exception as e:
                print(f"   [zawodnicy] ESPN: błąd {e}")
        if zaw is None and af and ile_zaw < a.zaw_max and not af.wyczerpane:
            ile_zaw += 1
            zaw = zawodnicy_meczu(af, w.get("Date"), h_pokaz, g_pokaz, pr["lam_h"], pr["lam_a"])
        if zaw:
            for x in zaw:
                print(f"   {x['p']:5.1f}%  {x['typ']}  seria {x['seria']} | ostatnie {x['ost']}")
            mecz_html["typy"] += zaw
        zapamietaj_typy(historia, kod, w, h, g, h_pokaz, g_pokaz, [
            {"typ": x["typ"], "p": round(x["p"] / 100, 4), "kurs": x["rynek"], "rk": x["rk"], "spec": x["spec"],
             "v": x["value"]}
            for x in mecz_html["typy"] if x.get("spec")])

    if ESPN_ZAW["licznik"]:
        print(f"  [zawodnicy] ESPN: {ESPN_ZAW['licznik']} zapytań (bez limitu dziennego; mecze zapamiętywane)")
    if os.environ.get("PUSH_SUB"):
        dzis_p = datetime.now().strftime("%Y-%m-%d")
        if a.push_teraz or (datetime.now().hour >= a.push_godzina and cache.get("push|wyslano") != dzis_p):
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            url = os.environ.get("STRONA_URL") or (
                f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if "/" in repo else "")
            if wyslij_powiadomienia(cache, raport_powiadomienie(mecze_html, historia, url)):
                cache.set("push|wyslano", dzis_p)
    cache.zapisz()
    historia = zapisz_historie(historia, a.historia)
    for m in mecze_html:
        for x in m["typy"]:
            x.pop("spec", None)
    if a.html:
        zapisz_rozliczenie(historia, os.path.join(os.path.dirname(a.html) or ".", "rozliczenie.html"))
        try:   # kopia bazy na stronie (zapas, gdyby pamięć GitHuba się wyczyściła)
            with open(os.path.join(os.path.dirname(a.html) or ".", "historia.json"), "w", encoding="utf-8") as f:
                json.dump(historia, f, ensure_ascii=False)
        except Exception:
            pass
    if af:
        print(f"\nAPI-Football: użyto {af.uzyte} zapytań w tym uruchomieniu")
    if a.html:
        import re
        bledy, widziane = [], set()
        for l in LOG:
            l2 = l.strip()
            if any(x in l2 for x in ("nie pobrano", "błąd", "Błąd", "API-Football: {", "nie wysłano")) and l2 not in widziane:
                widziane.add(l2)
                bledy.append(l2[:200])
        odds_left = None
        for l in LOG:
            m_ = re.search(r"zostało zapytań: (\d+)", l)
            if m_:
                odds_left = int(m_.group(1))
        zaw = None
        for l in LOG:
            m_ = re.search(r"z min\. 3 meczami w bazie: (\d+/\d+)", l)
            if m_:
                zaw = m_.group(1)
        ligi_n = {}
        for m in mecze_html:
            if m["typy"]:
                ligi_n[m["liga"]] = ligi_n.get(m["liga"], 0) + 1
        wcz = (datetime.now() - timedelta(days=1)).strftime("%d/%m/%Y")
        roz = [tp for e in historia.values() if e["data"] == wcz and "wynik" in e for tp in e["typy"] if tp.get("traf") is not None]
        STAN_DANYCH.update({
            "czas": datetime.now().strftime("%d.%m.%Y %H:%M"), "ligi": ligi_n, "odds_left": odds_left,
            "odds": any("Kursy " in l and "meczów" in l for l in LOG),
            "af_used": af.uzyte if af else None, "af_left": getattr(af, "pozostalo", None) if af else None,
            "zaw": zaw, "baza": sum(1 for e in historia.values() if "wynik" in e),
            "czeka": sum(1 for e in historia.values() if "wynik" not in e), "kal": bool(KALIBRACJA),
            "wczoraj": [sum(1 for t in roz if t["traf"]), len(roz)] if roz else None, "bledy": bledy[:15]})
        k_v = vapid_klucze(cache)
        zapisz_html(a.html, [m for m in mecze_html if m["typy"]], a.forma,
                    otworz and not a.nie_otwieraj, a.odswiez_url, k_v["pub"] if k_v else "")
        cache.zapisz()
    if a.xlsx:
        zapisz_xlsx(a.xlsx, wiersze_out)
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f, delimiter=";")
            wr.writerow(["Liga", "Data", "Godzina", "Gospodarz", "Gość", "Typ", "Prawd. %",
                         "Uczciwy kurs", "Kurs rynku", "Value", "Forma gospodarza",
                         "Forma gościa", "H2H"])
            wr.writerows(wiersze_out)
        print(f"\nZapisano {len(wiersze_out)} typów do {a.csv} (otwórz w Excelu)")
    if not terminarz:
        print("\nBrak meczów w terminarzu dla tych lig w najbliższych dniach. Spróbuj --dni 14.")


if __name__ == "__main__":
    main()
