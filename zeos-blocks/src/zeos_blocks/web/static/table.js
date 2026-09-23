// The page: draws the table, animates a move, shows the kernel's record.
//
// It draws from the *world state the kernel holds*, pushed on every change, rather than
// from anything the arm reports. That is deliberate. A page fed by the arm would be
// drawing the arm's account of the world, which is the one thing this demonstration
// argues against, and it would be blind to any change the arm did not cause. Drawing from
// the world means the picture and the stacker are looking at the same thing.

const COLOURS = { r: "red", g: "green", b: "blue", y: "yellow", o: "orange", p: "purple" };
const EMPTY = "-";

// How a move is drawn. The arm's own moves are the common case; a hand is slower, travels
// higher and tilts, because it is the thing worth noticing.
//
// `gap` is the daylight left between the *underside* of the block in flight and the top of
// the tallest stack it passes over. It is a gap and not the whole clearance: a block's own
// height is added in `travel`, because a transform positions a block by its top edge and
// leaving that out is what made a block skim through the stack it was crossing.
//
// A move is three phases, and `up` and `over` are the points in the animation where each
// ends: straight up, across at height, straight down.
const ARM = { duration: 760, gap: 18, tilt: 0, up: 0.3, over: 0.72 };
const HAND = { duration: 960, gap: 40, tilt: 4, up: 0.28, over: 0.7 };
// A block that did not move but is displaced because something under it did. No phases:
// it did not go anywhere, the floor moved under it.
const SETTLE = { duration: 360, easing: "cubic-bezier(.32,.78,.4,1)" };

const el = {
  table: document.getElementById("table"),
  transcript: document.getElementById("transcript"),
  journal: document.getElementById("journal"),
  panel: document.getElementById("panel"),
  toggle: document.getElementById("panel-toggle"),
  form: document.getElementById("say-form"),
  say: document.getElementById("say"),
  clear: document.getElementById("clear"),
  planner: document.getElementById("planner"),
  tidy: document.getElementById("tidy"),
  activity: document.getElementById("activity"),
  stopped: document.getElementById("stopped"),
  activityText: document.getElementById("activity-text"),
};

const still = window.matchMedia("(prefers-reduced-motion: reduce)");

// The last layout drawn. A move is found by diffing the new one against this, which is
// what lets a single block be animated rather than the whole table redrawn blankly.
let drawn = {};

// One element per block, kept for the life of the page.
//
// Rebuilding them on every update was the thing that made the motion look wrong: a fresh
// element has no history, so a block could only ever be faded in at its destination, and
// an animation still running when the next update arrived was thrown away mid-flight.
// Reusing the element means it can be *moved* between stacks, and `appendChild` on a node
// that is already in the document relocates it without disturbing anything running on it.
const nodes = new Map();

function parse(stacks) {
  const out = {};
  for (const [position, contents] of Object.entries(stacks)) {
    out[position] = (contents === EMPTY || contents === "(unset)")
      ? []
      : contents.split(",").filter(Boolean);
  }
  return out;
}

function placement(layout) {
  const at = {};
  for (const [position, blocks] of Object.entries(layout)) {
    blocks.forEach((b) => { at[b] = position; });
  }
  return at;
}

// Which single block changed position between two layouts.
//
// One move at a time is a rule of the table, not an assumption about timing: a block
// leaves one position and arrives at another with nothing observable in between. So if
// more than one block has moved, the page has been shown a state it was not told to
// expect, and it settles into it rather than inventing a journey it cannot describe.
function moved(before, after) {
  const was = placement(before);
  const now = placement(after);
  const changed = Object.keys(now).filter((b) => was[b] && was[b] !== now[b]);
  return changed.length === 1 ? changed[0] : null;
}

function blockNode(block) {
  let node = nodes.get(block);
  if (node) return node;
  node = document.createElement("div");
  node.className = `block ${COLOURS[block[0]] || ""}`;
  node.textContent = block;
  node.draggable = true;
  node.dataset.block = block;
  // Attached once, with the element, rather than re-bound on every redraw.
  //
  // Picking a block up stops the clock; it does not interrupt anything, because nothing
  // has happened yet. The interrupt is on the *drop*, where the world actually changes.
  node.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", block);
    dropped = false;
    post("/hand/start", { block });
  });
  // Fires after `drop`, and also when a drag is abandoned over nothing. The hand has to be
  // seen to leave either way: the clock is stopped while one is in the workspace, so a
  // drag nobody finished would stop the workspace for good.
  node.addEventListener("dragend", () => {
    if (!dropped) post("/hand/cancel", {});
  });
  nodes.set(block, node);
  return node;
}

function draw(stacks, by) {
  const layout = parse(stacks);
  const shifted = Object.keys(drawn).length ? moved(drawn, layout) : null;
  // `by` says whether the arm asked for this or somebody reached in. The journal cannot
  // answer that: both are device deliveries and both are recorded with no job against
  // them, so the server, which made the delivery, says so instead.
  const hand = Boolean(shifted) && by === "hand";

  // FIRST. Where everything is on screen, before the layout changes under it.
  const before = new Map();
  for (const [block, node] of nodes) {
    if (node.isConnected) before.set(block, node.getBoundingClientRect());
  }

  el.table.replaceChildren();
  for (const position of Object.keys(layout).sort()) {
    const wrap = document.createElement("div");
    wrap.className = "position-wrap";
    const slot = document.createElement("div");
    slot.className = "position";
    slot.dataset.position = position;
    for (const block of layout[position]) {
      const node = blockNode(block);
      node.classList.toggle("by-hand", hand && block === shifted);
      slot.appendChild(node);
    }
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = position;
    wrap.append(slot, label);
    el.table.appendChild(wrap);
  }

  // LAST, INVERT and PLAY, for everything that ended up somewhere new.
  if (!still.matches) {
    // The top of the tallest stack, in screen coordinates, taken over both the layout
    // being left and the one being arrived at. A block crossing the table has to clear
    // whatever is standing in between, not merely the two stacks it touches, and a stack
    // it passes over may be about to shrink or grow.
    const tops = [...nodes.values()]
      .filter((node) => node.isConnected)
      .map((node) => node.getBoundingClientRect().top)
      .concat([...before.values()].map((rect) => rect.top));
    const ceiling = tops.length ? Math.min(...tops) : 0;

    for (const [block, node] of nodes) {
      if (!node.isConnected) continue;
      const was = before.get(block);
      if (!was) continue;
      const now = node.getBoundingClientRect();
      const dx = was.left - now.left;
      const dy = was.top - now.top;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;
      if (block === shifted) travel(node, dx, dy, ceiling - now.top, hand ? HAND : ARM);
      else settle(node, dx, dy);
    }
  }

  drawn = layout;
  wireSlots();
}

// The moved block, going the way an arm would take it: straight up off its stack, across
// at height, then straight down onto the new one.
//
// Three phases rather than one curve, because that is what the motion *is*. A block
// travelling diagonally passes through whatever it is leaving and whatever it is arriving
// at; an arc avoids that only by accident of where the two stacks happen to stand. Going
// over the top avoids it by construction.
//
// `ceiling` is the top of the tallest stack, relative to where this block ends up, so the
// crossing height clears everything on the table rather than the two ends of the journey.
//
// The block's own height goes into that sum. A transform moves a block by its top edge, so
// raising the top to `ceiling - gap` leaves the *bottom* hanging a block's height lower,
// which is below the top of the stack being crossed: the block skimmed through it. What
// has to clear the stack is the underside, so the height of the block is part of the lift
// rather than something to leave out of it.
//
// Each phase eases in and out, so the block visibly stops at both corners. One easing
// spread over the whole path would round them off and put the arc back.
function travel(node, dx, dy, ceiling, style) {
  const corner = "cubic-bezier(.45,.02,.35,1)";
  const lift = node.getBoundingClientRect().height + style.gap;
  const height = Math.min(0, dy, ceiling) - lift;
  play(node, [
    { transform: `translate(${dx}px, ${dy}px) rotate(${-style.tilt}deg)`, easing: corner },
    { offset: style.up,
      transform: `translate(${dx}px, ${height}px) rotate(${-style.tilt}deg)`, easing: corner },
    { offset: style.over,
      transform: `translate(0px, ${height}px) rotate(${style.tilt}deg)`, easing: corner },
    { transform: "translate(0, 0) rotate(0deg)" },
  ], style.duration);
}

// A block displaced because something beneath it moved. No arc: it did not go anywhere,
// the floor moved under it.
function settle(node, dx, dy) {
  play(node, [
    { transform: `translate(${dx}px, ${dy}px)`, easing: SETTLE.easing },
    { transform: "translate(0, 0)" },
  ], SETTLE.duration);
}

function play(node, frames, duration) {
  // Transforms and opacity are the two things a browser can animate off the main thread.
  // `will-change` is set for the flight and cleared afterwards rather than left on, so a
  // table of forty blocks is not holding forty compositor layers while nothing moves.
  node.style.willChange = "transform";
  const animation = node.animate(frames, { duration, fill: "none" });
  animation.finished
    .then(() => { node.style.willChange = ""; })
    .catch(() => { node.style.willChange = ""; });
}

// -- being the hand --------------------------------------------------------
//
// A drag posts to /hand, which goes through the same Session.disturb a scheduled
// disturbance goes through, which delivers to the same actuators the arm writes to. One
// path for a hand, whoever the hand belongs to.

// Whether the drag in progress ended on a stack. Read by `dragend`, which cannot tell.
let dropped = false;

function wireSlots() {
  el.table.querySelectorAll(".position").forEach((slot) => {
    slot.addEventListener("dragover", (e) => { e.preventDefault(); slot.classList.add("over"); });
    slot.addEventListener("dragleave", () => slot.classList.remove("over"));
    slot.addEventListener("drop", async (e) => {
      e.preventDefault();
      slot.classList.remove("over");
      const block = e.dataTransfer.getData("text/plain");
      dropped = true;
      const reply = await post("/hand", { block, to: slot.dataset.position });
      line("hand", reply.said, "hand");
    });
  });
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return response.json();
}

// The jobs that have not finished, named, with the dot pulsing beside them.
//
// Absent entirely when there are none, which is how the workspace starts: this case boots
// nothing, so an untouched page shows no indicator at all rather than an idle one.
function activity({ live }) {
  el.activity.hidden = live.length === 0;
  // Asking twice would start a second `tidy`, and the two would share one arm and one
  // report and each reason about the other's refusals. The kernel does not stop this: a
  // descriptor binds pipes by literal name, so a second instance gets the same ones. The
  // button going quiet is the page being careful, not the system refusing.
  el.tidy.disabled = live.includes("tidy");
  if (live.length === 0) return;
  el.activityText.textContent = live.join(", ");
}

// -- what the person sees --------------------------------------------------

function line(who, text, kind) {
  const item = document.createElement("li");
  if (kind) item.className = `said-${kind}`;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who;
  item.append(label, document.createTextNode(text));
  el.transcript.appendChild(item);
  keepAtBottom(el.transcript);
}

// Scroll the list itself, never the page. `scrollIntoView` walks every scrollable
// ancestor including the document, so a new line would drag the table out of view, and
// the table is the thing worth watching. Only follow if the reader is already at the
// bottom, so scrolling back to read something is not undone by the next line.
function keepAtBottom(list) {
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  if (atBottom) list.scrollTop = list.scrollHeight;
}

// The tag is prefixed rather than used as a bare class name. A journal entry for a
// blocked job arrives tagged "block", and `.block` is the table's own style: 96px wide,
// 44px tall and grid-centred, so the entry was drawn as a coloured brick with its text
// wrapping out of the box and over the line beneath it.
function journal(tag, text) {
  const item = document.createElement("li");
  item.className = `ev-${tag}`;
  item.textContent = text;
  el.journal.appendChild(item);
  if (el.journal.children.length > 400) el.journal.firstChild.remove();
  keepAtBottom(el.journal);
}

el.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = el.say.value.trim();
  if (!text) return;
  line("you", text, "said");
  el.say.value = "";
  await post("/say", { text });
});

// A control, and deliberately not a sentence. The textbox is a front door: whatever goes
// through it is compiled, and the one phrasing this case declares matches everything, so
// anything typed there reaches the planner. `tidy` declares no phrasing at all, which is
// what makes it unreachable by anything anybody can type -- so the only way to ask for it
// is to ask for it by name, which is what a button is.
el.tidy.addEventListener("click", () => {
  line("you", "tidy up", "said");
  post("/tidy", {});
});

el.toggle.addEventListener("click", () => { el.panel.hidden = !el.panel.hidden; });

// Clears what is on screen and nothing else. The kernel's journal is the kernel's, it is
// still being written, and the panel keeps showing it; this only empties the reader's
// view of what has happened so far.
el.clear.addEventListener("click", () => {
  el.transcript.replaceChildren();
  el.say.focus();
});

const stream = new EventSource("/stream");
stream.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.kind === "table") draw(message.stacks, message.by);
  else if (message.kind === "reply") line("system", message.text);
  else if (message.kind === "command") line(message.who, message.text);
  else if (message.kind === "journal") journal(message.tag, message.text);
  else if (message.kind === "activity") activity(message);
  // Sent once, on connect, and never again: what decides a move does not change
  // while a page is open.
  else if (message.kind === "stopped") {
    el.stopped.textContent = `The workspace stopped: ${message.text}`;
    el.stopped.hidden = false;
    el.activity.hidden = true;
  }
  else if (message.kind === "planner") {
    el.planner.textContent = message.label;
    el.tidy.hidden = !message.tidy;
  }
};
