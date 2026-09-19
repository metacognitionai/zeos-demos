# zeos-chat — a chatbot built on ZEOS

A working chatbot, in a browser, with a classic chat look. What makes it worth looking at
is underneath: there is no chat loop anywhere in it. The conversation is a **job** run by
the ZEOS kernel — a transformer operating system — and the things a chatbot normally does
in application code (waiting for input, noticing you typed while it was answering,
stopping when you say stop) are done by the kernel instead, because they are scheduling,
and scheduling is what an operating system is for.

A chatbot was chosen because everybody already knows how one behaves, so the kernel's
mechanisms show up against a familiar backdrop. ZEOS is not intended for chatbots and this
is not an end product; it is a demonstration you can sit in front of.

## Running it

You need [uv](https://docs.astral.sh/uv/) and a checkout of `zeos-internal` beside
`zeos_demos`.

```bash
uv sync
uv run zeos-chat serve --open
```

That is the whole of it. A stub model answers, so there is no API key, no weights, no
network and nothing to configure. The page works, the kernel is real, and everything the
panel shows actually happened.

For a chatbot that can hold a conversation, put a Claude key in `.env` (below) and:

```bash
uv sync --extra claude
uv run zeos-chat serve --model claude --open
```

Two other commands, if you want them:

```bash
# Typecheck the descriptor tree, read against this case's own command vocabulary.
uv run zeos-chat lint

# Replay a fixed schedule of events in the terminal, with the journal printed as it goes.
uv run zeos-chat run --events cases/chat/events.jsonl
```

`serve` also takes `--host`, `--port`, and `--journal <path>` to write the kernel's
journal to a file.

## Configuring it

Settings resolve `.env` file < environment variable < command-line flag. Copy the template
and fill in what you need — `.env` is gitignored:

```bash
cp .env.example .env
```

### The Claude API key

Only `--model claude` needs this. The stub, the lint and the replay need nothing.

| Variable | Required | What it is |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | yes, for `--model claude` | your key from the Anthropic console |
| `ANTHROPIC_MODEL` | no | defaults to `claude-opus-5` |

These are the SDK's own names, so a shell already set up for Claude needs no `.env` at
all. Without a key, `--model claude` says so and stops rather than quietly falling back.

### The email address

The **Email** button sends the conversation to you. `MAIL_TRANSPORT` chooses how:

| | needs an account? | what a press does |
| --- | --- | --- |
| **`desktop`** (default) | no | writes the letter to `MAIL_OUTBOX_DIR` and opens it as a draft in your own mail client |
| `smtp` | yes | sends it in the background |
| `simulated` | no | neither; reports what would have happened |

The default needs nothing configured and cannot send on its own: you get a draft in your
usual mail client and press Send yourself. Set `MAIL_TO` to have it addressed for you, or
leave it out and fill in the To: field in the draft.

```bash
# .env — the whole of it, for the default transport
MAIL_TRANSPORT=desktop
MAIL_OUTBOX_DIR=outbox
MAIL_TO=you@example.com
```

The complete letter is always written to the outbox as an `.eml`, because a `mailto:` URI
is capped near 2000 characters and a long conversation will not fit in one — the draft is
trimmed with a note saying so, and the file keeps every word. On a machine with no mail
client registered, the letter is still written and the page says the client could not be
opened.

To send in the background instead, set `MAIL_TRANSPORT=smtp`, fill in the account, and arm
it for the run that means it:

| Variable | What it is |
| --- | --- |
| `MAIL_SMTP_HOST` | e.g. `smtp.gmail.com` |
| `MAIL_SMTP_PORT` | defaults to `587`, STARTTLS |
| `MAIL_USERNAME` | the account you authenticate as |
| `MAIL_PASSWORD` | an **app-specific** password |
| `MAIL_FROM` | defaults to `MAIL_USERNAME` |
| `MAIL_TO` | defaults to `MAIL_FROM` |

```bash
MAIL_LIVE=1 uv run zeos-chat serve --open
```

Use an app-specific password, not your account password. `535 5.7.8 BadCredentials` from
Gmail means the account password was used where an app password was needed; a Google app
password is 16 lowercase letters, pasted without the spaces.

Whichever transport is in play, the console says which before anybody presses the button:

```
mail: opens a draft addressed to you@example.com in your mail client; the letter is also written to outbox/
mail: LIVE -- a send will really go to you@example.com
mail: simulated -- nothing is sent and nothing is opened
```

## What you are looking at

Classic chat on the left. The **Kernel** button opens the journal on the right, and that
is the interesting half: it is what the kernel recorded, not a prettier copy of the
conversation. "A vector fired and preempted job 1" is a structural fact; "the reply
mentioned Kyoto" is not, and does not appear there.

The logo is the Metacognition mark, which is already a Pong board — two bars and a square
between them. It plays while the system has work outstanding and rests when it has none,
so the animation is a reading of the kernel rather than decoration. It keeps playing while
an answer is being composed, which is worth noticing: the conversation is parked on a
device and nothing at all is runnable, and that is not the same as idle.

**Interruptions are marked in the transcript itself**, as a rule across the conversation:

```
             you                    plan a week in Kyoto in November
  ──── interrupted — you spoke while the answer was still arriving ────
             you                    actually make it Osaka
  November is peak foliage in Kyoto, which means the best colour ...
  ──────────── stopped — the rest was not sent ────────────
```

A mark appears only when something really was interrupted. A message to an idle
conversation gets none, and neither does a stop that stopped nothing.

The **Email** button is solid green when a press opens a draft, recedes to an outline when
the run is simulated and nothing will happen, and turns the accent colour when a press
would really send.

## Things to try

**Type while it is answering.** Your message fires an interrupt, which preempts the answer
at the next token boundary — between one word and the next. Watch the journal: the vector
fires and the conversation gives up the machine. Nothing in the chatbot checked whether it
was busy, because nothing in the chatbot can.

**Press Stop mid-answer.** A reflex at priority 5 takes the machine, the driver abandons
the request so the model stops composing rather than finishing into the void, and the
words already written stay on the page.

**Press Email.** A separate job is dispatched at priority 40 to do it, and its single
write is the only effect in this whole system that leaves the machine.

**Ask it to research something.** Say "research the history of Kyoto temples", or "look
into" something, and the conversation hands the work to a second job instead of answering
it. You get an acknowledgement immediately — composed in Python, so it costs no model call
at all — and the conversation is listening again before the research has started.

**The findings come back as a document, not as talk.** When the job finishes, a file
appears in the transcript with the subject and its length; click it to read it. That is
deliberate: the findings are pages long, they answer something you asked many turns ago,
and poured into the conversation they bury whatever it is currently doing. As a document
they are what they actually are — a thing that was produced, which you open when you want
it — and the conversation underneath stays a conversation.

That job runs at priority 90, below everything. Keep talking while it works: your
questions are answered at once, because the research job is either blocked on the model or
outranked by anything you do. The journal shows it spawned, and shows the conversation
parked back on you a moment later. This is the arrangement the whole design is for, and it
is the one thing a single chat loop cannot imitate.

A bar under the header lists the background jobs while they run, one line each, and is
absent when there are none. Ask for a second thing to be looked into and both appear.

**Say nothing at all.** An idle conversation costs nothing. It is not polling and it is
not looping; it is blocked, and a blocked job runs no forward passes. The logo rests and
the journal shows the job waiting on a pipe.

## How it works

**Five descriptors, one per behaviour.** A descriptor is a small text file: frontmatter
declaring what the kernel enforces — priority, which pipes it may touch, what it may spawn
— and a body in plain English describing who it is.

```
cases/chat/
├── goals/converse.md              priority 60 — the resident conversation
├── handlers/new-message.md        priority 10 — barge-in
├── handlers/cancel.md             priority  5 — the stop reflex
├── services/deep-research.md      priority 90 — the long job
├── services/send-email.md         priority 40 — the consequential one
├── system/{pipes,vectors,world-state,boot}.yaml
└── events.jsonl
```

**Each job is a Python program**, not a model improvising. It issues kernel commands —
`read stdin;`, `write stdout ...;` — and the kernel decides when it runs and what it may
do. A program cannot cheat: naming a pipe its descriptor does not bind is refused at the
boundary. Three of the five never touch a model at all, and the two that do ask it one
question, which is what to say.

| Descriptor | Asks the model? | What for |
| --- | --- | --- |
| `converse` | yes, once per turn | composing the reply |
| `deep-research` | yes | the findings |
| `new-message` | no | — |
| `cancel` | no | — |
| `send-email` | no | — |

The two handlers do their whole work in their frontmatter. Being dispatched is what took
the machine away from whatever was running, and that is the entire job.

`deep-research` is dispatched by the conversation itself rather than from the page: when
you ask for something to be looked into, `converse` spawns it. It may do that because it
declares `deep-research` under `children:`, and that declaration is enforced by the kernel
— a job spawning anything it has not declared is a capability fault, not a missing check.

Nothing is passed to the child when it starts. It reads what it is for out of
`session.topic`, which the conversation wrote a moment earlier and which the kernel keeps
current in the child's window, so the brief survives however long the job runs.

**The model is a device on the end of a pipe.** A job that needs it writes a request and
blocks on the reply, exactly as it would for any device. That is what keeps the kernel
awake while the model thinks: the job is blocked, so the scheduler is free, a vector can
still fire and a reflex can still preempt. A chatbot that called the model from inside its
own loop is frozen for those seconds and can do none of this.

**The conversation's memory is the kernel's, not a list kept in Python.** `converse`
declares a context window, and when it fills, the kernel's pager evicts the coldest part
of the conversation and leaves a marker in its place. What the model can see shrinks
accordingly, and it is told plainly that something was discarded rather than left to
invent it.

**The interrupt table is where the behaviour a chat loop cannot have actually lives.**
Nothing in any descriptor body mentions interruption, cancellation, or checking whether a
message has arrived. That is the point: those are the kernel's business, and the
descriptors are free to be about holding a conversation.

One conversation at a time. A second browser tab is another window onto the same
conversation rather than a second one.
