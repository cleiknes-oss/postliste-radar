"""Henter poster fra postlister og lagrer dem i data/items.json.

Kjøres hver natt av GitHub Actions. Hver kilde håndteres for seg, så én kilde
som feiler stopper ikke resten. Status per kilde lagres i data/status.json.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "postliste-radar/1.0 (Enebakk kommune, ordfører; github.com/cleiknes-oss/postliste-radar)"
OSLO = dt.timezone(dt.timedelta(hours=1))

session = requests.Session()
session.headers["User-Agent"] = UA


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, **kw):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=40, **kw)
            if r.status_code in (429, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            log("  feil:", e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Fikk ikke svar fra {url}")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def query_of(term: str) -> str:
    return term.replace("+", " ")


def match_terms(text: str, keywords: list[dict]) -> list[str]:
    t = norm(text)
    hits = []
    for k in keywords:
        parts = [norm(p) for p in k["term"].split("+")]
        # ordgrense i starten, fri slutt (fanger bøyninger: Østmarka, Østmarkas …)
        if all(re.search(r"(?<![\wæøå])" + re.escape(p), t) for p in parts):
            hits.append(k["term"])
    return hits


def today() -> dt.date:
    return dt.datetime.now(OSLO).date()


DOCTYPE = {"I": "Inngående", "U": "Utgående", "N": "Notat", "X": "Notat", "S": "Saksframlegg"}


# ---------------------------------------------------------------- Elements
def elements_query(src, query, dfrom, dto):
    url = (
        f"{src['base']}/publikum/api/PredefinedQuery/CasesAndRegistryEntries?"
        + urlencode({
            "Query": query, "OrderBy": "DATE", "SortOrder": 1,
            "DateFrom": dfrom.isoformat(), "DateTo": dto.isoformat(),
            "IncludeCases": "false", "IncludeCaseWorker": "false",
            "IncludeSenderRecipient": "true",
        })
        + "&Skips=[]"
    )
    hdr = {
        "Tenant": src["tenant"], "Accept": "application/json", "Content-Type": "application/json",
        "Referer": f"{src['base']}/publikum/{src['tenant']}/",
    }
    r = get(url, headers=hdr)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    out = []
    for it in j.get("Items", []):
        if it.get("Type") != 2:
            continue
        out.append({
            "id": f"{src['id']}:{it['JP_ID']}",
            "title": it.get("JP_INNHOLD_G") or "",
            "party": it.get("JP_AVSMOT_G") or "",
            "case": it.get("SA_SAKSNR_XX") or "",
            "date": (it.get("JP_JDATO") or "")[:10],
            "doctype": it.get("ND_BETEGN") or DOCTYPE.get(it.get("JP_NDOKTYPE_ND"), ""),
            "direction": it.get("JP_NDOKTYPE_ND") or "",
            "url": f"{src['base']}/publikum/{src['tenant']}/RegistryEntry/{it['JP_ID']}",
        })
    return out, j.get("TotalCount", 0), len(j.get("Items", []))


def collect_elements(src, terms, cfg):
    t = today()
    since = t - dt.timedelta(days=cfg["settings"]["lookback_days"])
    items, notes = {}, []
    if src.get("primary"):
        # Hele postlisten, én dag om gangen (API-et gir maks 50 per kall)
        d = since
        while d <= t:
            got, total, n = elements_query(src, "", d, d)
            if total > n:
                notes.append(f"{d.isoformat()}: {total} poster, hentet {n}")
            for it in got:
                it["full"] = True
                items[it["id"]] = it
            d += dt.timedelta(days=1)
            time.sleep(0.3)
    for term in terms:
        got, total, n = elements_query(src, query_of(term), since, t)
        if total > n:
            notes.append(f"«{term}»: {total} treff, hentet {n}")
        for it in got:
            items.setdefault(it["id"], it)
        time.sleep(0.3)
    return list(items.values()), notes


# ---------------------------------------------------------------- Public 360
def parse_360(src, page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for li in soup.select("li[id^=documentrow_]"):
        a = li.select_one("h4.title a")
        title = li.select_one(".document-card-title")
        fields = {}
        for dl in li.select(".detailsList"):
            h = dl.select_one(".documentDetailHeader")
            c = dl.select_one(".documentDetailContent")
            if h and c:
                fields[h.get_text(strip=True)] = c.get_text(" ", strip=True)
        datestr = fields.get("Journaldato", "")
        try:
            date = dt.datetime.strptime(datestr, "%d.%m.%Y").date().isoformat()
        except ValueError:
            date = ""
        docno = fields.get("Dokumentnummer", li["id"].replace("documentrow_", ""))
        party_key = "Fra" if "Fra" in fields else ("Til" if "Til" in fields else None)
        href = a["href"] if a and a.has_attr("href") else ""
        out.append({
            "id": f"{src['id']}:{docno}",
            "title": title.get_text(" ", strip=True) if title else "",
            "party": fields.get(party_key, "") if party_key else "",
            "case": fields.get("Sak", ""),
            "caseno": docno,
            "date": date,
            "doctype": {"Fra": "Inngående", "Til": "Utgående"}.get(party_key, "Notat/annet"),
            "direction": {"Fra": "I", "Til": "U"}.get(party_key, "N"),
            "url": (src["base"] + html.unescape(href)) if href.startswith("/") else href,
        })
    return out


def collect_p360(src, terms, cfg):
    since = (today() - dt.timedelta(days=cfg["settings"]["lookback_days"])).isoformat()
    items, notes = {}, []
    for term in terms:
        offset = 0
        for _page in range(6):
            url = f"{src['base']}/Journal/SearchSimple?searchstring={quote(query_of(term))}"
            if offset:
                url += f"&offset={offset}"
            r = get(url)
            r.raise_for_status()
            got = parse_360(src, r.text)
            if not got:
                break
            for it in got:
                if it["date"] and it["date"] >= since:
                    items.setdefault(it["id"], it)
            # resultatene er sortert nyeste først – stopp når vi er forbi vinduet
            if min((g["date"] for g in got if g["date"]), default=since) < since:
                break
            offset += 10
            time.sleep(0.4)
        time.sleep(0.4)
    return list(items.values()), notes


# ---------------------------------------------------------------- eInnsyn
def einnsyn_group(name: str) -> tuple[str, str]:
    n = name.lower()
    if "akershus fylke" in n:
        return "akershusfk", "Akershus fylkeskommune"
    if "statsforvalter" in n and "oslo" in n:
        return "statsforvalteren", "Statsforvalteren i Østfold, Buskerud, Oslo og Akershus"
    if "departement" in n or "statsministerens" in n:
        return "regjeringen", "Regjeringen (departementene)"
    return "einnsyn_andre", "Andre offentlige virksomheter (eInnsyn)"


def collect_einnsyn(src, terms, cfg):
    since = (today() - dt.timedelta(days=cfg["settings"]["lookback_days"])).isoformat()
    excl = set(src.get("exclude_orgnr", []))
    items, notes = {}, []
    for term in terms:
        path = "/search?" + urlencode({
            "query": query_of(term), "entity": "Journalpost", "sortBy": "publisertDato",
            "sortOrder": "desc", "limit": 50,
        }) + "&expand=journalenhet&expand=korrespondansepart&expand=saksmappe"
        for _page in range(5):
            r = get("https://api.einnsyn.no" + path)
            if r.status_code != 200:
                notes.append(f"«{term}»: HTTP {r.status_code}")
                break
            j = r.json()
            batch = j.get("items", [])
            stop = False
            for it in batch:
                pub = (it.get("publisertDato") or "")[:10]
                if pub < since:
                    stop = True
                    continue
                enh = it.get("journalenhet") or {}
                if isinstance(enh, dict) and enh.get("orgnummer") in excl:
                    continue
                enh_name = enh.get("navn", "") if isinstance(enh, dict) else ""
                gid, glabel = einnsyn_group(enh_name)
                kps = [k for k in (it.get("korrespondansepart") or []) if isinstance(k, dict)]
                ext = [k.get("korrespondansepartNavn", "") for k in kps
                       if k.get("korrespondanseparttype", "").endswith(("avsender", "mottaker"))
                       and "intern" not in k.get("korrespondanseparttype", "")]
                # lange mottakerlister (høringer) gir falske treff
                party = "; ".join(ext) if len(ext) <= 3 else f"{len(ext)} mottakere"
                sm = it.get("saksmappe") if isinstance(it.get("saksmappe"), dict) else {}
                jt = it.get("journalposttype", "")
                url = "https://einnsyn.no/saksmappe?" + urlencode({
                    "id": sm.get("externalId") or "", "jid": it.get("externalId") or ""
                }) if sm.get("externalId") else "https://einnsyn.no/sok?" + urlencode({"q": it.get("offentligTittel", "")})
                items.setdefault(it["id"], {
                    "id": f"einnsyn:{it['id']}",
                    "source_id": gid, "source_label": glabel,
                    "agency": enh_name,
                    "title": it.get("offentligTittel") or "",
                    "party": party,
                    "case": sm.get("offentligTittel", "") if sm else "",
                    "caseno": f"{sm.get('saksaar','')}/{sm.get('sakssekvensnummer','')}" if sm.get("saksaar") else "",
                    "date": it.get("journaldato") or pub,
                    "published": pub,
                    "doctype": {"inngaaende_dokument": "Inngående", "utgaaende_dokument": "Utgående",
                                "organinternt_dokument_uten_oppfoelging": "Notat",
                                "organinternt_dokument_for_oppfoelging": "Notat",
                                "saksframlegg": "Saksframlegg", "moeteprotokoll": "Møteprotokoll"}.get(jt, jt.replace("_", " ")),
                    "direction": {"inngaaende_dokument": "I", "utgaaende_dokument": "U"}.get(jt, "N"),
                    "url": url,
                })
            nxt = j.get("next")
            if stop or not nxt or not batch:
                break
            path = nxt
            time.sleep(0.4)
        time.sleep(0.4)
    return list(items.values()), notes


# ---------------------------------------------------------------- Stortinget
def st_date(s):
    m = re.search(r"/Date\((\d+)", s or "")
    if not m:
        return ""
    return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, OSLO).date().isoformat()


def sessions():
    t = today()
    y = t.year if t.month >= 10 else t.year - 1
    return [f"{y}-{y+1}", f"{y-1}-{y}"]


def collect_stortinget(src, terms, cfg):
    since = (today() - dt.timedelta(days=cfg["settings"]["lookback_days"])).isoformat()
    kws = [{"term": t} for t in terms]
    items, notes = {}, []
    base = "https://data.stortinget.no/eksport"
    for ses in sessions():
        for kind, key, label in [
            ("skriftligesporsmal", "sporsmal_liste", "Skriftlig spørsmål"),
            ("sporretimesporsmal", "sporsmal_liste", "Spørretimespørsmål"),
            ("interpellasjoner", "sporsmal_liste", "Interpellasjon"),
        ]:
            r = get(f"{base}/{kind}?sesjonid={ses}&format=json")
            if r.status_code != 200:
                notes.append(f"{kind} {ses}: HTTP {r.status_code}")
                continue
            for s in r.json().get(key, []):
                d = st_date(s.get("datert_dato")) or st_date(s.get("sendt_dato"))
                if d < since:
                    continue
                fra = s.get("sporsmal_fra") or {}
                til = s.get("sporsmal_til_minister_tittel") or ""
                text = f"{s.get('tittel','')} {til}"
                if not match_terms(text, kws):
                    continue
                parti = (fra.get("parti") or {}).get("id", "") if isinstance(fra.get("parti"), dict) else ""
                items[f"st:{s['id']}"] = {
                    "id": f"stortinget:{s['id']}",
                    "title": s.get("tittel", ""),
                    "party": f"{fra.get('fornavn','')} {fra.get('etternavn','')} ({parti}) til {til}".strip(),
                    "case": "", "date": d, "doctype": label, "direction": "N",
                    "url": f"https://www.stortinget.no/no/Saker-og-publikasjoner/Sporsmal/Sporsmal/?qid={s['id']}",
                }
            time.sleep(0.3)
        r = get(f"{base}/saker?sesjonid={ses}&format=json")
        if r.status_code == 200:
            for s in r.json().get("saker_liste", []):
                d = st_date(s.get("sist_oppdatert_dato"))
                if d < since:
                    continue
                text = f"{s.get('tittel','')} {s.get('korttittel','')}"
                if not match_terms(text, kws):
                    continue
                items[f"sak:{s['id']}"] = {
                    "id": f"stortinget:sak:{s['id']}",
                    "title": s.get("korttittel") or s.get("tittel", ""),
                    "party": s.get("tittel", "") if s.get("korttittel") else "",
                    "case": s.get("henvisning", "") or "",
                    "date": d, "doctype": "Sak (oppdatert)", "direction": "N",
                    "url": f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={s['id']}",
                }
        else:
            notes.append(f"saker {ses}: HTTP {r.status_code}")
    return list(items.values()), notes


COLLECTORS = {
    "elements": collect_elements,
    "p360": collect_p360,
    "einnsyn": collect_einnsyn,
    "stortinget": collect_stortinget,
}


# ---------------------------------------------------------------- main
def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    kw_all = cfg["keywords"]
    excl = [re.compile(p, re.I) for p in cfg.get("exclude_patterns", [])]
    DATA.mkdir(exist_ok=True)
    store_path = DATA / "items.json"
    store = {i["id"]: i for i in json.loads(store_path.read_text(encoding="utf-8"))} if store_path.exists() else {}
    run_date = today().isoformat()
    run_ts = dt.datetime.now(OSLO).isoformat(timespec="minutes")
    status = []

    for src in cfg["sources"]:
        if src.get("enabled") is False:
            continue
        national = src["type"] in ("einnsyn", "stortinget")
        scopes = {"all"} if national else {"all", "regional"}
        terms = [k["term"] for k in kw_all if k["scope"] in scopes]
        # kun ord som gir mening å matche mot denne kilden
        match_kws = [k for k in kw_all if k["scope"] in scopes or (src.get("primary") and k["scope"] == "enebakk")]
        log(f"== {src['label']} ({len(terms)} søk)")
        t0 = time.time()
        try:
            got, notes = COLLECTORS[src["type"]](src, terms, cfg)
            kept = 0
            for it in got:
                it.setdefault("source_id", src["id"])
                it.setdefault("source_label", src["label"])
                if any(p.search(it["title"]) for p in excl):
                    continue
                text = " ".join([it["title"], it.get("party", ""), it.get("case", "")])
                hits = match_terms(text, match_kws)
                if not hits and not it.get("full"):
                    continue  # søketreff som ikke faktisk inneholder ordet
                it["matches"] = hits
                prev = store.get(it["id"])
                it["first_seen"] = prev["first_seen"] if prev else run_date
                if prev and prev.get("full"):
                    it["full"] = True
                store[it["id"]] = it
                kept += 1
            status.append({"source": src["label"], "ok": True, "items": kept, "notes": notes,
                           "seconds": round(time.time() - t0)})
            log(f"   {kept} poster lagret, {len(notes)} merknader")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status.append({"source": src["label"], "ok": False, "error": f"{type(e).__name__}: {e}",
                           "seconds": round(time.time() - t0)})

    cutoff = (today() - dt.timedelta(days=cfg["settings"]["keep_days"])).isoformat()
    items = sorted((i for i in store.values() if (i.get("date") or run_date) >= cutoff),
                   key=lambda i: (i.get("date") or "", i["id"]), reverse=True)
    store_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "status.json").write_text(json.dumps({"run": run_ts, "sources": status}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    log(f"Ferdig: {len(items)} poster i historikken")
    if not any(s["ok"] for s in status):
        sys.exit(1)


if __name__ == "__main__":
    main()
