// The page. It holds no state about the conversation beyond what it has been told:
// every message, including the one you just typed, arrives back over the event stream.
// That keeps one source of truth -- the kernel -- and means a second tab watching the
// same conversation sees exactly what this one does.

const messages = document.getElementById("messages");
const kernel = document.getElementById("kernel");
const input = document.getElementById("input");
const composer = document.getElementById("composer");
const stop = document.getElementById("stop");
const logo = document.querySelector(".logo");
const empty = document.getElementById("empty");
const panel = document.getElementById("panel");
const toggle = document.getElementById("panel-toggle");
const emailButton = document.getElementById("email");
const task = document.getElementById("task");
const speaker = document.getElementById("speaker");

let lastWasMine = null;
// The reply being written, if one is. An answer arrives as a stream of small writes, so
// they belong in one bubble that fills rather than a bubble each.
//: The bubble each speaking job is currently filling, by job. Two jobs write into this
//: transcript at once, so there is not one "current" bubble.
const open = new Map();

function bubble(text, mine) {
  empty.hidden = true;
  const li = document.createElement("li");
  li.className = mine ? "mine" : "theirs";
  if (lastWasMine === mine) li.classList.add("grouped");
  lastWasMine = mine;

  const div = document.createElement("div");
  div.className = "bubble";
  div.appendChild(document.createElement("p")).textContent = text;
  li.appendChild(div);
  messages.appendChild(li);
  li.scrollIntoView({ block: "end", behavior: "smooth" });
  return div;
}

// The model marks paragraph breaks with this rather than a newline, because a newline
// does not survive the trip through a pipe -- the seat splits a command on whitespace.
const PARAGRAPH = "|";

//: One open bubble per speaking job, because two jobs write into this transcript at the
//: same time. Keyed rather than single: with one `open` bubble the long job's findings and
//: the conversation's answer landed in whichever was current, and an answer that arrived
//: during research was buried inside it. Closing on every change of speaker was no better
//: -- it chopped a four-word answer into four bubbles interleaved with the research.
function say(chunk, job) {
  const who = job || "";
  if (!open.has(who)) open.set(who, bubble("", false));
  const into_bubble = open.get(who);
  const atEnd = Math.abs(
    messages.parentElement.scrollHeight - messages.parentElement.scrollTop
      - messages.parentElement.clientHeight) < 80;

  for (const [i, part] of chunk.split(PARAGRAPH).entries()) {
    if (i > 0) into_bubble.appendChild(document.createElement("p"));
    const into = into_bubble.lastElementChild;
    const words = part.trim();
    if (!words) continue;
    into.textContent = into.textContent ? `${into.textContent} ${words}` : words;
  }
  // Follow the reply only if already at the bottom, so reading back is not yanked away
  // by every word.
  if (atEnd) into_bubble.scrollIntoView({ block: "end" });
}

//: Every open bubble is closed, so what comes next starts fresh. A job still streaming
//: simply opens a new one, which reads as the work continuing rather than as one bubble
//: growing across somebody else's turn.
function endTurn() {
  open.clear();
}

//: What a mark says. The transcript shows the *consequence* -- the panel next to it shows
//: the vector that fired and the job it preempted, which is the structural version.
const MARKS = {
  "barge-in": "interrupted — you spoke while the answer was still arriving",
  stopped: "stopped — the rest was not sent",
  refused: "refused — the job reached the write and did not hold the capability for it",
};

function mark(which, text) {
  empty.hidden = true;
  endTurn();
  const li = document.createElement("li");
  li.className = `mark ${which}`;
  li.textContent = text || MARKS[which] || which;
  messages.appendChild(li);
  // A mark ends any grouping: what comes next is a new turn, not a continuation.
  lastWasMine = null;
  li.scrollIntoView({ block: "end", behavior: "smooth" });
}

//: What a press will actually do. Held because the button has to say so beforehand: a
//: draft you confirm and a mail that has already gone are not the same promise.
let mailer = { ready: false, mode: "simulated", to: "" };

//: Only the mode that really sends is set apart. A draft opening in your own mail client
//: is not a consequential effect until you press Send in it.
const MAIL_TITLES = {
  desktop: "Writes mail.outbox — opens a draft in your mail client, nothing is sent",
  simulated: "Writes mail.outbox — simulated on this run, nothing is sent",
};

function describeMailer() {
  if (!mailer.ready) {
    emailButton.disabled = true;
    emailButton.title = "no outbox on this run";
    return;
  }
  emailButton.disabled = false;
  emailButton.title =
    MAIL_TITLES[mailer.mode] || `Writes mail.outbox — really sends to ${mailer.to}`;
  // Solid green is the working default; `simulated` recedes and `live` takes the accent.
  emailButton.classList.toggle("simulated", mailer.mode === "simulated");
  emailButton.classList.toggle("live", mailer.mode === "live");
}

//: What became of a letter, in the transcript rather than only in the journal. The write
//: to `mail.outbox` is the one consequential effect in the system, so its outcome belongs
//: where a person is looking -- including when it was refused.
function posted(to, subject, outcome) {
  empty.hidden = true;
  endTurn();
  const li = document.createElement("li");
  li.className = "mark mail";
  // The arrow only when there is somewhere for it to point: an unconfigured run has no
  // address, and "→  —" reads as a missing value rather than a missing setting.
  li.textContent = to
    ? `mail — ${subject} → ${to} — ${outcome}`
    : `mail — ${subject} — ${outcome}`;
  messages.appendChild(li);
  lastWasMine = null;
  li.scrollIntoView({ block: "end", behavior: "smooth" });
}

//: The conversation as text, for the body of a letter. Read off the page rather than kept
//: alongside it: what the person asked to send is what they can see.
function transcript() {
  return Array.from(messages.children)
    .map((li) => {
      if (li.classList.contains("mark")) return `--- ${li.textContent} ---`;
      const who = li.classList.contains("mine") ? "you" : "assistant";
      return `${who}: ${li.textContent}`;
    })
    .join("\n\n");
}

//: What the background jobs are doing, one line each, and a line saying so when there are
//: none. No spinner on the running lines: the logo already animates for exactly as long as
//: a job is outstanding, and two things reporting the same fact is one more than is useful.
const NO_JOBS = "(no active research jobs)";

function showTasks(list) {
  const running = (list || []).filter((t) => t && t !== "none");
  task.textContent = "";
  task.classList.toggle("idle", running.length === 0);
  for (const what of running.length ? running : [NO_JOBS]) {
    const line = document.createElement("span");
    line.className = "job";
    line.textContent = what;
    task.appendChild(line);
  }
}

//: A finished report, as a document in the transcript rather than as speech.
//:
//: The long job's findings are pages long and answer something asked many turns ago.
//: Streamed into the conversation they bury whatever it is doing and read as though the
//: chatbot had started rambling. As a file they are what they are: a thing that was
//: produced, which you open when you want it.
function offerReport(id, subject, words) {
  empty.hidden = true;
  // Deliberately *not* closing the open bubbles. A document appearing is not the
  // conversation taking a turn, and closing them split an answer that was still arriving
  // into two halves either side of the document.
  const li = document.createElement("li");
  li.className = "theirs report";

  const card = document.createElement("div");
  card.className = "bubble document";

  const head = document.createElement("button");
  head.className = "document-head";
  head.type = "button";
  head.setAttribute("aria-expanded", "false");
  head.innerHTML =
    '<svg class="doc" viewBox="0 0 24 24" aria-hidden="true">' +
    '<path d="M6 2h8l4 4v16H6z"/><path class="fold" d="M14 2v5h5"/>' +
    '<path class="rule" d="M9 12h7M9 15h7M9 18h4"/></svg>';
  const label = document.createElement("span");
  label.className = "document-name";
  label.textContent = subject || "research";
  const size = document.createElement("span");
  size.className = "document-size";
  size.textContent = `${words} words`;
  head.append(label, size);

  const body = document.createElement("pre");
  body.className = "document-body";
  body.hidden = true;

  head.addEventListener("click", async () => {
    const open = body.hidden;
    head.setAttribute("aria-expanded", String(open));
    if (open && !body.textContent) {
      body.textContent = "opening…";
      try {
        body.textContent = await (await fetch(`/report/${id}`)).text();
      } catch (e) {
        body.textContent = `could not be opened: ${e}`;
      }
    }
    body.hidden = !open;
  });

  card.append(head, body);
  li.appendChild(card);
  messages.appendChild(li);
  lastWasMine = null;
  li.scrollIntoView({ block: "end", behavior: "smooth" });
}

function journal(kind, text) {
  // A streamed reply is one write per chunk, so the same line arrives many times over.
  // Folding them keeps the panel readable without hiding that they happened.
  const last = kernel.lastElementChild;
  if (last && last.dataset.text === text) {
    last.dataset.count = String(Number(last.dataset.count || 1) + 1);
    last.textContent = `${text}  ×${last.dataset.count}`;
    return;
  }
  const li = document.createElement("li");
  li.className = kind;
  li.dataset.text = text;
  li.textContent = text;
  kernel.appendChild(li);
  // Only follow the tail when already at it, so reading back through the journal is not
  // yanked away every time a job blocks.
  const atEnd = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 40;
  if (atEnd) li.scrollIntoView({ block: "end" });
}

const stream = new EventSource("/events");
stream.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.kind === "mark") mark(msg.mark, msg.text);
  else if (msg.kind === "said") { endTurn(); bubble(msg.text, true); }
  else if (msg.kind === "reply") say(msg.text, msg.job);
  else if (msg.kind === "kernel") journal(msg.class, msg.text);
  else if (msg.kind === "mailer") { mailer = msg; describeMailer(); }
  else if (msg.kind === "mail") posted(msg.to, msg.subject, msg.outcome);
  else if (msg.kind === "task") showTasks(msg.tasks);
  else if (msg.kind === "report") offerReport(msg.id, msg.subject, msg.words);
  else if (msg.kind === "busy") {
    // The logo animates exactly while the system has work outstanding.
    logo.classList.toggle("busy", msg.busy);
    stop.hidden = !msg.busy;
    // Work finished means the turn finished: the next reply starts a new bubble.
    if (!msg.busy) endTurn();
  }
};

async function post(path, body) {
  await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  input.style.height = "auto";
  // Not appended here: it comes back over the stream, like everything else.
  await post("/say", { text });
});

// Enter sends, shift-enter makes a line. The point of the demonstration is interrupting
// mid-answer, so sending must be as quick as it is in any other chatbot.
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 128) + "px";
});

stop.addEventListener("click", () => post("/stop"));

// A click is a device event, which is the whole of what this does: it writes the request
// to `mail.requests` and the kernel decides the rest. Nothing here checks whether mail is
// configured or whether this conversation is allowed to send -- that check is at the
// pipe, and deciding it here in advance would be deciding the thing it exists to decide.
emailButton.addEventListener("click", async () => {
  const body = transcript();
  if (!body) return;
  const first = messages.querySelector("li.mine");
  await post("/email", {
    subject: first ? first.textContent.slice(0, 60) : "Your ZEOS Chat conversation",
    body,
    speaker: speaker.value,
  });
});

toggle.addEventListener("click", () => {
  panel.hidden = !panel.hidden;
  toggle.setAttribute("aria-expanded", String(!panel.hidden));
});

describeMailer();
showTasks([]);
input.focus();
