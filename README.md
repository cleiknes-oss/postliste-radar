# Postliste-radar · Enebakk

Nattlig oversikt over poster i offentlige postlister som er relevante for Enebakk.

**Dashboard:** https://cleiknes-oss.github.io/postliste-radar/

## Kilder
- Enebakk kommune (hele postlisten + søkeord), alltid øverst
- Nordre Follo, Rælingen, Ås, Frogn, Nesodden og Vestby (søkeord)
- eInnsyn: Akershus fylkeskommune, Statsforvalteren, departementene og andre statlige virksomheter (søkeord)
- Stortinget: skriftlige spørsmål, spørretimespørsmål, interpellasjoner og saker (søkeord i tittel)

## Slik fungerer det
GitHub Actions (`.github/workflows/nightly.yml`) kjører hver natt ca. kl. 02:17:
1. `radar/collect.py` henter poster for siste 30 dager og lagrer historikk i `data/items.json`
2. `radar/build.py` bygger `site/index.html`, som publiseres på GitHub Pages

Kjør manuelt: **Actions → Nattlig innhenting → Run workflow**.

## Endre søkeord
Rediger `config.yaml`. Hvert søkeord har `weight` (relevans), `scope` (`all`, `regional`, `enebakk`) og `group` (farge/fokusboks). En endring som pushes til `main` starter en ny kjøring automatisk.
