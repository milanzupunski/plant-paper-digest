#!/usr/bin/env python3
"""
Plant paper digest
------------------
Pulls new papers from Europe PMC (PubMed + preprints), bioRxiv and Crossref
(selected journals), filters and scores them against profiles.txt, removes
duplicates and papers already shown in earlier digests, and writes:

    digests/digest_YYYY-MM-DD.html   (the reading list, with curation tools)
    digests/digest_YYYY-MM-DD.csv    (same list as a table)
    digests/latest.html              (copy of the newest digest)

With --site DIR (used by the GitHub workflow) it writes a small website
instead: DIR/index.html (newest digest), DIR/archive.html, DIR/feed.xml.

Usage
    python digest.py                     # last N days (N from profiles.txt)
    python digest.py --days 30           # longer window, e.g. first run
    python digest.py --from 2026-09-01 --to 2026-09-30
    python digest.py --ignore-seen       # show papers even if shown before
    python digest.py --no-crossref       # skip Crossref
Requires only the "requests" package (included in Anaconda).
"""

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("The 'requests' package is missing. Install it with:  pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "digests")
SEEN_FILE = os.path.join(HERE, "seen.json")
UA = "plant-paper-digest/1.0 (personal literature digest)"


# ======================================================================
# Profiles
# ======================================================================

def compile_term(term):
    """Turn a profiles.txt term into a compiled regex."""
    if term.startswith("re:"):
        return re.compile(term[3:])
    wildcard = term.endswith("*")
    core = term[:-1] if wildcard else term
    letters = [c for c in core if c.isalpha()]
    case_sensitive = len(letters) >= 2 and all(c.isupper() for c in letters)
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", core.strip()) if p]
    pat = r"[\s\-]+".join(parts)
    if wildcard:
        pat += r"[\w\-]*"
    pat = r"(?<![A-Za-z0-9])" + pat + r"(?![A-Za-z0-9])"
    return re.compile(pat, 0 if case_sensitive else re.IGNORECASE)


def parse_profiles(path):
    prof = {"settings": {}, "gate": [], "sections": [], "boost": [],
            "penalty": [], "journals": [], "journal_penalty": []}
    current = None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                head = m.group(1).strip()
                if head.lower().startswith("section:"):
                    current = ("section", head.split(":", 1)[1].strip())
                    prof["sections"].append({"name": current[1], "terms": []})
                else:
                    current = (head.lower(), None)
                continue
            if current is None:
                continue
            kind = current[0]
            if kind == "settings":
                if "=" in line:
                    k, v = line.split("=", 1)
                    prof["settings"][k.strip()] = v.strip()
                continue
            fields = [f.strip() for f in line.split("|")]
            # "term | 2": the weight is the number after the LAST bar, so a
            # regular expression may itself contain | characters
            wm = re.match(r"^(.*?)\s*\|\s*(-?[0-9]*\.?[0-9]+)\s*$", line)
            term, weight = (wm.group(1), float(wm.group(2))) if wm else (line, None)
            if kind == "journal penalty":
                prof["journal_penalty"].append(
                    {"name": term.lower(), "weight": 3.0 if weight is None else weight})
                continue
            if kind == "journals":
                if len(fields) >= 2:
                    prof["journals"].append({
                        "issn": fields[0], "name": fields[1],
                        "plant": len(fields) > 2 and fields[2].lower() == "plant"})
                continue
            if weight is None:
                weight = 1.0
            try:
                rx = compile_term(term)
            except re.error as e:
                print("WARNING: skipping invalid term in profiles.txt: %r (%s)" % (term, e))
                continue
            entry = {"term": term, "weight": weight, "rx": rx}
            if kind == "gate":
                prof["gate"].append(entry)
            elif kind == "section":
                prof["sections"][-1]["terms"].append(entry)
            elif kind in ("boost", "penalty"):
                prof[kind].append(entry)
    return prof


# ======================================================================
# HTTP helper
# ======================================================================

def get_json(url, params=None, tries=4):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=60,
                             headers={"User-Agent": UA})
            if r.status_code in (429, 500, 502, 503, 504):
                last = "HTTP %s" % r.status_code
                time.sleep(5 * (i + 1))
                continue
            if 400 <= r.status_code < 500:
                raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:200]))
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = str(e)
            time.sleep(5 * (i + 1))
    raise RuntimeError("request failed after %d tries: %s" % (tries, last))


def strip_tags(text):
    if not text:
        return ""
    # some sources send escaped markup (&lt;sup&gt;), so unescape first
    text = html.unescape(html.unescape(text))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_title(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())[:120]


# ======================================================================
# Sources
# ======================================================================

def epmc_term(term):
    """profiles.txt gate term -> Europe PMC title/abstract clause."""
    if term.startswith("re:"):
        return None
    if term.endswith("*") and " " not in term:
        t = term
    else:
        t = '"%s"' % term.rstrip("*").replace('"', "")
    return "TITLE:%s OR ABSTRACT:%s" % (t, t)


def fetch_europepmc(prof, d_from, d_to, log):
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    clauses = [c for c in (epmc_term(g["term"]) for g in prof["gate"]) if c]
    gate_q = "(" + " OR ".join(clauses) + ")"
    out = []
    # FIRST_IDATE = date first indexed in Europe PMC, which also catches
    # papers that were published a while ago but only just indexed.
    # Falls back to publication date if the index-date query returns nothing.
    for date_field in ("FIRST_IDATE", "FIRST_PDATE"):
        query = "%s AND %s:[%s TO %s]" % (gate_q, date_field, d_from, d_to)
        cursor, total, out = "*", None, []
        while True:
            try:
                data = get_json(url, {"query": query, "format": "json",
                                      "resultType": "core", "pageSize": 1000,
                                      "cursorMark": cursor})
            except RuntimeError as e:
                if total is None and date_field == "FIRST_IDATE":
                    log("  Europe PMC (FIRST_IDATE) query failed, trying publication date: %s" % e)
                    break
                raise
            if total is None:
                total = int(data.get("hitCount", 0))
                log("  Europe PMC (%s): %d hits" % (date_field, total))
                if total == 0:
                    break
            results = data.get("resultList", {}).get("result", [])
            for r in results:
                out.append(epmc_record(r))
            nxt = data.get("nextCursorMark")
            if not results or not nxt or nxt == cursor:
                break
            cursor = nxt
        if out:
            break
    return out


def epmc_record(r):
    is_preprint = r.get("source") == "PPR"
    journal = ""
    ji = r.get("journalInfo") or {}
    if ji.get("journal"):
        journal = ji["journal"].get("title") or ji["journal"].get("medlineAbbreviation") or ""
    if is_preprint:
        journal = ((r.get("bookOrReportDetails") or {}).get("publisher")) or "Preprint"
    authors = r.get("authorString") or ""
    if not authors:
        al = (r.get("authorList") or {}).get("author") or []
        authors = ", ".join(a.get("fullName", "") for a in al)
    doi = (r.get("doi") or "").lower()
    if doi:
        link = "https://doi.org/" + doi
    elif r.get("pmid"):
        link = "https://pubmed.ncbi.nlm.nih.gov/%s/" % r["pmid"]
    else:
        link = "https://europepmc.org/article/%s/%s" % (r.get("source", ""), r.get("id", ""))
    ptypes = " ".join((r.get("pubTypeList") or {}).get("pubType") or []).lower()
    return {
        "review": "review" in ptypes,
        "title": strip_tags(r.get("title")),
        "abstract": strip_tags(r.get("abstractText")),
        "authors": authors,
        "journal": strip_tags(journal),
        "date": r.get("firstPublicationDate") or "",
        "doi": doi,
        "link": link,
        "preprint": is_preprint,
        "source": "Europe PMC",
        "plant_by_source": False,
        "published_doi": "",
    }


def fetch_biorxiv(prof, d_from, d_to, log):
    """Fetched one day at a time, so a server error loses at most part of
    one day instead of the whole window."""
    new_only = prof["settings"].get("biorxiv_new_only", "yes").lower().startswith("y")
    out, total, failed_days = [], 0, []
    day = dt.date.fromisoformat(d_from)
    last = dt.date.fromisoformat(d_to)
    days_todo = []
    while day <= last:
        days_todo.append(day.isoformat())
        day += dt.timedelta(days=1)
    for attempt in (1, 2):
        if attempt == 2:
            if not failed_days:
                break
            time.sleep(30)
            days_todo, failed_days = failed_days, []
        for ds in days_todo:
            total, failed_days = biorxiv_day(ds, new_only, out, total, failed_days)
    log("  bioRxiv: %d records in window, %d kept as new preprints" % (total, len(out)))
    if failed_days:
        log("  bioRxiv: server errors on %s (those days may be incomplete)" % ", ".join(failed_days))
        if len(failed_days) > (last - dt.date.fromisoformat(d_from)).days:
            raise RuntimeError("all days failed")
    return out


def biorxiv_day(ds, new_only, out, total, failed_days):
    cursor, day_total = 0, None
    while True:
        try:
            data = get_json("https://api.biorxiv.org/details/biorxiv/%s/%s/%d"
                            % (ds, ds, cursor), tries=3)
        except RuntimeError:
            failed_days.append(ds)
            break
        msg = (data.get("messages") or [{}])[0]
        if day_total is None:
            try:
                day_total = int(msg.get("total", 0) or 0)
            except ValueError:
                day_total = 0
            total += day_total
        coll = data.get("collection") or []
        for r in coll:
            if new_only and str(r.get("version", "1")) != "1":
                continue
            out.append(biorxiv_record(r))
        cursor += len(coll)
        if not coll or cursor >= day_total:
            break
    return total, failed_days


def biorxiv_record(r):
    doi = (r.get("doi") or "").lower()
    pub = (r.get("published") or "").lower()
    return {
        "title": strip_tags(r.get("title")),
        "abstract": strip_tags(r.get("abstract")),
        "authors": (r.get("authors") or "").replace(";", ","),
        "journal": "bioRxiv",
        "date": r.get("date") or "",
        "doi": doi,
        "link": "https://doi.org/" + doi if doi else "",
        "preprint": True,
        "source": "bioRxiv",
        "plant_by_source": (r.get("category") or "").lower() == "plant biology",
        "published_doi": "" if pub in ("", "na") else pub,
        "category": r.get("category") or "",
    }


def fetch_crossref(prof, d_from, d_to, log):
    mailto = prof["settings"].get("crossref_mailto", "").strip()
    out, zero = [], []
    for j in prof["journals"]:
        params = {
            "filter": "issn:%s,from-created-date:%s,until-created-date:%s,type:journal-article"
                      % (j["issn"], d_from, d_to),
            "rows": 1000,
            "select": "DOI,title,author,container-title,abstract,created,published",
        }
        if mailto:
            params["mailto"] = mailto
        items, cursor, failed = [], "*", False
        while True:
            params["cursor"] = cursor
            try:
                data = get_json("https://api.crossref.org/works", params)
            except RuntimeError as e:
                log("  Crossref %s (%s): FAILED %s" % (j["name"], j["issn"], e))
                failed = True
                break
            msg = data.get("message") or {}
            page = msg.get("items") or []
            items += page
            nxt = msg.get("next-cursor")
            if len(page) < params["rows"] or not nxt or nxt == cursor:
                break
            cursor = nxt
        if failed and not items:
            continue
        if not items:
            zero.append("%s (%s)" % (j["name"], j["issn"]))
        for it in items:
            title = strip_tags(" ".join(it.get("title") or []))
            if not title:
                continue
            auth = it.get("author") or []
            names = []
            for a in auth:
                n = (a.get("family") or "")
                if a.get("given"):
                    n = "%s %s" % (n, "".join(p[0] for p in a["given"].replace("-", " ").split() if p))
                if n.strip():
                    names.append(n.strip())
            parts = ((it.get("published") or it.get("created") or {}).get("date-parts") or [[None]])[0]
            date = "-".join("%02d" % p if i else str(p) for i, p in enumerate(parts) if p)
            doi = (it.get("DOI") or "").lower()
            out.append({
                "title": title,
                "abstract": strip_tags(it.get("abstract")),
                "authors": ", ".join(names),
                "journal": strip_tags((it.get("container-title") or [j["name"]])[0]),
                "date": date,
                "doi": doi,
                "link": "https://doi.org/" + doi,
                "preprint": False,
                "source": "Crossref",
                "plant_by_source": j["plant"],
                "published_doi": "",
            })
        time.sleep(0.3)
    log("  Crossref: %d articles from %d journal ISSNs" % (len(out), len(prof["journals"])))
    return out, zero


# ======================================================================
# Scoring
# ======================================================================

def score_record(rec, prof):
    title, abstract = rec["title"], rec["abstract"]

    def hit(entry):
        if entry["rx"].search(title):
            return 2.0
        if entry["rx"].search(abstract):
            return 1.0
        return 0.0

    if not rec.get("plant_by_source"):
        if not any(hit(g) for g in prof["gate"]):
            return None

    sec_scores, matched = [], []
    for sec in prof["sections"]:
        s, terms = 0.0, []
        for t in sec["terms"]:
            h = hit(t)
            if h:
                s += h * t["weight"]
                terms.append(t["term"].replace("re:", ""))
        sec_scores.append(s)
        matched.append(terms)
    boost = sum(hit(b) * b["weight"] for b in prof["boost"])
    penalty = sum(b["weight"] for b in prof["penalty"] if hit(b))
    jname = (rec.get("journal") or "").lower()
    jclean = re.sub(r"[^a-z0-9 ]", " ", jname).split()
    for j in prof["journal_penalty"]:
        if j["name"].startswith("="):
            if jclean == re.sub(r"[^a-z0-9 ]", " ", j["name"][1:]).split():
                penalty += j["weight"]
        elif j["name"] in jname:
            penalty += j["weight"]
    total = sum(sec_scores) + boost - penalty

    best = max(range(len(sec_scores)), key=lambda i: sec_scores[i]) if sec_scores else 0
    if not sec_scores or sec_scores[best] == 0:
        section = "Other plant biology"
    else:
        section = prof["sections"][best]["name"]
    tags = [prof["sections"][i]["name"] for i in range(len(sec_scores))
            if sec_scores[i] > 0 and i != best]
    terms = []
    for lst in matched:
        for t in lst:
            if t not in terms:
                terms.append(t)
    return {"score": round(total, 1), "section": section, "tags": tags,
            "terms": terms, "penalized": penalty > 0}


# ======================================================================
# Dedup + seen list
# ======================================================================

def dedupe(records):
    """Merge records that are the same paper. Journal versions win over
    preprints; records with abstracts win over records without."""
    def rank(r):
        return (0 if r["preprint"] else 1, 1 if r["abstract"] else 0,
                {"Crossref": 0, "Europe PMC": 1, "bioRxiv": 1}.get(r["source"], 0))

    by_key, order = {}, []
    for r in records:
        keys = [k for k in (r["doi"], r.get("published_doi"), "t:" + norm_title(r["title"])) if k and k != "t:"]
        found = None
        for k in keys:
            if k in by_key:
                found = by_key[k]
                break
        if found is None:
            idx = len(order)
            order.append(r)
            r["also"] = []
        else:
            idx = found
            cur = order[idx]
            winner, loser = (r, cur) if rank(r) > rank(cur) else (cur, r)
            winner["also"] = cur.get("also", []) + [loser["journal"]]
            winner["review"] = winner.get("review") or loser.get("review")
            if len(loser["authors"]) > len(winner["authors"]):
                winner["authors"] = loser["authors"]
            if not winner["abstract"] and loser["abstract"]:
                winner["abstract"] = loser["abstract"]
            winner["plant_by_source"] = winner.get("plant_by_source") or loser.get("plant_by_source")
            order[idx] = winner
        for k in keys:
            by_key[k] = idx
        for k in (order[idx]["doi"], "t:" + norm_title(order[idx]["title"])):
            if k and k != "t:":
                by_key[k] = idx
    return order


def seen_keys(r):
    keys = []
    if r["doi"]:
        keys.append(r["doi"])
    if r.get("published_doi"):
        keys.append(r["published_doi"])
    nt = norm_title(r["title"])
    if nt:
        keys.append("t:" + nt)
    return keys


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


STATE_FILE = os.path.join(HERE, "state.json")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)


def save_seen(seen):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(seen, fh)
    os.replace(tmp, SEEN_FILE)


# ======================================================================
# Output
# ======================================================================

def first_last_authors(a, n=3):
    names = [x.strip() for x in re.split(r",\s*", a or "") if x.strip()]
    if len(names) <= n + 1:
        return ", ".join(names)
    return ", ".join(names[:n]) + " … " + names[-1]


TOP_N = 30  # papers shown per section before "Show more"

HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plant paper digest __DATE__</title>
<style>
:root{--bg:#fbfaf7;--card:#fff;--ink:#1f2421;--mute:#68716b;--line:#e3e1da;
--acc:#2f6b4f;--acc2:#e7f0ea;--pre:#8a5a00;--pre2:#fbf1dc;--sel:#fff7d6}
:root[data-theme=dark]{--bg:#141716;--card:#1c201e;--ink:#e6e8e4;
--mute:#9aa39d;--line:#2d3330;--acc:#7cc2a0;--acc2:#20302a;--pre:#e0b25c;--pre2:#33291a;--sel:#3a3520}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.meta{color:var(--mute);font-size:13px}
.meta a{color:var(--acc)}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;align-items:center}
input[type=search]{flex:1;min-width:180px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
button{padding:7px 11px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);cursor:pointer;font-size:13px}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
label.chk{font-size:13px;color:var(--mute);display:flex;gap:4px;align-items:center}
nav{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
nav a{font-size:12px;text-decoration:none;color:var(--acc);background:var(--acc2);padding:2px 8px;border-radius:10px}
main{padding:8px 16px 60px}
h2{font-size:16px;margin:26px 0 8px;padding-bottom:4px;border-bottom:2px solid var(--acc)}
h2 small{color:var(--mute);font-weight:normal}
.item{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0;display:flex;gap:10px}
.item.kept{background:var(--sel)}
.item input{margin-top:5px;width:17px;height:17px;flex:none}
.body{flex:1;min-width:0}
.t{font-weight:600;color:var(--ink);text-decoration:none}
.t:hover{text-decoration:underline}
.au{color:var(--mute);font-size:13px}
.src{font-size:13px;margin-top:2px}
.badge{display:inline-block;font-size:11px;padding:0 6px;border-radius:8px;background:var(--acc2);color:var(--acc);margin-right:4px}
.badge.rev{background:var(--line);color:var(--mute)}
.badge.pre{background:var(--pre2);color:var(--pre)}
.terms{font-size:12px;color:var(--mute);margin-top:3px}
details{margin-top:4px;font-size:14px}
summary{cursor:pointer;color:var(--acc);font-size:13px}
.score{float:right;font-size:12px;color:var(--mute)}
.notes{font-size:12px;color:var(--mute);margin-top:6px}
.hide{display:none}
.item.more{display:none}
section.open .item.more{display:flex}
section.open .item.more.hide{display:none}
section.open .morebtn{display:none}
.morebtn{margin:4px 0 0}
select{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);font-size:13px}
.btnlike{padding:7px 11px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);cursor:pointer;font-size:13px}
.rm{margin-top:6px;font-size:12px;padding:3px 8px}
.src a,.meta a{color:var(--acc)}
</style>
<script>try{if(localStorage.getItem("plantdigest-theme")==="dark")document.documentElement.dataset.theme="dark"}catch(e){}</script>
</head>
"""

HTML_SCRIPT = r"""
<script>
const SKEY='plantdigest-saved';
function loadSaved(){try{return JSON.parse(localStorage.getItem(SKEY)||'{}')}catch(e){return {}}}
function storeSaved(o){try{localStorage.setItem(SKEY,JSON.stringify(o));return true}catch(e){alert('Your browser blocked saving. Saved papers cannot be stored in this window (private mode?).');return false}}
function refLine(r){return `${r.authors} (${(r.date||'').slice(0,4)}) ${r.title}. ${r.journal}. ${r.link}`}
function copyRefs(list){
  if(!list.length){alert('Nothing to copy yet.');return}
  const txt=list.map(refLine).join('\n\n');
  navigator.clipboard.writeText(txt).then(()=>flash('Copied '+list.length+' references'),()=>{prompt('Copy:',txt)});
}
function downloadFile(name,text,type){
  const blob=new Blob([text],{type:type});const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();a.remove();
}
function risFor(list,name){
  if(!list.length){alert('Nothing to export yet.');return}
  const lines=[];
  list.forEach(r=>{
    lines.push('TY  - '+(r.preprint?'UNPB':'JOUR'));
    (r.authors||'').split(/,\s*/).filter(Boolean).forEach(a=>lines.push('AU  - '+a));
    lines.push('TI  - '+r.title);lines.push('T2  - '+r.journal);
    if(r.date)lines.push('PY  - '+r.date.slice(0,4));
    if(r.doi)lines.push('DO  - '+r.doi);
    lines.push('UR  - '+r.link);
    if(r.abstract)lines.push('AB  - '+r.abstract);
    lines.push('ER  - ','');
  });
  downloadFile(name,lines.join('\r\n'),'application/x-research-info-systems');
}
function flash(m){const b=document.getElementById('flash');if(b){b.textContent=m;setTimeout(()=>b.textContent='',2500)}}
function setThemeLabel(){const b=document.getElementById("themebtn");if(b)b.textContent=document.documentElement.dataset.theme==="dark"?"Light mode":"Dark mode"}
function toggleTheme(){const d=document.documentElement;const dark=d.dataset.theme!=="dark";if(dark)d.dataset.theme="dark";else delete d.dataset.theme;try{localStorage.setItem("plantdigest-theme",dark?"dark":"light")}catch(e){}setThemeLabel()}
setThemeLabel();

const DATE=document.body.dataset.date;
let saved=loadSaved();
function recOf(el){const r=JSON.parse(el.dataset.rec);const p=el.querySelector('details p');r.abstract=p?p.textContent:'';r.review=el.dataset.rev==='1';return r}
// one-time move of ticks made before the Saved page existed
try{const old=JSON.parse(localStorage.getItem('plantdigest-'+DATE)||'null');
  if(old){document.querySelectorAll('.item').forEach(el=>{if(old[el.dataset.id]&&!saved[el.dataset.id]){saved[el.dataset.id]=Object.assign(recOf(el),{savedOn:DATE,digest:DATE})}});
    storeSaved(saved);localStorage.removeItem('plantdigest-'+DATE)}}catch(e){}
function refreshCount(){
  const here=[...document.querySelectorAll('.item')].filter(el=>saved[el.dataset.id]).length;
  document.getElementById('nkept').textContent=here;
  const t=document.getElementById('ntotal');if(t)t.textContent=Object.keys(saved).length;
}
document.querySelectorAll('.item').forEach(el=>{
  const cb=el.querySelector('input');const id=el.dataset.id;
  if(saved[id]){cb.checked=true;el.classList.add('kept')}
  cb.addEventListener('change',()=>{
    saved=loadSaved();
    if(cb.checked){saved[id]=Object.assign(recOf(el),{savedOn:new Date().toISOString().slice(0,10),digest:DATE});el.classList.add('kept')}
    else{delete saved[id];el.classList.remove('kept')}
    storeSaved(saved);refreshCount();applyFilter();
  });
});
refreshCount();
function keepItem(el,show){
  const pre=el.dataset.pre==='1',rev=el.dataset.rev==='1';
  switch(show){
    case 'saved':return !!saved[el.dataset.id];
    case 'research':return !pre&&!rev;
    case 'reviews':return rev;
    case 'preprints':return pre;
    case 'nopre':return !pre;
    default:return true;
  }
}
function applyFilter(){
  const q=document.getElementById('q').value.toLowerCase().trim();
  const show=document.getElementById('show').value;
  document.querySelectorAll('section').forEach(sec=>{
    if(q||show!=='all')sec.classList.add('open');
    let n=0;
    sec.querySelectorAll('.item').forEach(el=>{
      const ok=(!q||el.textContent.toLowerCase().includes(q))&&keepItem(el,show);
      el.classList.toggle('hide',!ok); if(ok)n++;
    });
    sec.classList.toggle('hide',n===0);
  });
}
['q','show'].forEach(id=>document.getElementById(id).addEventListener('input',applyFilter));
function savedHere(){return [...document.querySelectorAll('.item')].filter(el=>saved[el.dataset.id]).map(recOf)}
function copyList(){copyRefs(savedHere())}
function ris(){risFor(savedHere(),'digest_'+DATE+'_saved.ris')}
function showMore(b){b.closest('section').classList.add('open')}
</script>
"""


def write_html(path, items, sections_order, info, d_from, d_to, site=False):
    today = dt.date.today().isoformat()
    by_sec = {}
    for it in items:
        by_sec.setdefault(it["section"], []).append(it)
    for lst in by_sec.values():
        lst.sort(key=lambda r: (-r["score"], r["title"]))
    order = [s for s in sections_order if s in by_sec] + \
            [s for s in by_sec if s not in sections_order]

    e = html.escape
    out = [HTML_HEAD.replace("__DATE__", today)]
    out.append('<body data-date="%s">' % today)
    out.append('<header><div class="wrap"><h1>Plant paper digest</h1>')
    out.append('<div class="meta">%d papers, window %s to %s, generated %s%s</div>'
               % (len(items), d_from, d_to, today,
                  ' · <a href="archive.html">earlier digests</a>' if site else ""))
    out.append('<div class="meta"><a href="saved.html">Saved papers (<span id="ntotal">0</span>)</a></div>')
    out.append('<div class="bar"><input id="q" type="search" placeholder="Filter by any word…">'
               '<label class="chk">Show <select id="show">'
               '<option value="all">all papers</option><option value="saved">saved</option>'
               '<option value="research">research papers only</option><option value="reviews">reviews only</option>'
               '<option value="preprints">preprints only</option><option value="nopre">no preprints</option></select></label>'
               '<button class="primary" onclick="copyList()">Copy saved here (<span id="nkept">0</span>)</button>'
               '<button onclick="ris()">Download as RIS (Zotero)</button>'
               '<button id="themebtn" onclick="toggleTheme()">Dark mode</button><span id="flash" class="meta"></span></div>')
    out.append("<nav>")
    for s in order:
        out.append('<a href="#s%d">%s (%d)</a>' % (order.index(s), e(s), len(by_sec[s])))
    out.append("</nav></div></header><main><div class='wrap'>")

    for si, s in enumerate(order):
        out.append('<section id="s%d"><h2>%s <small>%d</small></h2>' % (si, e(s), len(by_sec[s])))
        for rank, it in enumerate(by_sec[s]):
            rec = {k: it[k] for k in ("title", "authors", "journal", "date", "doi", "link", "preprint")}
            rec["review"] = bool(it.get("review"))
            rec_json = json.dumps(rec,
                                  ensure_ascii=False)
            iid = it["doi"] or ("t:" + norm_title(it["title"]))
            badges = ""
            if it["preprint"]:
                badges += '<span class="badge pre">preprint</span>'
            if it.get("review"):
                badges += '<span class="badge rev">review</span>'
            for tg in it["tags"][:3]:
                badges += '<span class="badge">%s</span>' % e(tg.split(":")[0].split(",")[0])
            also = ""
            jn = norm_title(it["journal"])
            others = sorted({a for a in it.get("also", []) if norm_title(a) != jn})
            if others:
                also = ' · also: %s' % e(", ".join(others))
            extra = " more" if rank >= TOP_N else ""
            out.append('<div class="item%s" data-id="%s" data-pre="%d" data-rev="%d" data-rec="%s">'
                       % (extra, e(iid), 1 if it["preprint"] else 0, 1 if it.get("review") else 0, e(rec_json)))
            out.append('<input type="checkbox" title="Keep"><div class="body">')
            out.append('<span class="score">%s</span>' % it["score"])
            out.append('<a class="t" href="%s" target="_blank" rel="noopener">%s</a>'
                       % (e(it["link"]), e(it["title"])))
            out.append('<div class="au">%s</div>' % e(first_last_authors(it["authors"])))
            out.append('<div class="src">%s<i>%s</i> · %s%s</div>'
                       % (badges, e(it["journal"]), e(it["date"]), also))
            if it["terms"]:
                out.append('<div class="terms">matched: %s</div>' % e(", ".join(it["terms"][:12])))
            if it["abstract"]:
                out.append('<details><summary>abstract</summary><p>%s</p></details>' % e(it["abstract"]))
            out.append("</div></div>")
        if len(by_sec[s]) > TOP_N:
            out.append('<button class="morebtn" onclick="showMore(this)">Show %d more in this section</button>'
                       % (len(by_sec[s]) - TOP_N))
        out.append("</section>")

    out.append('<div class="notes"><b>Run report</b><br>%s</div>' % "<br>".join(e(x) for x in info))
    out.append("</div></main>")
    out.append(HTML_SCRIPT)
    out.append("</body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


def write_csv(path, items):
    cols = ["section", "score", "title", "authors", "journal", "date", "preprint",
            "doi", "link", "terms", "source"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for it in sorted(items, key=lambda r: (r["section"], -r["score"])):
            w.writerow([it["section"], it["score"], it["title"], it["authors"], it["journal"],
                        it["date"], "yes" if it["preprint"] else "", it["doi"], it["link"],
                        "; ".join(it["terms"]), it["source"]])


SAVED_BODY = r"""
<body data-date="saved">
<header><div class="wrap"><h1>Saved papers</h1>
<div class="meta"><span id="count">0</span> papers saved in this browser · <a href="index.html">newest digest</a></div>
<div class="bar"><input id="q" type="search" placeholder="Filter by any word…">
<select id="sort"><option value="saved">Newest saved first</option><option value="pub">Newest published first</option><option value="title">Title A to Z</option></select>
<button class="primary" onclick="copyRefs(visible())">Copy all</button>
<button onclick="risFor(visible(),'saved_papers.ris')">Download as RIS (Zotero)</button>
<button onclick="backup()">Backup</button>
<label class="btnlike">Restore<input type="file" id="restore" accept=".json,application/json" hidden></label>
<button id="themebtn" onclick="toggleTheme()">Dark mode</button><span id="flash" class="meta"></span></div>
<div class="meta">Saved papers live in this browser only. Use <b>Backup</b> to keep a copy or to move them to another device, then <b>Restore</b> there.</div>
</div></header>
<main><div class="wrap"><div id="list"></div><p id="empty" class="meta hide">Nothing saved yet. Tick papers in a digest and they appear here.</p></div></main>
<script>""" + HTML_SCRIPT.split("<script>", 1)[1].split("const DATE=", 1)[0] + r"""
let saved=loadSaved();
const esc=t=>String(t||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function entries(){
  const q=document.getElementById('q').value.toLowerCase().trim();
  let l=Object.entries(saved).map(([id,r])=>Object.assign({id},r));
  if(q)l=l.filter(r=>(r.title+' '+r.authors+' '+r.journal+' '+(r.abstract||'')).toLowerCase().includes(q));
  const k=document.getElementById('sort').value;
  l.sort((a,b)=>k==='title'?a.title.localeCompare(b.title):k==='pub'?(b.date||'').localeCompare(a.date||''):(b.savedOn||'').localeCompare(a.savedOn||''));
  return l;
}
function visible(){return entries()}
function render(){
  const l=entries();
  document.getElementById('count').textContent=Object.keys(saved).length;
  document.getElementById('empty').classList.toggle('hide',Object.keys(saved).length>0);
  document.getElementById('list').innerHTML=l.map(r=>`<div class="item kept"><div class="body">
    <a class="t" href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.title)}</a>
    <div class="au">${esc(r.authors)}</div>
    <div class="src">${r.preprint?'<span class="badge pre">preprint</span>':''}${r.review?'<span class="badge rev">review</span>':''}<i>${esc(r.journal)}</i> · ${esc(r.date)} · saved ${esc(r.savedOn)}${r.digest?` from <a href="digest_${esc(r.digest)}.html">${esc(r.digest)} digest</a>`:''}</div>
    ${r.abstract?`<details><summary>abstract</summary><p>${esc(r.abstract)}</p></details>`:''}
    <button class="rm" data-id="${esc(r.id)}">Remove</button></div></div>`).join('');
  document.querySelectorAll('.rm').forEach(b=>b.onclick=()=>{if(confirm('Remove this paper from Saved?')){saved=loadSaved();delete saved[b.dataset.id];storeSaved(saved);render()}});
}
function backup(){downloadFile('saved_papers_'+new Date().toISOString().slice(0,10)+'.json',JSON.stringify(loadSaved(),null,1),'application/json')}
document.getElementById('restore').addEventListener('change',e=>{
  const f=e.target.files[0];if(!f)return;
  f.text().then(t=>{let add;try{add=JSON.parse(t)}catch(err){alert('That file is not a saved-papers backup.');return}
    saved=loadSaved();let n=0;for(const[k,v]of Object.entries(add)){if(!saved[k]&&v&&v.title){saved[k]=v;n++}}
    storeSaved(saved);render();flash('Restored '+n+(n===1?' paper':' papers'));});
});
['q','sort'].forEach(id=>document.getElementById(id).addEventListener('input',render));
render();
</script>
</body></html>
"""


def write_saved_page(out_dir):
    with open(os.path.join(out_dir, "saved.html"), "w", encoding="utf-8") as fh:
        fh.write(HTML_HEAD.replace("Plant paper digest __DATE__", "Saved papers") + SAVED_BODY)


def write_site(out_dir, base, n_papers, d_from, d_to, prof):
    """index.html (= newest digest), archive.html, feed.xml, and a run log."""
    shutil.copyfile(os.path.join(out_dir, base + ".html"), os.path.join(out_dir, "index.html"))
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    runs_path = os.path.join(out_dir, "runs.json")
    try:
        with open(runs_path, encoding="utf-8") as fh:
            runs = json.load(fh)
    except (OSError, ValueError):
        runs = []
    runs = [r for r in runs if r.get("file") != base + ".html"]
    runs.insert(0, {"file": base + ".html", "date": dt.date.today().isoformat(),
                    "papers": n_papers, "from": d_from, "to": d_to})
    with open(runs_path, "w", encoding="utf-8") as fh:
        json.dump(runs, fh, indent=1)

    e = html.escape
    rows = "\n".join('<li><a href="%s">%s</a> · %d papers <span>(%s to %s)</span></li>'
                     % (e(r["file"]), e(r["date"]), r["papers"], e(r["from"]), e(r["to"]))
                     for r in runs)
    with open(os.path.join(out_dir, "archive.html"), "w", encoding="utf-8") as fh:
        fh.write("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Earlier digests</title>
<style>body{font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#1f2421;background:#fbfaf7}
a{color:#2f6b4f}span{color:#68716b;font-size:14px}li{margin:6px 0}</style>
</head><body><h1>Earlier digests</h1><p><a href="index.html">Newest digest</a> · <a href="feed.xml">RSS feed</a></p>
<ul>%s</ul></body></html>""" % rows)

    # Atom feed, so a feed reader (Feedly, Inoreader, Zotero) can notify you of each new digest
    site_url = prof["settings"].get("site_url", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not site_url and "/" in repo:
        owner, name = repo.split("/", 1)
        site_url = ("https://%s.github.io/" % owner.lower()) + ("" if name.lower() == owner.lower() + ".github.io" else name + "/")
    site_url = site_url.rstrip("/") + "/" if site_url else ""
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = "".join("""
  <entry><title>Plant paper digest %s: %d papers</title>
    <link href="%s%s"/><id>%s%s</id><updated>%sT06:00:00Z</updated>
    <summary>%d new papers, window %s to %s</summary></entry>""" % (
        r["date"], r["papers"], e(site_url), e(r["file"]), e(site_url), e(r["file"]), r["date"],
        r["papers"], r["from"], r["to"]) for r in runs[:30])
    with open(os.path.join(out_dir, "feed.xml"), "w", encoding="utf-8") as fh:
        fh.write("""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Plant paper digest</title>
  <link href="%s"/><id>%s</id><updated>%s</updated>%s
</feed>
""" % (e(site_url) or "index.html", e(site_url) or "plant-paper-digest", now, entries))


# ======================================================================
# Main
# ======================================================================

def main():
    ap = argparse.ArgumentParser(description="Weekly plant biology paper digest")
    ap.add_argument("--days", type=int)
    ap.add_argument("--from", dest="d_from")
    ap.add_argument("--to", dest="d_to")
    ap.add_argument("--profile", default=os.path.join(HERE, "profiles.txt"))
    ap.add_argument("--ignore-seen", action="store_true")
    ap.add_argument("--no-crossref", action="store_true")
    ap.add_argument("--no-europepmc", action="store_true")
    ap.add_argument("--no-biorxiv", action="store_true")
    ap.add_argument("--site", metavar="DIR",
                    help="write a small website into DIR (index.html, archive.html, feed.xml); "
                         "used by the GitHub Actions workflow")
    ap.add_argument("--fixture", help=argparse.SUPPRESS)  # offline testing
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    prof = parse_profiles(args.profile)
    days = args.days or int(prof["settings"].get("days", 8) or 8)
    min_score = float(prof["settings"].get("min_score", 2) or 2)
    d_to = args.d_to or dt.date.today().isoformat()
    d_from = args.d_from or (dt.date.fromisoformat(d_to) - dt.timedelta(days=days)).isoformat()

    info = []

    def log(msg):
        print(msg)
        info.append(msg.strip())

    log("Window %s to %s" % (d_from, d_to))
    records = []
    if args.fixture:
        with open(args.fixture, encoding="utf-8") as fh:
            records = json.load(fh)
        log("  fixture: %d records" % len(records))
    else:
        # If a source failed or the computer was off, start that source from
        # the end of its last successful run (max 60 days back), so nothing
        # falls through the gap between runs.
        state = {} if args.d_from else load_state()
        floor = (dt.date.fromisoformat(d_to) - dt.timedelta(days=60)).isoformat()

        def start_for(name):
            last = state.get(name)
            if last:
                s = (dt.date.fromisoformat(last) - dt.timedelta(days=1)).isoformat()
                return max(min(d_from, s), floor)
            return d_from

        def run(name, fn):
            s = start_for(name)
            if s != d_from:
                log("  %s: catching up from %s (last successful run %s)" % (name, s, state[name]))
            try:
                res = fn(prof, s, d_to, log)
                state[name] = d_to
                return res
            except Exception as e:
                log("  %s FAILED: %s" % (name, e))
                return None

        for attempt in (1, 2):
            if not args.no_europepmc:
                records += run("europepmc", fetch_europepmc) or []
            if not args.no_biorxiv:
                records += run("biorxiv", fetch_biorxiv) or []
            if not args.no_crossref:
                res = run("crossref", fetch_crossref)
                if res:
                    records += res[0]
                    if res[1]:
                        log("  Crossref journals with 0 papers this window: " + "; ".join(res[1]))
            if records or attempt == 2:
                break
            log("  Nothing could be fetched (no internet yet?). Waiting 2 minutes and trying again.")
            time.sleep(120)
        if not args.d_from:
            save_state(state)
        if not records:
            log("Nothing could be fetched. The previous digest is left unchanged; "
                "the next run will catch up on this window.")
            sys.exit(1)

    records = dedupe(records)
    log("After merging duplicates: %d unique papers" % len(records))

    seen = {} if args.ignore_seen else load_seen()
    max_age = int(prof["settings"].get("max_age_days", 365) or 365)
    oldest = (dt.date.fromisoformat(d_to) - dt.timedelta(days=max_age)).isoformat()
    kept, n_gate, n_low, n_seen, n_old = [], 0, 0, 0, 0
    for r in records:
        if r["date"] and r["date"][:10] < oldest[:len(r["date"][:10])]:
            n_old += 1
            continue
        s = score_record(r, prof)
        if s is None:
            n_gate += 1
            continue
        if s["score"] < min_score:
            n_low += 1
            continue
        if any(k in seen for k in seen_keys(r)):
            n_seen += 1
            continue
        r.update(s)
        kept.append(r)
    log("Older than %d days: %d | not plant papers: %d | below min_score %.1f: %d | "
        "shown in an earlier digest: %d | in this digest: %d"
        % (max_age, n_old, n_gate, min_score, n_low, n_seen, len(kept)))

    out_dir = os.path.abspath(args.site) if args.site else OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.date.today().isoformat()
    base, n = "digest_%s" % stamp, 2
    while os.path.exists(os.path.join(out_dir, base + ".html")) and not args.ignore_seen:
        base, n = "digest_%s_%d" % (stamp, n), n + 1
    html_path = os.path.join(out_dir, base + ".html")
    csv_path = os.path.join(out_dir, base + ".csv")
    sections_order = [s["name"] for s in prof["sections"]] + ["Other plant biology"]
    write_html(html_path, kept, sections_order, info, d_from, d_to, site=bool(args.site))
    write_csv(csv_path, kept)
    write_saved_page(out_dir)
    if not args.site:
        shutil.copyfile(html_path, os.path.join(out_dir, "latest.html"))
    else:
        write_site(out_dir, base, len(kept), d_from, d_to, prof)

    if not args.fixture and not args.ignore_seen:
        for r in kept:
            for k in seen_keys(r):
                seen[k] = stamp
        save_seen(seen)

    print("Wrote %s" % html_path)


if __name__ == "__main__":
    main()
