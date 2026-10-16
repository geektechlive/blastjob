"""Round 2, R12: one row-action responder (`view_helpers.row_response`), not a copy in app.py."""

from __future__ import annotations

import inspect

from blastjob.web import app


def test_app_py_does_not_carry_its_own_copy_of_the_row_responder():
    assert "def _row_response" not in inspect.getsource(app)
