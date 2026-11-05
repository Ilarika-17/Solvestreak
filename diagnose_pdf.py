"""
One-off diagnostic: inspects the most recent run's stored content (no PDF
building involved, so this can't hang) and reports anything that could
plausibly cause the PDF step to choke - total size, and the single longest
unbroken "word" in each field (a giant unbroken token is the classic cause
of PDF layout engines getting stuck or erroring).

Run with:
    python diagnose_pdf.py
"""
import history

runs = history.get_all_runs()
if not runs:
    print("No runs found in history.db.")
    raise SystemExit

run_id = runs[0]["id"]
print(f"Inspecting most recent run: #{run_id}\n")
run_data = history.get_run(run_id)


def longest_word(text):
    if not text:
        return 0, ""
    words = text.split()
    if not words:
        return 0, ""
    longest = max(words, key=len)
    return len(longest), longest


def report(label, text):
    if text is None:
        return
    length = len(text)
    lw_len, lw_text = longest_word(text)
    flag = ""
    if length > 5000:
        flag += " [LONG FIELD]"
    if lw_len > 80:
        flag += " [LONG UNBROKEN WORD]"
    print(f"{label}: {length} chars, longest unbroken word = {lw_len} chars{flag}")
    if lw_len > 80:
        print(f"    preview: {lw_text[:120]}...")


if run_data.get("notes"):
    for note in run_data["notes"]["notes"]:
        report(f"[notes/{note.get('topic', '?')}] explanation", note.get("explanation"))
        report(f"[notes/{note.get('topic', '?')}] code_skeleton", note.get("code_skeleton"))
        for p in note.get("key_patterns", []):
            report(f"[notes/{note.get('topic', '?')}] pattern", p)

if run_data.get("paper"):
    for problem in run_data["paper"]["problems"]:
        report(f"[paper/problem {problem.get('id')}] statement", problem.get("statement"))

if run_data.get("solutions"):
    for s in run_data["solutions"]["solutions"]:
        report(f"[solutions/{s.get('id')}] approach", s.get("approach"))
        report(f"[solutions/{s.get('id')}] code", s.get("code"))

print("\nDone. Anything flagged [LONG FIELD] or [LONG UNBROKEN WORD] above is the likely culprit.")