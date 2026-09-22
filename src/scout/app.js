"use strict";
const $ = (id) => document.getElementById(id);
const countryNames = { US: "United States", CA: "Canada", FR: "France" };
const flags = { US: "🇺🇸", CA: "🇨🇦", FR: "🇫🇷" };
// Display labels stay distinct across countries; the optional final value is
// the Marketplace city anchor when its URL differs from the city name.
const cityCatalog = {
  CA: [
    "Abbotsford|BC", "Barrie|ON", "Brantford|ON", "Calgary|AB",
    "Charlottetown|PE", "Edmonton|AB", "Guelph|ON", "Halifax|NS",
    "Hamilton|ON", "Kamloops|BC", "Kelowna|BC", "Kingston|ON",
    "Kitchener|ON", "Lethbridge|AB", "London|ON", "Moncton|NB",
    "Montréal|QC|montreal", "Nanaimo|BC", "Oshawa|ON", "Ottawa|ON",
    "Peterborough|ON", "Québec|QC|quebec", "Regina|SK", "Saskatoon|SK",
    "Sherbrooke|QC", "St. Catharines|ON", "St. John's|NL",
    "Sudbury|ON", "Thunder Bay|ON", "Toronto|ON", "Trois-Rivières|QC",
    "Vancouver|BC", "Victoria|BC", "Windsor|ON", "Winnipeg|MB",
  ],
  US: [
    "Albuquerque|NM", "Atlanta|GA", "Austin|TX", "Baltimore|MD",
    "Boston|MA", "Charlotte|NC", "Chicago|IL", "Cleveland|OH",
    "Columbus|OH", "Dallas|TX", "Denver|CO", "Detroit|MI",
    "El Paso|TX", "Fort Worth|TX|114148045261892", "Fresno|CA",
    "Honolulu|HI|110444738976181", "Houston|TX", "Indianapolis|IN",
    "Jacksonville|FL", "Las Vegas|NV", "Los Angeles|CA|la",
    "Memphis|TN", "Miami|FL", "Milwaukee|WI", "Minneapolis|MN",
    "Nashville|TN", "New Orleans|LA", "New York|NY", "Oklahoma City|OK",
    "Philadelphia|PA", "Phoenix|AZ", "Pittsburgh|PA", "Portland|OR",
    "Sacramento|CA", "San Antonio|TX", "San Diego|CA", "San Francisco|CA",
    "San Jose|CA", "Seattle|WA", "Tampa|FL", "Tucson|AZ",
    "Washington|DC",
  ],
  FR: [
    "Aix-en-Provence", "Amiens", "Angers", "Annecy", "Avignon",
    "Besançon", "Bordeaux", "Brest", "Caen", "Clermont-Ferrand",
    "Dijon", "Grenoble", "Le Havre", "Le Mans", "Lille", "Limoges",
    "Lyon", "Marseille", "Metz", "Montpellier|115100621840245",
    "Mulhouse", "Nancy", "Nantes", "Nice", "Nîmes", "Orléans",
    "Paris", "Perpignan", "Reims", "Rennes", "Rouen", "Saint-Étienne",
    "Strasbourg", "Toulon", "Toulouse", "Tours", "Villeurbanne",
  ],
};
const cityOptions = Object.entries(cityCatalog).flatMap(([country, cities]) =>
  cities.map((entry) => {
    const [name, regionOrAnchor, anchor] = entry.split("|");
    const region = country === "FR" ? "" : regionOrAnchor;
    return {
      label: `${name}${region ? `, ${region}` : ", France"}`,
      country,
      anchor: anchor || (country === "FR" && regionOrAnchor) || citySlug(name),
    };
  }),
);
let authenticated = false,
  current = null,
  country = null,
  displayLimit = 60,
  refreshing = false,
  openRequest = 0,
  pricingRender = 0,
  pricingSubmitting = false;
const viewPaths = {
  search: "/explore",
  pricing: "/sell-price",
  watches: "/watches",
  status: "/status",
};
function viewFromPath() {
  return Object.keys(viewPaths).find((name) => viewPaths[name] === location.pathname) || "search";
}
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
      "Content-Type": "application/json",
    },
  });
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 401 && authenticated) {
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
function view(name, updateHistory = true) {
  for (const n of ["search", "pricing", "watches", "status"])
    $("view-" + n).hidden = n !== name;
  $("results").hidden = !current || !!current.spec.pricing;
  $("pricing-results").hidden = !current?.spec?.pricing;
  $("welcome").hidden = !!current && !current.spec.pricing;
  document
    .querySelectorAll("[data-view]")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  if (updateHistory && location.pathname !== viewPaths[name])
    history.pushState(null, "", viewPaths[name]);
}
window.addEventListener("popstate", () => view(viewFromPath(), false));
function lock() {
  authenticated = false;
  current = null;
  openRequest++;
  pricingRender++;
  $("app").hidden = true;
  $("login").hidden = false;
  $("token").value = "";
}
$("disconnect").onclick = async () => {
  try {
    await api("/session", { method: "DELETE" });
    lock();
  } catch (err) {
    notice(err.message);
  }
};
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
for (const city of cityOptions) {
  const option = node("option");
  option.value = city.label;
  $("pricing-city-options").append(option);
}
$("pricing-city").onchange = () => {
  const chosen = cityOption($("pricing-city").value);
  if (chosen) $("pricing-country").value = chosen.country;
};
async function unlock() {
  await refreshBasics();
  authenticated = true;
  $("token").value = "";
  $("login").hidden = true;
  $("app").hidden = false;
  $("login-error").textContent = "";
}
$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/session", {
      method: "POST", body: JSON.stringify({ token: $("token").value.trim() }),
    });
    await unlock();
  } catch (err) {
    $("login-error").textContent = err.message;
  } finally {
    $("token").value = "";
  }
};
async function restoreSession() {
  const ticket = new URLSearchParams(location.hash.slice(1)).get("signin");
  if (ticket) history.replaceState(null, "", location.pathname + location.search);
  if (location.pathname === "/") history.replaceState(null, "", viewPaths.search + location.search);
  let linkError = "";
  if (ticket) {
    try {
      await api("/session", { method: "POST", body: JSON.stringify({ ticket }) });
    } catch (err) {
      linkError = err.message;
    }
  }
  try {
    await unlock();
  } catch (err) {
    if (ticket) $("login-error").textContent = linkError || err.message;
  }
}
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
function citySlug(value) {
  const raw = String(value || "").trim();
  if (/^\d+$/.test(raw)) return raw;
  return raw
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-");
}
function cityLabel(value) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, " ");
}
function cityOption(value) {
  const normalized = citySlug(cityLabel(value));
  return cityOptions.find((option) => citySlug(option.label) === normalized);
}
function pricingSpecFromForm() {
  const label = cityLabel($("pricing-city").value);
  const chosen = cityOption(label);
  const city = chosen?.anchor || citySlug(label.split(",", 1)[0]);
  const radius = Number($("pricing-radius").value);
  if (chosen && chosen.country !== $("pricing-country").value)
    throw new Error(`Selected city is in ${countryNames[chosen.country]}. Choose that country or enter another city.`);
  if (!city || !/^(?:\d+|[a-z0-9]+(?:-[a-z0-9]+)*)$/.test(city))
    throw new Error("Enter a Marketplace city name, URL slug, or numeric city ID.");
  if (!Number.isInteger(radius) || radius < 1 || radius > 805)
    throw new Error("Radius must be a whole number between 1 and 805 km.");
  return {
    query: $("pricing-query").value.trim(),
    countries: [$("pricing-country").value],
    include: phrases("pricing-include"),
    exclude: phrases("pricing-exclude"),
    pricing: { city, label: label || city, radius_km: radius },
  };
}
function fillPricingForm(spec) {
  const pricing = spec.pricing || {};
  $("pricing-query").value = spec.query || "";
  $("pricing-country").value = spec.countries?.[0] || "CA";
  $("pricing-city").value = pricing.label || pricing.city || "";
  $("pricing-radius").value = pricing.radius_km ?? 20;
  $("pricing-include").value = (spec.include || []).join(", ");
  $("pricing-exclude").value = (spec.exclude || []).join(", ");
  $("pricing-filters").open = !!(spec.include?.length || spec.exclude?.length);
}
function formatMoney(value, currency) {
  if (value === null || value === undefined || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return `${value} ${currency || ""}`.trim();
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency || ""}`.trim();
  }
}
function pricingState(message, state = "") {
  const target = $("pricing-state");
  target.className = `pricing-state${state ? " " + state : ""}`;
  target.textContent = message;
  target.hidden = !message;
}
function pricingProgress(snapshot) {
  const total = Number(snapshot.total_regions) || 0;
  const finished = Number(snapshot.finished_regions) || 0;
  $("pricing-progress-fill").style.width =
    (total ? Math.min(100, (finished / total) * 100) : 0) + "%";
  const label = snapshot.status === "running"
    ? "Checking"
    : snapshot.status.charAt(0).toUpperCase() + snapshot.status.slice(1);
  $("pricing-progress-text").textContent = `${label} · ${finished} / ${total} areas finished`;
}
function pricingError(snapshot) {
  return snapshot.countries?.[0]?.regions?.find((region) => region.error)?.error ||
    "The price check could not be completed. Try running it again.";
}
function renderPricingCards(data) {
  const currency = data.currency || "USD";
  const cards = [
    ["Minimum", data.minimum],
    ["Average", data.average],
    ["Suggested", data.suggested_price],
    ["Maximum", data.maximum],
    ["Active sample", data.sample_size == null ? "—" : String(data.sample_size), true],
  ];
  $("pricing-cards").replaceChildren(
    ...cards.map(([label, value, count]) => {
      const card = node("div", undefined, "pricing-card");
      card.append(
        node("span", label),
        node("strong", count ? value : formatMoney(value, currency), "pricing-card-value"),
      );
      if (label === "Suggested" && data.suggested_price == null)
        card.append(node("small", "Needs 3 active matches"));
      return card;
    }),
  );
  const basis = data.recommendation_basis || "";
  const alternatives = data.quick_sale_price == null && data.patient_price == null
    ? ""
    : ` Competitive starting point: ${formatMoney(data.quick_sale_price, currency)} · Patient starting point: ${formatMoney(data.patient_price, currency)}.`;
  $("pricing-basis").textContent = basis
    ? `${basis} · Currency: ${currency}.${alternatives}`
    : `Prices are shown in ${currency}.${alternatives}`;
}
function renderPricingHistogram(data) {
  const target = $("pricing-histogram");
  target.replaceChildren();
  const bins = Array.isArray(data.histogram) ? data.histogram : [];
  if (!bins.length) {
    target.append(node("span", "Not enough priced listings for a range chart.", "chart-empty"));
    return;
  }
  const max = Math.max(...bins.map((bin) => Number(bin.count) || 0), 1);
  const currency = data.currency || "USD";
  for (const bin of bins) {
    const count = Number(bin.count) || 0;
    const wrap = node("div", undefined, "histogram-bin");
    const bar = node("span", undefined, "histogram-bar");
    bar.style.height = `${Math.max(4, (count / max) * 100)}%`;
    bar.title = `${formatMoney(bin.low, currency)}–${formatMoney(bin.high, currency)}: ${count}`;
    wrap.append(bar, node("small", formatMoney(bin.low, currency)));
    target.append(wrap);
  }
}
function renderPricingSpeed(data) {
  const target = $("pricing-speed");
  target.replaceChildren();
  const points = (Array.isArray(data.speed_points) ? data.speed_points : []).filter(
    (point) => Number.isFinite(Number(point.price)) && Number.isFinite(Number(point.days)),
  );
  if (!points.length) {
    target.append(node("span", "No sold observations yet for a speed signal.", "chart-empty"));
    return;
  }
  const width = 620,
    height = 230,
    pad = { top: 18, right: 20, bottom: 40, left: 120 },
    minPrice = Math.min(...points.map((p) => Number(p.price))),
    maxPrice = Math.max(...points.map((p) => Number(p.price))),
    maxDays = Math.max(...points.map((p) => Number(p.days)), 1),
    priceSpan = Math.max(maxPrice - minPrice, 1),
    svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("aria-hidden", "true");
  const line = (x1, y1, x2, y2, className) => {
    const value = document.createElementNS("http://www.w3.org/2000/svg", "line");
    value.setAttribute("x1", x1);
    value.setAttribute("y1", y1);
    value.setAttribute("x2", x2);
    value.setAttribute("y2", y2);
    value.setAttribute("class", className);
    svg.append(value);
  };
  line(pad.left, pad.top, pad.left, height - pad.bottom, "axis");
  line(pad.left, height - pad.bottom, width - pad.right, height - pad.bottom, "axis");
  const text = (x, y, value, className = "axis-label") => {
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", x);
    label.setAttribute("y", y);
    label.setAttribute("class", className);
    label.textContent = value;
    svg.append(label);
  };
  const flatPrice = minPrice === maxPrice;
  const midY = (pad.top + height - pad.bottom) / 2;
  text(pad.left - 7, flatPrice ? midY + 4 : pad.top + 4, formatMoney(maxPrice, data.currency), "axis-label axis-value");
  if (!flatPrice)
    text(pad.left - 7, height - pad.bottom, formatMoney(minPrice, data.currency), "axis-label axis-value");
  text(pad.left, height - pad.bottom + 18, "0", "axis-label");
  text(width - pad.right, height - pad.bottom + 18, `${maxDays}d`, "axis-label");
  for (const point of points) {
    const x = pad.left + (Number(point.days) / maxDays) * (width - pad.left - pad.right);
    const y = flatPrice ? midY : pad.top + ((maxPrice - Number(point.price)) / priceSpan) * (height - pad.top - pad.bottom);
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", x);
    circle.setAttribute("cy", y);
    circle.setAttribute("r", 5);
    circle.setAttribute("class", "speed-point");
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `${point.title || "Listing"}: ${formatMoney(point.price, data.currency)} · ${point.days} observed days`;
    circle.append(title);
    svg.append(circle);
  }
  const xLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
  xLabel.setAttribute("x", width / 2);
  xLabel.setAttribute("y", height - 8);
  xLabel.setAttribute("class", "axis-label");
  xLabel.textContent = "observed days";
  svg.append(xLabel);
  target.append(svg);
  const list = node("ul", undefined, "speed-points");
  for (const point of points.slice(0, 8))
    list.append(node("li", `${formatMoney(point.price, data.currency)} · ${point.days} days · ${point.title || "Listing"}`));
  target.append(list);
}
function soldListingCard(item, currency) {
  const card = listingCard({
    ...item,
    price: item.last_active_price ?? item.price,
    currency: item.last_active_price != null
      ? item.last_active_currency || currency
      : item.currency || currency,
    matched: false,
    status: "sold",
  });
  const body = card.querySelector(".listing-body");
  const first = item.first_seen ? new Date(Number(item.first_seen) * 1000).toLocaleDateString() : "unknown";
  const sold = item.sold_seen ? new Date(Number(item.sold_seen) * 1000).toLocaleDateString() : "recently";
  body.append(node("p", `Observed ${first} → sold observed ${sold}${item.observed_days == null ? "" : ` · ${item.observed_days} days`}`, "fine"));
  return card;
}
function renderPricingReport(data, snapshot) {
  const report = data || {};
  const currency = report.currency || "USD";
  $("pricing-report").hidden = false;
  if (snapshot?.status === "running")
    pricingState("Scan still running. This report will update as the local area finishes.", "running");
  else if (snapshot?.status === "failed") pricingState(pricingError(snapshot), "error");
  else if (snapshot?.status === "cancelled") pricingState("This price check was stopped. Run it again when you are ready.", "empty");
  else pricingState("");
  renderPricingCards(report);
  renderPricingHistogram(report);
  renderPricingSpeed(report);
  const warnings = Array.isArray(report.warnings) ? report.warnings.filter(Boolean) : [];
  $("pricing-warnings").hidden = !warnings.length;
  $("pricing-warnings").replaceChildren(...warnings.map((warning) => node("p", warning)));
  const active = Array.isArray(report.items) ? report.items : [];
  $("pricing-items").replaceChildren(...active.map((item) => listingCard({ ...item, matched: true })));
  $("pricing-empty-items").hidden = active.length > 0;
  $("pricing-empty-items").textContent = report.sample_size
    ? "No active listings with valid prices were returned."
    : "No active priced matches were found in this area.";
  $("pricing-comparables-note").textContent = `${active.length} active comparable${active.length === 1 ? "" : "s"} shown · ${report.excluded_count || 0} listing${report.excluded_count === 1 ? "" : "s"} excluded without a valid price.`;
  const sold = Array.isArray(report.sold_items) ? report.sold_items : [];
  $("pricing-sold-section").hidden = !sold.length;
  $("pricing-sold-items").replaceChildren(...sold.map((item) => soldListingCard(item, currency)));
}
async function renderPricing() {
  if (!current?.spec?.pricing) return;
  const token = ++pricingRender;
  const snapshot = current;
  const id = snapshot.id;
  const pricing = snapshot.spec.pricing;
  $("pricing-results").hidden = false;
  $("pricing-report").hidden = true;
  $("pricing-result-title").textContent = snapshot.spec.query;
  $("pricing-result-summary").textContent = `${pricing.label || pricing.city} · ${pricing.radius_km} km · ${countryNames[snapshot.spec.countries[0]] || snapshot.spec.countries[0]}`;
  pricingProgress(snapshot);
  $("cancel-pricing").hidden = snapshot.status !== "running";
  $("rerun-pricing").hidden = snapshot.status === "running";
  if (snapshot.status === "running") pricingState("Checking local listings and recent history…", "running");
  else if (snapshot.status === "failed") pricingState(pricingError(snapshot), "error");
  else if (snapshot.status === "cancelled") pricingState("This price check was stopped. Run it again when you are ready.", "empty");
  else pricingState("Loading the pricing report…", "running");
  let data;
  try {
    data = await api(`/searches/${id}/pricing`);
  } catch (error) {
    if (token !== pricingRender || current?.id !== id) return;
    if (snapshot.status === "running") pricingState("The scan is still running. Pricing will appear when it finishes.", "running");
    else pricingState(error.message, "error");
    return;
  }
  if (token !== pricingRender || current?.id !== id) return;
  renderPricingReport(data, snapshot);
}
async function startPricing() {
  if (pricingSubmitting) return;
  let spec;
  try {
    spec = pricingSpecFromForm();
  } catch (error) {
    pricingState(error.message, "error");
    return;
  }
  if (spec.query.length < 2) {
    pricingState("Enter at least two characters for the item you are selling.", "error");
    return;
  }
  pricingSubmitting = true;
  $("pricing-submit").disabled = true;
  $("rerun-pricing").disabled = true;
  try {
    const result = await api("/searches", { method: "POST", body: JSON.stringify(spec) });
    notice("Price check started. Results will appear as the local scan finishes.");
    await openSearch(result.id);
    await refreshBasics();
  } catch (error) {
    pricingState(error.message, "error");
  } finally {
    pricingSubmitting = false;
    $("pricing-submit").disabled = false;
    $("rerun-pricing").disabled = false;
  }
}
$("pricing-form").onsubmit = async (event) => {
  event.preventDefault();
  await startPricing();
};
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
  const request = ++openRequest;
  const data = await api("/searches/" + id);
  if (request !== openRequest) return;
  current = data;
  if (data.spec.pricing) {
    fillPricingForm(data.spec);
    view("pricing");
    await renderPricing();
    return;
  }
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
    ? "Online"
    : "Offline";
  $("worker-dot").classList.toggle("online", h.worker_running);
  $("watch-count").textContent = watches.length;
  document.querySelectorAll(".topic-link").forEach((a) => {
    if (h.telegram_topic_url) a.href = h.telegram_topic_url;
    else a.removeAttribute("href");
  });
  $("recent").replaceChildren();
  for (const r of recent.filter((entry) => !entry.spec.pricing)) {
    const b = node(
      "button",
      r.spec.query + (r.status === "running" ? " · searching" : ""),
      r.id === current?.id ? "selected" : "",
    );
    b.title = r.spec.countries.map((c) => countryNames[c]).join(", ");
    b.onclick = () => openSearch(r.id).catch((e) => notice(e.message));
    $("recent").append(b);
  }
  $("recent").closest(".recent-line").hidden = !$("recent").childElementCount;
  $("pricing-recent").replaceChildren();
  for (const r of recent.filter((entry) => entry.spec.pricing)) {
    const pricing = r.spec.pricing;
    const label = `${r.spec.query} · ${pricing.label || pricing.city}`;
    const b = node(
      "button",
      label + (r.status === "running" ? " · checking" : ""),
      r.id === current?.id ? "selected" : "",
    );
    b.title = `${pricing.label || pricing.city} · ${pricing.radius_km} km · ${countryNames[r.spec.countries[0]] || r.spec.countries[0]}`;
    b.onclick = () => openSearch(r.id).catch((e) => notice(e.message));
    $("pricing-recent").append(b);
  }
  $("pricing-recent").closest(".recent-line").hidden = !$("pricing-recent").childElementCount;
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
          if (current.spec.pricing) await renderPricing();
          else await renderSearch();
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
  const recoveringHome = h.home_recovery?.pending;
  for (const [title, detail] of [
    [h.worker_running ? (recoveringHome ? "Paused" : "Online") : "Offline", "Marketplace search worker"],
    [
      h.telegram_configured ? "Connected" : "Setup needed",
      "Telegram notifications",
    ],
  ]) {
    const c = node("div", undefined, "health-card");
    c.append(node("b", title), node("span", detail));
    $("health-cards").append(c);
  }
  if (recoveringHome) {
    const c = node("div", undefined, "health-card");
    const retry = h.home_recovery.retry_at
      ? ` Next attempt: ${new Date(h.home_recovery.retry_at * 1000).toLocaleTimeString()}.`
      : "";
    c.append(
      node("b", "Restoring your Marketplace location"),
      node("span", `${h.home_recovery.error || "Checking your saved location."}${retry} Searches wait until this is resolved.`),
    );
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
  if (!current || current.spec.pricing) return;
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
    node("span", item.status === "sold" ? "Sold observation" : item.matched ? "Title match" : "Suggestion", "badge"),
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
  if (typeof item.distance_km === "number" && Number.isFinite(item.distance_km))
    body.append(node("p", `${item.distance_km.toFixed(1)} km from search center · approximate location`, "fine"));
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
$("cancel-pricing").onclick = async () => {
  if (!current) return;
  try {
    await api("/searches/" + current.id + "/cancel", { method: "POST" });
    current = await api("/searches/" + current.id);
    await renderPricing();
    await refreshBasics();
  } catch (error) {
    pricingState(error.message, "error");
  }
};
$("rerun-pricing").onclick = () => startPricing();
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
  if (!authenticated || refreshing || document.hidden) return;
  refreshing = true;
  try {
    if (current?.status === "running") {
      const id = current.id,
        data = await api("/searches/" + id);
      if (current?.id === id) {
        current = data;
        if (current.spec.pricing) await renderPricing();
        else await renderSearch();
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

view(viewFromPath(), false);
restoreSession();
