---
name: Bug report
about: A demonstration did something other than what you expected
title: ""
labels: bug

---

**Which demonstration**
e.g. `zeos-chat`, and the commit or date of your checkout.

**What you did, in order**
The sequence matters more than the symptom: what happened before it is usually what
the bug is about. e.g. "asked for research, opened the document chip, pressed Email as
owner".

**What you expected, and what happened instead**

**The journal**
Every demonstration writes one. Attach it, or paste the lines around the problem: which
job held the machine, what blocked, what preempted what, which fault landed. It settles
in one read what a description can only suggest. `zeos-chat serve --journal <path>`
writes it to a file; the Kernel panel in the page shows it live.

**Environment**
* Python version
* OS
* model in use (`stub`, `claude`), without the key
