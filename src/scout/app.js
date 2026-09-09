"use strict";
const $ = (id) => document.getElementById(id);
const countryNames = { US: "United States", CA: "Canada", FR: "France" };
const flags = { US: "🇺🇸", CA: "🇨🇦", FR: "🇫🇷" };
let token = "",
  current = null,
  country = null,
  displayLimit = 60,
  refreshing = false;
function node(tag, text, className) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (className) e.className = className;
  return e;
}
function notice(text) {
  $("notice").textContent = text;
  $("notice").hidden = !text;
}
async function api(path, options = {}) {
  const res = await fetch("/api" + path, {
    ...options,
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/json",
    },
  });
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 401 && token) {
      lock();
    }
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Check the search fields and try again.",
    );
  }
  return data;
}
function view(name) {
  for (const n of ["search", "watches", "status"])
    $("view-" + n).hidden = n !== name;
  document
    .querySelectorAll("[data-view]")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === name));
}
function lock() {
  token = "";
  current = null;
  $("app").hidden = true;
  $("login").hidden = false;
  $("token").value = "";
}
$("disconnect").onclick = lock;
document.querySelectorAll("[data-view]").forEach(
  (b) =>
    (b.onclick = () => {
      view(b.dataset.view);
      refreshBasics().catch((e) => notice(e.message));
    }),
);
$("new-search").onclick = () => {
  view("search");
  $("query").focus();
};
$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  token = $("token").value.trim();
  try {
    await refreshBasics();
    $("token").value = "";
    $("login").hidden = true;
    $("app").hidden = false;
    $("login-error").textContent = "";
  } catch (err) {
    $("login-error").textContent = err.message;
  }
};
function phrases(id) {
  return $(id)
    .value.split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}
function specFromForm() {
  const max_prices = {};
  for (const c of ["USD", "CAD", "EUR"])
    if ($("price-" + c).value !== "") max_prices[c] = $("price-" + c).value;
  return {
    query: $("query").value.trim(),
    countries: [...document.querySelectorAll("[name=country]:checked")].map(
      (e) => e.value,
    ),
    sources: ["facebook"],
    image_profile: $("image-profile").value || null,
    include: phrases("include"),
    exclude: phrases("exclude"),
    include_any: $("include-any")
      .value.split("\n")
      .map((line) =>
        line
          .split(",")
          .map((x) => x.trim())
          .filter(Boolean),
      )
      .filter((group) => group.length),
    max_prices,
  };
}
function selectImageProfile(id) {
  const select = $("image-profile");
  if (id && ![...select.options].some((option) => option.value === id)) {
    const missing = node("option", "Photo reference unavailable");
    missing.value = id;
    missing.disabled = true;
    select.append(missing);
  }
  select.value = id;
  $("photo-profile-field").hidden = !id && select.options.length === 1;
}
function updateImageProfiles(profiles) {
  const selected = $("image-profile").value;
  const plain = node("option", "Title filters only");
  plain.value = "";
  $("image-profile").replaceChildren(plain);
  for (const profile of profiles) {
    const option = node("option", profile.label);
    option.value = profile.id;
    $("image-profile").append(option);
  }
  selectImageProfile(selected);
}
function fillForm(spec) {
  $("query").value = spec.query;
  selectImageProfile(spec.image_profile || "");
  document
    .querySelectorAll("[name=country]")
    .forEach((e) => (e.checked = spec.countries.includes(e.value)));
  $("include").value = spec.include.join(", ");
  $("include-any").value = (spec.include_any || [])
    .map((group) => group.join(", "))
    .join("\n");
  $("exclude").value = spec.exclude.join(", ");
  for (const c of ["USD", "CAD", "EUR"])
    $("price-" + c).value = spec.max_prices[c] ?? "";
  $("filters").open = !!(
    spec.image_profile ||
    (spec.include_any || []).length ||
    spec.include.length ||
    spec.exclude.length ||
    Object.keys(spec.max_prices).length
  );
}
$("search-form").onsubmit = async (e) => {
  e.preventDefault();
  const spec = specFromForm();
  if (!spec.countries.length) {
    notice("Choose at least one country.");
    return;
  }
  $("search-submit").disabled = true;
  try {
    const result = await api("/searches", {
      method: "POST",
      body: JSON.stringify(spec),
    });
    notice(
      "Search started. Each region takes a little time; you can browse results as they arrive.",
    );
    await openSearch(result.id);
    await refreshBasics();
  } catch (err) {
    notice(err.message);
  } finally {
    $("search-submit").disabled = false;
  }
};
async function openSearch(id) {
  const data = await api("/searches/" + id);
  current = data;
  country = data.spec.countries[0];
  displayLimit = 60;
  $("suggestions").checked = false;
  fillForm(data.spec);
  view("search");
  await renderSearch();
}
async function refreshBasics() {
  const [h, watches, recent] = await Promise.all([
    api("/health"),
    api("/watches"),
    api("/searches"),
  ]);
  updateImageProfiles(h.image_profiles || []);
  $("worker-label").textContent = h.worker_running
    ? "Search worker online"
    : "Search worker offline";
  $("worker-dot").classList.toggle("online", h.worker_running);
  $("watch-count").textContent = watches.length;
  document.querySelectorAll(".topic-link").forEach((a) => {
    if (h.telegram_topic_url) a.href = h.telegram_topic_url;
    else a.removeAttribute("href");
  });
  $("recent").replaceChildren();
  for (const r of recent) {
    const b = node(
      "button",
      r.spec.query + (r.status === "running" ? " · searching" : ""),
      r.id === current?.id ? "selected" : "",
    );
    b.title = r.spec.countries.map((c) => countryNames[c]).join(", ");
    b.onclick = () => openSearch(r.id).catch((e) => notice(e.message));
    $("recent").append(b);
  }
  if (!recent.length)
    $("recent").append(node("small", "Your searches will appear here."));
  $("watches").replaceChildren();
  for (const w of watches) {
    const card = node("article", undefined, "watch-card");
    card.append(
      node("h2", w.name),
      node(
        "p",
        w.countries.map((c) => flags[c] + " " + countryNames[c]).join("  ·  "),
      ),
      node(
        "p",
        `Every ${w.interval_minutes} minutes · ${w.sources.join(" + ")}${w.enabled ? "" : " · Paused"}`,
      ),
    );
    if (w.exclude.length)
      card.append(node("p", "Excluding: " + w.exclude.join(", ")));
    const actions = node("div", undefined, "actions");
    const browse = node("button", "Use this search ↗", "secondary");
    browse.onclick = () => {
      fillForm(w);
      view("search");
      $("query").focus();
      notice(
        "Search loaded. Select Search Marketplace to fetch fresh results.",
      );
    };
    const del = node("button", "Delete", "quiet");
    del.onclick = async () => {
      if (!confirm("Delete this watch and stop its notifications?")) return;
      try {
        await api("/watches/" + w.id, { method: "DELETE" });
        await refreshBasics();
        if (current) {
          current = await api("/searches/" + current.id);
          await renderSearch();
        }
      } catch (e) {
        notice(e.message);
      }
    };
    if (w.image_profile) {
      card.append(
        node(
          "p",
          "Photo check enabled: uncertain candidates stay here instead of sending alerts.",
        ),
      );
      const review = node("button", "Review photos", "secondary");
      review.onclick = () =>
        openPhotoReview(w.id).catch((e) => notice(e.message));
      actions.append(review);
    }
    actions.append(browse, del);
    card.append(actions);
    $("watches").append(card);
  }
  if (!watches.length)
    $("watches").append(
      node(
        "div",
        "No watches yet. Start with a search, then save it for notifications.",
        "empty",
      ),
    );
  $("health-cards").replaceChildren();
  for (const [title, detail] of [
    [h.worker_running ? "Online" : "Offline", "Marketplace search worker"],
    [
      h.telegram_configured ? "Connected" : "Setup needed",
      "Telegram notifications",
    ],
  ]) {
    const c = node("div", undefined, "health-card");
    c.append(node("b", title), node("span", detail));
    $("health-cards").append(c);
  }
  $("scan-status").replaceChildren();
  for (const s of h.scans) {
    const row = node("div", undefined, "region-row");
    row.append(
      node("span", `${flags[s.country]} ${s.region_name} · ${s.source}`),
      node(
        "span",
        s.error ||
          (s.last_success
            ? `${s.result_count ?? 0} listings checked · ${new Date(s.last_success * 1000).toLocaleString()}${s.radius_km ? " · " + s.radius_km + " km" : ""}`
            : "Waiting for first check"),
      ),
    );
    $("scan-status").append(row);
  }
}
async function renderSearch() {
  if (!current) return;
  const snapshot = current,
    id = snapshot.id,
    selectedCountry = country;
  $("welcome").hidden = true;
  $("results").hidden = false;
  $("result-title").textContent = snapshot.spec.query;
  $("result-summary").textContent =
    snapshot.spec.countries.map((c) => countryNames[c]).join(" · ") +
    " · " +
    snapshot.spec.sources.join(" + ");
  $("progress-fill").style.width =
    (snapshot.finished_regions / snapshot.total_regions) * 100 + "%";
  $("progress-text").textContent =
    `${snapshot.status === "running" ? "Searching" : snapshot.status.charAt(0).toUpperCase() + snapshot.status.slice(1)} · ${snapshot.finished_regions} / ${snapshot.total_regions} regions finished`;
  $("cancel-search").hidden = snapshot.status !== "running";
  $("create-watch").disabled = !!snapshot.watch_id;
  $("create-watch").textContent = snapshot.watch_id
    ? "✓ Watch created"
    : "◉ Create watch";
  $("country-tabs").replaceChildren();
  for (const c of snapshot.countries) {
    const b = node("button", flags[c.country] + " " + countryNames[c.country]);
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(c.country === country));
    b.append(node("b", c.matches));
    b.onclick = () => {
      country = c.country;
      displayLimit = 60;
      renderSearch().catch((e) => notice(e.message));
    };
    $("country-tabs").append(b);
  }
  const broad = $("suggestions").checked;
  const pages = [];
  for (let offset = 0; offset < displayLimit; offset += 100) {
    const page = await api(
      `/searches/${id}/results?country=${selectedCountry}&suggestions=${broad}&offset=${offset}&limit=${Math.min(100, displayLimit - offset)}`,
    );
    pages.push(page);
    if (offset + page.items.length >= page.total) break;
  }
  if (
    current?.id !== id ||
    country !== selectedCountry ||
    $("suggestions").checked !== broad
  )
    return;
  const items = pages.flatMap((p) => p.items),
    total = pages[0]?.total || 0,
    c = snapshot.countries.find((c) => c.country === country);
  $("listing-count").textContent =
    `${total} ${broad ? "collected listings" : "title matches"} · ${c.done} / ${c.total} regions checked`;
  $("listing-grid").replaceChildren(...items.map(listingCard));
  $("empty-results").hidden = items.length > 0;
  $("empty-results").textContent =
    c.failed === c.total
      ? "This country could not be searched. See regional details below."
      : c.done + c.failed + c.cancelled < c.total
        ? "Searching this country… Matching results will appear here as regions finish."
        : broad
          ? "No listings collected for this country."
          : `No title matches found in the checked regions.${c.listings ? " Try including broader suggestions to inspect Facebook’s other results." : ""}${c.failed ? " Some regions failed; see details below." : ""}`;
  $("load-more").hidden = items.length >= total;
  $("region-list").replaceChildren();
  for (const r of c.regions) {
    const row = node("div", undefined, "region-row");
    row.append(
      node("span", r.name + " · " + r.source),
      node(
        "span",
        r.error ||
          `${r.state}${r.result_count !== null ? " · " + r.result_count + " listings" : ""}${r.radius_km ? " · " + r.radius_km + " km" : ""}${r.saturated ? " · More results may exist" : ""}${r.coverage_warning ? " · " + r.coverage_warning : ""}`,
      ),
    );
    $("region-list").append(row);
  }
}
function safeUrl(value, photo = false) {
  try {
    const u = new URL(value);
    if (u.protocol !== "https:") return null;
    const domains = photo
      ? ["fbcdn.net"]
      : ["facebook.com"];
    return domains.some((d) => u.hostname === d || u.hostname.endsWith("." + d))
      ? u.href
      : null;
  } catch {
    return null;
  }
}
function listingCard(item) {
  const url = safeUrl(item.url),
    card = node(url ? "a" : "article", undefined, "listing");
  if (url) {
    card.href = url;
    card.target = "_blank";
    card.rel = "noopener noreferrer";
  }
  const photo = node("div", "s", "photo");
  const src = safeUrl(item.image_url, true);
  if (src) {
    const img = document.createElement("img");
    img.src = src;
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    img.onerror = () => img.remove();
    photo.append(img);
  }
  photo.append(
    node("span", item.matched ? "Title match" : "Suggestion", "badge"),
  );
  const body = node("div", undefined, "listing-body");
  let price = "Price unknown";
  if (item.price !== null && item.price !== undefined) {
    try {
      price =
        new Intl.NumberFormat(undefined, {
          style: "currency",
          currency: item.currency,
        }).format(Number(item.price)) +
        " " +
        item.currency;
    } catch {
      price = item.price + " " + (item.currency || "");
    }
  }
  body.append(node("div", price, "listing-price"), node("h3", item.title));
  const place = node("div", undefined, "listing-location");
  place.append(
    node("span", item.location || "Seller location unavailable"),
    node("span", "Marketplace ↗"),
  );
  body.append(place);
  card.append(photo, body);
  return card;
}
$("suggestions").onchange = () => {
  displayLimit = 60;
  renderSearch().catch((e) => notice(e.message));
};
$("load-more").onclick = () => {
  displayLimit += 60;
  renderSearch().catch((e) => notice(e.message));
};
$("cancel-search").onclick = async () => {
  try {
    await api("/searches/" + current.id + "/cancel", { method: "POST" });
    current = await api("/searches/" + current.id);
    await renderSearch();
    await refreshBasics();
  } catch (e) {
    notice(e.message);
  }
};
$("create-watch").onclick = () => {
  $("watch-name").value = current.spec.query.slice(0, 100);
  $("watch-error").textContent = "";
  $("watch-dialog").showModal();
};
$("close-dialog").onclick = () => $("watch-dialog").close();
$("watch-form").onsubmit = async (e) => {
  e.preventDefault();
  $("save-watch").disabled = true;
  try {
    await api("/searches/" + current.id + "/watch", {
      method: "POST",
      body: JSON.stringify({
        name: $("watch-name").value,
        interval_minutes: Number($("watch-interval").value),
      }),
    });
    $("watch-dialog").close();
    notice(
      "Watch created. Newly found matches will be sent to your Telegram topic.",
    );
    current = await api("/searches/" + current.id);
    await renderSearch();
    await refreshBasics();
  } catch (e) {
    $("watch-error").textContent = e.message;
  } finally {
    $("save-watch").disabled = false;
  }
};
let ticks = 0;
setInterval(async () => {
  if (!token || refreshing || document.hidden) return;
  refreshing = true;
  try {
    if (current?.status === "running") {
      const id = current.id,
        data = await api("/searches/" + id);
      if (current?.id === id) {
        current = data;
        await renderSearch();
      }
    }
    if (++ticks % 4 === 0) await refreshBasics();
  } catch (e) {
    notice(e.message);
  } finally {
    refreshing = false;
  }
}, 4000);

let reviewWatch = null,
  reviewOffset = 0;
async function openPhotoReview(id, append = false) {
  if (!append) {
    reviewWatch = id;
    reviewOffset = 0;
    $("photo-review-grid").replaceChildren();
  }
  const data = await api(`/watches/${id}/review?offset=${reviewOffset}`);
  $("photo-review").hidden = false;
  if (!data.total)
    $("photo-review-grid").append(node("p", "No uncertain photos waiting."));
  for (const item of data.items) {
    const wrap = node("div");
    wrap.append(
      listingCard({ ...item, matched: false }),
      node("p", item.reason, "fine"),
    );
    const approve = node("button", "Send this to Telegram", "secondary");
    approve.onclick = async () => {
      approve.disabled = true;
      try {
        await api(`/watches/${id}/review/${item.source}/${item.id}/notify`, {
          method: "POST",
        });
        wrap.remove();
        if (reviewWatch === id) reviewOffset = Math.max(0, reviewOffset - 1);
        notice("Confirmed candidate queued for Telegram.");
      } catch (e) {
        approve.disabled = false;
        notice(e.message);
      }
    };
    wrap.append(approve);
    $("photo-review-grid").append(wrap);
  }
  reviewOffset += data.items.length;
  $("photo-review-more").hidden = reviewOffset >= data.total;
}
$("photo-review-more").onclick = () =>
  openPhotoReview(reviewWatch, true).catch((e) => notice(e.message));
