---
name: ask-find
priority: 40
on_fault: retry
pipes:
  tools: requests.find
capabilities:
  - pipe: requests.find
    min_integrity: 2
utterances:
  - "find my {item}"
  - "where did i park the {item}"
  - "where is the {item}"
script:
  # The canonical morning (events.jsonl), command for command.
  - emit: "write tools item=motorbike;"
  - emit: "exit;"
---

# Task: pass on what the farmer wants found

The farmer asked for something to be found, and what it is arrived with you as the
value of `item`. Ask for a search: `write tools item=ITEM;`, with any spaces in ITEM
replaced by hyphens, then `exit;`.
