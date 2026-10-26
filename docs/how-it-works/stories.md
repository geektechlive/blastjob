# Story bank

**What this covers:** How the story bank stores interview stories, the version check on edit and delete, and how it lists figures that are not in the work history. It is for anyone changing `core/stories.py` or `web/routes_stories.py`. For the owner's view, see [Interview stories](../using/stories.md).

## Shape

The bank is storage only. A story is the owner's own text (situation, task, action, result), a title, an optional employer and year, and tags. Nothing reads the `stories` table except the Stories page: not the prep pack, not a packet, not a model prompt. Reusing a story in a prep pack would make owner-written text a second source of career claims next to `work_history.md`, which changes a [truth guarantee](../truth-guarantees.md), so it waits for the owner's decision.

Routes (all in `web/routes_stories.py`; every POST is behind `require_csrf`):

- `GET /stories`: the list and the add form.
- `POST /stories`: add. `POST /stories/{id}`: edit. `POST /stories/{id}/delete`: delete.

## Limits

`core/stories.py` checks every save: a title (80 characters at most), at least one of the four STAR fields (1,500 characters each), an employer (80), tags (200) and a year that is four digits from 1970 to the current year, or empty. A broken rule answers 400 and the form is shown again with what was typed. The table also carries `CHECK` constraints on the lengths.

## Version check

The `version` column is an integer, 1 on creation. An edit or delete sends the version the form was opened at and runs as one conditional statement (`... WHERE id=? AND version=?`); an edit also sets `version=version+1`. If no row matched, the story either changed or is gone: `StaleStory` (the route answers 409 and shows the stored text, with the unsaved text kept in the edit box) or `StoryNotFound` (404). So two tabs, or a double submit, can never overwrite or delete newer text.

The list is ordered `updated_at DESC, id DESC`, and `updated_at` has microsecond resolution, so the order does not depend on insertion luck.

## Number check

Each story card lists the figures that are not in the work history: "Not in your work history: 95%". It reuses the prep pack's whole-token rule through `prep.unverified_figures` (the same `figure_keys` the prep pack's `mark_unverified_numbers` uses), so `$1,200` is not cleared by a trusted `$1,200,000`. The check reads `work_history.md` on each render. If that file can't be read, the check did not run, and each card says "Couldn't check figures" instead of showing a clean result. A story id or version above 2^62 is refused with a 422, and CRLF line breaks are stored as LF so they don't count toward the length caps. It is a prompt to check, never a block: saving does not wait on it.

## Tests

`tests/test_stories.py`: validation, stale edit and delete, two quick edits, order, the figure check, the migration (71) on an existing database, CSRF and Origin, and HTML escaping in every field.
