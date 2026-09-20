# Transform run log

- Run: 2026-09-20T21:11:56
- Input: `data/Rideshare_Issue_Tracker.csv`
- Input SHA-256: `2743f02cd5bfa758fc8eb316c8c750f8ee87f9a081771d34846bd53b81ef8b5f`
- Rows read: 200
- Rows written: 199
- Rows dropped at read: 1 (line(s) 201)
- Rows discarded after read: 0

## Files written

| File | Rows | Import? |
| --- | --- | --- |
| `data/linear_import.csv` | 169 | Yes |
| `data/linear_import_flagged.csv` | 30 | Only after a human reviews it |
| `data/missing_key_values.csv` | 0 | Once the blank fields are filled in |
| `data/duplicates_review.csv` | 34 | **No** — these rows are copies |

`duplicates_review.csv` is a review sheet. Every row in it has already been written to one of the other two files, named in its `Routed To` column. Importing it as well would create each of those issues a second time.

## Rows missing key values

`Status, Priority, Size` decide how an issue behaves in Linear. A blank one does not stop the import, it makes it quietly wrong: no Status lands the issue in the team's default state, no Priority becomes No priority, and no Size leaves it with no estimate. Rows with a blank are withheld from the import file and written to `missing_key_values.csv` instead, with a `Missing Fields` column naming what to fill in.

Only blankness is checked. The value mappings are settled for this export, so an unrecognised value is not a case this script handles.

Junk and duplicates are routed first, so a row that is already flagged or already in a duplicate group keeps that routing even when a key field is blank — both already demand human attention, and splitting a duplicate group across files would defeat the label.

**No rows were withheld.** Every row has a value for `Status`, `Priority`, `Size`, so `missing_key_values.csv` was written with a header and no data rows.

## Rules applied

| Rule | Rows | Detail |
| --- | --- | --- |
| R1 Drop blank rows | 1 | no Title; crashes the importer mid-run |
| R2 Unwrap title newlines | 199 | "\nTitle\n" -> "Title" |
| R3 Clean descriptions | 111 | whitespace-only bodies -> empty |
| R4 Priority Critical->Urgent | 56 | unmapped values fall back to No priority |
| R5 Size -> Estimate | 199 | XS=1 S=2 M=3 L=5 XL=8 |
| R6 Build labels | 199 | Platform/ Source/ Duplicate/ groups + flat tags |
| R7 Embed image markdown | 87 | re-uploaded to Linear's CDN on import |
| R8 Append footer | 199 | reporter, email and original Notion date |
| R9 Fence malformed email | 8 | renders as code, not broken markdown |
| R10 Created -> ISO | 199 | "November 5, 2024" -> 2024-11-05 |
| R11 Blank Assignee/Archived | 199 | Notion reporters are not workspace users |
| R12 Classify rows | 199 | junk withheld; duplicates labelled and kept |
| R13 Write flag columns | 30 | trailing columns the importer never reads |
| R14 Withhold missing values | 0 | blank Status, Priority or Size |

Labels created: **35** across 3 groups (`Platform`, `Source`, `Duplicate`, plus flat tags).

## Duplicate groups

34 rows across 13 groups. Titles are clustered by an exact match after case-folding, collapsing whitespace and stripping trailing sentence punctuation — no fuzzy scoring, so the grouping is reproducible. Groups are numbered by first appearance in the source file.

Every member carries a `Duplicate/Group NN` label, including members withheld as junk. In Linear the customer filters to one group and sees the copies side by side, which is what makes the merge-or-keep call quick.

| Group | Rows | Source lines | Title |
| --- | --- | --- | --- |
| Group 01 | 2 | 5, 40 | Implement 2025 brand guidelines |
| Group 02 | 3 | 11, 38, 181 | Testing Rider App Keyboard functionality |
| Group 03 | 3 | 48, 64, 69 | As a rider in Mexico, I want to pay with OXXO Pay or SPEI... |
| Group 04 | 3 | 49, 65, 68 | As a rider, I want to see only payment methods available ... |
| Group 05 | 3 | 50, 66, 67 | As a rider in Spain, I want to use Bizum, so I can pay di... |
| Group 06 | 2 | 55, 98 | Live ride view drops off after app backgrounding |
| Group 07 | 2 | 83, 144 | Update brand assets |
| Group 08 | 2 | 96, 125 | Webhook retries failing silently |
| Group 09 | 2 | 122, 123 | Ask from Gino Fordiani |
| Group 10 | 2 | 134, 145 | xyz |
| Group 11 | 2 | 146, 196 | test |
| Group 12 | 6 | 148, 149, 150, 152, 154, 155 | Share sales enablement with GTM teams |
| Group 13 | 2 | 183, 184 | Investigate significant latency issues |

4 of these rows also tripped junk detection and were withheld from `linear_import.csv`. They keep their duplicate label in `linear_import_flagged.csv`, so the group stays intact if they are later imported.

## Flagged rows

Nothing is deleted and nothing is auto-archived. Flagged rows are withheld from the import file and written to their own file with the reason attached, so the decision stays with the customer and is auditable.

### Tier 1 — deterministic (12 rows)

Mechanical signals. Safe to call junk.

| Source line | Title | Reason |
| --- | --- | --- |
| 97 | `afserve` | `Description is an unfilled issue template (contains '[Brief title]')` |
| 114 | `Captcha` | `Description is an unfilled issue template (contains '<Issue description>')` |
| 134 | `xyz` | `Placeholder title ('xyz') -- not a real issue` |
| 137 | `abc` | `Placeholder title ('abc') -- not a real issue` |
| 145 | `xyz` | `Placeholder title ('xyz') -- not a real issue` |
| 146 | `test` | `Placeholder title ('test') -- not a real issue` |
| 157 | `Test issue` | `Placeholder title ('Test issue') -- not a real issue` |
| 158 | `**Vulnerability Name:** \[Brief title\] **Date Identified:** \[MM/DD/YYYY\] **Severity:** \[Cri...` | `Title is an unfilled issue template (contains '[Brief title]')` |
| 160 | `Add logo to marketing page` | `Description is an unfilled issue template (contains '<Issue description>')` |
| 179 | `DDoS attack` | `Description is an unfilled issue template (contains '[Brief title]')` |
| 196 | `test` | `Placeholder title ('test') -- not a real issue` |
| 200 | `s` | `Title is 1 character(s) long -- identifies nothing` |

### Tier 2 — heuristic (18 rows)

Flagged as *review*, never as junk. These rules will catch real work: a short title is not evidence of a bad issue, only of a terse one.

| Source line | Title | Reason |
| --- | --- | --- |
| 9 | `Sheila review` | `Short, vague title (13 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 14 | `requiremetn 1` | `Short, vague title (13 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 42 | `Thign 1` | `Short, vague title (7 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 58 | `RELEASE 5` | `Short, vague title (9 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 74 | `Mockup` | `Short, vague title (6 chars, 1 word(s)) -- heuristic; titles like this are often real work` |
| 99 | `afsdnl;adsjnkads` | `Single all-lowercase token ('afsdnl;adsjnkads') with no spaces -- reads as a stub or keyboard mash, but may be a real shorthand` |
| 109 | `Update API` | `Short, vague title (10 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 111 | `Write spec` | `Short, vague title (10 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 165 | `dofanboben` | `Single all-lowercase token ('dofanboben') with no spaces -- reads as a stub or keyboard mash, but may be a real shorthand` |
| 169 | `Project audit` | `Short, vague title (13 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 173 | `Data Analysis` | `Short, vague title (13 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 175 | `Coding Agents` | `Short, vague title (13 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 176 | `Client meeting` | `Short, vague title (14 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 182 | `fdsaabklbfsdabjdsfa` | `Single all-lowercase token ('fdsaabklbfsdabjdsfa') with no spaces -- reads as a stub or keyboard mash, but may be a real shorthand` |
| 185 | `Relatesd toX` | `Short, vague title (12 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 190 | `kljdaskhbadsbhlads` | `Single all-lowercase token ('kljdaskhbadsbhlads') with no spaces -- reads as a stub or keyboard mash, but may be a real shorthand` |
| 191 | `app crashed` | `Short, vague title (11 chars, 2 word(s)) -- heuristic; titles like this are often real work` |
| 197 | `Design request` | `Short, vague title (14 chars, 2 word(s)) -- heuristic; titles like this are often real work` |

### Tier 3 — not detectable

Whether a terse but plausible title is a real request or somebody's scratch note cannot be settled from the file. It needs the person who wrote it. Typo detection would need a spellchecker, and a spellchecker would fire on product nouns as readily as on genuine mistakes. This is left undone deliberately rather than shipped as false confidence — the limit is part of the result.

## Malformed email addresses

8 addresses failed a basic well-formedness check and were wrapped in backticks so they render as code rather than as broken markdown. The original string is preserved exactly; nothing was corrected.

| Source line | Original | In the footer |
| --- | --- | --- |
| 43 | `wayne.stewart dds@riderapp.com` | `` `wayne.stewart dds@riderapp.com` `` |
| 55 | `dr..heather horne@riderapp.com` | `` `dr..heather horne@riderapp.com` `` |
| 70 | `annette.pearson dds@riderapp.com` | `` `annette.pearson dds@riderapp.com` `` |
| 103 | `mrs..linda green@riderapp.com` | `` `mrs..linda green@riderapp.com` `` |
| 129 | `heather.sanchez phd@riderapp.com` | `` `heather.sanchez phd@riderapp.com` `` |
| 131 | `kyle.watts phd@riderapp.com` | `` `kyle.watts phd@riderapp.com` `` |
| 178 | `mr..david rogers@riderapp.com` | `` `mr..david rogers@riderapp.com` `` |
| 195 | `mr..james jones@riderapp.com` | `` `mr..james jones@riderapp.com` `` |

## Before importing

The importer matches workflow states case-insensitively by name, and auto-creates any it cannot find. It can only auto-create three types — backlog, started and completed — and it picks between them using transition timestamps, which this output deliberately does not contain. Every one of these states must therefore already exist on the target team, spelled exactly like this, or those issues land silently in the backlog. Two of them are Linear's own states renamed, so that the team does not end up carrying both the built-in name and the migrated one:

| Status | Category | Action in Linear |
| --- | --- | --- |
| Backlog | backlog | native, no action |
| Needs Review | backlog | add it |
| In Progress | started | native, no action |
| Shipped | completed | rename Linear's "Done" |
| Won't Do | canceled | rename Linear's "Canceled" |

