#!/usr/bin/env python3
"""Transform a Notion issue export into Linear-importable CSVs.

Run it, pick the export from the list, and it writes three files plus a run log.

The Linear CLI importer reads only these columns from a CSV, matched exactly and
case-sensitively: Title, Description, Priority, Status, Assignee, Labels, Estimate,
Created, Started, Completed, Archived. Anything else in the file is inert. Several
of the rules below exist specifically because the importer fails *silently* rather
than loudly -- an unrecognised priority becomes "No priority", a non-integer
estimate is dropped, and a row with an empty title throws part-way through a run
that has already created issues and will not roll them back.
"""

import collections
import csv
import datetime
import hashlib
import os
import re
import sys

# --- Source schema -------------------------------------------------------------
# transform.py is built for this exact Notion export shape and refuses anything else.

SOURCE_COLUMNS = [
    "Title", "Created Date", "Description", "Image", "Platform", "Priority",
    "Reporter", "Size", "Source", "Status", "Tags", "email",
]

# --- Output schema -------------------------------------------------------------
# `Started` and `Completed` are deliberately absent. Notion recorded no transition
# timestamps, so any value there would be inferred rather than migrated. The
# importer treats an absent column and an empty one identically, so omitting them
# is equivalent to leaving them blank and makes the intent visible in the header.

IMPORT_COLUMNS = [
    "Title", "Description", "Status", "Estimate", "Priority", "Labels",
    "Assignee", "Created", "Archived",
]
FLAG_COLUMNS = ["Flag Tier", "Flag Reason"]
DUPLICATE_REVIEW_COLUMNS = ["Duplicate ID", "Routed To"] + IMPORT_COLUMNS
MISSING_COLUMNS = IMPORT_COLUMNS + ["Missing Fields"]

SOURCE_FILE_HINT = "Rideshare_Issue_Tracker.csv"
OUT_DIR = "data"
LOG_PATH = os.path.join("out", "transform_log.md")
F_IMPORT = os.path.join(OUT_DIR, "linear_import.csv")
F_FLAGGED = os.path.join(OUT_DIR, "linear_import_flagged.csv")
F_DUPES = os.path.join(OUT_DIR, "duplicates_review.csv")
F_MISSING = os.path.join(OUT_DIR, "missing_key_values.csv")

# --- Rule table ----------------------------------------------------------------
# Single source of truth for the console report and the run log, so the two can
# never drift apart.

RULES = [
    ("R1",  "Drop blank rows",        "no Title; crashes the importer mid-run"),
    ("R2",  "Unwrap title newlines",  '"\\nTitle\\n" -> "Title"'),
    ("R3",  "Clean descriptions",     "whitespace-only bodies -> empty"),
    ("R4",  "Priority Critical->Urgent", "unmapped values fall back to No priority"),
    ("R5",  "Size -> Estimate",       "XS=1 S=2 M=3 L=5 XL=8"),
    ("R6",  "Build labels",           "Platform/ Source/ Duplicate/ groups + flat tags"),
    ("R7",  "Embed image markdown",   "re-uploaded to Linear's CDN on import"),
    ("R8",  "Append footer",          "reporter, email and original Notion date"),
    ("R9",  "Fence malformed email",  "renders as code, not broken markdown"),
    ("R10", "Created -> ISO",         '"November 5, 2024" -> 2024-11-05'),
    ("R11", "Blank Assignee/Archived", "Notion reporters are not workspace users"),
    ("R12", "Classify rows",          "junk withheld; duplicates labelled and kept"),
    ("R13", "Write flag columns",     "trailing columns the importer never reads"),
    ("R14", "Withhold missing values", "blank Status, Priority or Size"),
]

# --- Enum maps -----------------------------------------------------------------

PRIORITY_MAP = {"Critical": "Urgent", "High": "High", "Medium": "Medium", "Low": "Low"}

# The importer's own priority lookup accepts exactly these strings.
VALID_PRIORITIES = {"No priority", "Urgent", "High", "Medium", "Low"}

SIZE_TO_ESTIMATE = {"XS": 1, "S": 2, "M": 3, "L": 5, "XL": 8}

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}

# The importer only ever auto-creates workflow states of type backlog, started or
# completed, and it derives which from completedAt/startedAt. With no transition
# timestamps in the output, every unmatched status name would be created as
# backlog. These five must exist on the target team before the import runs.
REQUIRED_STATES = [
    ("Backlog", "backlog"),
    ("Needs Review", "unstarted"),
    ("In Progress", "started"),
    ("Shipped", "completed"),
    ("Won't Do", "canceled"),
]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")

# --- Junk signals --------------------------------------------------------------
# Tier 1 is mechanical and defensible. Tier 2 is a heuristic that will catch real
# work, so it is reported as "review", never as junk. Tier 3 is not solvable by
# code and is documented rather than guessed at.

PLACEHOLDER_TITLES = {
    "test", "tests", "test issue", "testing", "xyz", "abc", "asdf", "qwerty",
    "foo", "bar", "baz", "tbd", "todo", "na", "n/a", "temp", "tmp", "dummy",
    "sample", "placeholder", "untitled", "new issue", "issue", "delete me",
}

# Markers left behind when someone pastes an issue template and never fills it in.
# Compared against the description with backslashes stripped, because the Notion
# export escapes the brackets.
TEMPLATE_MARKERS = [
    "[Brief title]", "<Issue description>", "[MM/DD/", "[List affected",
    "[Critical / High", "[Brief overview", "[Describe ", "[Insert ",
]

# Source fields that decide how an issue behaves once it is in Linear. A blank
# one does not stop the import -- it makes it quietly wrong -- so those rows are
# withheld instead. Only blankness is checked: the value mappings are settled for
# this export, so an unrecognised value is not a case this script has to handle.
KEY_FIELDS = ["Status", "Priority", "Size"]

MIN_TITLE_CHARS = 3
VAGUE_MAX_CHARS = 15
VAGUE_MAX_WORDS = 2

FOOTER_SEPARATOR = "---"
FOOTER_MARKER = "Migrated from Notion"


# --- File picker ---------------------------------------------------------------

def list_csvs():
    """Every .csv in the working directory and in data/, as relative paths."""
    found = []
    for directory in (".", OUT_DIR):
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.lower().endswith(".csv"):
                path = os.path.normpath(os.path.join(directory, name))
                if path not in found:
                    found.append(path)
    return found


def pick_csv(prompt):
    """Ask the user to choose a CSV. Falls back to argv when stdin is not a TTY."""
    if not sys.stdin.isatty():
        if len(sys.argv) > 1:
            return sys.argv[1]
        sys.exit("No TTY to prompt on. Pass the CSV path as an argument instead.")

    candidates = list_csvs()
    if not candidates:
        sys.exit("No CSV files found in the working directory or in data/.")

    print(prompt)
    for index, path in enumerate(candidates, 1):
        size = os.path.getsize(path)
        hint = "  <- looks like the source export" if path.endswith(SOURCE_FILE_HINT) else ""
        print(f"  [{index}] {path}  ({size:,} bytes){hint}")
    print()

    while True:
        try:
            raw = input(f"Select a file [1-{len(candidates)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("Cancelled.")
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        print(f"  Enter a number between 1 and {len(candidates)}.")


# --- Rules ---------------------------------------------------------------------

def drop_blank_row(row, counters):
    """R1. The importer calls .replace() on every Title without a guard, so a row
    with no title throws. The throw happens part-way through the run, after issues
    have already been created, and nothing is rolled back."""
    if not (row.get("Title") or "").strip():
        counters["R1"] += 1
        return True
    return False


def unwrap_title(value, counters):
    """R2. Notion exports each title wrapped in newlines. Linear would keep the
    whitespace verbatim, so every issue would render with a blank first line."""
    cleaned = (value or "").strip()
    if cleaned != (value or ""):
        counters["R2"] += 1
    return cleaned


def clean_description(value, counters):
    """R3. A "blank" Notion description is the two-character string "\\n\\n", not an
    empty cell. Left alone it produces a description that is pure whitespace."""
    cleaned = (value or "").strip()
    if cleaned:
        counters["R3"] += 1
    return cleaned


def map_priority(value, counters):
    """R4. The importer's priority lookup is a literal dict with a `|| 0` fallback,
    so any string it does not recognise becomes 0 -- No priority -- with no warning.
    Critical is not one of its keys, so those rows would arrive unprioritised."""
    source = (value or "").strip()
    mapped = PRIORITY_MAP.get(source, "")
    if mapped and mapped != source:
        counters["R4"] += 1
    return mapped


def map_estimate(value, counters):
    """R5. The importer parses the estimate with parseInt and discards anything
    that comes back NaN, so a t-shirt size like "M" silently loses the estimate."""
    size = (value or "").strip()
    if size in SIZE_TO_ESTIMATE:
        counters["R5"] += 1
        return str(SIZE_TO_ESTIMATE[size])
    return ""


def build_labels(row, duplicate_group, counters):
    """R6. The importer splits the Labels cell on the literal two-character string
    ", " and treats "/" as a group separator, taking the last segment as the label
    and everything before it as the group. Platform and Source become groups;
    the free-text tags stay flat."""
    labels = []
    platform = (row.get("Platform") or "").strip()
    if platform:
        labels.append(f"Platform/{platform}")
    source = (row.get("Source") or "").strip()
    if source:
        labels.append(f"Source/{source}")
    for tag in (row.get("Tags") or "").split(","):
        tag = tag.strip()
        if tag:
            labels.append(tag)
    if duplicate_group:
        labels.append(f"Duplicate/{duplicate_group}")
    if labels:
        counters["R6"] += 1
    return ", ".join(labels)


def embed_image(description, row, counters):
    """R7. The importer scans the description for markdown images, downloads each
    one and re-uploads it to Linear's CDN. That is the only path by which the
    Notion Image column can survive, so the URL is appended to the body."""
    url = (row.get("Image") or "").strip()
    if not url:
        return description
    counters["R7"] += 1
    return (description + "\n\n" if description else "") + f"![]({url})"


def fence_email(address, counters):
    """R9. Eight addresses contain spaces or doubled dots. Left bare in markdown
    they render as broken text; wrapped in backticks they render as code, which
    preserves the original string exactly and signals that it needs fixing."""
    address = (address or "").strip()
    if address and not EMAIL_RE.match(address):
        counters["R9"] += 1
        return f"`{address}`"
    return address


def append_footer(description, row, counters):
    """R8. Assignees resolve only against existing workspace users, by lowercased
    email then name, so none of the Notion reporters can match and the column has
    to be left empty. The attribution is preserved in the body instead. The
    separator is emitted even on an empty description -- it marks the issue as
    migrated."""
    reporter = (row.get("Reporter") or "").strip()
    address = fence_email(row.get("email"), counters)
    when = (row.get("Created Date") or "").strip()

    parts = [FOOTER_MARKER]
    if when:
        parts.append(when)
    if reporter:
        parts.append(f"Reported by {reporter} {address}".strip())
    elif address:
        parts.append(address)

    counters["R8"] += 1
    footer = f"{FOOTER_SEPARATOR}\n" + " · ".join(parts)
    return (description + "\n\n" if description else "") + footer


def to_iso_date(value, counters):
    """R10. Dates arrive as "November 5, 2024". The importer hands the string to
    JavaScript's Date constructor, whose parsing of non-ISO formats is
    implementation-defined. An explicit month map avoids both that and any
    dependence on the machine's locale."""
    raw = (value or "").strip()
    match = re.match(r"^([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})$", raw)
    if not match:
        return ""
    month_name, day, year = match.groups()
    if month_name not in MONTHS:
        return ""
    counters["R10"] += 1
    return f"{int(year):04d}-{MONTHS[month_name]:02d}-{int(day):02d}"


# --- Duplicate detection -------------------------------------------------------

def normalize_title(title):
    """Titles are compared case-insensitively with runs of whitespace collapsed and
    trailing sentence punctuation removed. Every cluster this finds in the source
    is an exact match under that normalisation, so no fuzzy scoring is involved
    and the grouping is reproducible."""
    return re.sub(r"\s+", " ", (title or "").strip().lower()).rstrip(".!?")


def find_duplicate_groups(records):
    """Cluster rows by normalised title, numbering groups by first appearance so
    the numbering is stable across runs."""
    by_title = collections.OrderedDict()
    for record in records:
        by_title.setdefault(normalize_title(record["title"]), []).append(record)

    groups = collections.OrderedDict()
    for members in by_title.values():
        if len(members) > 1:
            group_id = f"Group {len(groups) + 1:02d}"
            groups[group_id] = members
            for member in members:
                member["duplicate_group"] = group_id
    return groups


# --- Junk classification -------------------------------------------------------

def find_template_marker(text):
    stripped = (text or "").replace("\\", "")
    for marker in TEMPLATE_MARKERS:
        if marker in stripped:
            return marker
    return None


def find_missing_key_fields(record):
    """R14. Returns the KEY_FIELDS left blank on this row.

    Each blank costs something specific and silent: no Status means the issue
    lands in whatever default state the team has, no Priority becomes No
    priority, and no Size leaves the issue with no estimate. The import still
    succeeds in every case, which is the problem."""
    return [field for field in KEY_FIELDS if not (record["row"].get(field) or "").strip()]


def classify(record):
    """R12. Returns (tier, reason) or (None, None).

    Tier 1 signals are mechanical: an exact placeholder title, an issue template
    that was never filled in, or a title too short to identify anything.

    Tier 2 signals are heuristics. They will catch real work -- "Update API" and
    "Client meeting" are plausibly genuine requests -- so they are reported as
    needing review, never as junk, and the reason says so.

    Tier 3 is everything a script cannot decide: whether a terse title is a real
    request or someone's scratch note needs the person who wrote it. Detecting
    typos would need a spellchecker, which would also fire on product nouns. It
    is left out rather than shipped as false confidence."""
    title = record["title"]
    lowered = normalize_title(title)
    words = title.split()

    if lowered in PLACEHOLDER_TITLES:
        return 1, f"Placeholder title ({title!r}) -- not a real issue"

    marker = find_template_marker(title)
    if marker:
        return 1, f"Title is an unfilled issue template (contains {marker!r})"

    if len(title) < MIN_TITLE_CHARS:
        return 1, f"Title is {len(title)} character(s) long -- identifies nothing"

    marker = find_template_marker(record["source_description"])
    if marker:
        return 1, f"Description is an unfilled issue template (contains {marker!r})"

    if len(words) == 1 and title.islower():
        return 2, (
            f"Single all-lowercase token ({title!r}) with no spaces -- "
            "reads as a stub or keyboard mash, but may be a real shorthand"
        )

    if len(title) < VAGUE_MAX_CHARS and len(words) <= VAGUE_MAX_WORDS:
        return 2, (
            f"Short, vague title ({len(title)} chars, {len(words)} word(s)) -- "
            "heuristic; titles like this are often real work"
        )

    return None, None


# --- Pipeline ------------------------------------------------------------------

def read_source(path, counters):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        if header != SOURCE_COLUMNS:
            sys.exit(
                "That file does not have the expected Notion export columns.\n"
                f"  expected: {SOURCE_COLUMNS}\n"
                f"  found:    {header}\n"
                "transform.py is written for this specific export and will not "
                "guess at a different schema."
            )
        rows = []
        for line_number, row in enumerate(reader, start=2):
            if drop_blank_row(row, counters):
                counters["_dropped_lines"].append(line_number)
                continue
            rows.append((line_number, row))
    return rows


def build_records(rows, counters):
    records = []
    for line_number, row in rows:
        records.append({
            "line": line_number,
            "title": unwrap_title(row.get("Title"), counters),
            "source_description": clean_description(row.get("Description"), counters),
            "row": row,
            "duplicate_group": None,
        })
    return records


def finish_records(records, counters):
    for record in records:
        row = record["row"]
        description = embed_image(record["source_description"], row, counters)
        description = append_footer(description, row, counters)

        record["out"] = {
            "Title": record["title"],
            "Description": description,
            "Status": (row.get("Status") or "").strip(),
            "Estimate": map_estimate(row.get("Size"), counters),
            "Priority": map_priority(row.get("Priority"), counters),
            "Labels": build_labels(row, record["duplicate_group"], counters),
            "Assignee": "",
            "Created": to_iso_date(row.get("Created Date"), counters),
            "Archived": "",
        }
        counters["R11"] += 1

        tier, reason = classify(record)
        record["tier"] = tier
        record["reason"] = reason
        if tier:
            counters[f"tier{tier}"] += 1
            counters["R13"] += 1

        # Junk and duplicates are decided first and keep their routing, so a
        # missing value only redirects a row that would otherwise be clean.
        record["missing"] = find_missing_key_fields(record)
        if record["missing"] and not tier and not record["duplicate_group"]:
            record["withheld_for_missing"] = True
            counters["R14"] += 1
        else:
            record["withheld_for_missing"] = False
        counters["R12"] += 1


def write_csv(path, columns, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(records, groups):
    flagged = [r for r in records if r["tier"]]
    missing = [r for r in records if r["withheld_for_missing"]]
    importable = [r for r in records if not r["tier"] and not r["withheld_for_missing"]]

    write_csv(F_IMPORT, IMPORT_COLUMNS, [r["out"] for r in importable])

    write_csv(F_MISSING, MISSING_COLUMNS, [
        dict(r["out"], **{"Missing Fields": ", ".join(r["missing"])}) for r in missing
    ])

    write_csv(F_FLAGGED, IMPORT_COLUMNS + FLAG_COLUMNS, [
        dict(r["out"], **{"Flag Tier": f"Tier {r['tier']}", "Flag Reason": r["reason"]})
        for r in flagged
    ])

    review_rows = []
    for group_id, members in groups.items():
        for member in members:
            destination = F_FLAGGED if member["tier"] else F_IMPORT
            # Duplicates outrank the missing-value check, so no member can be
            # routed to the missing-values file.
            review_rows.append(dict(
                member["out"],
                **{"Duplicate ID": group_id.replace("Group ", "G"),
                   "Routed To": os.path.basename(destination)},
            ))
    write_csv(F_DUPES, DUPLICATE_REVIEW_COLUMNS, review_rows)

    return importable, flagged, missing, review_rows


# --- Reporting -----------------------------------------------------------------

RULE_WIDTH = 30
BAR = "  " + "\u2500" * 64


def print_report(source_path, counters, records, groups, importable, flagged, missing, review_rows):
    tier1 = [r for r in flagged if r["tier"] == 1]
    tier2 = [r for r in flagged if r["tier"] == 2]
    dropped = len(counters["_dropped_lines"])

    print()
    print("  Notion \u2192 Linear transform")
    print(f"  {source_path} \u2192 4 files")
    print()
    print(f"  {'Rule':<{RULE_WIDTH}}{'Rows':>5}    Detail")
    print(BAR)
    for rule_id, name, detail in RULES:
        label = f"{rule_id} {name}"
        print(f"  {label:<{RULE_WIDTH}}{counters[rule_id]:>5}    {detail}")
    print(BAR)

    label_count, group_count = label_stats(records)
    print(f"  {'Labels created':<{RULE_WIDTH}}{label_count:>5}    across {group_count} groups")
    print(f"  {'Duplicates':<{RULE_WIDTH}}{len(review_rows):>5}    "
          f"in {len(groups)} groups; "
          f"{sum(1 for r in review_rows if r['Routed To'] == os.path.basename(F_IMPORT))}"
          f" kept for import, "
          f"{sum(1 for r in review_rows if r['Routed To'] == os.path.basename(F_FLAGGED))}"
          f" withheld as junk")
    print(f"  {'Flagged  tier 1':<{RULE_WIDTH}}{len(tier1):>5}    deterministic \u2014 placeholder, template, too short")
    print(f"  {'Flagged  tier 2':<{RULE_WIDTH}}{len(tier2):>5}    heuristic \u2014 needs human review, not junk")
    print(f"  {'Missing key values':<{RULE_WIDTH}}{len(missing):>5}    blank {', '.join(KEY_FIELDS)}")
    print(BAR)
    print(f"  {os.path.basename(F_IMPORT):<{RULE_WIDTH}}{len(importable):>5}    importable")
    print(f"  {os.path.basename(F_FLAGGED):<{RULE_WIDTH}}{len(flagged):>5}    withheld for review")
    print(f"  {os.path.basename(F_MISSING):<{RULE_WIDTH}}{len(missing):>5}    importable once the blanks are filled")
    print(f"  {os.path.basename(F_DUPES):<{RULE_WIDTH}}{len(review_rows):>5}    review sheet \u2014 do not import")
    print(BAR)
    print(f"  {len(records)} rows written \u00b7 {dropped} row(s) dropped at read \u00b7 0 discarded")
    print()
    print(f"  Log: {LOG_PATH}")
    print()
    print("  Before importing, create these workflow states on the target team.")
    print("  The importer can only auto-create backlog, started and completed")
    print("  types, so anything missing here lands silently in the backlog:")
    for name, state_type in REQUIRED_STATES:
        print(f"    {name:<16} {state_type}")
    print()


def label_stats(records):
    labels = set()
    for record in records:
        for label in record["out"]["Labels"].split(", "):
            if label:
                labels.add(label)
    groups = {label.split("/")[0] for label in labels if "/" in label}
    return len(labels), len(groups)


def write_log(source_path, digest, counters, records, groups, importable, flagged, missing, review_rows):
    tier1 = [r for r in flagged if r["tier"] == 1]
    tier2 = [r for r in flagged if r["tier"] == 2]
    label_count, group_count = label_stats(records)
    fenced = [r for r in records if "`" in r["out"]["Description"]]

    lines = []
    add = lines.append
    add("# Transform run log")
    add("")
    add(f"- Run: {datetime.datetime.now().isoformat(timespec='seconds')}")
    add(f"- Input: `{source_path}`")
    add(f"- Input SHA-256: `{digest}`")
    add(f"- Rows read: {len(records) + len(counters['_dropped_lines'])}")
    add(f"- Rows written: {len(records)}")
    add(f"- Rows dropped at read: {len(counters['_dropped_lines'])} "
        f"(line(s) {', '.join(str(n) for n in counters['_dropped_lines']) or 'none'})")
    add("- Rows discarded after read: 0")
    add("")

    add("## Files written")
    add("")
    add("| File | Rows | Import? |")
    add("| --- | --- | --- |")
    add(f"| `{F_IMPORT}` | {len(importable)} | Yes |")
    add(f"| `{F_FLAGGED}` | {len(flagged)} | Only after a human reviews it |")
    add(f"| `{F_MISSING}` | {len(missing)} | Once the blank fields are filled in |")
    add(f"| `{F_DUPES}` | {len(review_rows)} | **No** \u2014 these rows are copies |")
    add("")
    add(f"`{os.path.basename(F_DUPES)}` is a review sheet. Every row in it has already "
        "been written to one of the other two files, named in its `Routed To` column. "
        "Importing it as well would create each of those issues a second time.")
    add("")

    add("## Rows missing key values")
    add("")
    add(f"`{', '.join(KEY_FIELDS)}` decide how an issue behaves in Linear. A blank "
        "one does not stop the import, it makes it quietly wrong: no Status lands the "
        "issue in the team's default state, no Priority becomes No priority, and no "
        "Size leaves it with no estimate. Rows with a blank are withheld from the "
        f"import file and written to `{os.path.basename(F_MISSING)}` instead, with a "
        "`Missing Fields` column naming what to fill in.")
    add("")
    add("Only blankness is checked. The value mappings are settled for this export, so "
        "an unrecognised value is not a case this script handles.")
    add("")
    add("Junk and duplicates are routed first, so a row that is already flagged or "
        "already in a duplicate group keeps that routing even when a key field is "
        "blank \u2014 both already demand human attention, and splitting a duplicate "
        "group across files would defeat the label.")
    add("")
    if missing:
        add("| Source line | Title | Missing |")
        add("| --- | --- | --- |")
        for record in missing:
            add(f"| {record['line']} | {format_cell(record['title'])} | "
                f"{format_cell(', '.join(record['missing']))} |")
    else:
        add(f"**No rows were withheld.** Every row has a value for "
            f"{', '.join(f'`{f}`' for f in KEY_FIELDS)}, so "
            f"`{os.path.basename(F_MISSING)}` was written with a header and no data rows.")
    add("")

    add("## Rules applied")
    add("")
    add("| Rule | Rows | Detail |")
    add("| --- | --- | --- |")
    for rule_id, name, detail in RULES:
        add(f"| {rule_id} {name} | {counters[rule_id]} | {detail} |")
    add("")
    add(f"Labels created: **{label_count}** across {group_count} groups "
        "(`Platform`, `Source`, `Duplicate`, plus flat tags).")
    add("")

    add("## Duplicate groups")
    add("")
    add(f"{len(review_rows)} rows across {len(groups)} groups. Titles are clustered by "
        "an exact match after case-folding, collapsing whitespace and stripping trailing "
        "sentence punctuation \u2014 no fuzzy scoring, so the grouping is reproducible. "
        "Groups are numbered by first appearance in the source file.")
    add("")
    add("Every member carries a `Duplicate/Group NN` label, including members withheld "
        "as junk. In Linear the customer filters to one group and sees the copies side "
        "by side, which is what makes the merge-or-keep call quick.")
    add("")
    add("| Group | Rows | Source lines | Title |")
    add("| --- | --- | --- | --- |")
    for group_id, members in groups.items():
        lines_list = ", ".join(str(m["line"]) for m in members)
        title = members[0]["title"].replace("|", "\\|")
        if len(title) > 60:
            title = title[:57] + "..."
        add(f"| {group_id} | {len(members)} | {lines_list} | {title} |")
    add("")
    withheld = [r for r in review_rows if r["Routed To"] == os.path.basename(F_FLAGGED)]
    if withheld:
        add(f"{len(withheld)} of these rows also tripped junk detection and were withheld "
            f"from `{os.path.basename(F_IMPORT)}`. They keep their duplicate label in "
            f"`{os.path.basename(F_FLAGGED)}`, so the group stays intact if they are "
            "later imported.")
        add("")

    add("## Flagged rows")
    add("")
    add("Nothing is deleted and nothing is auto-archived. Flagged rows are withheld from "
        "the import file and written to their own file with the reason attached, so the "
        "decision stays with the customer and is auditable.")
    add("")
    add(f"### Tier 1 \u2014 deterministic ({len(tier1)} rows)")
    add("")
    add("Mechanical signals. Safe to call junk.")
    add("")
    add("| Source line | Title | Reason |")
    add("| --- | --- | --- |")
    for record in tier1:
        add(f"| {record['line']} | {format_cell(record['title'])} | {format_cell(record['reason'])} |")
    add("")
    add(f"### Tier 2 \u2014 heuristic ({len(tier2)} rows)")
    add("")
    add("Flagged as *review*, never as junk. These rules will catch real work: a short "
        "title is not evidence of a bad issue, only of a terse one.")
    add("")
    add("| Source line | Title | Reason |")
    add("| --- | --- | --- |")
    for record in tier2:
        add(f"| {record['line']} | {format_cell(record['title'])} | {format_cell(record['reason'])} |")
    add("")
    add("### Tier 3 \u2014 not detectable")
    add("")
    add("Whether a terse but plausible title is a real request or somebody's scratch note "
        "cannot be settled from the file. It needs the person who wrote it. Typo "
        "detection would need a spellchecker, and a spellchecker would fire on product "
        "nouns as readily as on genuine mistakes. This is left undone deliberately rather "
        "than shipped as false confidence \u2014 the limit is part of the result.")
    add("")

    add("## Malformed email addresses")
    add("")
    add(f"{counters['R9']} addresses failed a basic well-formedness check and were wrapped "
        "in backticks so they render as code rather than as broken markdown. The original "
        "string is preserved exactly; nothing was corrected.")
    add("")
    if fenced:
        add("| Source line | Original | In the footer |")
        add("| --- | --- | --- |")
        for record in fenced:
            original = (record["row"].get("email") or "").strip()
            add(f"| {record['line']} | {format_cell(original)} | `` `{original}` `` |")
        add("")

    add("## Before importing")
    add("")
    add("The importer matches workflow states case-insensitively by name, and auto-creates "
        "any it cannot find. It can only auto-create three types \u2014 backlog, started and "
        "completed \u2014 and it picks between them using transition timestamps, which this "
        "output deliberately does not contain. Every one of these states must therefore "
        "already exist on the target team, spelled exactly like this, or those issues land "
        "silently in the backlog:")
    add("")
    add("| Status | Required type |")
    add("| --- | --- |")
    for name, state_type in REQUIRED_STATES:
        add(f"| {name} | {state_type} |")
    add("")

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def format_cell(text):
    text = (text or "").replace("|", "\\|").replace("\n", " ")
    return f"`{text}`" if text else ""


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    source_path = pick_csv("Select the Notion export to transform:")
    if not os.path.isfile(source_path):
        sys.exit(f"No such file: {source_path}")

    counters = collections.Counter()
    counters["_dropped_lines"] = []

    rows = read_source(source_path, counters)
    records = build_records(rows, counters)
    groups = find_duplicate_groups(records)
    finish_records(records, counters)

    os.makedirs(OUT_DIR, exist_ok=True)
    importable, flagged, missing, review_rows = write_outputs(records, groups)

    write_log(source_path, sha256_of(source_path), counters, records, groups,
              importable, flagged, missing, review_rows)
    print_report(source_path, counters, records, groups,
                 importable, flagged, missing, review_rows)


if __name__ == "__main__":
    main()
