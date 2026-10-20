// Minimal DOM harness verifies the real rendering code without network access.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.textContent = "";
    this.classList = {add() {}, toggle() {}};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren() { this.children = []; }
  addEventListener() {}
  querySelectorAll(selector) {
    const result = [];
    for (const child of this.children) {
      if (selector === "details" && child.tag === "details" ||
          selector === "details[open]" && child.tag === "details" && child.open) result.push(child);
      result.push(...child.querySelectorAll(selector));
    }
    return result;
  }
}
const nodes = Object.fromEntries(["app", "choose", "run", "disconnect", "feedback", "runs", "logout"]
  .map(id => [id, new Node("div")]));
const context = vm.createContext({
  document: {getElementById: id => nodes[id], createElement: tag => new Node(tag)},
  window: {addEventListener() {}}, setTimeout() {},
});
vm.runInContext(fs.readFileSync("figurefinder/beta_static/beta.js", "utf8"), context);
context.jobs = [{
  id: "job1", status: "done", title: "<script>bad()</script>", deck_id: "deck",
  result: {inserted: 0, skipped: 1, errors: 0, slides: [{
    number: 1, status: "skipped", reason: "No room", candidate_debug: {
      total: 1, note: "Recorded reasons", comparison: "Clear labels", winner_id: "a",
      candidates: [{id: "a", status: "selected", title: "<img onerror=bad()>",
        preview: "data:image/jpeg;base64,AA==", score: 81, reason: "Chosen",
        verdict: "", missing: [], flags: [], caveat: "", scores: {subject_match: 5},
        source_url: "javascript:bad()"}]
    }
  }]}
}];
vm.runInContext("drawRuns(jobs)", context);
function all(node) { return [node, ...node.children.flatMap(all)]; }
const rendered = all(nodes.runs);
assert(rendered.some(n => n.textContent === "Selected for layout"));
assert(rendered.some(n => n.textContent === "<img onerror=bad()>" && n.tag === "h4"));
assert(!rendered.some(n => n.tag === "a" && n.href === "javascript:bad()"));
assert(rendered.some(n => n.tag === "img" && n.src.startsWith("data:image/jpeg")));
const debug = nodes.runs.querySelectorAll("details").find(n => n.dataset.key === "job1-candidates-1");
debug.open = true;
vm.runInContext("drawRuns(jobs)", context);
assert(nodes.runs.querySelectorAll("details").find(n => n.dataset.key === "job1-candidates-1").open);
console.log("Debug UI: selection labels, safe rendering, previews and expanded state passed.");
const slide = context.jobs[0].result.slides[0];
slide.status = "added"; slide.inserted = 2;
slide.figures = [1, 2].map(index => ({
  index, status: "added", caption: `Figure ${index}: Heart, labelled view.`,
  candidate_debug: slide.candidate_debug
}));
vm.runInContext("drawRuns(jobs)", context);
const keys = nodes.runs.querySelectorAll("details").map(node => node.dataset.key);
assert(keys.includes("job1-candidates-1-figure-1"));
assert(keys.includes("job1-candidates-1-figure-2"));
assert.equal(keys.length, new Set(keys).size);
assert(all(nodes.runs).some(node => node.textContent === "Slide 1 · added: 2 figures added"));
console.log("Multiple figures retain independent debug panels and per-slide counts.");
