const state = {
  days: 15,
  status: "all",
  q: "",
};

const $ = (id) => document.getElementById(id);

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function renderActivity(items) {
  const list = $("activity");
  if (!list) return;
  if (!items || !items.length) {
    list.innerHTML = "<li>No extraction output yet. Click Start to process PDFs already in invoices.</li>";
    return;
  }
  list.innerHTML = items
    .slice(0, 12)
    .map((item) => `<li class="${escapeHtml(item.kind || "")}">${escapeHtml(item.message)}</li>`)
    .join("");
}

function setWatcher(info) {
  const pill = $("watcher-pill");
  const hint = $("watcher-hint");
  pill.textContent = info.running ? "watching" : "offline";
  pill.classList.toggle("on", Boolean(info.running));
  $("btn-start").disabled = Boolean(info.running);
  $("btn-stop").disabled = !info.running;
  if (info.invoices_dir) {
    hint.textContent = info.running
      ? `Watching ${info.invoices_dir}. Existing and new PDFs are ingested automatically.`
      : `Drop PDFs into ${info.invoices_dir}, or upload here. Click Start to process files already in that folder.`;
  }
  if (info.message) hint.textContent = info.message;
  renderActivity(info.activity);
}

function renderStats(stats) {
  $("stat-total").textContent = stats.total ?? 0;
  $("stat-expired").textContent = stats.expired ?? 0;
  $("stat-expiring").textContent = stats.expiring ?? 0;
  $("stat-healthy").textContent = stats.healthy ?? 0;
}

function renderRows(products) {
  const body = $("rows");
  if (!products.length) {
    body.innerHTML =
      '<tr><td colspan="4" class="empty">No products in this view. Upload an invoice PDF or start the folder watcher.</td></tr>';
    return;
  }

  body.innerHTML = products
    .map((p) => {
      const updated = String(p.last_updated || "").replace("T", " ").slice(0, 19);
      return `<tr>
        <td>${escapeHtml(p.product_name)}</td>
        <td>${escapeHtml(p.expiry_date)}</td>
        <td>${escapeHtml(updated)}</td>
        <td><span class="badge ${p.status}">${escapeHtml(p.status_label)}</span></td>
      </tr>`;
    })
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function refresh() {
  const days = Number($("days").value || 15);
  state.days = Number.isFinite(days) && days >= 0 ? days : 15;
  const params = new URLSearchParams({
    days: String(state.days),
    status: state.status,
    q: state.q,
  });
  const [stats, inventory, watcher] = await Promise.all([
    api(`/api/stats?days=${state.days}`),
    api(`/api/inventory?${params}`),
    api("/api/watcher"),
  ]);
  renderStats(stats);
  renderRows(inventory.products);
  setWatcher(watcher);
}

async function onUpload(file) {
  if (!file) return;
  const status = $("upload-status");
  status.textContent = `Reading ${file.name}…`;
  const body = new FormData();
  body.append("file", file);
  try {
    const result = await api("/api/upload", { method: "POST", body });
    status.textContent = result.message
      || `Stored ${result.stored}/${result.extracted} products from ${result.saved_as}.`;
    await refresh();
  } catch (err) {
    status.textContent = err.message;
  }
}

$("btn-start").addEventListener("click", async () => {
  try {
    setWatcher(await api("/api/watcher/start", { method: "POST" }));
    await refresh();
  } catch (err) {
    $("watcher-hint").textContent = err.message;
  }
});

$("btn-stop").addEventListener("click", async () => {
  try {
    setWatcher(await api("/api/watcher/stop", { method: "POST" }));
  } catch (err) {
    $("watcher-hint").textContent = err.message;
  }
});

$("btn-refresh").addEventListener("click", () => refresh().catch(console.error));
$("days").addEventListener("change", () => refresh().catch(console.error));
$("search").addEventListener(
  "input",
  debounce(() => {
    state.q = $("search").value.trim();
    refresh().catch(console.error);
  }, 200),
);

document.querySelectorAll("#filters .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#filters .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.status = chip.dataset.status;
    refresh().catch(console.error);
  });
});

document.querySelectorAll(".stat").forEach((card) => {
  card.addEventListener("click", () => {
    const status = card.dataset.filter;
    const chip = document.querySelector(`#filters .chip[data-status="${status}"]`);
    if (chip) chip.click();
  });
});

$("btn-browse").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  onUpload(file);
  e.target.value = "";
});

const zone = $("upload-form");
["dragenter", "dragover"].forEach((type) => {
  zone.addEventListener(type, (e) => {
    e.preventDefault();
    zone.classList.add("drag");
  });
});
["dragleave", "drop"].forEach((type) => {
  zone.addEventListener(type, (e) => {
    e.preventDefault();
    zone.classList.remove("drag");
  });
});
zone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  onUpload(file);
});

refresh().catch((err) => {
  $("rows").innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(err.message)}</td></tr>`;
});

setInterval(() => {
  refresh().catch(() => {});
}, 2500);
