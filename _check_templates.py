from app import app

with app.test_request_context("/"):
    index_html = app.jinja_env.get_template("index.html").render(
        weak_topics=["Arrays", "Hash Table", "DP"],
        topics_tracked=42,
    )
with app.test_request_context("/notes"):
    notes_html = app.jinja_env.get_template("notes.html").render(
        notes={
            "notes": [
                {
                    "topic": "Arrays",
                    "explanation": "x",
                    "key_patterns": ["a", "b"],
                    "code_skeleton": "def f():\n  pass",
                }
            ]
        }
    )
    notes_empty = app.jinja_env.get_template("notes.html").render(notes={"notes": []})
with app.test_request_context("/paper"):
    paper_html = app.jinja_env.get_template("paper.html").render(
        paper={
            "problems": [
                {
                    "id": 1,
                    "title": "T",
                    "topic": "Arrays",
                    "difficulty": "Easy",
                    "statement": "Do x",
                }
            ]
        }
    )
    paper_empty = app.jinja_env.get_template("paper.html").render(paper={"problems": []})
with app.test_request_context("/solutions"):
    solutions_html = app.jinja_env.get_template("solutions.html").render(
        solutions={
            "solutions": [
                {
                    "id": 1,
                    "approach": "try",
                    "code": "print(1)",
                    "complexity": "O(n) time, O(1) space",
                }
            ]
        }
    )
with app.test_request_context("/deadline"):
    form_html = app.jinja_env.get_template("deadline_form.html").render(
        suggested_topics="Arrays"
    )
    plan_html = app.jinja_env.get_template("plan.html").render(
        skeleton=[
            {"date": "2026-09-30", "status": "free", "topic": "Arrays", "task": "Solve 2"},
            {"date": "2026-10-01", "status": "busy"},
        ]
    )
    error_html = app.jinja_env.get_template("error.html").render(
        title="Invalid deadline",
        message="past",
        back_href="/deadline",
        back_label="Back",
    )

assert 'action="/notes"' in index_html and 'method="post"' in index_html
assert 'action="/paper"' in notes_html
assert 'action="/solutions"' in paper_html
assert 'action="/deadline"' in form_html
assert 'name="deadline"' in form_html and 'name="topics"' in form_html
assert "No notes yet." in notes_empty
assert "No practice paper yet." in paper_empty
assert "Something went wrong" in error_html
print("templates ok")
