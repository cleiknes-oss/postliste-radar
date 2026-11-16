"""Bygger site/index.html fra data/items.json og data/status.json."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SITE = ROOT / "site"
OSLO = dt.timezone(dt.timedelta(hours=1))


def norm(s):
    return re.sub(r"[^\wæøå]+", " ", (s or "").lower()).strip()


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    st = cfg["settings"]
    items = json.loads((DATA / "items.json").read_text(encoding="utf-8"))
    status = json.loads((DATA / "status.json").read_text(encoding="utf-8"))
    kw = {k["term"]: k for k in cfg["keywords"]}
    today = dt.datetime.now(OSLO).date()
    since = (today - dt.timedelta(days=st["lookback_days"])).isoformat()
    full_since = (today - dt.timedelta(days=st["enebakk_full_days"])).isoformat()
    newest_seen = max((i.get("first_seen", "") for i in items), default="")
    primary = next(s["id"] for s in cfg["sources"] if s.get("primary"))

    def age(i):
        try:
            return (today - dt.date.fromisoformat(i["date"])).days
        except (ValueError, KeyError):
            return 30

    # dedupliser: samme tittel + dato i flere kilder → behold direktekilden
    seen, uniq = {}, []
    for i in sorted(items, key=lambda i: i["id"].startswith("einnsyn:")):
        key = (norm(i["title"])[:120], i.get("date"))
        if key in seen and i["source_id"] != seen[key]:
            continue
        seen[key] = i["source_id"]
        uniq.append(i)

    out = []
    for i in uniq:
        if (i.get("date") or "") < since:
            continue
        terms = list(dict.fromkeys(i.get("matches", [])))
        if i["source_id"] != primary:
            terms = [t for t in terms if kw.get(t, {}).get("scope") != "enebakk"]
            if not terms:
                continue
        score = sum(kw.get(t, {}).get("weight", 1) for t in terms)
        groups = sorted({kw.get(t, {}).get("group", "annet") for t in terms})
        out.append({
            **{k: i.get(k, "") for k in ("id", "source_id", "source_label", "agency", "title", "party",
                                          "case", "caseno", "date", "doctype", "direction", "url")},
            "terms": terms, "groups": groups, "score": score,
            "relevance": round(score * 0.5 ** (age(i) / st["relevance_halflife_days"]), 3),
            "new": i.get("first_seen") == newest_seen,
            "full": bool(i.get("full")) and i["date"] >= full_since,
        })

    by_date = lambda l: sorted(l, key=lambda i: (i["date"], i["score"]), reverse=True)  # noqa: E731
    by_rel = lambda l: sorted(l, key=lambda i: (i["relevance"], i["date"]), reverse=True)  # noqa: E731

    enebakk = [i for i in out if i["source_id"] == primary]
    others = [i for i in out if i["source_id"] != primary]
    sources = {}
    for i in others:
        sources.setdefault(i["source_id"], {"id": i["source_id"], "label": i["source_label"], "items": []})["items"].append(i)
    ranked = sorted(sources.values(), key=lambda s: sum(i["relevance"] for i in s["items"]), reverse=True)
    for s in ranked:
        s["items"] = by_rel(s["items"])
        s["relevance"] = round(sum(i["relevance"] for i in s["items"]), 2)
    configured = []
    for s in cfg["sources"]:
        if s.get("primary"):
            continue
        if s["type"] == "einnsyn":
            configured += ["Akershus fylkeskommune", "Statsforvalteren i Østfold, Buskerud, Oslo og Akershus",
                           "Regjeringen (departementene)"]
        else:
            configured.append(s["label"])

    payload = {
        "generated": dt.datetime.now(OSLO).strftime("%d.%m.%Y kl. %H:%M"),
        "lookback": st["lookback_days"], "fullDays": st["enebakk_full_days"],
        "enebakkHits": by_date([i for i in enebakk if i["score"] > 0]),
        "enebakkAll": by_date([i for i in enebakk if i["full"]]),
        "ostmarka": sorted([i for i in out if "ostmarka" in i["groups"]],
                           key=lambda i: (i["score"], i["date"]), reverse=True),
        "mentioned": by_date([i for i in others if "enebakk" in i["groups"]]),
        "sources": ranked,
        "quiet": [l for l in configured if l not in {s["label"] for s in ranked}],
        "status": status,
        "keywords": cfg["keywords"],
    }
    SITE.mkdir(exist_ok=True)
    tpl = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    (SITE / "index.html").write_text(tpl.replace("/*__DATA__*/null", data), encoding="utf-8")
    (SITE / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"Bygget site/index.html: {len(enebakk)} Enebakk, {len(others)} andre, {len(ranked)} kilder med treff")


if __name__ == "__main__":
    main()
