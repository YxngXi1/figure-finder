/* Google tokens stay in this page's memory: never cookies or browser storage. */
"use strict";
const app = document.getElementById("app");
const choose = document.getElementById("choose");
const run = document.getElementById("run");
const disconnect = document.getElementById("disconnect");
const feedback = document.getElementById("feedback");
let accessToken = null, selected = null, tokenClient = null, busy = false, submitting = false, pollingStopped = false;

function tell(message, error = false) {
  feedback.textContent = message;
  feedback.classList.toggle("error", error);
}
function updateButtons() {
  run.disabled = busy || submitting || !selected || !accessToken;
  if (choose) choose.disabled = busy || submitting || !tokenClient;
  if (disconnect) disconnect.disabled = busy || submitting;
  document.getElementById("logout").disabled = busy || submitting;
}
async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {method: body === undefined ? "GET" : "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": app.dataset.csrf},
      body: body === undefined ? undefined : JSON.stringify(body)});
  } catch (_) { throw new Error("Connection lost. Keep this page open; status will retry shortly. Check your deck before starting another run."); }
  if (response.status === 401) pollingStopped = true;
  let data;
  try { data = await response.json(); }
  catch (_) { throw new Error("The server is unavailable. Try again shortly; check your deck before retrying a run."); }
  if (!response.ok) throw new Error(data.error || "The request could not finish.");
  return data;
}
function showPicker() {
  const view = new google.picker.DocsView(google.picker.ViewId.PRESENTATIONS)
    .setMimeTypes("application/vnd.google-apps.presentation");
  new google.picker.PickerBuilder().addView(view).setOAuthToken(accessToken)
    .setDeveloperKey(app.dataset.pickerKey).setAppId(app.dataset.project)
    .setOrigin(window.location.origin).setTitle("Choose a presentation to illustrate")
    .setCallback(data => {
      if (data.action === google.picker.Action.PICKED) {
        selected = data.docs[0];
        document.getElementById("selection").textContent = selected.name;
        tell("Ready. Click Add figures to edit this presentation.");
        updateButtons();
      }
    }).build().setVisible(true);
}
function setupGoogle() {
  gapi.load("picker", {callback: () => {
    tokenClient = google.accounts.oauth2.initTokenClient({
      client_id: app.dataset.clientId, scope: "https://www.googleapis.com/auth/drive.file",
      include_granted_scopes: false,
      callback: response => {
        if (response.error || !response.access_token || !google.accounts.oauth2.hasGrantedAllScopes(response, "https://www.googleapis.com/auth/drive.file")) {
          tell("Google access wasn’t granted. Choose your presentation to try again.", true); return;
        }
        accessToken = response.access_token;
        disconnect.hidden = false;
        showPicker();
      },
      error_callback: () => tell("Google sign-in was closed or blocked. Allow pop-ups and try again.", true)
    });
    choose.textContent = "Choose with Google";
    updateButtons();
  }, onerror: () => tell("Google’s picker could not load. Reload this page to try again.", true)});
}
if (choose) choose.addEventListener("click", () => {
  selected = null;
  document.getElementById("selection").textContent = "No presentation selected.";
  updateButtons();
  tokenClient.requestAccessToken({prompt: accessToken ? "" : "select_account"});
});
if (disconnect) disconnect.addEventListener("click", () => {
  google.accounts.oauth2.revoke(accessToken, result => {
    if (!result.successful) { tell("Google could not disconnect. Try again or remove access in your Google account connections.", true); return; }
    accessToken = null; selected = null; disconnect.hidden = true;
    document.getElementById("selection").textContent = "No presentation selected.";
    tell("Google disconnected."); updateButtons();
  });
});
run.addEventListener("click", async () => {
  submitting = true; updateButtons(); tell("Checking access and starting your run…");
  try {
    await api("/api/jobs", {access_token: accessToken, deck_id: selected.id,
      figures_per_slide: Number(document.getElementById("figures").value)});
    busy = true; tell("Working on your presentation. Keep this page open.");
    await refreshRuns();
  } catch (error) { tell(error.message, true); }
  finally { submitting = false; updateButtons(); }
});
function element(tag, text, className) {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}
function candidateDebug(slide, jobId) {
  const debug = slide.candidate_debug;
  const details = element("details", "", "candidate-debug");
  details.dataset.key = `${jobId}-candidates-${slide.number}${slide.index ? `-figure-${slide.index}` : ""}`;
  details.append(element("summary", `Debug: image candidates (${debug.total})`));
  details.append(element("p", debug.note, "muted"));
  if (slide.status !== "added") details.append(element("p",
    "No image was added to this slide. “Selected for layout” below means an image was chosen before placement or insertion.", "muted"));
  if (debug.comparison) {
    details.append(element("p", `Final AI comparison · preferred ${debug.winner_id}${debug.runner_up_id ? ` · runner-up ${debug.runner_up_id}` : ""}`));
    details.append(element("blockquote", debug.comparison));
  }
  const grid = element("div", "", "candidate-grid");
  debug.candidates.forEach(candidate => {
    const card = element("article", "", "candidate-option");
    if (candidate.status === "selected") card.classList.add("selected");
    card.append(element("p", candidate.status === "selected" ? (slide.status === "added" ? "Selected · added" : "Selected for layout") : candidate.status, "candidate-label"));
    if (candidate.preview?.startsWith("data:image/jpeg;base64,")) {
      const image = document.createElement("img");
      image.src = candidate.preview; image.alt = candidate.title; image.loading = "lazy";
      card.append(image);
    } else {
      card.append(element("p", "Preview unavailable — see source link below.", "muted"));
    }
    card.append(element("h4", candidate.title));
    card.append(element("p", `${candidate.id} · ${candidate.score === null ? "Not scored" : `${candidate.score.toFixed(1)}/100`}`, "muted"));
    card.append(element("p", candidate.reason));
    if (candidate.verdict) card.append(element("p", "AI assessment: " + candidate.verdict));
    if (candidate.missing.length) card.append(element("p", "Missing: " + candidate.missing.join(", ")));
    if (candidate.flags.length) card.append(element("p", "Flags: " + candidate.flags.join(", ")));
    if (candidate.caveat) card.append(element("p", "Caveat: " + candidate.caveat));
    if (Object.keys(candidate.scores).length) {
      const scores = element("details", "");
      scores.dataset.key = `${details.dataset.key}-${candidate.id}-scores`;
      scores.append(element("summary", "Score breakdown"));
      const list = element("ul", "");
      Object.entries(candidate.scores).forEach(([name, score]) =>
        list.append(element("li", `${name.replaceAll("_", " ")}: ${score}`)));
      scores.append(list); card.append(scores);
    }
    if (/^https?:\/\//i.test(candidate.source_url)) {
      const link = element("a", "Open image source ↗");
      link.href = candidate.source_url; link.target = "_blank"; link.rel = "noopener noreferrer";
      card.append(link);
    }
    grid.append(card);
  });
  details.append(grid);
  if (debug.total > debug.candidates.length)
    details.append(element("p", `Showing ${debug.candidates.length} of ${debug.total} candidates.`, "muted"));
  return details;
}
function drawRuns(jobs) {
  const container = document.getElementById("runs");
  // Polling must not collapse the debug section while someone is reading it.
  const openDetails = new Set(Array.from(container.querySelectorAll("details[open]"), node => node.dataset.key));
  container.replaceChildren();
  if (!jobs.length) container.append(element("p", "Your results will appear here.", "muted"));
  jobs.forEach(job => {
    const card = element("article", "", "run");
    card.append(element("p", job.status, "status " + job.status), element("h3", job.title));
    card.append(element("p", job.result ? `${job.result.inserted} figures added · ${job.result.skipped} slides skipped · ${job.result.errors} slide errors` : job.progress));
    const link = element("a", "Open presentation ↗");
    link.href = "https://docs.google.com/presentation/d/" + encodeURIComponent(job.deck_id) + "/edit";
    link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link);
    if (job.result) {
      const details = element("details", "");
      details.dataset.key = `${job.id}-slides`;
      details.append(element("summary", "Slide-by-slide results"));
      const list = element("ul", "");
      job.result.slides.forEach(slide => {
        const item = element("li", `Slide ${slide.number} · ${slide.status}: ${slide.status === "added" ? (slide.figures ? `${slide.inserted} figures added` : slide.caption) : slide.reason}`);
        if (slide.figures?.length) {
          slide.figures.forEach(figure => {
            const section = element("section", "", "figure-result");
            section.append(element("p", `Figure ${figure.index} · ${figure.status}: ${figure.status === "added" ? figure.caption : figure.reason}`));
            if (figure.candidate_debug) section.append(candidateDebug({...figure, number: slide.number}, job.id));
            item.append(section);
          });
        }
        else if (slide.candidate_debug) item.append(candidateDebug(slide, job.id));
        else item.append(element("p", "No candidate debug data was recorded for this slide.", "muted"));
        list.append(item);
      });
      details.append(list); card.append(details);
    }
    if (!["starting", "running"].includes(job.status)) {
      const clear = element("button", "Clear this run", "text-button");
      clear.addEventListener("click", async () => {
        try { await api(`/api/jobs/${job.id}/delete`, {}); await refreshRuns(); }
        catch (error) { tell(error.message, true); }
      });
      card.append(document.createElement("br"), clear);
    }
    container.append(card);
  });
  container.querySelectorAll("details").forEach(node => { node.open = openDetails.has(node.dataset.key); });
}
async function refreshRuns() {
  const data = await api("/api/jobs");
  busy = data.jobs.some(job => ["starting", "running"].includes(job.status));
  updateButtons(); drawRuns(data.jobs);
}
async function poll() {
  try { await refreshRuns(); } catch (error) { tell(error.message, true); }
  if (!pollingStopped) setTimeout(poll, busy ? 5000 : 30000);
}
window.addEventListener("load", () => {
  if (app.dataset.ready === "true") {
    if (window.gapi && window.google?.accounts?.oauth2) setupGoogle();
    else tell("Google could not load. Check your connection and reload the page.", true);
  }
  poll();
});
