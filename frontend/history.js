/* Anonymous browser history; credentials go only to our own history endpoints. */
(() => {
  const panel = document.querySelector("#panel-plan");
  if (!panel) return;
  const section = document.createElement("section");
  section.className = "field history";
  section.setAttribute("aria-label", "Saved hikes");
  section.innerHTML = `<label class="label" for="saved-hikes">My saved hikes</label>
    <select id="saved-hikes"><option value="">Choose a saved hike…</option></select>
    <div class="field-row"><button type="button" class="btn btn--small" id="save-hike" disabled>Save current hike</button>
    <button type="button" class="btn btn--small" id="refresh-hikes">Refresh history</button></div>
    <p class="source-line" id="snapshot-notice" hidden></p>
    <p class="status" role="status" id="history-status"></p>`;
  panel.prepend(section);
  const select = section.querySelector("select");
  const save = section.querySelector("#save-hike");
  const refresh = section.querySelector("#refresh-hikes");
  const status = section.querySelector("#history-status");
  const notice = section.querySelector("#snapshot-notice");
  let current = null;
  let revision = 0;
  let listSeq = 0;
  let saving = false;
  let token;
  try {
    token = localStorage.getItem("trekchek-history-token");
    if (!/^[a-f0-9]{64}$/.test(token || "")) {
      token = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, "0")).join("");
      localStorage.setItem("trekchek-history-token", token);
    }
  } catch {
    status.textContent = "Enable browser storage to use saved hikes.";
    refresh.disabled = true;
    select.disabled = true;
    return;
  }
  const canSave = () => !!current && !saving;

  async function api(path = "", options = {}) {
    const response = await fetch(`/history${path}`, {
      ...options, headers: { "Content-Type": "application/json", "X-History-Token": token },
    });
    let body;
    try { body = await response.json(); } catch { throw new Error("History request failed. Try again."); }
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "History request failed.");
    return body;
  }

  async function loadList() {
    const seq = ++listSeq;
    let data;
    try { data = await api(); } catch (error) {
      if (seq !== listSeq) return null;
      throw error;
    }
    if (seq !== listSeq) return null;
    const selected = select.value;
    select.replaceChildren(new Option("Choose a saved hike…", ""));
    for (const item of data.items) {
      select.add(new Option(`${item.trail_name} · ${new Date(item.saved_at).toLocaleString()}`, item.id));
    }
    select.value = selected;
    return data.items.length;
  }

  window.addEventListener("trekchek:loading", () => {
    revision++;
    current = null;
    save.disabled = true;
    notice.hidden = true;
    status.textContent = "Loading hike…";
  });
  window.addEventListener("trekchek:plan", event => {
    revision++;
    current = event.detail.mock || !event.detail.plan?.trail?.coords?.length ? null : event.detail.plan;
    save.disabled = !canSave();
    select.value = "";
    const savedAt = event.detail.savedAt;
    notice.hidden = !savedAt;
    notice.textContent = savedAt
      ? `Saved snapshot from ${new Date(savedAt).toLocaleString()}. Weather and warnings have not been refreshed. Generate a new plan before hiking.` : "";
    status.textContent = "Save a snapshot to reopen it in this browser.";
  });

  save.addEventListener("click", async () => {
    if (!canSave()) return;
    const snapshot = current;
    const rev = revision;
    saving = true;
    save.disabled = true;
    status.textContent = "Saving to Atlas…";
    try {
      const result = await api("", { method: "POST", body: JSON.stringify({ plan: snapshot }) });
      if (rev === revision) status.textContent = `Saved to Atlas at ${new Date(result.saved_at).toLocaleString()}.`;
      try { await loadList(); } catch {
        if (rev === revision) status.textContent += " Refresh history to reload the list.";
      }
    } catch (error) { if (rev === revision) status.textContent = error.message; }
    finally { saving = false; save.disabled = !canSave(); }
  });

  select.addEventListener("change", async () => {
    const id = select.value;
    if (!id) return;
    const seq = ++requestSeq;
    setLoading(true, "history");
    status.textContent = "Loading saved hike…";
    try {
      const saved = await api(`/${encodeURIComponent(id)}`);
      if (seq !== requestSeq) return;
      els.request.value = saved.plan.request || "";
      renderPlan(saved.plan, false, saved.saved_at);
      select.value = id;
      status.textContent = "Saved hike reopened.";
    } catch (error) {
      if (seq === requestSeq) { status.textContent = error.message; showError(error.message); }
    } finally { if (seq === requestSeq) setLoading(false); }
  });

  async function refreshList() {
    const rev = revision;
    refresh.disabled = true;
    try {
      const count = await loadList();
      if (count !== null && rev === revision) status.textContent = count
        ? `${count} recent saved hikes in this browser.` : "No saved hikes yet. Generate a plan, then save it.";
    } catch (error) { if (rev === revision) status.textContent = error.message; }
    finally { refresh.disabled = false; }
  }
  refresh.addEventListener("click", refreshList);
  refreshList();
})();
