"""#118: a school on the resume without dates is not an "undated employer".

Leaving dates off an education entry is normal and often deliberate, so the
fit-score report must not tell the owner to add them. Employers keep the check.
"""

from __future__ import annotations

from blastjob.core import resume_dates

HISTORY = """\
# Work History

## Experience

### Director of IT | Acme Corp | Springfield, OH
Jan 2018 - Dec 2023

- Ran IT.

### IT Manager | Globex | Springfield, OH
Mar 2012 - Dec 2017

- Ran a team.

## Education

###  | Example State University

###  | Fabrikam Institute of Design (Fabrikam College)
"""

RESUME = """\
# Pat Example

## Experience

### Director of IT
**Acme Corp** | Springfield, OH | Jan 2018 - Dec 2023

### IT Manager
**Globex** | Springfield, OH

## Education

- Example State University
- Fabrikam Institute of Design
"""


def test_schools_are_not_reported_as_undated_employers() -> None:
    _, undated = resume_dates.verify(RESUME, HISTORY)
    assert "Example State University" not in undated
    assert "Fabrikam Institute of Design" not in undated


def test_an_employer_without_dates_is_still_reported() -> None:
    _, undated = resume_dates.verify(RESUME, HISTORY)
    assert undated == ["Globex"]
