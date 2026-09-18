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

let lastWasMine = null;
// The reply being written, if one is. An answer arrives as a stream of small writes, so
// they belong in one bubble that fills rather than a bubble each.
let open = null;

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

function say(chunk) {
  if (open === null) open = bubble("", false);
  const atEnd = Math.abs(
    messages.parentElement.scrollHeight - messages.parentElement.scrollTop
      - messages.parentElement.clientHeight) < 80;

  for (const [i, part] of chunk.split(PARAGRAPH).entries()) {
    if (i > 0) open.appendChild(document.createElement("p"));
    const into = open.lastElementChild;
    const words = part.trim();
    if (!words) continue;
    into.textContent = into.textContent ? `${into.textContent} ${words}` : words;
  }
  // Follow the reply only if already at the bottom, so reading back is not yanked away
  // by every word.
  if (atEnd) open.scrollIntoView({ block: "end" });
}

function endTurn() {
  open = null;
}

//: What a mark says. The transcript shows the *consequence* -- the panel next to it shows
//: the vector that fired and the job it preempted, which is the structural version.
const MARKS = {
  "barge-in": "interrupted — you spoke while the answer was still arriving",
  stopped: "stopped — the rest was not sent",
};

function mark(which) {
  empty.hidden = true;
  endTurn();
  const li = document.createElement("li");
  li.className = `mark ${which}`;
  li.textContent = MARKS[which] || which;
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
  if (msg.kind === "mark") mark(msg.mark);
  else if (msg.kind === "said") { endTurn(); bubble(msg.text, true); }
  else if (msg.kind === "reply") say(msg.text);
  else if (msg.kind === "kernel") journal(msg.class, msg.text);
  else if (msg.kind === "mailer") { mailer = msg; describeMailer(); }
  else if (msg.kind === "mail") posted(msg.to, msg.subject, msg.outcome);
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
  });
});

toggle.addEventListener("click", () => {
  panel.hidden = !panel.hidden;
  toggle.setAttribute("aria-expanded", String(!panel.hidden));
});

describeMailer();
input.focus();
