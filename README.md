# Notion → Linear migration

Two scripts that move a Notion issue export into Linear and check that it
arrived intact. They clean and transform the file, and validate the result.
They never talk to Linear — the import itself is run separately with
`npx @linear/import`.

Python 3, standard library only. Nothing to install.

```
python3 transform.py     # pick the Notion export  → three CSVs + a run log
python3 validate.py      # pick the Linear export, then the file(s) you imported
```

Both prompt you to choose from the CSVs in the working directory and `data/`.

---

## Before you import

Create these five workflow states on the target team first, spelled exactly
like this:

| Status | Type |
| --- | --- |
| Backlog | backlog |
| Needs Review | unstarted |
| In Progress | started |
| Shipped | completed |
| Won't Do | canceled |

This step is not optional, and getting it wrong fails silently.

The importer matches states case-insensitively by name and creates any it
cannot find. It can only ever create three types — backlog, started and
completed — and it chooses between them by looking at the issue's transition
timestamps. This output deliberately contains none (see *Dates*, below), so
every state the importer has to invent is created as backlog. If `Needs
Review` and `Won't Do` do not already exist, those issues land in the backlog
and nothing reports an error.

`validate.py` cannot see the Linear side, so it cannot verify this for you. It
prints the list at the end of every run instead.

---

## What the transform does

The importer reads only these columns, matched exactly and case-sensitively:
`Title`, `Description`, `Priority`, `Status`, `Assignee`, `Labels`,
`Estimate`, `Created`, `Started`, `Completed`, `Archived`. Everything else in
a CSV is inert. Each rule below exists because of something specific the
importer does — usually something it does quietly.

| | Rule | Why |
| --- | --- | --- |
| R1 | Drop rows with no title | The importer calls `.replace()` on every title without a guard. A blank one throws part-way through a run that has already created issues, and nothing is rolled back. |
| R2 | Unwrap newline-wrapped titles | Notion wraps each title in newlines; Linear keeps the whitespace and every issue renders with a blank first line. |
| R3 | Empty whitespace-only descriptions | A "blank" Notion description is the string `"\n\n"`, not an empty cell. |
| R4 | `Critical` → `Urgent` | The priority lookup is a literal dict with a `\|\| 0` fallback. `Critical` is not one of its keys, so those rows would arrive as *No priority* with no warning. |
| R5 | Size → Estimate (`XS=1 S=2 M=3 L=5 XL=8`) | Estimates are parsed with `parseInt`; anything that comes back `NaN` is dropped, so a t-shirt size silently loses the estimate. |
| R6 | Build labels | The cell is split on the literal two-character string `", "`, and `/` separates a group from its label. `Platform` and `Source` become groups; free-text tags stay flat. |
| R7 | Append the image to the description | Markdown images in the body are downloaded and re-uploaded to Linear's CDN. That is the only route by which Notion's `Image` column can survive. |
| R8 | Append a reporter footer | Assignees resolve only against existing workspace users, so no Notion reporter can match. The attribution is preserved in the body instead. |
| R9 | Backtick-fence malformed emails | Some addresses contain spaces or doubled dots. Bare, they render as broken markdown; as code, the original string is preserved exactly and visibly needs fixing. |
| R10 | `November 5, 2024` → `2024-11-05` | The raw string is handed to JavaScript's `Date`, whose parsing of non-ISO formats is implementation-defined. An explicit month map avoids that and any locale dependency. |
| R11 | Leave `Assignee` and `Archived` empty | See R8. Nothing is auto-archived. |

Nothing is corrected on the customer's behalf. Malformed addresses are marked,
not fixed; titles are never edited.

### Dates

`Created` is the only timestamp in the output. `Started` and `Completed` are
absent from the header entirely, because Notion recorded no transition
timestamps and a date that looks migrated but was actually computed is worse
than an absent one. The importer treats an absent column and an empty one
identically, so leaving them out changes nothing except making the intent
visible. The cost is the pre-flight step above, which is why it is not
optional.

---

## The three files

| File | Import it? |
| --- | --- |
| `data/linear_import.csv` | Yes |
| `data/linear_import_flagged.csv` | Only after a human has read it |
| `data/duplicates_review.csv` | **No** — every row in it is a copy |

Row counts for any given run are in `out/transform_log.md`, along with each
duplicate group, every flagged row and its reason, and the emails that were
fenced.

### Why junk is withheld rather than marked

Four mechanisms were available for a row the script suspects is junk.

Trailing `Flag` columns are invisible to the importer, so they document a row
without affecting it — but they vanish the moment the row is imported, and
nothing carries into Linear. A `Review/Tier 1` label does carry through and is
filterable, at the cost of putting migration artifacts into the customer's
label namespace for them to clean up afterwards. Setting `Archived` makes the
importer skip the row, which preserves it in the file but requires a second
pass to bring any of it in. A title prefix like `[REVIEW]` is the most visible
and the most destructive, because it edits customer data.

What this repo does instead: **suspect rows are simply not in the import
file.** They are written to `linear_import_flagged.csv` with the reason
attached, in the same importable shape, and a person decides. This keeps the
customer's Linear workspace clean — no review labels to delete later, no
prefixes to strip, nothing imported that someone then has to find and remove —
while nothing is deleted and no data is edited. The flagged file is importable
as-is once reviewed; the `Flag Tier` and `Flag Reason` columns are trailing
columns the importer never reads, so they can stay in the file.

Tier stays legible in the CSV itself rather than depending on which file a row
is in, because the distinction matters more than the routing does.

### Why duplicates are kept

Near-duplicate rows go into the import file like anything else, carrying a
`Duplicate/Group NN` label. Merging them is a product decision that needs
someone who knows which copy is authoritative, and that decision is much
easier to make in Linear — filter to one group, see the copies side by side —
than in a spreadsheet. Using a label group rather than a flat label is what
makes that filter two clicks.

Titles are clustered by an exact match after case-folding, collapsing
whitespace and stripping trailing sentence punctuation. Every cluster found in
this export matches exactly under that normalisation, so there is no fuzzy
scoring and no similarity threshold to defend. Groups are numbered by first
appearance, so the numbering is stable across runs.

A row that is both a duplicate and junk is withheld, and keeps its duplicate
label so its group stays intact if it is imported later.
`duplicates_review.csv` lists every member of every group with a `Routed To`
column naming the file it actually went to. It exists to be read, not
imported — importing it as well would create each of those issues twice.

### The three junk tiers

**Tier 1 is deterministic.** An exact placeholder title, a description that is
still an unfilled issue template, or a title too short to identify anything.
Mechanical, and safe to call junk.

**Tier 2 is a heuristic, and it will catch real work.** A short, vague title,
or a single all-lowercase token. `Update API` and `Client meeting` are
plausibly genuine requests. These are reported as needing review, never as
junk, and each reason says so.

**Tier 3 is not detectable and is not attempted.** Whether a terse but
plausible title is a real request or somebody's scratch note needs the person
who wrote it. Detecting typos would need a spellchecker, and a spellchecker
fires on product nouns as readily as on genuine mistakes. Leaving this out is
deliberate — the limit is part of the result, and it is stated in the run log
rather than papered over.

---

## What validate.py checks

It takes **two** inputs: the CSV you exported from Linear, and the file(s) you
actually imported. The imported file is the benchmark. That is the point of
the design — the script holds no expected row counts and no notion of a
correct migration in the abstract. It compares what came back against what was
sent, so a failure names the issues involved.

Migrated issues are picked out of the export by the footer marker, so a
workspace that already contains issues does not produce spurious extras.

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
| V9 | Labels match what was sent |
| V10 | Label groups survived |
| V11 | Images were re-hosted by Linear, not left pointing at the original host |
| V12 | The migration footer is intact |
| V13 | `Created` holds the original Notion dates, not the import time |
| V14 | `Assignee` is unset, as sent |

Exits 0 when everything passes, 1 otherwise. A column missing from the export
downgrades its check to a skip rather than failing it.

V8 is the one to watch. If the pre-flight step was skipped, `Needs Review` and
`Won't Do` will have collapsed into the backlog, and this is the check that
says so.

### What it cannot tell you

It reads a CSV, so it cannot confirm the five workflow states were created
with the *right type* — only that issues came back carrying those names. It
cannot verify that description markdown renders correctly. It cannot recover
what a dropped row contained. And it cannot distinguish a deliberate decision
not to import the flagged file from a failure to import it, which is why it
asks you which files you imported rather than guessing.
