# WBS Alumni Impact Engine (free-tier MVP)

A resumable command-line pipeline that finds Warwick Business School alumni who are likely to count for QS Alumni Impact, proves the WBS link with quoted public sources, and writes the result into the existing *Alumni candidate output template*.

It replaces the conversational agent approach. Nothing lives in a chat context: every person, page, claim and decision is stored in one SQLite file, so you can stop at any point and carry on later.

## How it works

1. **Ingest** finite lists of influential people from Wikidata, mirroring QS's list families: national cabinets and heads of state/government, heads of intergovernmental organisations and central banks, leaders and board members of listed companies, and university leaders. Plus alumni-first lists: everyone Wikidata records as educated at Warwick, your own CSV lists, and the previous NebulaOne output.
2. **Screen** every person cheaply for a Warwick link (Wikidata education data and the person's Wikipedia article). No AI calls. Most people stop here.
3. **Fetch** 3-5 pages for the survivors: the references their Wikipedia article cites, plus free-tier web search. robots.txt is respected; LinkedIn and data brokers are never fetched.
4. **Extract** claims. Rules pick out every sentence naming Warwick; the free Gemini model adds structured education, role and award claims. Each claim must quote the page word for word, and code checks the quote is really there. Invented quotes are discarded.
5. **Score** with fixed rules: WBS identity grade, QS list fit, attribution (can an outsider link them to Warwick?), proposed influence level, and priority.
6. **Export** to the WBS template with real source URLs, plus Evidence, Coverage, Needs-a-human, Broader impact and Warwick-not-WBS sheets.

## Setup (about 10 minutes)

Needs Python 3.10 or newer.

```bash
cd wbs-alumni-engine
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

Edit `.env`:

| Setting | What to put | Cost |
| --- | --- | --- |
| `CONTACT_EMAIL` | Your Warwick email. Wikimedia requires a contact address from automated clients. | Free |
| `GEMINI_API_KEY` | A key from Google AI Studio (aistudio.google.com/apikey). Check the current free-tier model ID and limits there and set `GEMINI_MODEL`, `LLM_RPM`, `LLM_DAILY_CAP` below them. | Free tier |
| `TAVILY_API_KEY` | Optional. Free plan, no card. Leave `SEARCH_MONTHLY_CAP` at 900 to stay inside it. | Free tier |

Without a Gemini key, set `LLM_PROVIDER=none`: the pipeline still runs with rule-based extraction. Without a search key, it uses Wikipedia references only.

Then check everything:

```bash
python -m engine doctor --llm
```

This confirms the Wikidata class IDs, finds the University of Warwick and WBS items, and makes one test model call.

## Running the demo

```bash
# 1. Lists (cached; safe to re-run)
python -m engine ingest seed                    # people Wikidata says studied at Warwick
python -m engine ingest political               # cabinets of ~50 starter countries (or --countries NG,GH or --all)
python -m engine ingest igo
python -m engine ingest central_bank
python -m engine ingest university
python -m engine ingest business                # largest; run after the others
python -m engine ingest-previous "path/to/2026-08-19 QS Alumni Impact Data - academic.xlsx"

# 2. Pipeline (each step only processes what is still pending)
python -m engine screen
python -m engine fetch
python -m engine extract                        # stops cleanly at the daily free-tier cap; re-run tomorrow
python -m engine export                         # writes data/exports/WBS_Alumni_Impact_Candidates_<date>.xlsx

python -m engine status                         # progress and free-tier usage at any time
```

`python -m engine run` does screen, fetch, extract and export in one go. Add `--limit 200` to any step to try it on a small batch first.

**Before showing results:** check that known alumni appear without being named anywhere in the setup. Dr Bosun Tijani should surface from the Nigeria cabinet list (political universe, `NG`) with his WBS link found from public pages. If he does not, look at his row in the database or the Coverage sheet to see which step missed him, before trusting the rest.

### Your own lists

Any official list you copy by hand (WBS alumni stories, honorary graduate pages, award rolls, public appointment lists) can go in as a CSV with a header row:

```text
name,role,organisation,country,source_url
```

```bash
python -m engine ingest-csv wbs_alumni_stories.csv --universe wbs_alumni_stories --family alumni_first
```

### Checking quality before anything is shared

```bash
python -m engine regrade --before "path/to/the workbook people saw.xlsx"   # before/after table + tests
python -m engine test-report                                                 # tests only
python -m engine sample --n 20                                               # 20 random Confirmed/Probable rows to check by hand
python -m engine explain "Wendy Alexander" "Bernardo Hees"                  # every quote and decision behind a name
python -m engine merge 1270 2683                                         # merge a near-duplicate record into another
```

The fixed test set is `testset/known_people.csv`: known alumni who must be Confirmed, known non-alumni who must never be Confirmed or Probable, and the cases found in review. Add a row whenever WBS confirms or rules out a person. `export` runs the whole test set first and refuses to write a workbook if any test fails (a WARN, where a detail such as a staff flag cannot be shown from evidence, is reported but does not block); the test report is saved in `data/exports/` either way.

### Language model and data governance

All model calls go through one interface, chosen in `.env` (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`), so the engine can switch to whichever model the university's IDG review approves without code changes. Leave `LLM_PROVIDER=none` until then: the rules alone extract and grade the evidence. Every call is logged with its provider, model and run, and summarised in the workbook's Engine notes. Only public names, public role context and public page text are ever sent to a model.

### Re-grading after a rules change

```bash
python -m engine regrade
```

Re-checks every saved quote with the current rules (who the quote is about, study versus staff role, department), re-scores everyone, prints a before and after table, and runs the acceptance tests. It uses only saved page text: no network, no API quota. `export` refuses to write a workbook in which a Confirmed or Probable row has no valid study quote naming WBS.

### Reviewer decisions

Edit *Suggested submission status* and *Reviewer comments* in the exported workbook, then:

```bash
python -m engine import-decisions data/exports/WBS_Alumni_Impact_Candidates_<date>.xlsx
```

Later runs keep those decisions.

## Reading the output

| Engine priority | Meaning | Sheet |
| --- | --- | --- |
| P1_COUNT | Proven WBS alumnus, QS-type role in the last 10 years, and a non-Warwick source names Warwick | Candidate Register |
| P1_FIX | Same, but only Warwick's own pages name the link, so QS probably cannot see it | Candidate Register |
| P2 | Proven alumnus in a QS-type role without dates, or a university leader | Candidate Register |
| P3 | QS-type role, but evidence names Warwick without the department | Candidate Register |
| BROADER | Proven alumnus outside QS's list families | Broader impact |
| LEAD | Weak evidence | Leads (weak evidence) |
| HANDOFF | Warwick graduate from another department | Warwick not WBS |

Identity grades: **Confirmed** needs WBS (or a Warwick MBA) named on an official Warwick/WBS page, or on two independent credible sources. **Probable** is one credible source. Wikipedia and Wikidata alone never confirm anyone. Honorary degrees are flagged, not counted.

The *Engine notes* sheet in every export explains these rules for readers.

## Free-tier limits and what they mean

- **Wikidata and Wikipedia** are free and need no key. Screening tens of thousands of people takes hours, so run it overnight.
- **Gemini free tier** limits requests per minute and per day. The engine paces itself and stops at `LLM_DAILY_CAP`; re-run `extract` the next day to continue. Only public web text is sent. Check Google's current terms for how free-tier inputs are used.
- **Tavily free tier** is about 1,000 searches a month. The engine spends them only on shortlisted people and stops at `SEARCH_MONTHLY_CAP`.
- With a paid budget, the same code scales up by raising these caps or pointing the LLM wrapper (`engine/llm.py`) at another model.

## Known limits of the MVP

- Wikidata role data can be stale or incomplete, so every role carries a "check it is current" note.
- Wikidata's coverage of company boards and smaller countries is uneven. The Coverage sheet shows what each list produced.
- Alumni whose public bios never mention their education cannot be found from public sources.
- The WBS programme list in `engine/warwick.py` is a draft. Ask WBS for the full historic list before the real run.
- There is no automatic adversarial check yet. Reviewers check P1 rows against the quoted sources.

## Tests

```bash
pytest
```

The tests run the whole pipeline against an offline fake network (invented people), including a check that a made-up model quote is discarded.

## Project layout

```text
engine/
  __main__.py      command line
  config.py db.py http.py           settings, SQLite, polite HTTP with caching
  wikidata.py wikipedia.py search.py llm.py pages.py
  warwick.py       Warwick/WBS rules and the department check
  sources.py       source tiers and blocked sites
  evidence.py      sources, claims, quote verification
  stages/          ingest, screen, fetch, extract, score, export, decisions
templates/         the WBS candidate output template
tests/
```
