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
DOMYSLNE_LIGI = "E0,E1,SP1,I1,D1,F1,N1,P1,B1,T1,SC0,ARG,BRA,LM,LE,LK"
NOWE_LIGI = {"ARG": "Argentina", "BRA": "Brazil"}           # pliki /new/ (bez kartek i rożnych)
PUCHARY = {"LM": "champions-league", "LE": "europa-league", "LK": "conference-league"}
# zapasowy terminarz lig z fixturedownload.com (cały sezon rozpisany z góry)
TERMINARZ_FD = {"E0": "epl", "E1": "championship", "SP1": "la-liga", "I1": "serie-a", "D1": "bundesliga",
                "F1": "ligue-1", "N1": "eredivisie", "P1": "primeira-liga", "T1": "super-lig",
                "SC0": "scottish-premiership"}
LIGI_DLA_PUCHAROW = ["E0", "E1", "SP1", "I1", "D1", "F1", "N1", "P1", "B1", "T1", "SC0", "G1"]
# przybliżona siła lig (do meczów pucharowych między ligami)
SILA = {"E0": 1.0, "SP1": 0.93, "I1": 0.92, "D1": 0.92, "F1": 0.88, "P1": 0.80, "N1": 0.80,
        "E1": 0.74, "B1": 0.76, "T1": 0.76, "SC0": 0.68, "G1": 0.70, "ARG": 0.72, "BRA": 0.76}
SILA_POTEGA = 2.0
# Ustawienia modelu (nadpisywane parametrami). "stary" model = bez ulepszeń, do porównania w backteście.
MODEL = {"polowa": 120, "rho": -0.08, "waga_modelu": 0.5, "sedzia": True, "kalibracja": True}
MODEL_STARY = {"polowa": None, "rho": 0.0, "waga_modelu": 1.0, "sedzia": False, "kalibracja": False}
KALIBRACJA = {}   # kategoria rynku -> [(średnia prognoza, faktycznie weszło), ...]   # jak mocno różnica siły lig wpływa na gole w pucharach
NAZWY = {"E0": "Premier League", "E1": "Championship", "SP1": "LaLiga", "I1": "Serie A",
         "D1": "Bundesliga", "F1": "Ligue 1", "N1": "Eredivisie", "P1": "Portugalia",
         "T1": "Turcja", "B1": "Belgia", "SC0": "Szkocja", "G1": "Grecja",
         "ARG": "Argentyna", "BRA": "Brazylia", "LM": "Liga Mistrzów", "LE": "Liga Europy",
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


def ostatnie_mecze(liga, druzyna, n):
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
    "BRA": "soccer_brazil_campeonato", "LM": "soccer_uefa_champs_league",
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

    def get(self, sciezka, ttl_h=None, zapisz=True):
        dane = self.cache.get("af|" + sciezka, ttl_h)
        if dane is not None:
            return dane
        if self.wyczerpane:
            return self.cache.get("af|" + sciezka)   # stare dane lepsze niż żadne
        import time
        try:
            j, nag = pobierz_json(AF + sciezka, {"x-apisports-key": self.klucz})
            self.uzyte += 1
            time.sleep(6.5)                       # darmowy plan: max 10 zapytań na minutę
            if j.get("errors"):
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


def zawodnicy_meczu(af, data, h, g, lam_h, lam_a, ile=10):
    """Typy na zawodników z serią albo dużą szansą. Zwraca listę typów (jak dla meczu)."""
    import math
    if not data:
        return []
    dzien = datetime.strptime(data, "%d/%m/%Y").strftime("%Y-%m-%d")
    lista = af.get(f"/fixtures?date={dzien}&timezone=Europe/Warsaw", ttl_h=12) or []
    idx = {}
    for f in lista:
        idx.setdefault(normalizuj(f["teams"]["home"]["name"]), []).append(f)
    k = znajdz(h, idx)
    mecz = None
    for f in idx.get(k, []):
        if znajdz(g, {normalizuj(f["teams"]["away"]["name"]): 1}):
            mecz = f
    if not mecz:
        print(f"   [zawodnicy] nie znaleziono meczu {h} - {g} w API-Football "
              f"({len(lista)} meczów tego dnia" + ("" if lista else " - pusta odpowiedź, sprawdź błędy wyżej") + ")")
        return []
    druzyny = [(mecz["teams"]["home"], lam_h, "H"), (mecz["teams"]["away"], lam_a, "A")]
    ostatnie, info = {}, {}
    for t, _, _ in druzyny:
        r = af.get(f"/fixtures?team={t['id']}&last={ile}", ttl_h=20) or []
        ostatnie[t["id"]] = [f["fixture"]["id"] for f in r]
        for f in r:
            info[f["fixture"]["id"]] = {"gole": f.get("goals"), "teams": f.get("teams")}

    def zapisz_fx(fid, gracze_bloki):
        dane = info.get(fid, {})
        af.cache.set(f"af|fx|{fid}", {
            "gole": dane.get("gole"), "teams": dane.get("teams"),
            "players": [{"team": p["team"]["id"], "players": [
                {"name": x["player"]["name"], "s": x["statistics"][0] if x.get("statistics") else {}}
                for x in p.get("players", [])]} for p in gracze_bloki]})

    def ma_dane(fid):
        c = af.cache.get(f"af|fx|{fid}")
        return bool(c and c.get("players"))

    # 1) zbiorczo: do 20 meczów w jednym zapytaniu
    potrzebne = [i for ids in ostatnie.values() for i in ids if not ma_dane(i)]
    for n in range(0, len(potrzebne), 20):
        paczka = potrzebne[n:n + 20]
        for f in af.get("/fixtures?ids=" + "-".join(map(str, paczka)), zapisz=False) or []:
            fid = f["fixture"]["id"]
            info.setdefault(fid, {"gole": f.get("goals"), "teams": f.get("teams")})
            if f.get("players"):
                zapisz_fx(fid, f["players"])
    # 2) zapas: gdy plan nie zwraca zawodników zbiorczo -> osobno, 5 ostatnich meczów każdej drużyny
    for t, _, _ in druzyny:
        for fid in ostatnie.get(t["id"], [])[:5]:
            if ma_dane(fid) or af.wyczerpane:
                continue
            r = af.get(f"/fixtures/players?fixture={fid}", zapisz=False)
            if r:
                zapisz_fx(fid, r)
    for t, _, _ in druzyny:
        n_ok = sum(1 for i in ostatnie.get(t["id"], []) if ma_dane(i))
        print(f"   [zawodnicy] {t['name']}: mecze ze statystykami zawodników {n_ok}/{len(ostatnie.get(t['id'], []))}")
    if not any(ostatnie.values()):
        print(f"   [zawodnicy] API-Football nie zwróciło ostatnich meczów drużyn {h} / {g}")
    typy_zaw = []
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
        sr_gole = max(0.5, sum(gole_druz) / len(gole_druz)) if gole_druz else max(0.5, lam)
        f_atak = min(1.6, max(0.6, lam / sr_gole))
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
                        "kar": (c.get("yellow") or 0) + (c.get("red") or 0)})
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
            ]
            for nazwa, pole, prog, f_adj, p_min, seria_min in rynki:
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
                        "h2h": [0, 0], "seria": seria,
                        "ost": "".join("✓" if x else "✗" for x in traf[:5])})
    typy_zaw.sort(key=lambda x: (-x["seria"], -x["p"]))
    print(f"   [zawodnicy] {h} - {g}: typów na zawodników {len(typy_zaw)}")
    return typy_zaw[:12]


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


# ---------- strona HTML ----------
STRONA = """<!DOCTYPE html>
<html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prognozy __DATA__</title>
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
</style></head><body>
<header><h1>Prognozy &middot; wygenerowano __DATA__ __BT__</h1>
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
<label>Typy <select id="kat"><option value="">wszystkie</option><option value="mecz">cały mecz</option>
<option value="druz">statystyki drużyn</option><option value="gosp">tylko gospodarz</option><option value="gosc">tylko gość</option><option value="zaw">zawodnicy</option></select></label>
<label><input type="checkbox" id="razem"> wszystkie typy w jednej liście</label>
<input type="search" id="szukaj" placeholder="Szukaj drużyny lub ligi">

</div>
<div class="chipy" id="chipy">
<button class="chip on" data-rk="">Wszystkie</button><button class="chip" data-rk="wynik">&#x1F3C6; Wynik</button>
<button class="chip" data-rk="bramki">&#x26BD; Bramki</button><button class="chip" data-rk="rozne">&#x1F6A9; Rożne</button>
<button class="chip" data-rk="kartki">&#x1F7E8; Kartki</button><button class="chip" data-rk="strzaly">&#x1F3AF; Strzały</button>
<button class="chip" data-rk="zaw">&#x1F464; Zawodnicy</button></div>
<div class="chipy" id="dni"></div></header>
<main><p class="leg">Forma = ile razy dany typ wszedł w ostatnich __N__ meczach drużyny (dla gościa z jego perspektywy). H2H = w bezpośrednich meczach z 5 sezonów. Kolory: zielony = częściej, pomarańczowy = rzadziej.</p>
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
<div id="lista"></div></main>
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
const wiersz=x=>`<td><button class="dod${K.has(x.id)?" on":""}" data-id="${x.id}" title="Dodaj do kuponu">${K.has(x.id)?"&#10003;":"+"}</button></td><td>${x.typ}${seria(x)}</td><td><span class="p ${kl(x.p)}">${x.p}%</span></td><td>${x.kurs.toFixed(2)}</td>
<td>${x.rynek?x.rynek.toFixed(2):"-"} ${x.value?'<span class="val">VALUE</span>':""}${x.buk?`<br><span class="mm">${x.buk}</span>`:""}</td><td>${st(x.fh)}</td><td>${st(x.fg)}</td><td>${st(x.h2h)}</td>`;
const kl=p=>p>=80?"z1":p>=70?"z2":p>=60?"z3":"z4";
const kol=q=>q>=.8?"#00b050":q>=.6?"#92d050":q>=.4?"#ffd84d":"#f4a582";
function st(s){if(!s[1])return'<span style="color:var(--mut)">-</span>';const q=s[0]/s[1];
return`<span class="st">${s[0]}/${s[1]}<span class="bar"><i style="width:${q*100}%;background:${kol(q)}"></i></span></span>`}
const r=s=>s[1]?s[0]/s[1]:-1;
const KLUCZ={p:x=>-x.p,t:x=>x.m.ts,kd:x=>-x.kurs,kr:x=>x.kurs,rd:x=>-(x.rynek||0),f:x=>-((r(x.fh)+r(x.fg))/2),h:x=>-r(x.h2h)};
const sortuj=a=>a.sort((x,y)=>KLUCZ[sort.value](x)-KLUCZ[sort.value](y)||y.p-x.p);
let RK="",DZIEN="";
const pasujeKat=x=>(!DZIEN||x.m.data===DZIEN)&&(!RK||x.rk===RK)&&(!kat.value||x.kat===kat.value||(kat.value==="druz"&&(x.kat==="gosp"||x.kat==="gosc")));
// dni: przyciski z datami meczów
const ts=(d,g)=>{const [dd,mm,rr]=(d||"").split("/").map(Number),[hh,mi]=(g||"00:00").split(":").map(Number);return rr?new Date(rr,mm-1,dd,hh||0,mi||0).getTime():0};
D.forEach(m=>m.ts=ts(m.data,m.godz));
(()=>{const DNI=["Nd","Pn","Wt","Śr","Cz","Pt","So"],dz=new Date();dz.setHours(0,0,0,0);
const daty=[...new Set(D.map(m=>m.data).filter(Boolean))].sort((a,b)=>ts(a)-ts(b));
const ety=d=>{const t=new Date(ts(d)),r=Math.round((t-dz)/864e5),dm=d.slice(0,5).replace("/",".");
return r===0?`Dziś ${dm}`:r===1?`Jutro ${dm}`:`${DNI[t.getDay()]} ${dm}`};
dni.innerHTML=`<button class="chip on" data-dz="">&#x1F4C5; Wszystkie dni</button>`+daty.map(d=>{
const n=D.filter(m=>m.data===d).length;return`<button class="chip" data-dz="${d}">${ety(d)} <span style="opacity:.7">(${n})</span></button>`}).join("");
dni.addEventListener("click",e=>{const c=e.target.closest("[data-dz]");if(!c)return;DZIEN=c.dataset.dz;
dni.querySelectorAll(".chip").forEach(b=>b.classList.toggle("on",b===c));rys()})})();
chipy.addEventListener("click",e=>{const c=e.target.closest("[data-rk]");if(!c)return;RK=c.dataset.rk;
chipy.querySelectorAll(".chip").forEach(b=>b.classList.toggle("on",b===c));rys()});
function value(){const pod=vpod.checked?0.88:1,mn=(+vmin.value||0)/100,pm=+vpmin.value||0;
const t=D.flatMap(m=>m.typy).filter(x=>x.rynek&&x.p>=pm&&pasujeKat(x)).map(x=>({x,ev:x.p/100*x.rynek*pod-1})).filter(z=>z.ev>=mn&&z.ev<=(+vmax.value||40)/100);
const VS={ev:(a,b)=>b.ev-a.ev,kd:(a,b)=>b.x.rynek-a.x.rynek,kr:(a,b)=>a.x.rynek-b.x.rynek,p:(a,b)=>b.x.p-a.x.p,t:(a,b)=>a.x.m.ts-b.x.m.ts};
t.sort((a,b)=>VS[vsort.value](a,b)||b.ev-a.ev);
vlista.innerHTML=t.length?`<table><tr><th></th><th>Mecz</th><th>Typ</th><th>Szansa</th><th>Uczciwy kurs</th><th>Kurs</th><th>Przewaga</th></tr>
${t.map(({x,ev})=>`<tr><td><button class="dod${K.has(x.id)?" on":""}" data-id="${x.id}">${K.has(x.id)?"&#10003;":"+"}</button></td>
<td><b>${x.m.h} &ndash; ${x.m.g}</b><br><span class="mm">${x.m.liga} &middot; ${x.m.data} ${x.m.godz}</span></td><td>${x.typ}</td>
<td><span class="p ${kl(x.p)}">${x.p}%</span></td><td>${x.kurs.toFixed(2)}</td><td><b>${x.rynek.toFixed(2)}</b>${x.buk?`<br><span class="mm">${x.buk}</span>`:""}</td>
<td class="plus">+${(ev*100).toFixed(1)}%</td></tr>`).join("")}</table>`:'<p class="knot">Brak value betów przy tych ustawieniach. Kursy bukmacherów są tylko dla 1X2 i goli (więcej rynków z kluczem The Odds API).</p>'}
for(const e of [vpod,vmin,vpmin,vmax,vsort])e.addEventListener("input",value);
function rys(){value();const mn=+min.value,mf=+minf.value/100,v=val.checked,q=szukaj.value.toLowerCase();
minv.textContent=mn;minfv.textContent=minf.value;let html="";
const ok=x=>pasujeKat(x)&&x.p>=mn&&(!v||x.value)&&(!mf||[x.fh,x.fg].every(s=>s[1]&&s[0]/s[1]>=mf));
const pasuje=m=>!q||(m.h+" "+m.g+" "+m.liga).toLowerCase().includes(q);
if(razem.checked){const t=sortuj(D.filter(pasuje).flatMap(m=>m.typy.filter(ok)));
lista.innerHTML=t.length?`<div class="mecz"><div class="wrap"><table><tr><th></th><th>Mecz</th><th>Typ</th><th>Prawd.</th><th>Uczciwy kurs</th><th>Kurs rynku</th><th>Forma gosp.</th><th>Forma gościa</th><th>H2H</th></tr>
${t.map(x=>`<tr>${wiersz(x).replace("</td>",`</td><td><b>${x.m.h} &ndash; ${x.m.g}</b><br><span style="color:var(--mut);font-size:11px">${x.m.liga} &middot; ${x.m.data} ${x.m.godz}</span></td>`)}</tr>`).join("")}</table></div></div>`:'<p>Brak typów przy tych filtrach.</p>';return}
const mecze=D.filter(pasuje).map(m=>({m,t:sortuj(m.typy.filter(ok))})).filter(z=>z.t.length);
if(sort.value!=="p")mecze.sort((a,b)=>KLUCZ[sort.value](a.t[0])-KLUCZ[sort.value](b.t[0]));
for(const {m,t} of mecze){
html+=`<div class="mecz"><div class="top"><span class="m">${m.liga} &middot; ${m.data} ${m.godz}</span><br><b>${m.h} &ndash; ${m.g}</b>
<div class="info"><span>Oczekiwane gole: <b>${m.lh} : ${m.la}</b></span>${m.kartki?`<span>Kartki ~<b>${m.kartki}</b></span>`:""}${m.rozne?`<span>Rożne ~<b>${m.rozne}</b></span>`:""}${m.sedzia?`<span>Sędzia: <b>${m.sedzia}</b></span>`:""}
<span>Najczęstsze wyniki: ${m.wyniki.join(", ")}</span></div>
<div class="info"><span>H2H: ${m.h2h.length?m.h2h.join(" &middot; "):"brak meczów"}</span></div></div>
<div class="wrap"><table><tr><th></th><th>Typ</th><th>Prawd.</th><th>Uczciwy kurs</th><th>Kurs rynku</th><th>Forma ${m.h}</th><th>Forma ${m.g}</th><th>H2H</th></tr>
${t.map(x=>`<tr>${wiersz(x)}</tr>`).join("")}
</table></div></div>`}
lista.innerHTML=html||'<p>Brak typów przy tych filtrach.</p>'}
if(location.hostname==="localhost"||location.hostname==="127.0.0.1")setTimeout(()=>location.reload(),15*60*1000);
for(const e of [min,minf,val,szukaj,sort,razem,kat])e.addEventListener("input",rys);wczytaj();rys();kupon();generuj();
</script></body></html>"""


def zapisz_html(sciezka, mecze, n_forma, otworz=True, odswiez_url=""):
    import json
    import os
    import webbrowser
    strona = (STRONA.replace("__JSON__", json.dumps(mecze, ensure_ascii=False))
              .replace("__DATA__", datetime.now().strftime("%d.%m.%Y %H:%M"))
              .replace("__N__", str(n_forma))
              .replace("__BT__", ' &middot; <a href="backtest.html" style="font-size:14px;color:#4a7bd0">&#x1F4CA; Backtest</a>'
                       if os.path.exists(os.path.join(os.path.dirname(sciezka) or ".", "backtest.html")) else "")
              )
    with open(sciezka, "w", encoding="utf-8") as f:
        f.write(strona)
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
    ap.add_argument("--zaw-max", type=int, default=12, help="dla ilu meczów liczyć zawodników (limit 100 zapytań/dzień)")
    ap.add_argument("--value-prog", type=float, default=1.05, help="value bet gdy szansa x kurs > próg")
    ap.add_argument("--cache", default="cache_api.json", help="plik pamięci podręcznej API")
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
    wiersze_out, mecze_html = [], []
    for w in terminarz:
        kod = w["Div"]
        h_pokaz, g_pokaz = w["HomeTeam"].strip(), w["AwayTeam"].strip()
        if kod in PUCHARY:
            pop = lambda n: "Paris SG" if normalizuj(n) == "paris" else n
            zh, zg = indeks.get(znajdz(pop(h_pokaz), indeks)), indeks.get(znajdz(pop(g_pokaz), indeks))
            if not zh or not zg:
                brak = h_pokaz if not zh else g_pokaz
                print(f"\n{NAZWY[kod]} {h_pokaz} - {g_pokaz}: brak danych o {brak} (liga spoza bazy)")
                continue
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
        mecz_html = {"liga": NAZWY.get(kod, kod), "data": w.get("Date", ""), "godz": w.get("Time", ""),
                     "h": h_pokaz, "g": g_pokaz, "lh": round(pr["lam_h"], 2), "la": round(pr["lam_a"], 2),
                     "kartki": round(pr["kartki"], 1) if pr.get("kartki") else None,
                     "rozne": round(pr["rozne"], 1) if pr.get("rozne") else None,
                     "sedzia": f'{pr["sedzia"][0]} (kartki x{pr["sedzia"][1]:.2f})' if pr.get("sedzia") else None,
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
            jest_value = bool(kurs_rynku and p * kurs_rynku > a.value_prog)
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
                                      "rynek": kurs_rynku, "buk": buk, "value": jest_value,
                                      "fh": list(sh), "fg": list(sg), "h2h": list(s2)})
        if af and ile_zaw < a.zaw_max and not af.wyczerpane:
            ile_zaw += 1
            zaw = zawodnicy_meczu(af, w.get("Date"), h_pokaz, g_pokaz, pr["lam_h"], pr["lam_a"])
            for x in zaw:
                print(f"   {x['p']:5.1f}%  {x['typ']}  seria {x['seria']} | ostatnie {x['ost']}")
            mecz_html["typy"] += zaw

    cache.zapisz()
    if af:
        print(f"\nAPI-Football: użyto {af.uzyte} zapytań w tym uruchomieniu")
    if a.html:
        zapisz_html(a.html, [m for m in mecze_html if m["typy"]], a.forma,
                    otworz and not a.nie_otwieraj, a.odswiez_url)
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
