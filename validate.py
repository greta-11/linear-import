#!/usr/bin/env python3
"""Reconcile a Linear CSV export against the CSV(s) that were imported into it.

Run it after the import has finished. It asks for two things: the export you
downloaded from Linear, and the import file(s) you actually fed to the importer.

The import file is the benchmark. That is the whole design: this script holds no
expected row counts, no expected label totals and no hard-coded notion of what a
correct migration looks like. It compares what Linear returned against what was
sent, so a failure names the specific issues that went missing or came back
wrong instead of reporting that a number is off.

Exits 0 when every check passes, 1 otherwise.
"""

import collections
import csv
import os
import re
import sys

# Linear's export column names are not identical across workspaces and versions,
# so columns are resolved by alias rather than assumed. Title and Description are
# required -- without them there is nothing to reconcile. Everything else degrades
# to a skipped check with a note, rather than a crash or a false failure.
COLUMN_ALIASES = {
    "Title":       ["Title", "Issue Title", "Name"],
    "Description": ["Description", "Body", "Issue Description"],
    "Status":      ["Status", "State", "Workflow State", "State Name"],
    "Priority":    ["Priority", "Priority Label"],
    "Estimate":    ["Estimate", "Estimate Points", "Points", "Story Points"],
    "Labels":      ["Labels", "Label", "Label Names"],
    "Assignee":    ["Assignee", "Assignee Name", "Assigned To"],
    "Created":     ["Created", "Created At", "Created Date", "createdAt"],
    "Started":     ["Started", "Started At", "startedAt"],
    "Completed":   ["Completed", "Completed At", "completedAt"],
    "Canceled":    ["Canceled", "Cancelled", "Canceled At", "canceledAt"],
    "Identifier":  ["ID", "Identifier", "Issue ID", "Key", "Issue Key"],
}
REQUIRED = ["Title", "Description"]

# The footer transform.py writes onto every migrated description. It is what
# separates migrated issues from whatever else already lived in the workspace.
FOOTER_MARKER = "Migrated from Notion"
FOOTER_SEPARATOR = "---"

# Linear renders priority as a name in some exports and as the underlying integer
# in others. Both are normalised to the name before comparing.
PRIORITY_BY_NUMBER = {"0": "No priority", "1": "Urgent", "2": "High", "3": "Medium", "4": "Low"}
UNPRIORITISED = "No priority"

# Linear stamps exactly one of these timestamps when an issue is created, chosen
# by the *type* of the workflow state it lands in. That makes the type readable
# from an export even though no column names it directly. Backlog and unstarted
# both stamp nothing, so a state that stamps nothing is known to be one of the
# two without which being determinable -- immaterial here, since no state in this
# mapping is expected to be unstarted.
STATE_TYPE_SIGNALS = [("Canceled", "canceled"), ("Completed", "completed"),
                      ("Started", "started")]
UNSTAMPED = "backlog or unstarted"

REQUIRED_STATES = [
    ("Backlog", "backlog", "native, no action"),
    ("Needs Review", "backlog", "add it"),
    ("In Progress", "started", "native, no action"),
    ("Shipped", "completed", 'rename Linear\'s "Done"'),
    ("Won't Do", "canceled", 'rename Linear\'s "Canceled"'),
]

EXPECTED_STATE_TYPES = None  # built from REQUIRED_STATES below

IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")

# Linear re-hosts imported images on its own CDN, so a URL still pointing at the
# source host means that upload did not happen. A URL already on Linear's CDN in
# the file that was imported is not evidence of anything, and must not be treated
# as a source host -- otherwise re-validating a migration that was itself built
# from a Linear export would flag every image.
LINEAR_ASSET_HOSTS = {"uploads.linear.app", "public.linear.app"}

EXPECTED_STATE_TYPES = {name: category for name, category, _ in REQUIRED_STATES}

MAX_SHOWN = 3


# --- File picker ---------------------------------------------------------------

def list_csvs():
    found = []
    for directory in (".", "data"):
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.lower().endswith(".csv"):
                path = os.path.normpath(os.path.join(directory, name))
                if path not in found:
                    found.append(path)
    return found


def show_candidates(candidates):
    for index, path in enumerate(candidates, 1):
        print(f"  [{index}] {path}  ({os.path.getsize(path):,} bytes)")
    print()


def pick_one(candidates, prompt):
    print(prompt)
    show_candidates(candidates)
    while True:
        try:
            raw = input(f"Select a file [1-{len(candidates)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("Cancelled.")
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        print(f"  Enter a number between 1 and {len(candidates)}.")


def pick_many(candidates, prompt):
    print(prompt)
    show_candidates(candidates)
    while True:
        try:
            raw = input(f"Select one or more, comma-separated [1-{len(candidates)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("Cancelled.")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts and all(p.isdigit() and 1 <= int(p) <= len(candidates) for p in parts):
            chosen = []
            for p in parts:
                path = candidates[int(p) - 1]
                if path not in chosen:
                    chosen.append(path)
            return chosen
        print(f"  Enter numbers between 1 and {len(candidates)}, separated by commas.")


def gather_inputs():
    """Two inputs, or both from argv when there is no TTY to prompt on."""
    if not sys.stdin.isatty():
        if len(sys.argv) >= 3:
            return sys.argv[1], sys.argv[2:]
        sys.exit("No TTY to prompt on. Usage: validate.py <linear_export.csv> "
                 "<import.csv> [more_import.csv ...]")

    candidates = list_csvs()
    if len(candidates) < 2:
        sys.exit("Need at least two CSVs present: the Linear export and the file "
                 "that was imported. Put the export in this directory first.")

    export_path = pick_one(candidates, "\nStep 1 of 2 — select the CSV you exported from Linear:")
    print()
    remaining = [p for p in candidates if p != export_path]
    import_paths = pick_many(
        remaining,
        "Step 2 of 2 — select the file(s) you imported into Linear.\n"
        "These are the benchmark the export is checked against, so include every "
        "file you actually imported and nothing you did not.",
    )
    return export_path, import_paths


# --- Loading -------------------------------------------------------------------

def resolve_columns(header):
    resolved, missing = {}, []
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in header:
                resolved[canonical] = alias
                break
        else:
            missing.append(canonical)
    return resolved, missing


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames or [], list(reader)


def infer_state_type(row, columns):
    """Read a workflow state's type off the timestamp Linear stamped at import.

    Returns "canceled", "completed", "started", or UNSTAMPED when nothing was
    stamped, which narrows it to backlog or unstarted without separating them."""
    for canonical, state_type in STATE_TYPE_SIGNALS:
        column = columns.get(canonical)
        if column and (row.get(column) or "").strip():
            return state_type
    return UNSTAMPED


def strip_export_escape(text):
    """Linear's CSV export prefixes a single quote to any text field starting with
    a character a spreadsheet would try to interpret -- in practice "@", ">" and
    "---". A description that is nothing but the migration footer starts with
    "---", so this affects every issue whose body was empty. The importer strips
    the same character on the way in, so removing it here restores the text that
    was actually sent."""
    text = text or ""
    return text[1:] if text.startswith("'") else text


def normalize_title(title):
    return re.sub(r"\s+", " ", (title or "").strip().lower()).rstrip(".!?")


def normalize_priority(value):
    value = (value or "").strip()
    return PRIORITY_BY_NUMBER.get(value, value)


def normalize_estimate(value):
    """Exports render estimates as "3" or "3.0" depending on the workspace."""
    value = (value or "").strip()
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number == int(number) else value


def normalize_date(value):
    value = (value or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else value


def label_set(value, strip_groups=False):
    labels = {part.strip() for part in (value or "").split(",") if part.strip()}
    if strip_groups:
        labels = {label.split("/")[-1] for label in labels}
    return labels


def get(row, columns, canonical):
    column = columns.get(canonical)
    return row.get(column, "") if column else ""


# --- Check plumbing ------------------------------------------------------------

class Report:
    def __init__(self):
        self.failures = 0
        self.skipped = 0

    def check(self, check_id, name, ok, count="", detail="", offenders=None):
        status = "PASS" if ok else "FAIL"
        if not ok:
            self.failures += 1
        line = f"  {check_id:<4} {status}  {name:<44} {str(count):>5}"
        if detail:
            line += f"  {detail}"
        print(line)
        for item in (offenders or [])[:MAX_SHOWN]:
            print(f"         └─ {item}")
        if offenders and len(offenders) > MAX_SHOWN:
            print(f"         └─ ... and {len(offenders) - MAX_SHOWN} more")

    def skip(self, check_id, name, reason):
        self.skipped += 1
        print(f"  {check_id:<4} SKIP  {name:<44} {'':>5}  {reason}")


def compare_field(report, check_id, name, pairs, columns, canonical,
                  normalize=lambda v: (v or "").strip(), detail=""):
    """Compare one field between matched import and export rows.

    Rows are grouped by normalised title and compared as multisets within each
    group, because near-duplicate issues legitimately share a title and there is
    no stable way to say which copy in the export corresponds to which copy in
    the import."""
    if canonical not in columns:
        report.skip(check_id, name, f"no {canonical} column in the export")
        return
    mismatches = []
    for title, (sent, got) in pairs.items():
        want = sorted(normalize(r["_import"].get(canonical, "")) for r in sent)
        have = sorted(normalize(get(r, columns, canonical)) for r in got)
        if want != have:
            mismatches.append(f"{title!r}: sent {want}, export has {have}")
    report.check(check_id, name, not mismatches, len(mismatches) or len(pairs),
                 detail, mismatches)


# --- Main ----------------------------------------------------------------------

def main():
    export_path, import_paths = gather_inputs()

    export_header, export_rows = read_csv(export_path)
    columns, missing = resolve_columns(export_header)

    for canonical in ("Title", "Description"):
        column = columns.get(canonical)
        if column:
            for row in export_rows:
                row[column] = strip_export_escape(row.get(column))

    print()
    print("  Linear import validation")
    print(f"  export: {export_path}")
    for path in import_paths:
        print(f"  sent:   {path}")
    print()

    report = Report()
    bar = "  " + "─" * 76
    print(f"  {'ID':<4} {'':4}  {'Check':<44} {'Rows':>5}")
    print(bar)

    blocking = [c for c in REQUIRED if c in missing]
    report.check("V1", "Export columns resolved", not blocking,
                 len(columns),
                 f"missing: {', '.join(missing)}" if missing else "all mapped")
    if blocking:
        print(bar)
        print(f"\n  Cannot continue without {', '.join(blocking)}.")
        print(f"  Header found in the export:\n    {export_header}")
        sys.exit(1)

    # Load what was sent.
    import_rows = []
    for path in import_paths:
        _, rows = read_csv(path)
        for row in rows:
            row["_source_file"] = os.path.basename(path)
            import_rows.append(row)

    # Select migrated issues out of the export, so a non-empty workspace does not
    # produce spurious extras.
    migrated, foreign = [], []
    for row in export_rows:
        (migrated if FOOTER_MARKER in get(row, columns, "Description") else foreign).append(row)

    print(f"  {'--':<4} {'INFO':4}  {'Issues in export':<44} {len(export_rows):>5}"
          f"  {len(migrated)} migrated, {len(foreign)} pre-existing")

    sent_by_title = collections.defaultdict(list)
    for row in import_rows:
        sent_by_title[normalize_title(row.get("Title"))].append(row)
    got_by_title = collections.defaultdict(list)
    for row in migrated:
        got_by_title[normalize_title(get(row, columns, "Title"))].append(row)

    # V2/V3 -- coverage in both directions.
    missing_titles = []
    for title, sent in sent_by_title.items():
        got = got_by_title.get(title, [])
        if len(got) < len(sent):
            missing_titles.append(f"{sent[0].get('Title')!r}: sent {len(sent)}, found {len(got)}")
    report.check("V2", "Every imported row present in the export", not missing_titles,
                 len(import_rows), "issues sent", missing_titles)

    extra_titles = []
    for title, got in got_by_title.items():
        sent = sent_by_title.get(title, [])
        if len(got) > len(sent):
            label = get(got[0], columns, "Title")
            extra_titles.append(f"{label!r}: sent {len(sent)}, found {len(got)}")
    report.check("V3", "No unexpected extra migrated issues", not extra_titles,
                 len(migrated), "migrated issues found", extra_titles)

    # Pair up only the titles present on both sides; V2/V3 already reported the rest.
    pairs = {}
    for title, sent in sent_by_title.items():
        got = got_by_title.get(title)
        if got:
            for row in sent:
                row["_import"] = row
            pairs[sent[0].get("Title")] = (sent, got)

    compare_field(report, "V4", "Priority survived the import", pairs, columns,
                  "Priority", normalize_priority, "per matched title")
    unprioritised = [get(r, columns, "Title") for r in migrated
                     if normalize_priority(get(r, columns, "Priority")) == UNPRIORITISED]
    report.check("V5", f"No issue landed as {UNPRIORITISED!r}", not unprioritised,
                 len(unprioritised), "a priority string the importer did not recognise",
                 unprioritised)

    compare_field(report, "V6", "Estimate survived the import", pairs, columns,
                  "Estimate", normalize_estimate, "per matched title")
    compare_field(report, "V7", "Status survived the import", pairs, columns,
                  "Status", detail="per matched title")

    # V8 -- did every state name that was sent come back at all? V9 then asks the
    # harder question of whether each one has the right type.
    if "Status" in columns:
        sent_statuses = {(r.get("Status") or "").strip() for r in import_rows}
        sent_statuses.discard("")
        got_statuses = collections.Counter(get(r, columns, "Status").strip() for r in migrated)
        vanished = sorted(s for s in sent_statuses if not got_statuses.get(s))
        report.check("V8", "Every workflow state exists in the export", not vanished,
                     len(got_statuses), "distinct states",
                     [f"{s!r} was sent but no issue came back with it — likely "
                      "collapsed into the backlog" for s in vanished])
    else:
        report.skip("V8", "Every workflow state exists in the export", "no Status column")

    # V9 -- the type each workflow state was created with. This is the check the
    # pre-flight step exists for: a state the importer had to invent is created as
    # backlog, so an "In Progress" that never got a Started stamp, or a "Won't Do"
    # that never got a Canceled stamp, means the state did not exist beforehand.
    if any(c in columns for c, _ in STATE_TYPE_SIGNALS):
        observed = collections.defaultdict(collections.Counter)
        for row in migrated:
            observed[get(row, columns, "Status").strip()][infer_state_type(row, columns)] += 1

        problems = []
        for status, types in sorted(observed.items()):
            actual = types.most_common(1)[0][0]
            expected = EXPECTED_STATE_TYPES.get(status)
            if len(types) > 1:
                spread = ", ".join(f"{t} x{n}" for t, n in types.most_common())
                problems.append(f"{status!r} is inconsistent across issues: {spread}")
            elif expected is None:
                continue
            elif expected in ("backlog", "unstarted"):
                if actual != UNSTAMPED:
                    problems.append(
                        f"{status!r} behaves as {actual!r}, expected {expected!r}")
            elif actual != expected:
                problems.append(
                    f"{status!r} behaves as {actual!r}, expected {expected!r} \u2014 the "
                    "state was most likely auto-created by the import because it did "
                    "not already exist")

        summary = "; ".join(
            f"{status}={types.most_common(1)[0][0]}" for status, types in sorted(observed.items()))
        report.check("V9", "Workflow state types are as required", not problems,
                     len(observed), summary, problems)

        undecidable = sorted(
            status for status, types in observed.items()
            if types.most_common(1)[0][0] == UNSTAMPED
            and EXPECTED_STATE_TYPES.get(status) in ("backlog", "unstarted"))
        if undecidable:
            print("         \u2514\u2500 note: no timestamp stamped for "
                  f"{', '.join(repr(s) for s in undecidable)}; confirmed as "
                  "backlog-or-unstarted, not separated")
    else:
        report.skip("V9", "Workflow state types are as required",
                    "export has no Started/Completed/Canceled columns")

    # V10 -- labels. Some exports carry the group prefix, some only the child name.
    if "Labels" in columns:
        sent_all = {lab for r in import_rows for lab in label_set(r.get("Labels"))}
        got_all = {lab for r in migrated for lab in label_set(get(r, columns, "Labels"))}
        grouped = any("/" in lab for lab in got_all)
        mismatches = []
        for title, (sent, got) in pairs.items():
            want = sorted(label_set(", ".join(r.get("Labels", "") for r in sent), not grouped))
            have = sorted(label_set(", ".join(get(r, columns, "Labels") for r in got), not grouped))
            if want != have:
                mismatches.append(f"{title!r}: sent {want}, export has {have}")
        report.check("V10", "Labels survived the import", not mismatches,
                     len(got_all), f"distinct labels ({'grouped' if grouped else 'flat'} in export)",
                     mismatches)

        sent_groups = collections.defaultdict(set)
        for lab in sent_all:
            if "/" in lab:
                sent_groups[lab.split("/")[0]].add(lab)
        if grouped:
            lost = [g for g in sent_groups if not any(l.startswith(g + "/") for l in got_all)]
            report.check("V11", "Label groups preserved", not lost, len(sent_groups),
                         "groups sent", [f"group {g!r} is absent from the export" for g in lost])
        else:
            report.skip("V11", "Label groups preserved",
                        "export renders labels without group prefixes")
    else:
        report.skip("V10", "Labels survived the import", "no Labels column")
        report.skip("V11", "Label groups preserved", "no Labels column")

    # V12 -- images. The importer re-hosts markdown images on Linear's CDN. Any URL
    # still pointing at the original host means that upload did not happen.
    sent_hosts = set()
    for row in import_rows:
        for url in IMAGE_RE.findall(row.get("Description", "")):
            host = url.split("/")[2] if "//" in url else url
            if host not in LINEAR_ASSET_HOSTS:
                sent_hosts.add(host)
    if sent_hosts:
        stale = []
        for row in migrated:
            for url in IMAGE_RE.findall(get(row, columns, "Description")):
                host = url.split("/")[2] if "//" in url else url
                if host in sent_hosts:
                    stale.append(f"{get(row, columns, 'Title')!r} still points at {host}")
        with_images = sum(1 for r in migrated if IMAGE_RE.search(get(r, columns, "Description")))
        report.check("V12", "Images re-hosted by Linear", not stale, with_images,
                     f"issues with an image; original host(s): {', '.join(sorted(sent_hosts))}",
                     stale)
    else:
        report.skip("V12", "Images re-hosted by Linear", "no images in the imported file(s)")

    # V13 -- the footer carries the reporter attribution that the Assignee column
    # could not, so losing it loses the only record of who raised the issue.
    no_footer = [get(r, columns, "Title") for r in migrated
                 if FOOTER_SEPARATOR not in get(r, columns, "Description")]
    report.check("V13", "Migration footer intact", not no_footer, len(migrated),
                 "migrated issues", no_footer)

    compare_field(report, "V14", "Created date preserved", pairs, columns,
                  "Created", normalize_date, "original Notion dates, not import time")

    if "Assignee" in columns:
        assigned = [get(r, columns, "Title") for r in migrated
                    if get(r, columns, "Assignee").strip()]
        report.check("V15", "Assignee left unset, as sent", not assigned, len(migrated),
                     "migrated issues", assigned)
    else:
        report.skip("V15", "Assignee left unset, as sent", "no Assignee column")

    print(bar)
    verdict = "FAILED" if report.failures else "PASSED"
    print(f"  {verdict}  ·  {report.failures} failing check(s)"
          f"{f', {report.skipped} skipped' if report.skipped else ''}")
    print()

    print("  V9 reads each state's type from the timestamp Linear stamps at import.")
    print("  A state that stamps nothing is backlog or unstarted; V9 does not")
    print("  separate those two, which is immaterial when none should be unstarted:")
    for name, category, action in REQUIRED_STATES:
        print(f"    {name:<16} {category:<11} {action}")
    print()
    print("  Also outside its reach: whether description markdown renders correctly,")
    print("  what a dropped row contained, and whether a missing flagged issue was a")
    print("  deliberate decision not to import that file or a failure to import it.")
    print()

    sys.exit(1 if report.failures else 0)


if __name__ == "__main__":
    main()
