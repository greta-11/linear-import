# Notion → Linear migration

Two scripts that move a Notion issue export into Linear and check it arrived
intact. They never talk to Linear; the import itself is run separately with
`npx @linear/import`.

Python 3, standard library only.

```
python3 transform.py     # pick the Notion export  → four CSVs + a run log
python3 validate.py      # pick the Linear export, then the file(s) you imported
```

Both prompt you to choose from the CSVs in the working directory and `data/`.

## Before you import

Create these five workflow states on the target team first, spelled exactly:

| Status | Type |
| --- | --- |
| Backlog | backlog |
| Needs Review | unstarted |
| In Progress | started |
| Shipped | completed |
| Won't Do | canceled |

The importer creates any state it cannot find, but only as backlog, started or
completed — and it picks between them using transition timestamps this output
omits. So every state it invents becomes backlog. Miss one and those issues land
in the backlog with no error. `validate.py` detects most of this afterwards (V9).

## What the transform does

The importer reads only `Title`, `Description`, `Priority`, `Status`,
`Assignee`, `Labels`, `Estimate`, `Created`, `Started`, `Completed` and
`Archived`, matched case-sensitively. Every other column is inert. Each rule
below exists because of something the importer does quietly.

| | Rule | Why |
| --- | --- | --- |
| R1 | Drop rows with no title | The importer calls `.replace()` on every title unguarded. A blank one throws mid-run, after issues are already created, with no rollback. |
| R2 | Unwrap newline-wrapped titles | Notion wraps titles in newlines; Linear keeps them and every issue renders with a blank first line. |
| R3 | Empty whitespace-only descriptions | A "blank" Notion description is `"\n\n"`, not an empty cell. |
| R4 | `Critical` → `Urgent` | The priority lookup is a literal dict with a `\|\| 0` fallback. `Critical` is not a key, so those rows would arrive as *No priority*, silently. |
| R5 | Size → Estimate (`XS=1 S=2 M=3 L=5 XL=8`) | Estimates are parsed with `parseInt`; `NaN` is dropped, so a t-shirt size loses the estimate. |
| R6 | Build labels | The cell splits on the literal `", "`, and `/` separates group from label. `Platform` and `Source` become groups; tags stay flat. |
| R7 | Append the image to the description | Markdown images in the body are re-uploaded to Linear's CDN. That is the only route by which Notion's `Image` column survives. |
| R8 | Append a reporter footer | Assignees resolve only against existing workspace users, so no Notion reporter can match. Attribution is preserved in the body instead. |
| R9 | Backtick-fence malformed emails | Bare, they render as broken markdown. As code, the original string is preserved exactly and visibly needs fixing. |
| R10 | `November 5, 2024` → `2024-11-05` | The raw string is handed to JavaScript's `Date`, whose non-ISO parsing is implementation-defined. |
| R11 | Leave `Assignee` and `Archived` empty | See R8. Nothing is auto-archived. |
| R12–R14 | Route each row to one of three files | See below. |

Nothing is corrected on the customer's behalf. Malformed addresses are marked,
not fixed; titles are never edited.

### Dates

`Created` is the only timestamp in the output. `Started` and `Completed` are
absent from the header because Notion recorded no transition timestamps, and a
date that looks migrated but was computed is worse than an absent one. The cost
is the pre-flight step above, which is why it is not optional.

## The four files

| File | Import it? |
| --- | --- |
| `data/linear_import.csv` | Yes |
| `data/linear_import_flagged.csv` | After a human reads it |
| `data/missing_key_values.csv` | After the blank fields are filled in |
| `data/duplicates_review.csv` | **No** — every row is a copy |

Row counts for a given run are in `out/transform_log.md`, with each duplicate
group, every flagged row and its reason, and the fenced emails.

Routing precedence, which decides every row: **junk** → flagged file;
**duplicate** → import file with its label; **missing a key value** → missing
file; everything else → import file. A duplicate with a blank `Status` therefore
still ships in the import file, deliberately — it already carries a label
demanding attention, and splitting a group across files would defeat that label.

### Why junk is withheld rather than marked

Four mechanisms were available. Trailing `Flag` columns document a row but
vanish on import. A `Review/Tier 1` label carries through, at the cost of
migration artifacts in the customer's label namespace. `Archived` makes the
importer skip the row but needs a second pass. A `[REVIEW]` title prefix edits
customer data.

Instead, suspect rows are **not in the import file**. They go to
`linear_import_flagged.csv` with the reason attached, in the same importable
shape, and a person decides. The workspace stays clean, nothing is deleted, and
no data is edited. `Flag Tier` and `Flag Reason` are trailing columns the
importer never reads, so tier stays legible in the CSV.

### The junk tiers

| Tier | Signals | Treatment |
| --- | --- | --- |
| 1, deterministic | Exact placeholder title; description is an unfilled issue template; title too short to identify anything | Safe to call junk |
| 2, heuristic | Short, vague title; single all-lowercase token | Reported as *needs review*, never junk — `Update API` and `Client meeting` are plausibly real, and each reason says so |
| 3, not detectable | Whether a terse but plausible title is real work or a scratch note | Not attempted; a spellchecker would fire on product nouns as readily as on typos. Stated as a limit in the log |

### Why duplicates are kept

Merging needs someone who knows which copy is authoritative, and that is easier
in Linear — filter to one group, see the copies side by side — than in a
spreadsheet. So near-duplicates import normally with a `Duplicate/Group NN`
label; the group is what makes the filter two clicks.

Titles are clustered by exact match after case-folding, collapsing whitespace
and stripping trailing sentence punctuation — no fuzzy scoring, no threshold.
Groups are numbered by first appearance, so numbering is stable across runs. A
row that is both duplicate and junk is withheld but keeps its label.
`duplicates_review.csv` lists every member with a `Routed To` column; importing
it too would create each issue twice.

### Missing key values

`Status`, `Priority` and `Size` decide how an issue behaves. A blank one does
not stop the import, it makes it quietly wrong: no Status lands the issue in the
team's default state, no Priority becomes *No priority*, no Size leaves it with
no estimate. Those rows are withheld to `missing_key_values.csv` with a
`Missing Fields` column naming what to fill in.

Only blankness is checked. The value mappings are settled for this export, so an
unrecognised value is not a case this script handles.

## What validate.py checks

Two inputs: the CSV you exported from Linear, and the file(s) you actually
imported. The imported file is the benchmark — the script holds no expected row
counts and no abstract notion of a correct migration, so a failure names the
issues involved. Migrated issues are picked out by the footer marker, so a
workspace that already contains issues produces no spurious extras.

| | Check |
| --- | --- |
| V1 | Export columns resolve (by alias — names vary between workspaces) |
| V2 | Every imported row is present in the export |
| V3 | No unexpected extra migrated issues |
| V4 | Priority matches what was sent |
| V5 | Nothing landed as *No priority* |
| V6 | Estimate matches what was sent |
| V7 | Status matches what was sent |
| V8 | Every workflow state that was sent exists in the export |
| V9 | Each workflow state has the **type** it was supposed to have |
| V10 | Labels match what was sent |
| V11 | Label groups survived, when the export carries them |
| V12 | Images were re-hosted by Linear, not left pointing at the original host |
| V13 | The migration footer is intact |
| V14 | `Created` holds the original Notion dates, not the import time |
| V15 | `Assignee` is unset, as sent |

Exits 0 on all-pass, 1 otherwise. A column missing from the export downgrades
its check to a skip.

V8 only asks whether a state *name* came back, which a wrongly-typed state
passes. V9 catches it: no export column names a state's type, but Linear stamps
one timestamp at creation chosen by that type — `Started`, `Completed` or
`Canceled`. An `In Progress` with no `Started` stamp was auto-created as backlog.

### What it cannot tell you

- Backlog vs unstarted. Both stamp no timestamp, so `Backlog` and `Needs Review`
  are indistinguishable in an export. V9 reports that pair as undecidable rather
  than passing it; check by hand.
- Label grouping. Linear's export renders `Labels` as child names only —
  `Platform/Backend` comes back as `Backend` — so group structure is invisible
  even when correct. V10 compares on child names when prefixes are absent; V11
  skips. Check groups in Linear's label settings.
- Whether description markdown renders correctly.
- What a dropped row contained.
- Whether a missing flagged issue was a decision not to import that file or a
  failure to import it — which is why it asks which files you imported.

Two export quirks are handled: text fields beginning `@`, `>` or `---` come back
with a single quote prefixed, which matters because a footer-only description
starts with `---`; and image URLs already on Linear's CDN in the sent file are
not treated as source hosts, so V12 cannot false-fail on re-validation.
