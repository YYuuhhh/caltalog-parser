"use strict";
const $ = (id) => document.getElementById(id);
const stages = [
  ["catalog", "Catalogs & manufacturers"],
  ["extract", "Extract pages"],
  ["review", "Review evidence"],
  ["mapping", "Manufacturer product list"],
  ["structure", "Structure & compare"],
  ["final", "Final validation"],
  ["results", "Run results"],
];
let bootstrap,
  selected = new Map(),
  runs = [],
  selectedRun,
  currentMapping,
  currentTask;
let queueState = { paused: false, active_run: null, queued_runs: [] };
const pendingControls = new Set();
const inputs = new Map();
const modelPickers = new Map();
let localInstalls = [],
  localDiscoveryBusy = false,
  localDiscoverySerial = 0;
let installPollBusy = false,
  localDiscoveryAt = 0;
let localDeletion = null,
  modelDeleteConfirmation = null,
  modelDeletionError = "";
let pollBusy = false;
const refreshedMappings = new Set();
const runPanels = new Map();
const stoppagePanels = new Map();
let renderedRun = "",
  renderedSignature = "",
  manualTools = [],
  manualJob;
let ocrLanguageOverride = false;
let ocrAvailability = { url: "", installed: false };
let viewingStage = "catalog",
  processingStage,
  manualIndex = 0,
  manualTab = "parameters",
  manualZoom = 100;
const manualPositions = new Map();
const manualDrafts = new Map();
const stageSection = {
  catalog: "catalog",
  parse: "extract",
  raw: "extract",
  correctness: "review",
  completeness: "review",
  internet: "review",
  mapping: "mapping",
  structure: "structure",
  duplicates: "structure",
  final: "final",
  validate: "results",
};
const languageChoices = [
  ["en", "English"],
  ["ru", "Russian"],
  ["zh", "Chinese"],
  ["de", "German"],
  ["fr", "French"],
  ["es", "Spanish"],
  ["it", "Italian"],
];
function buildLanguages() {
  const box = $("language-fields");
  const title = node("span", "field-title", "Languages for processing");
  title.append(helpButton(parameterHelp.languages, "Languages for processing"));
  const input = node("input");
  input.type = "hidden";
  input.name = "languages";
  input.value = bootstrap.defaults.languages;
  inputs.set("languages", input);
  const chips = node("div", "language-chips");
  for (const [code, label] of languageChoices) {
    const chip = node("label", "language-chip");
    const check = node("input");
    check.type = "checkbox";
    check.value = code;
    check.dataset.language = code;
    check.addEventListener("change", () => {
      const chosen = [
        ...box.querySelectorAll("input[data-language]:checked"),
      ].map((c) => c.value);
      input.value = chosen.join(",") || "auto";
      syncLanguages();
      refresh();
    });
    chip.append(check, node("span", "", label));
    chips.append(chip);
  }
  box.append(
    title,
    input,
    chips,
    node(
      "small",
      "help",
      "Detected languages are shown on each PDF. No selection uses automatic detection.",
    ),
  );
  syncLanguages();
}
function syncLanguages() {
  const chosen = inputs
    .get("languages")
    .value.split(",")
    .map((c) => c.trim());
  for (const check of $("language-fields").querySelectorAll(
    "input[data-language]",
  ))
    check.checked = chosen.includes(check.value);
}
function viewStage(id) {
  viewingStage = id;
  $("pipeline-viewing").textContent =
    "Viewing " +
    String(stages.findIndex((s) => s[0] === id) + 1).padStart(2, "0") +
    " · " +
    stages.find((s) => s[0] === id)[1];
  for (const link of $("navigation").children) {
    if (link.hash === "#" + id) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
}
function jumpStage(id) {
  showView("pipeline");
  viewStage(id);
  $(id).scrollIntoView({ behavior: "smooth", block: "start" });
}

const parameterHelp = {
  revision:
    "Catalog edition or publication year. Different editions keep separate source evidence. Leave unknown when the edition is not stated.",
  languages:
    "Languages used to interpret catalog text: English, Russian and Chinese are selected by default. A per-catalog language correction takes priority. OCR chooses its script from the detected source language.",
  families:
    "Tool categories that select the normalization schemas, for example milling_cutters or drills. This does not filter the PDF pages.",
  extraction_model:
    "The installed CPU bundle that recognizes page layout, tables and text. Review models are selected separately in stages 03 and 06.",
  method:
    "Auto chooses native PDF text or OCR. Native text is fastest for searchable PDFs; OCR recognizes scanned images.",
  tables:
    "Recognize table cells and their row/column relationships. Turn this on for catalog parameter tables.",
  formulas:
    "Recognize mathematical expressions. Formula extraction can take additional CPU time.",
  page_selection:
    "Limits extraction to the chosen physical PDF pages. Off processes the whole catalog. A partial catalog cannot receive full-catalog validation.",
  first_page:
    "First physical PDF page, starting at 1. This can differ from a page number printed in the catalog.",
  last_page:
    "Last physical PDF page, inclusive. Leave empty to process through the end of the PDF.",
  automatic_quality:
    "Retry weak pages using different CPU extraction methods, keeping a stronger result only if it does not lose table cells.",
  confidence_threshold:
    "Minimum diagnostic/review confidence for an acceptable result. Higher values trigger more retries and leave more uncertain findings unresolved. It is not a calibrated probability.",
  max_quality_passes:
    "Maximum distinct extraction attempts per page, including the initial attempt. Available methods are auto, OCR and native text.",
  parser_language:
    "OCR writing-system group. Upload language detection selects a suitable group automatically; this override is useful when a scan is hard to identify.",
  force_parse:
    "Extract again instead of reusing a matching cached result. Old extraction evidence is retained.",
  parse_timeout:
    "Maximum seconds allowed for an extraction request before the run stops with a saved error.",
  mineru_url:
    "Address of the local CPU extraction service. The running service must match the installed CPU bundle.",
  model:
    "Vision model for page correctness, completeness, internet evidence and manufacturer mapping. Local review sends the current page to your Ollama server without Work or API calls.",
  review_provider:
    "Choose local Ollama or the Work subscription worker for evidence review. Local is the default.",
  final_provider:
    "Independent final validation engine. Local validation is the default and receives a fresh task with the original page and tool data.",
  local_ram_gb:
    "RAM on the inference machine, not GPU VRAM. Applies a model pair suited to 32, 128 or 750 GB. Only one model is loaded at a time.",
  local_url:
    "Ollama server base URL. The bundled service is http://local-llm:11434; a private LAN server can run on another machine.",
  local_context_tokens:
    "Maximum local context including the page image, prompt and answer. Oversized evidence is rejected instead of silently truncated. Larger contexts require more RAM.",
  local_output_tokens:
    "Maximum answer and thinking tokens for a local task. An answer cut off at this limit cannot pass validation.",
  local_temperature:
    "Sampling temperature for local reviews. Lower values make repeated checks more consistent.",
  local_cpu_threads:
    "CPU threads per model request. The default uses the detected physical core budget. Zero lets Ollama choose. More threads can reduce speed when memory bandwidth is the limit.",
  baidu_url:
    "Private address of the CPU Unlimited-OCR extraction service. Starting it does not download weights; use Install after checking availability.",
  baidu_output_tokens:
    "Maximum new OCR tokens for each PDF page. A truncated page fails extraction and must be retried with a larger budget.",
  extraction_dpi:
    "Resolution used to render pages for Unlimited-OCR. Automatic quality retries increase this resolution up to 300 DPI and retain the stronger complete extraction.",
  local_keep_loaded:
    "Keep weights resident until Ollama restarts, unloads them, or switches models. Task evidence remains fresh. Turn off to unload after every response and disable early preloading.",
  local_preload:
    "Start loading the first selected local model while CPU extraction runs. Requires Keep model weights loaded. A different final model loads during structuring; only one model loads at a time. No tokens are generated by preloading.",
  local_load_timeout:
    "Seconds allowed for cold model loading from disk, separate from each review's response timeout. Default: two hours. The inference server's OLLAMA_LOAD_TIMEOUT must also allow a slow HDD load.",
  local_load_into_ram:
    "Read CPU model weights fully into RAM instead of relying on memory-mapped disk pages. Helps HDD inference when the model, context and extractor fit available RAM. Turn off to use memory mapping. The operating system can still swap under memory pressure.",
  local_source_urls:
    "Optional additional pages on the manufacturer's approved domains. The official origin is resolved automatically before reviewing and the chosen evidence is frozen for the run.",
  local_search_url:
    "Optional private SearXNG server base URL for finding manufacturer pages. Without fetched sources, internet-dependent checks remain unverified.",
  reasoning:
    "Local models expose thinking on/off when supported. Work models expose their advertised reasoning levels. Thinking consumes more local CPU time or Work allowance, depending on the selected engine.",
  final_model:
    "Independent stage 06 model choice for tool, drawing, PDF and internet validation. No model skips this model check and keeps the catalog unvalidated.",
  final_reasoning:
    "Independent final-task thinking setting. Local Instruct models use Off; thinking-capable local models offer Off/On. Work models use their advertised effort tiers. Disabled when No model is selected.",
  automatic_review:
    "Run fresh model tasks through the signed-in local Codex worker with each stage's selected model and reasoning. This consumes ChatGPT Work allowance. Off prepares tasks for manual Work handoff.",
  prepare_manual_review:
    "Prepare a card for every normalized tool, including its final parameters, original PDF pages, review findings and saved official internet evidence. Preparing cards makes no model or web generation calls.",
  review_dpi:
    "Resolution of the original PDF page image sent to review. Higher DPI makes small dimensions easier to read but increases image size.",
  review_batch_rows:
    "Maximum RAW cells in a page-review packet. The context-size limit can split it further. Final review handles at most five tools per task.",
  review_timeout:
    "Time available for each automatic or imported review response. A timeout leaves the task unapproved.",
  force_structure:
    "Rebuild normalized and comparison workbooks from the saved RAW data and current mapping instead of reusing an identical ETL result.",
};
function helpButton(text, label) {
  const info = button(
    "i",
    (event) => {
      event.preventDefault();
      event.stopPropagation();
      showHelp(info, text);
    },
    "parameter-info",
  );
  info.setAttribute("aria-label", "About " + label);
  info.setAttribute("aria-describedby", "parameter-tooltip");
  info.addEventListener("mouseenter", () => showHelp(info, text));
  info.addEventListener("focus", () => showHelp(info, text));
  info.addEventListener("mouseleave", hideHelp);
  info.addEventListener("blur", hideHelp);
  return info;
}
let helpAnchor;
function showHelp(anchor, text) {
  helpAnchor = anchor;
  const tip = $("parameter-tooltip");
  tip.textContent = text;
  tip.hidden = false;
  positionHelp(anchor);
}
function positionHelp(anchor) {
  const tip = $("parameter-tooltip");
  const rect = anchor.getBoundingClientRect();
  tip.style.left =
    Math.max(
      8,
      Math.min(rect.left, innerWidth - Math.min(310, innerWidth - 16) - 8),
    ) + "px";
  tip.style.top =
    Math.max(8, Math.min(rect.bottom + 8, innerHeight - tip.offsetHeight - 8)) +
    "px";
}
function hideHelp() {
  helpAnchor = null;
  $("parameter-tooltip").hidden = true;
}
const terminal = new Set([
  "completed",
  "quality_blocked",
  "failed",
  "cancelled",
  "interrupted",
]);
function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}
function button(text, action, className = "button outline") {
  const element = node("button", className, text);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}
function showError(error) {
  $("error").textContent = error.message || String(error);
  $("error").hidden = false;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.raw
      ? undefined
      : { "Content-Type": "application/json", ...options.headers },
  });
  const body = await response.text();
  let data;
  try {
    data = body ? JSON.parse(body) : null;
  } catch {
    throw new Error(
      `${path.split("?")[0]} returned HTTP ${response.status} without a JSON response. Check pipeline-ui logs for the server error.`,
    );
  }
  if (!response.ok) {
    const detail =
      typeof data?.detail === "string"
        ? data.detail
        : JSON.stringify(data?.detail);
    throw new Error(
      `${path.split("?")[0]} (HTTP ${response.status}): ${detail || "Request failed"}`,
    );
  }
  return data;
}
function options(select, manufacturers, current) {
  select.replaceChildren(
    ...manufacturers.map((m) => {
      const option = node("option", "", m.name);
      option.value = m.id;
      return option;
    }),
  );
  select.value = current || manufacturers[0]?.id || "";
}
function buildField(container, name, label, type = "text", choices, hint) {
  const isModel = ["model", "final_model"].includes(name);
  const box = node(
    isModel ? "div" : "label",
    type === "checkbox" ? "toggle-field" : "field",
  );
  const input = document.createElement(type === "select" ? "select" : "input");
  input.name = name;
  if (type !== "select") input.type = type;
  if (choices)
    for (const [value, title] of choices) {
      const option = node("option", "", title);
      option.value = value;
      input.append(option);
    }
  const value = bootstrap.defaults[name];
  if (type === "checkbox") input.checked = Boolean(value);
  else input.value = value ?? "";
  if (type === "number") {
    input.step = ["confidence_threshold", "local_temperature"].includes(name)
      ? ".01"
      : "1";
    const limits = {
      first_page: [1],
      last_page: [1],
      confidence_threshold: [0.5, 1],
      max_quality_passes: [1, 3],
      parse_timeout: [10, 86400],
      review_timeout: [30, 86400],
      local_load_timeout: [10, 86400],
      review_dpi: [100, 300],
      review_batch_rows: [10, 100],
      local_context_tokens: [16384, 131072],
      local_output_tokens: [512, 16384],
      local_temperature: [0, 2],
    };
    if (limits[name]) {
      input.min = limits[name][0];
      if (limits[name][1]) input.max = limits[name][1];
    }
  }
  const title = node("span", "field-title", label);
  title.id = `${name}-label`;
  input.setAttribute("aria-labelledby", title.id);
  title.append(helpButton(parameterHelp[name] || hint || label, label));
  if (type === "checkbox") box.append(input, title);
  else box.append(title, input);
  if (hint) box.append(node("small", "", hint));
  container.append(box);
  inputs.set(name, input);
  if (isModel) buildModelPicker(name, box);
  input.addEventListener("change", refresh);
  if (name === "parser_language")
    input.addEventListener("change", () => {
      ocrLanguageOverride = true;
      refresh();
    });
  input.addEventListener("input", refresh);
}
function settings() {
  const data = { ...bootstrap.defaults };
  for (const [name, input] of inputs) {
    data[name] =
      input.type === "range"
        ? input.dataset.reasoning
        : input.type === "checkbox"
          ? input.checked
          : input.type === "number"
            ? input.value === ""
              ? null
              : Number(input.value)
            : input.value;
  }
  data.local_ram_gb = Number(data.local_ram_gb);
  return data;
}
function applySettings(data) {
  const previousServer = localServerUrl();
  data = { ...data };
  data.baidu_url ??= data.ocr_check_url;
  data.baidu_output_tokens ??= data.ocr_check_tokens;
  if (data.model === "chatgpt") data.model = bootstrap.defaults.model;
  if (data.final_model === "chatgpt")
    data.final_model = bootstrap.defaults.final_model;
  for (const [provider, model] of [
    ["review_provider", "model"],
    ["final_provider", "final_model"],
  ]) {
    data[provider] ??= data[model]?.startsWith("gpt-") ? "work" : "local";
    inputs.get(provider).value = data[provider];
  }
  updateModels(true);
  for (const [name, input] of inputs)
    if (name in data) {
      if (input.type === "range") input.dataset.reasoning = data[name];
      else if (input.type === "checkbox") input.checked = Boolean(data[name]);
      else {
        if (
          ["model", "final_model"].includes(name) &&
          data[name] &&
          ![...input.options].some((o) => o.value === data[name]) &&
          data[name === "model" ? "review_provider" : "final_provider"] ===
            "local"
        )
          input.add(new Option(data[name], data[name]));
        input.value = data[name] ?? "";
      }
    }
  for (const name of ["reasoning", "final_reasoning"])
    if (!(name in data))
      inputs.get(name).dataset.reasoning = bootstrap.defaults[name];
  syncLanguages();
  if (previousServer !== localServerUrl()) {
    resetLocalModels();
    refreshLocalModels();
  }
  refresh();
}
function configFor(catalog) {
  const data = settings();
  const detectedLanguages = catalog.detection?.language?.codes || [];
  const languages =
    catalog.language_override ||
    (data.languages === "auto"
      ? detectedLanguages.join(",") || "und"
      : data.languages);
  const codes = (
    catalog.language_override ||
    detectedLanguages.join(",") ||
    languages
  )
    .split(",")
    .map((c) => c.trim());
  const group = codes.some((c) => ["ru", "uk", "be"].includes(c))
    ? "east_slavic"
    : codes.some((c) => ["ar", "fa", "ur"].includes(c))
      ? "arabic"
      : codes.includes("ko")
        ? "korean"
        : "ch";
  return {
    ...data,
    languages,
    parser_language:
      ocrLanguageOverride ||
      data.parser_language !== bootstrap.defaults.parser_language
        ? data.parser_language
        : group,
    source_pdf: catalog.path,
    manufacturer_id: selected.get(catalog.path),
  };
}
function build() {
  for (const [index, [id, title]] of stages.entries()) {
    const a = node("a");
    a.href = "#" + id;
    a.append(
      node("span", "nav-number", String(index + 1).padStart(2, "0")),
      node("span", "", title),
    );
    a.addEventListener("click", () => {
      showView("pipeline");
      viewStage(id);
    });
    $("navigation").append(a);
  }
  options(
    $("target-manufacturer"),
    bootstrap.manufacturers,
    bootstrap.target_manufacturer_id,
  );
  options(
    $("mapping-manufacturer"),
    bootstrap.manufacturers,
    bootstrap.defaults.manufacturer_id,
  );
  const add = (container, ...args) => buildField($(container), ...args);
  add(
    "metadata-fields",
    "revision",
    "Catalog revision",
    "text",
    null,
    "Edition/year, if known; otherwise unknown.",
  );
  buildLanguages();
  add(
    "metadata-fields",
    "families",
    "Tool families",
    "text",
    null,
    "Milling cutters, drills, holders, inserts: this selects the data schema.",
  );
  add(
    "extract-fields",
    "extraction_model",
    "CPU extraction model",
    "select",
    [
      ["mineru-3.4.5-pipeline", "MinerU 3.4.5 · CPU model bundle"],
      ["baidu/Unlimited-OCR-cpu", "○ Baidu Unlimited-OCR · experimental CPU"],
    ],
    "The installed CPU bundle. No GPU parser is required.",
  );
  add(
    "baidu-fields",
    "baidu_output_tokens",
    "Maximum OCR output tokens",
    "number",
  );
  add("baidu-fields", "extraction_dpi", "Page rendering DPI", "number");
  inputs
    .get("extraction_model")
    .querySelector('option[value="baidu/Unlimited-OCR-cpu"]').disabled = true;
  const ocrActions = node("div", "ocr-model-actions");
  const ocrCheck = button("Check CPU OCR service", () =>
    checkOCR().catch(showError),
  );
  const ocrInstall = button("Install Baidu weights", async () => {
    try {
      ocrInstall.disabled = true;
      await api("/api/ocr/baidu/install", {
        method: "POST",
        body: JSON.stringify({ url: inputs.get("baidu_url").value }),
      });
      await checkOCR();
    } catch (error) {
      showError(error);
      ocrInstall.disabled = false;
    }
  });
  ocrInstall.id = "ocr-install";
  ocrInstall.disabled = true;
  const ocrStatus = node(
    "small",
    "help",
    "Optional container: baidu-ocr. Check service availability before installation.",
  );
  ocrStatus.id = "ocr-status";
  ocrActions.append(ocrCheck, ocrInstall, ocrStatus);
  const ocrSetup = node("details", "ocr-runtime-setup");
  const ocrRuntime = node("div", "field-grid");
  buildField(ocrRuntime, "baidu_url", "Baidu CPU service", "url");
  inputs.get("baidu_url").addEventListener("input", () => {
    ocrAvailability = { url: "", installed: false };
    refresh();
  });
  ocrSetup.append(
    node("summary", "", "Optional Baidu CPU runtime"),
    ocrRuntime,
    ocrActions,
  );
  $("extract-fields").append(ocrSetup);
  add("extract-fields", "method", "Initial extraction method", "select", [
    ["auto", "Automatic"],
    ["txt", "Native PDF text"],
    ["ocr", "OCR"],
  ]);
  add("extract-fields", "tables", "Extract tables", "checkbox");
  add("extract-fields", "formulas", "Extract formulas", "checkbox");
  add("extract-fields", "page_selection", "Choose specific pages", "checkbox");
  add("page-fields", "first_page", "First PDF page", "number");
  add(
    "page-fields",
    "last_page",
    "Last PDF page",
    "number",
    null,
    "Leave blank for the end. PDF page numbers start at 1.",
  );
  add(
    "quality-fields",
    "automatic_quality",
    "Adapt quality automatically",
    "checkbox",
  );
  add(
    "quality-fields",
    "confidence_threshold",
    "Required confidence",
    "number",
    null,
    "Also applies to model reviews; a lower score remains unresolved.",
  );
  add(
    "quality-fields",
    "max_quality_passes",
    "Maximum extraction passes per page",
    "number",
  );
  add("quality-fields", "parser_language", "OCR language group", "select", [
    ["ch", "Latin / English / Chinese / Japanese"],
    ["ch_server", "Latin / multilingual server"],
    ["east_slavic", "Russian / Ukrainian / Belarusian"],
    ["cyrillic", "Cyrillic"],
    ["korean", "Korean"],
    ["arabic", "Arabic / Persian"],
    ["devanagari", "Devanagari"],
    ["el", "Greek"],
    ["th", "Thai"],
    ["ka", "Georgian"],
    ["ta", "Tamil"],
    ["te", "Telugu"],
  ]);
  add("quality-fields", "force_parse", "Force a fresh extraction", "checkbox");
  add(
    "quality-fields",
    "parse_timeout",
    "Extraction timeout (seconds)",
    "number",
  );
  add("quality-fields", "mineru_url", "CPU service URL", "url");
  add("review-fields", "review_provider", "Evidence review engine", "select", [
    ["local", "Local LLM · Ollama"],
    ["work", "ChatGPT Work"],
  ]);
  add("review-fields", "model", "Stage 03 review model", "select");
  buildReasoningField("review-fields", "reasoning", "Stage 03 reasoning");
  add(
    "review-fields",
    "automatic_review",
    "Automatic Work review (uses Work allowance)",
    "checkbox",
  );
  add("final-fields", "final_provider", "Final validation engine", "select", [
    ["local", "Local LLM · Ollama"],
    ["work", "ChatGPT Work"],
  ]);
  add(
    "final-fields",
    "final_model",
    "Stage 06 final validation model",
    "select",
    [["none", "No model · leave unvalidated"]],
  );
  buildReasoningField("final-fields", "final_reasoning", "Stage 06 reasoning");
  add(
    "final-fields",
    "prepare_manual_review",
    "Prepare for manual review",
    "checkbox",
  );
  $("model-refresh").onclick = async () => {
    bootstrap.models = await api("/api/models");
    updateModels(true);
    refresh();
  };
  $("model-catalog-info").textContent = bootstrap.models.note;
  add("local-fields", "local_ram_gb", "Inference machine RAM", "select", [
    ["32", "32 GB · 8B / 9B vision"],
    ["128", "128 GB · 32B vision"],
    ["750", "750 GB · 235B vision"],
  ]);
  add("local-fields", "local_url", "Local Ollama server", "url");
  add("local-fields", "local_context_tokens", "Context tokens", "number");
  add("local-fields", "local_output_tokens", "Maximum output tokens", "number");
  add("local-fields", "local_temperature", "Temperature", "number");
  add("local-fields", "local_cpu_threads", "CPU threads", "number");
  add(
    "local-fields",
    "local_keep_loaded",
    "Keep model weights loaded",
    "checkbox",
  );
  add("local-fields", "local_preload", "Preload while parsing", "checkbox");
  add(
    "local-fields",
    "local_load_into_ram",
    "Load weights fully into RAM",
    "checkbox",
  );
  add(
    "local-fields",
    "local_load_timeout",
    "Model load timeout (seconds)",
    "number",
  );
  add(
    "source-fields",
    "local_source_urls",
    "Additional official pages (optional)",
    "text",
  );
  add(
    "source-fields",
    "local_search_url",
    "Optional source discovery server",
    "url",
  );
  inputs.get("local_ram_gb").addEventListener("change", () => {
    const preset = bootstrap.local_llm.presets.find(
      (p) => p.ram_gb === Number(inputs.get("local_ram_gb").value),
    );
    for (const [provider, name] of [
      ["review_provider", "model"],
      ["final_provider", "final_model"],
    ]) {
      if (inputs.get(provider).value === "local")
        inputs.get(name).value =
          name === "final_model"
            ? preset.final_model || preset.model
            : preset.model;
    }
    refresh();
  });
  $("local-model-refresh").onclick = () => refreshLocalModels();
  let serverTimer;
  inputs.get("local_url").addEventListener("input", () => {
    clearTimeout(serverTimer);
    resetLocalModels();
    updateModels(true);
    refresh();
    serverTimer = setTimeout(() => refreshLocalModels(), 600);
  });
  $("target-manufacturer").parentElement.append(
    helpButton(
      "One target manufacturer for parameter comparisons across the library. This does not determine the manufacturer of uploaded PDFs.",
      "Target manufacturer",
    ),
  );
  $("mapping-manufacturer").parentElement.append(
    helpButton(
      "Select the manufacturer's normalized product workbook. Its symbol dictionary is worksheet 2.",
      "Manufacturer product list",
    ),
  );
  add(
    "context-fields",
    "review_dpi",
    "PDF review image resolution (DPI)",
    "number",
  );
  add(
    "context-fields",
    "review_batch_rows",
    "RAW cells per review task",
    "number",
    null,
    "Smaller tasks keep evidence within a bounded context. Final validation reviews at most five tools per task.",
  );
  add(
    "context-fields",
    "review_timeout",
    "Time limit per review task (seconds)",
    "number",
  );
  add(
    "structure-fields",
    "force_structure",
    "Rebuild structured workbooks",
    "checkbox",
  );
  if (bootstrap.catalogs.length)
    selected.set(
      bootstrap.defaults.source_pdf,
      bootstrap.catalogs.find((c) => c.path === bootstrap.defaults.source_pdf)
        ?.detection?.manufacturer?.id || bootstrap.defaults.manufacturer_id,
    );
  renderSources();
  refresh();
}
function renderSources() {
  const search = $("catalog-search").value.toLowerCase();
  $("source-list").replaceChildren();
  for (const catalog of bootstrap.catalogs.filter((c) =>
    c.name.toLowerCase().includes(search),
  )) {
    const row = node("div", "source-row");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = selected.has(catalog.path);
    check.id = "source-" + bootstrap.catalogs.indexOf(catalog);
    const label = node("label", "", catalog.name);
    label.htmlFor = check.id;
    label.append(
      node(
        "small",
        "",
        `${catalog.pages} PDF pages · ${(catalog.bytes / 1024 / 1024).toFixed(1)} MB${catalog.origin ? " · " + catalog.origin : ""}`,
      ),
    );
    const metadata = catalog.detection;
    if (metadata)
      label.append(
        node(
          "small",
          "detection-info",
          `${metadata.manufacturer?.name || "Manufacturer unknown — choose below"} · ${metadata.language?.codes?.join(", ") || "language unknown"} · ${metadata.method}${metadata.cached ? " · cached" : ""}`,
        ),
      );
    const manufacturer = document.createElement("select");
    manufacturer.setAttribute("aria-label", "Manufacturer for " + catalog.name);
    options(
      manufacturer,
      bootstrap.manufacturers,
      selected.get(catalog.path) ||
        catalog.detection?.manufacturer?.id ||
        catalog.manufacturer_hint ||
        "",
    );
    manufacturer.prepend(
      Object.assign(node("option", "", "Choose manufacturer…"), { value: "" }),
    );
    manufacturer.value =
      selected.get(catalog.path) ||
      catalog.detection?.manufacturer?.id ||
      catalog.manufacturer_hint ||
      "";
    const language = node("input", "catalog-language");
    language.value =
      catalog.language_override ||
      metadata?.language?.codes?.join(",") ||
      "und";
    language.setAttribute("aria-label", "Languages for " + catalog.name);
    language.title = "Detected catalog languages; editable language codes";
    language.addEventListener("change", () => {
      catalog.language_override = language.value.trim() || "und";
      refresh();
    });
    const controls = node("div", "source-controls");
    controls.append(
      manufacturer,
      helpButton(
        "Detected from the opening pages. Change the source manufacturer when the catalog belongs to another brand; this is separate from the library target.",
        "Source manufacturer",
      ),
      language,
      helpButton(
        "Language codes detected locally. You can correct multilingual or uncertain results. These codes guide interpretation and OCR script selection.",
        "Catalog languages",
      ),
    );
    check.addEventListener("change", () => {
      if (check.checked) selected.set(catalog.path, manufacturer.value);
      else selected.delete(catalog.path);
      refresh();
    });
    manufacturer.addEventListener("change", () => {
      if (check.checked) selected.set(catalog.path, manufacturer.value);
      refresh();
    });
    row.append(check, label, controls);
    $("source-list").append(row);
  }
}
function refresh() {
  updateModels();
  const data = settings();
  const isBaidu = data.extraction_model === "baidu/Unlimited-OCR-cpu";
  $("baidu-fields").hidden = !isBaidu;
  for (const name of ["method", "parser_language", "mineru_url", "formulas"]) {
    inputs.get(name).disabled = isBaidu;
  }
  if (isBaidu) inputs.get("formulas").checked = true;
  inputs.get("local_preload").disabled = !data.local_keep_loaded;
  $("page-fields").hidden = !data.page_selection;
  $("page-scope-note").textContent = data.page_selection
    ? `Selected pages · ${data.first_page} to ${data.last_page || "end"} · partial catalogs remain unvalidated`
    : "Full catalog · every PDF page";
  $("selected-count").textContent = `${selected.size} selected`;
  const missingModels = [
    ["review_provider", "model"],
    ["final_provider", "final_model"],
  ].filter(
    ([provider, name]) =>
      data[provider] === "local" &&
      data[name] !== "none" &&
      (!bootstrap.local_llm.models.some((model) => model.id === data[name]) ||
        (localDeletion?.url === data.local_url.replace(/\/$/, "") &&
          localDeletion.model === data[name])),
  );
  const invalidSelection =
    !selected.size || [...selected.values()].some((v) => !v);
  $("run").disabled =
    invalidSelection ||
    missingModels.length > 0 ||
    (data.extraction_model === "baidu/Unlimited-OCR-cpu" &&
      (!ocrAvailability.installed || ocrAvailability.url !== data.baidu_url));
  $("preview").disabled = invalidSelection;
  $("queue-summary").replaceChildren();
  let total = 0;
  for (const catalog of bootstrap.catalogs.filter((c) =>
    selected.has(c.path),
  )) {
    total += data.page_selection
      ? (data.last_page || catalog.pages) - data.first_page + 1
      : catalog.pages;
    const item = node("div", "queue-item", catalog.name);
    const maker = bootstrap.manufacturers.find(
      (m) => m.id === selected.get(catalog.path),
    );
    item.append(
      node("small", "", `${maker?.name || "Unknown"} · ${catalog.pages} pages`),
    );
    $("queue-summary").append(item);
  }
  $("queue-summary").prepend(
    node(
      "strong",
      "",
      `${selected.size} catalogs · ${Math.max(0, total)} selected pages`,
    ),
  );
  $("local-settings").hidden =
    data.review_provider !== "local" && data.final_provider !== "local";
  $("model-refresh").hidden =
    data.review_provider === "local" && data.final_provider === "local";
  $("model-catalog-info").hidden = $("model-refresh").hidden;
  inputs.get("automatic_review").closest("label").hidden =
    data.review_provider === "local" && data.final_provider === "local";
  $("local-model-status").textContent = localDiscoveryBusy
    ? "Checking local server and installed vision models…"
    : bootstrap.local_llm.available
      ? `Local server connected · ${bootstrap.local_llm.models.length} installed vision models. No Work allowance is used for local tasks.`
      : bootstrap.local_llm.detail ||
        "Local server unavailable. Start the local-llm service and check its address to install models.";
  $("review-mode-note").textContent =
    data.model === "none"
      ? "No evidence model: page reviews, internet verification and model-assisted mapping are skipped. Extraction and structuring still run; the catalog remains unvalidated."
      : data.review_provider === "local"
        ? "Local evidence review · fresh image-backed tasks · no Work or Platform API fallback. Retrieved web sources are saved with each task."
        : data.automatic_review
          ? "Automatic Work review uses subscription allowance. Every task starts a fresh session with its stage's selected model and reasoning."
          : "Manual Work handoff: download each fresh task and image, select its model and reasoning in Work, then import its JSON response. Enable automatic review to use the subscription worker.";
  $("final-model-note").textContent =
    data.final_model === "none"
      ? "No final model: workbook integrity still runs, but tool validation is skipped and the catalog remains unvalidated."
      : `${data.final_provider === "local" ? "Local" : "Work"} final validation receives fresh tool-specific evidence and the source drawing. No previous task history is reused.`;
  $("reviewer-status").textContent = missingModels.length
    ? "Install the selected local models from their menus, choose an installed model, or choose No model to continue unvalidated."
    : data.model === "none"
      ? "Evidence model checks are disabled. No review requests will be sent."
      : data.review_provider === "local"
        ? "Local vision model installed and ready."
        : !data.automatic_review
          ? "Manual review selected. No automatic Work allowance is consumed."
          : bootstrap.reviewer.available
            ? "Subscription reviewer connected."
            : "Subscription worker is offline. Start make review-worker on the host to run automatic Work reviews.";
}
const reasoningLabels = {
  none: "None",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Maximum",
};
function buildReasoningField(container, name, label) {
  const box = node("label", "field reasoning-field");
  const header = node("span", "reasoning-header");
  const title = node("span", "field-title", label);
  title.append(helpButton(parameterHelp[name], label));
  const output = node("output", "reasoning-value");
  output.id = `${name}-value`;
  const input = document.createElement("input");
  Object.assign(input, { type: "range", name, min: "0", step: "1" });
  input.id = `${name}-slider`;
  input.setAttribute("aria-label", label);
  input.dataset.reasoning = bootstrap.defaults[name];
  output.htmlFor = input.id;
  header.append(title, output);
  const endpoints = node("span", "reasoning-endpoints");
  endpoints.id = `${name}-endpoints`;
  endpoints.setAttribute("aria-hidden", "true");
  box.append(header, input, endpoints);
  $(container).append(box);
  inputs.set(name, input);
  const change = () => {
    input.dataset.reasoning = JSON.parse(input.dataset.levels)[
      Number(input.value)
    ];
    refresh();
  };
  input.addEventListener("input", change);
  input.addEventListener("change", change);
}
function updateReasoning(name, modelName) {
  const input = inputs.get(name),
    model = inputs.get(modelName).value;
  const isLocal =
    inputs.get(modelName === "model" ? "review_provider" : "final_provider")
      .value === "local";
  const catalog = isLocal ? localModelChoices() : bootstrap.models.work_models;
  const levels =
    model === "none"
      ? JSON.parse(input.dataset.levels || '["high"]')
      : catalog.find((m) => m.id === model)?.reasoning_levels || ["high"];
  const current = input.dataset.reasoning;
  const effort = levels.includes(current)
    ? current
    : levels.includes(bootstrap.defaults[name])
      ? bootstrap.defaults[name]
      : levels.includes("medium")
        ? "medium"
        : levels[0];
  input.dataset.reasoning = effort;
  input.dataset.levels = JSON.stringify(levels);
  input.max = String(levels.length - 1);
  input.value = String(levels.indexOf(effort));
  input.disabled = model === "none" || levels.length < 2;
  input.closest("label").classList.toggle("disabled", model === "none");
  input.setAttribute("aria-valuetext", reasoningLabels[effort]);
  $(`${name}-value`).textContent =
    model === "none"
      ? "Off"
      : isLocal
        ? effort === "none"
          ? "Off"
          : "Thinking on"
        : reasoningLabels[effort];
  $(`${name}-endpoints`).replaceChildren(
    node("span", "", reasoningLabels[levels[0]]),
    node("span", "", reasoningLabels[levels.at(-1)]),
  );
}
let modelsLoaded = false;
function localModelChoices() {
  const choices = new Map();
  for (const job of localInstalls)
    if (job.model !== "none" && !choices.has(job.model))
      choices.set(job.model, job.model_info || { id: job.model });
  for (const name of ["model", "final_model"])
    if (
      inputs.get(name === "model" ? "review_provider" : "final_provider")
        .value === "local"
    ) {
      const id = inputs.get(name).value;
      if (
        id &&
        id !== "none" &&
        !bootstrap.models.work_models.some((m) => m.id === id) &&
        !choices.has(id)
      )
        choices.set(id, { id });
    }
  for (const preset of bootstrap.local_llm.presets) {
    for (const id of [preset.model, preset.final_model || preset.model])
      choices.set(id, {
        id,
        label: id,
        weights_gb: preset.weights_gb,
        reasoning_levels: id.includes("instruct")
          ? ["none"]
          : ["none", "medium"],
      });
  }
  for (const model of bootstrap.local_llm.models || [])
    choices.set(model.id, model);
  return [...choices.values()];
}
function updateModels(force = false) {
  const signature =
    inputs.get("review_provider").value + inputs.get("final_provider").value;
  if (modelsLoaded !== signature || force) {
    modelsLoaded = signature;
    for (const name of ["model", "final_model"]) {
      const isLocal =
        inputs.get(name === "model" ? "review_provider" : "final_provider")
          .value === "local";
      const choices = (
        isLocal ? localModelChoices() : bootstrap.models.work_models
      ).map((m) => [
        m.id,
        `${m.label || m.id}${isLocal || m.account_verified ? "" : " · access not confirmed"}`,
      ]);
      const input = inputs.get(name),
        old = input.value;
      const list = [...choices, ["none", "No model · leave unvalidated"]];
      input.replaceChildren(
        ...list.map(([value, label]) =>
          Object.assign(node("option", "", label), {
            value,
            disabled:
              isLocal &&
              value !== "none" &&
              !bootstrap.local_llm.models.some((m) => m.id === value),
          }),
        ),
      );
      input.value = list.some(([v]) => v === old)
        ? old
        : list.some(([v]) => v === bootstrap.defaults[name])
          ? bootstrap.defaults[name]
          : list.find(([v]) => v === "gpt-6.1-sol")?.[0] || list[0]?.[0] || "";
    }
  }
  updateReasoning("reasoning", "model");
  updateReasoning("final_reasoning", "final_model");
  for (const name of ["model", "final_model"]) renderModelPicker(name);
}

function localServerUrl() {
  return inputs.get("local_url").value.trim().replace(/\/$/, "");
}
function resetLocalModels() {
  ++localDiscoverySerial;
  bootstrap.local_llm = { presets: bootstrap.local_llm.presets, models: [] };
  localInstalls = [];
  localDiscoveryBusy = false;
  localDiscoveryAt = 0;
  modelDeleteConfirmation = null;
  modelDeletionError = "";
}
function closeModelPickers() {
  for (const picker of modelPickers.values()) {
    picker.panel.hidden = true;
    picker.trigger.setAttribute("aria-expanded", "false");
    picker.root.classList.remove("open");
  }
}
function buildModelPicker(name, box) {
  const root = node("div", "model-picker");
  root.dataset.field = name;
  const trigger = button(
    "",
    () => {
      const opening = panel.hidden;
      closeModelPickers();
      if (!opening) return;
      panel.hidden = false;
      root.classList.add("open");
      trigger.setAttribute("aria-expanded", "true");
      const overflow =
        root.getBoundingClientRect().left + panel.offsetWidth >
        window.innerWidth - 20;
      panel.style.left = overflow ? "auto" : "0";
      panel.style.right = overflow ? "0" : "auto";
      const rect = root.getBoundingClientRect();
      const below = window.innerHeight - rect.bottom - 20;
      const above = rect.top - 20;
      const upward = below < panel.offsetHeight && above > below;
      panel.style.top = upward ? "auto" : "calc(100% + 7px)";
      panel.style.bottom = upward ? "calc(100% + 7px)" : "auto";
      list.style.maxHeight = `${Math.max(100, Math.min(390, (upward ? above : below) - 95))}px`;
      if (Date.now() - localDiscoveryAt > 15000) refreshLocalModels();
    },
    "model-picker-trigger",
  );
  trigger.id = `${name}-picker`;
  trigger.setAttribute("aria-labelledby", `${name}-label ${name}-selection`);
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-controls", `${name}-model-menu`);
  const icon = node("span", "model-state-icon");
  const selection = node("span", "model-selection");
  selection.id = `${name}-selection`;
  trigger.append(icon, selection, node("span", "model-chevron", "⌄"));
  const panel = node("div", "model-menu");
  panel.id = `${name}-model-menu`;
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-labelledby", `${name}-label`);
  const heading = node("div", "model-menu-heading");
  heading.append(
    node("strong", "", "Local vision models"),
    button("Refresh", () => refreshLocalModels(), "model-refresh-inline"),
  );
  const status = node("p", "model-menu-status");
  status.setAttribute("role", "status");
  const list = node("div", "model-menu-list");
  list.setAttribute("role", "list");
  panel.append(heading, status, list);
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      closeModelPickers();
      trigger.focus();
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      const buttons = [...panel.querySelectorAll("button:not(:disabled)")];
      let index = buttons.indexOf(document.activeElement);
      index =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? buttons.length - 1
            : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) %
              buttons.length;
      buttons[index]?.focus();
      event.preventDefault();
    }
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      if (panel.hidden) trigger.click();
      panel.querySelector("button:not(:disabled)")?.focus();
      event.preventDefault();
    }
  });
  root.append(trigger, panel);
  box.append(root);
  modelPickers.set(name, {
    root,
    trigger,
    panel,
    list,
    status,
    icon,
    selection,
  });
}
function modelState(id) {
  if (id === "none")
    return { icon: "∅", text: "Model checks disabled", installed: true };
  const job = localInstalls.find((j) => j.model === id);
  if (localDeletion?.url === localServerUrl() && localDeletion.model === id)
    return { icon: "◌", text: "Deleting weights…", deleting: true };
  if (bootstrap.local_llm.models.some((m) => m.id === id))
    return { icon: "✓", text: "Installed", installed: true };
  if (job && ["queued", "downloading"].includes(job.status))
    return {
      icon: "◌",
      text: job.percent == null ? job.message : `Installing ${job.percent}%`,
      job,
      active: true,
    };
  if (job && ["failed", "interrupted"].includes(job.status))
    return { icon: "!", text: job.error || job.message, job, failed: true };
  return bootstrap.local_llm.available
    ? { icon: "↓", text: "Not installed" }
    : {
        icon: "?",
        text: localDiscoveryBusy
          ? "Checking installation…"
          : "Server unavailable",
      };
}
function renderModelPicker(name) {
  const picker = modelPickers.get(name);
  if (!picker) return;
  const input = inputs.get(name);
  const isLocal =
    inputs.get(name === "model" ? "review_provider" : "final_provider")
      .value === "local";
  input.hidden = isLocal;
  picker.root.hidden = !isLocal;
  if (!isLocal) {
    picker.panel.hidden = true;
    picker.trigger.setAttribute("aria-expanded", "false");
    picker.root.classList.remove("open");
    return;
  }
  const current = modelState(input.value);
  picker.trigger.classList.toggle("unavailable", !current.installed);
  picker.trigger.title = current.text;
  picker.selection.textContent =
    input.value === "none" ? "No model · leave unvalidated" : input.value;
  picker.icon.textContent = current.icon;
  picker.icon.classList.toggle("installed", current.installed);
  picker.status.textContent = localDiscoveryBusy
    ? "Checking server…"
    : bootstrap.local_llm.available
      ? "✓ Installed · ↓ Not installed. Install downloads to the selected server."
      : "Server unavailable. Check the local server address to enable installation.";
  const choices = [...localModelChoices(), { id: "none" }];
  if (!choices.some((m) => m.id === input.value) && input.value)
    choices.unshift({ id: input.value });
  const signature = JSON.stringify([
    choices,
    input.value,
    localInstalls,
    bootstrap.local_llm.available,
    localDeletion,
    modelDeleteConfirmation,
    modelDeletionError,
  ]);
  if (picker.signature === signature) return;
  picker.signature = signature;
  const focused = picker.list.contains(document.activeElement)
    ? document.activeElement.dataset
    : null;
  picker.list.replaceChildren(
    ...choices.map((model) => {
      const state = modelState(model.id);
      const row = node(
        "div",
        `model-entry${state.installed ? " installed" : " unavailable"}${model.id === input.value ? " selected" : ""}`,
      );
      row.dataset.model = model.id;
      row.setAttribute("role", "listitem");
      const choose = button(
        "",
        () => {
          input.value = model.id;
          input.dispatchEvent(new Event("change", { bubbles: true }));
          closeModelPickers();
          picker.trigger.focus();
        },
        "model-choice",
      );
      choose.dataset.model = model.id;
      choose.dataset.action = "choose";
      choose.disabled = !state.installed;
      choose.setAttribute(
        "aria-label",
        `Choose ${model.id === "none" ? "No model" : model.id}`,
      );
      choose.setAttribute("aria-pressed", String(model.id === input.value));
      const text = node("span", "model-choice-text");
      text.append(
        node(
          "strong",
          "",
          model.id === "none"
            ? "No model · leave unvalidated"
            : model.label || model.id,
        ),
        node(
          "small",
          "",
          `${state.text}${model.weights_gb ? ` · ≈${model.weights_gb} GB` : ""}`,
        ),
      );
      choose.append(node("span", "model-state-icon", state.icon), text);
      row.append(choose);
      if (model.id !== "none" && (state.installed || state.deleting)) {
        const remove = button(
          state.deleting ? "Deleting…" : "Delete",
          () => {
            modelDeleteConfirmation = model.id;
            modelDeletionError = "";
            refresh();
          },
          "model-delete",
        );
        remove.dataset.model = model.id;
        remove.dataset.action = "delete";
        remove.setAttribute("aria-label", `Delete ${model.id}`);
        remove.title =
          "Remove this model's installed weights from the selected server";
        remove.disabled =
          !!localDeletion ||
          !bootstrap.local_llm.available ||
          localInstalls.some((j) =>
            ["queued", "downloading"].includes(j.status),
          );
        row.append(remove);
      } else if (model.id !== "none") {
        const install = button(
          state.active ? "Installing…" : state.failed ? "Retry" : "Install",
          () => installModel(model.id),
          "model-install",
        );
        install.dataset.model = model.id;
        install.dataset.action = "install";
        install.setAttribute(
          "aria-label",
          `${state.failed ? "Retry installing" : "Install"} ${model.id}`,
        );
        install.disabled =
          state.active ||
          !!localDeletion ||
          !bootstrap.local_llm.available ||
          localInstalls.some((j) =>
            ["queued", "downloading"].includes(j.status),
          );
        row.append(install);
      }
      if (
        modelDeleteConfirmation === model.id &&
        (state.installed || state.deleting)
      ) {
        const confirmation = node("div", "model-delete-confirmation");
        confirmation.setAttribute("role", "group");
        confirmation.setAttribute("aria-label", `Delete installed ${model.id}`);
        confirmation.append(
          node(
            "p",
            "",
            "Remove installed weights from this server? You can install the model again.",
          ),
        );
        if (modelDeletionError)
          confirmation.append(
            node("p", "model-delete-error", modelDeletionError),
          );
        const confirm = button(
          "Delete weights",
          () => deleteModel(model.id),
          "model-delete",
        );
        confirm.setAttribute("aria-label", `Confirm delete ${model.id}`);
        const cancel = button(
          "Cancel",
          () => {
            modelDeleteConfirmation = null;
            modelDeletionError = "";
            refresh();
          },
          "model-refresh-inline",
        );
        cancel.setAttribute("aria-label", `Cancel deleting ${model.id}`);
        confirm.dataset.model = cancel.dataset.model = model.id;
        confirm.dataset.action = "confirm-delete";
        cancel.dataset.action = "cancel-delete";
        confirm.disabled = row.querySelector(".model-delete").disabled;
        cancel.disabled = !!localDeletion;
        confirmation.append(confirm, cancel);
        row.append(confirmation);
      }
      if (state.active) {
        const progress = node("progress", "model-install-progress");
        progress.max = 100;
        if (state.job.percent != null) progress.value = state.job.percent;
        progress.setAttribute("aria-label", `Installing ${model.id}`);
        row.append(progress);
      }
      return row;
    }),
  );
  if (focused?.model) {
    [...picker.list.querySelectorAll("button")]
      .find(
        (b) =>
          b.dataset.model === focused.model &&
          b.dataset.action === focused.action,
      )
      ?.focus();
  }
}
async function refreshLocalModels() {
  const url = localServerUrl(),
    serial = ++localDiscoverySerial;
  localDiscoveryBusy = true;
  refresh();
  try {
    const [metadata, progress] = await Promise.all([
      api("/api/local-models", {
        method: "POST",
        body: JSON.stringify({ url }),
      }),
      api("/api/local-model-installs?url=" + encodeURIComponent(url)),
    ]);
    if (serial !== localDiscoverySerial || url !== localServerUrl()) return;
    bootstrap.local_llm = { ...metadata, url };
    localInstalls = progress.installs;
    localDiscoveryAt = Date.now();
  } catch (error) {
    if (serial === localDiscoverySerial)
      bootstrap.local_llm = {
        presets: bootstrap.local_llm.presets,
        models: [],
        available: false,
        detail: error.message,
        url,
      };
  } finally {
    if (serial === localDiscoverySerial) {
      localDiscoveryBusy = false;
      updateModels(true);
      refresh();
    }
  }
}
async function installModel(model) {
  const url = localServerUrl();
  try {
    const job = await api("/api/local-model-installs", {
      method: "POST",
      body: JSON.stringify({ url, model }),
    });
    if (url !== localServerUrl()) return;
    localInstalls = [job, ...localInstalls.filter((j) => j.model !== model)];
    refresh();
    pollModelInstalls();
  } catch (error) {
    showError(error);
  }
}
async function deleteModel(model) {
  if (localDeletion) return;
  const url = localServerUrl();
  localDeletion = { url, model };
  modelDeletionError = "";
  refresh();
  try {
    await api("/api/local-models", {
      method: "DELETE",
      body: JSON.stringify({ url, model }),
    });
    if (url !== localServerUrl()) return;
    bootstrap.local_llm.models = bootstrap.local_llm.models.filter(
      (m) => m.id !== model,
    );
    modelDeleteConfirmation = null;
    await refreshLocalModels();
  } catch (error) {
    if (url === localServerUrl()) modelDeletionError = error.message;
  } finally {
    localDeletion = null;
    refresh();
  }
}
async function pollModelInstalls() {
  if (
    installPollBusy ||
    localDiscoveryBusy ||
    localDeletion ||
    !bootstrap.local_llm.available
  )
    return;
  installPollBusy = true;
  const url = localServerUrl();
  try {
    const result = await api(
      "/api/local-model-installs?url=" + encodeURIComponent(url),
    );
    if (url !== localServerUrl()) return;
    const completed = result.installs.some(
      (j) =>
        j.status === "installed" &&
        (!localInstalls.some(
          (old) => old.id === j.id && old.status === "installed",
        ) ||
          (!bootstrap.local_llm.models.some((m) => m.id === j.model) &&
            Date.now() - Date.parse(j.updated_at) < 10000)),
    );
    localInstalls = result.installs;
    if (completed) await refreshLocalModels();
    else refresh();
  } catch (error) {
    $("local-model-status").textContent = error.message;
  } finally {
    installPollBusy = false;
  }
}
function showView(view) {
  $("pipeline-view").hidden = view !== "pipeline";
  $("library-view").hidden = view !== "library";
  $("show-pipeline").classList.toggle("active", view === "pipeline");
  $("show-library").classList.toggle("active", view === "library");
  $("sidebar-library").classList.toggle("active", view === "library");
  if (view === "library") loadLibrary().catch(showError);
}
async function upload(files) {
  $("upload-status").textContent = `Uploading ${files.length} PDFs…`;
  try {
    for (const file of files) {
      const result = await api(
        "/api/upload?name=" + encodeURIComponent(file.name),
        { method: "POST", body: file, raw: true },
      );
      bootstrap.catalogs.unshift(result);
      const maker = result.detection?.manufacturer?.id || "";
      selected.set(result.path, maker);
    }
    $("upload-status").textContent =
      `${files.length} PDFs added. Check each manufacturer before running.`;
    $("catalog-search").value = "";
    renderSources();
    refresh();
  } catch (error) {
    showError(error);
    $("upload-status").textContent = "Upload failed.";
  }
  $("pdf-upload").value = "";
}
function table(headers, rows, metadata = []) {
  const result = document.createElement("table");
  const head = document.createElement("thead"),
    tr = document.createElement("tr");
  for (const header of headers) tr.append(node("th", "", header));
  head.append(tr);
  result.append(head);
  const body = document.createElement("tbody");
  for (const [index, row] of rows.entries()) {
    const tr = document.createElement("tr");
    if (metadata[index]) {
      tr.className = metadata[index].className;
      tr.dataset.status = metadata[index].status;
      tr.title = metadata[index].label;
      tr.setAttribute("aria-label", metadata[index].label);
    }
    for (const value of row) {
      const td = document.createElement("td");
      if (value instanceof Node) td.append(value);
      else td.textContent = value ?? "—";
      tr.append(td);
    }
    body.append(tr);
  }
  result.append(body);
  return result;
}
async function loadMapping() {
  currentMapping = await api(
    `/api/manufacturers/${$("mapping-manufacturer").value}/mapping`,
  );
  $("mapping-json").value = JSON.stringify(currentMapping, null, 2);
  const dictionary = currentMapping.symbol_dictionary || [];
  const products = await api(
    `/api/manufacturers/${$("mapping-manufacturer").value}/products`,
  );
  $("manufacturer-products").replaceChildren(
    products.rows.length
      ? table(
          ["Tool", "Catalog", "Identity", "Parameters"],
          products.rows.map((p) => [
            p.order_number || p.article_number || p.product_version_id,
            p.catalog_name,
            p.identity_status,
            p.attributes
              .map(
                (a) =>
                  `${a.canonical_field}: ${a.value_decimal ?? a.value_text ?? a.value_min ?? "?"} ${a.canonical_unit || ""}`,
              )
              .join(" · "),
          ]),
        )
      : node(
          "p",
          "help",
          "No normalized tools yet. This manufacturer's product list will fill after structuring.",
        ),
  );
  $("manufacturer-workbook").href = products.workbook_url;
  $("dictionary-summary").textContent =
    `${currentMapping.manufacturer} · ${currentMapping.series.length} known series · ${currentMapping.rules.length} rules · ${dictionary.length} symbol definitions. ${dictionary.length ? "Definitions are limited to their stated families and series." : "Detailed meanings will be proposed from the page and manufacturer evidence. Existing field codes alone do not establish geometric meanings."}`;
  const rows = dictionary.length
    ? dictionary.map((s) => [
        s.symbol,
        s.canonical_field,
        s.description,
        s.unit || "—",
        [s.family, ...s.series].join(" · "),
        s.status,
        s.source_page,
      ])
    : currentMapping.rules.map((r) => [
        r.raw_headers.join(", "),
        r.canonical_field,
        "Definition pending manufacturer review",
        r.canonical_unit,
        [...r.families, ...(r.series || [])].join(" · "),
        "Unverified meaning",
        "—",
      ]);
  $("dictionary").replaceChildren(
    rows.length
      ? table(
          [
            "Letters",
            "Parameter",
            "Concise meaning",
            "Unit",
            "Scope",
            "Review",
            "PDF evidence",
          ],
          rows,
        )
      : node(
          "p",
          "help",
          "This manufacturer has its own empty draft. Review will create its mapping from the catalog; another manufacturer’s profile is never substituted.",
        ),
  );
}
function duplicateElement(d) {
  const box = node("div", "duplicate");
  const left = d.left.product,
    right = d.right.product;
  box.append(
    node(
      "strong",
      "",
      d.kind === "same_parameters"
        ? "Matching available parameters — duplicate candidate"
        : "Same name — conflicting or incomplete parameters",
    ),
  );
  box.append(
    node(
      "p",
      "",
      `${left.order_number || left.article_number || left.product_version_id} ↔ ${right.order_number || right.article_number || right.product_version_id}`,
    ),
  );
  box.append(node("p", "", `${d.left.catalog_id} ↔ ${d.right.catalog_id}`));
  const details = node("details");
  details.append(node("summary", "", "Compare parameter values"));
  details.append(
    node(
      "pre",
      "",
      JSON.stringify(
        { left: left.attributes, right: right.attributes },
        null,
        2,
      ),
    ),
  );
  box.append(details);
  return box;
}
async function loadLibrary() {
  const data = await api("/api/library");
  $("library-count").textContent = String(data.catalogs.length);
  const search = $("library-search").value.toLowerCase();
  $("library-manufacturers").replaceChildren(
    ...data.manufacturers
      .filter((m) => m.name.toLowerCase().includes(search))
      .map((m) => {
        const tile = node(
          "div",
          "manufacturer-tile" +
            (m.id === data.target_manufacturer_id ? " target" : ""),
          m.name,
        );
        tile.append(
          node(
            "small",
            "",
            `${m.catalog_count} catalogs · ${m.validated_count || 0} validated${m.pending_count ? ` · ${m.pending_count} awaiting parsing` : ""}${m.id === data.target_manufacturer_id ? " · TARGET" : ""}`,
          ),
        );
        return tile;
      }),
  );
  const catalogs = data.catalogs.filter((c) =>
    (c.name + c.manufacturer).toLowerCase().includes(search),
  );
  $("library-catalogs").replaceChildren(
    catalogs.length
      ? table(
          [
            "Catalog",
            "Manufacturer",
            "Revision",
            "Pages",
            "Tools",
            "Processed / source modified",
            "Confidence · average / worst",
            "Technical history",
            "Result",
          ],
          catalogs.map((c) => [
            c.name,
            c.manufacturer,
            c.revision,
            c.pages,
            c.product_count,
            c.technical?.completed_at || c.source_modified_at
              ? new Date(
                  c.technical?.completed_at || c.source_modified_at,
                ).toLocaleString()
              : "—",
            c.technical?.confidence?.average == null
              ? "Not assessed"
              : `${(100 * c.technical.confidence.average).toFixed(1)}% / ${(100 * c.technical.confidence.worst).toFixed(1)}%`,
            catalogTechnical(c),
            c.last_run
              ? button("Open", () => {
                  selectedRun = c.last_run;
                  showView("pipeline");
                  fetchRun(c.last_run);
                  $("results").scrollIntoView({ behavior: "smooth" });
                })
              : "—",
          ]),
          catalogs.map((c) => ({
            className: `catalog-row ${["loaded", "registered"].includes(c.status) ? "loaded" : c.status === "validated" ? "validated" : "attention"}`,
            status: c.status,
            label: `${c.name}: ${c.status === "loaded" ? "Loaded, not parsed" : c.status}`,
          })),
        )
      : node("p", "help", "No loaded or registered catalogs match."),
  );
  $("library-duplicates").replaceChildren(
    ...(data.duplicates.length
      ? data.duplicates.map(duplicateElement)
      : [
          node(
            "p",
            "help",
            "No duplicate candidates found in structured library records.",
          ),
        ]),
  );
}
function catalogTechnical(catalog) {
  const info = catalog.technical;
  if (!info) return "Awaiting parsing";
  const detail = node("details", "catalog-technical");
  detail.append(node("summary", "", "Models & measurements"));
  detail.append(
    node("p", "help", `Extractor: ${info.extraction_model || "Unknown"}`),
  );
  for (const check of info.checks || [])
    detail.append(
      node(
        "p",
        "help",
        `${check.stage}: ${check.model} · ${check.reasoning} · ${check.provider}${check.tasks ? ` · ${check.tasks} tasks` : ""}${check.tokens_per_second != null ? ` · ${check.tokens_per_second} tokens/s` : ""}`,
      ),
    );
  if (!info.checks?.length)
    detail.append(
      node(
        "p",
        "help",
        "No recorded model execution. Requested models are not proof that a check ran.",
      ),
    );
  const parameters = node(
    "pre",
    "technical-values",
    JSON.stringify(info.parameters, null, 2),
  );
  detail.append(parameters);
  for (const previous of (catalog.history || []).slice(1))
    detail.append(
      node(
        "p",
        "help",
        `Previous run: ${previous.completed_at ? new Date(previous.completed_at).toLocaleString() : "unfinished"} · ${previous.status} · ${previous.run_id}`,
      ),
    );
  for (const stage of info.stages || []) {
    const elapsed =
      stage.started_at && stage.completed_at
        ? `${Math.round((Date.parse(stage.completed_at) - Date.parse(stage.started_at)) / 1000)}s`
        : "—";
    detail.append(
      node("p", "help", `${stage.title}: ${stage.status} · ${elapsed}`),
    );
  }
  if (info.performance?.peak)
    detail.append(
      node(
        "p",
        "help",
        `Peak host CPU ${info.performance.peak.cpu_percent ?? "—"}% · RAM ${formatGiB(info.performance.peak.memory_used_bytes)} · swap ${formatGiB(info.performance.peak.swap_used_bytes)}. Host measurements include other applications.`,
      ),
    );
  return detail;
}
function formatGiB(bytes) {
  return bytes == null ? "—" : `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}
async function checkOCR() {
  const url = inputs.get("baidu_url").value;
  const info = await api("/api/ocr/baidu?url=" + encodeURIComponent(url));
  if (url !== inputs.get("baidu_url").value) return;
  ocrAvailability = { url, installed: !!info.installed };
  const option = inputs
    .get("extraction_model")
    .querySelector('option[value="baidu/Unlimited-OCR-cpu"]');
  option.textContent = `${info.installed ? "●" : "○"} Baidu Unlimited-OCR · experimental CPU${info.installed ? " · installed" : " · not installed"}`;
  option.disabled = !info.installed;
  const busy = info.installation?.status === "installing";
  $("ocr-install").disabled = !info.available || info.installed || busy;
  $("ocr-install").textContent = busy
    ? "Installing…"
    : info.installed
      ? "Installed"
      : "Install Baidu weights";
  $("ocr-status").textContent = info.available
    ? info.installed
      ? "CPU extraction model ready. Select it above to extract page text and RAW tables."
      : busy
        ? "Downloading pinned model files. Progress is logged in the Baidu CPU container."
        : info.installation?.error ||
          "CPU service ready; weights are not installed."
    : "Start the optional baidu-ocr CPU container. See deploy/baidu-cpu/README.md.";
  if (busy) setTimeout(() => checkOCR().catch(showError), 5000);
  refresh();
}
function renderOfficialSources(run) {
  const box = $("official-source-status"),
    source = run?.official_sources;
  box.hidden = !source;
  if (!source) return;
  box.replaceChildren(
    node("strong", "", "Official manufacturer sources · fixed for this run"),
  );
  for (const url of source.official_urls || []) {
    const link = node("a", "", url);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const line = node("p");
    line.append(link);
    box.append(line);
  }
  box.append(
    node(
      "small",
      "help",
      source.status === "ready"
        ? "Resolved automatically before review. All review tasks use the same saved source snapshot."
        : source.reason ||
            "Official evidence is unavailable. Internet checks remain unverified.",
    ),
  );
}
function renderPerformance(run) {
  let box = $("performance-status");
  if (!box) {
    box = node("details", "performance-status");
    box.id = "performance-status";
    box.open = true;
  }
  $(processingStage || "extract")
    .querySelector(":scope > .stage-mini-progress")
    .after(box);
  const open = box.open,
    p = run?.performance,
    g =
      run?.current_stage === "parse" &&
      ["loading", "queued", "generating", "prefill"].includes(
        run?.extraction_generation?.status,
      )
        ? run.extraction_generation
        : run?.generation;
  box.hidden = !p?.current && !g;
  if (box.hidden) return;
  box.replaceChildren(node("summary", "", "Live performance & bottlenecks"));
  box.open = open;
  const sample = p?.current || {},
    peak = p?.peak || {};
  const activeStage = run.stages?.find(
    (stage) => stage.id === run.current_stage,
  );
  if (activeStage?.started_at)
    box.append(
      node(
        "p",
        "help",
        `${activeStage.title} · ${Math.max(0, Math.round((activeStage.completed_at || run.completed_at ? Date.parse(activeStage.completed_at || run.completed_at) : Date.now()) / 1000 - Date.parse(activeStage.started_at) / 1000))}s wall time`,
      ),
    );
  if (p?.hardware)
    box.append(
      node(
        "p",
        "help",
        `${p.hardware.cpu} · ${formatGiB(p.hardware.memory_bytes)} usable RAM · ${p.hardware.physical_cores} physical cores · model threads ${run.config?.local_cpu_threads ?? "auto"}`,
      ),
    );
  if (g?.model) {
    const rate =
      g.tokens_per_second == null
        ? "Waiting for first token / processing prompt"
        : `${g.tokens_estimated ? "≈ " : ""}${g.tokens_per_second} ${g.tokens_estimated ? "token chunks/s (estimate)" : "tokens/s (measured)"}`;
    box.append(
      node(
        "p",
        "performance-model",
        `${g.model} · ${g.stage && g.stage !== run.current_stage ? `last ${g.stage} task · ` : ""}${g.status} · ${rate}`,
      ),
    );
    if (g.started_at)
      box.append(
        node(
          "small",
          "help",
          `Elapsed ${Math.round(["prefill", "generating", "loading", "queued"].includes(g.status) ? Date.now() / 1000 - g.started_at : (g.elapsed_seconds ?? Date.now() / 1000 - g.started_at))}s`,
        ),
      );
    box.append(
      node(
        "p",
        "help",
        `${g.tokens ?? g.chunks ?? 0} ${g.tokens_estimated ? "chunks" : "tokens"} · first token ${g.first_token_seconds ?? "—"}s · prompt ${g.prompt_tokens_per_second ?? "—"} tokens/s`,
      ),
    );
  }
  box.append(
    node(
      "p",
      "performance-metrics",
      `CPU ${sample.cpu_percent ?? "—"}% · peak ${peak.cpu_percent ?? "—"}% | RAM available ${formatGiB(sample.memory_available_bytes)} · peak used ${formatGiB(peak.memory_used_bytes)} | I/O wait ${sample.iowait_percent ?? "—"}%`,
    ),
  );
  const swapActive =
    (sample.swap_in_bytes_sec || 0) + (sample.swap_out_bytes_sec || 0) > 0;
  const swapKnown =
    sample.swap_in_bytes_sec != null && sample.swap_out_bytes_sec != null;
  box.append(
    node(
      "p",
      swapActive ? "swap-active" : "help",
      `Swap: ${formatGiB(sample.swap_used_bytes)} used${swapKnown ? ` · in ${Math.round(sample.swap_in_bytes_sec / 1024)} KiB/s · out ${Math.round(sample.swap_out_bytes_sec / 1024)} KiB/s${swapActive ? " · ACTIVE PAGING" : " · no active paging measured"}` : " · awaiting rate measurements"}`,
    ),
  );
  for (const finding of p?.bottlenecks || [])
    box.append(node("p", "help", finding.text));
  const runner = p?.inference_process;
  if (runner?.available)
    box.append(
      node(
        "p",
        runner.swap_bytes > 0 ? "swap-active" : "performance-metrics",
        `Ollama processes: RAM ${formatGiB(runner.rss_bytes)} · sampled peak ${formatGiB(peak.inference_rss_bytes ?? runner.rss_bytes)} · swapped ${formatGiB(runner.swap_bytes)} · CPU cores used ${runner.cpu_cores_used ?? "—"} (peak ${peak.inference_cpu_cores ?? "—"}) · major faults/s ${runner.major_faults_per_second ?? "—"}`,
      ),
    );
  for (const model of p?.inference_models || [])
    box.append(
      node(
        "p",
        "help",
        `Ollama allocation: ${model.name} · ${formatGiB(model.size)} total · ${formatGiB(model.size_vram)} GPU · context ${model.context_length ?? "—"}. Allocation is not a measurement of resident RAM.`,
      ),
    );
  box.append(
    node(
      "small",
      "help",
      p?.scope ||
        "Token rates apply to the selected model. Resource counters describe this application host.",
    ),
  );
}
function stageProgress(run, stage) {
  const stopped = [
    "paused",
    "pausing",
    "stopping",
    "failed",
    "cancelled",
    "interrupted",
  ].includes(run?.status);
  const status =
    stage.status === "running" && stopped ? run.status : stage.status;
  if (status === "completed")
    return {
      percent: 100,
      status,
      detail: stage.quality_blocked ? "Findings need review" : "Completed",
    };
  if (status === "skipped")
    return { percent: 0, status, detail: "Skipped · no model" };
  if (Number.isFinite(stage.progress?.percent))
    return {
      percent: Math.max(0, Math.min(99, Math.round(stage.progress.percent))),
      estimated: stage.progress.estimated,
      detail: stage.progress.detail || status.replaceAll("_", " "),
      status,
    };
  const start = Date.parse(stage.started_at);
  if (!Number.isFinite(start))
    return {
      percent: 0,
      status,
      detail: status === "queued" ? "Waiting" : status.replaceAll("_", " "),
    };
  const end =
    status === "running" && !stopped
      ? Date.now()
      : Date.parse(
          stage.completed_at ||
            run.stoppage?.occurred_at ||
            run.completed_at ||
            stage.started_at,
        );
  const elapsed = Math.max(0, (end - start) / 1000) || 0;
  const pages = run.pages?.total || 1;
  const expected =
    {
      catalog: 5,
      raw: Math.max(15, pages * 0.25),
      structure: Math.max(25, pages * 0.5),
      duplicates: 20,
      validate: Math.max(10, pages * 0.2),
    }[stage.id] || 60;
  return {
    percent: Math.min(95, Math.floor((95 * elapsed) / (elapsed + expected))),
    estimated: true,
    status,
    detail: `${status.replaceAll("_", " ")} · time-based estimate`,
  };
}
function sectionProgress(run, section) {
  const members = (run?.stages || []).filter(
    (s) => stageSection[s.id] === section,
  );
  const enabled = members.filter((s) => s.status !== "skipped");
  if (!enabled.length)
    return {
      percent: 0,
      status: members.length ? "skipped" : "queued",
      detail: members.length ? "Skipped · no model" : "No catalog selected",
    };
  const values = enabled.map((s) => ({
    ...stageProgress(run, s),
    title: s.title,
  }));
  const current =
    values.find(
      (s) => !["completed", "queued", "not_run"].includes(s.status),
    ) ||
    values.find((s) => s.status !== "completed") ||
    values.at(-1);
  return {
    ...current,
    detail:
      values.length > 1 && current.status !== "completed"
        ? `${current.title} · ${current.detail}`
        : current.detail,
    percent: Math.min(
      values.every((s) => s.status === "completed") ? 100 : 99,
      Math.round(values.reduce((sum, s) => sum + s.percent, 0) / values.length),
    ),
    estimated:
      values.some((s) => s.estimated) ||
      (values.length > 1 &&
        values.some((s) => s.percent > 0) &&
        current.status !== "completed"),
  };
}
function updateMiniProgress(box, label, value) {
  if (!box.firstChild) {
    const caption = node("div", "stage-mini-caption");
    caption.append(
      node("span", "stage-mini-detail"),
      node("strong", "stage-mini-value"),
    );
    const track = node("div", "stage-mini-track");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.append(node("span", "stage-mini-fill"));
    box.append(caption, track);
  }
  box.className = `stage-mini-progress ${value.status}`;
  box.title = value.estimated
    ? "Approximate progress. Page/task counters are used when available; other operations use elapsed time. 100% appears only after the stage completes."
    : value.detail;
  box.querySelector(".stage-mini-detail").textContent = value.detail;
  const text =
    value.status === "skipped"
      ? "—"
      : `${value.estimated ? "≈" : ""}${value.percent}%`;
  box.querySelector(".stage-mini-value").textContent = text;
  const track = box.querySelector(".stage-mini-track");
  track.setAttribute("aria-label", `${label} progress`);
  track.setAttribute("aria-valuenow", String(value.percent));
  track.setAttribute("aria-valuetext", `${text} · ${value.detail}`);
  box.querySelector(".stage-mini-fill").style.width = value.percent + "%";
}
function miniProgress(label, value) {
  const box = node("div", "stage-mini-progress");
  updateMiniProgress(box, label, value);
  return box;
}
function renderProgress() {
  renderQueueControls();
  const active = runs.filter((r) => !terminal.has(r.status));
  const current = runs.find((r) => r.id === selectedRun) || active[0];
  const recent = [
    ...active,
    ...(current && !active.includes(current) ? [current] : []),
  ];
  $("progress-summary").textContent = active.length
    ? `${active.length} catalog${active.length === 1 ? "" : "s"} · ${active.filter((r) => r.status === "queued").length} queued`
    : current
      ? "Saved run · " + current.status.replaceAll("_", " ")
      : "No active catalogs";
  $("catalog-progress-list").replaceChildren(
    ...recent.map((run) => {
      const card = node(
        "div",
        "progress-catalog" + (run.id === selectedRun ? " selected" : ""),
      );
      card.dataset.runId = run.id;
      const select = button(
        "",
        () => {
          selectedRun = run.id;
          $("run-select").value = run.id;
          renderProgress();
          renderRun(run);
          showView("pipeline");
        },
        "progress-catalog-select",
      );
      const done = run.stages.filter((s) =>
        ["completed", "skipped"].includes(s.status),
      ).length;
      const percentage = Math.min(
        run.stages.every((s) => ["completed", "skipped"].includes(s.status))
          ? 100
          : 99,
        Math.round(
          run.stages.reduce(
            (sum, stage) =>
              sum +
              (stage.status === "skipped"
                ? 100
                : stageProgress(run, stage).percent),
            0,
          ) / (run.stages.length || 1),
        ),
      );
      const stage = run.stages.find((s) =>
        run.stoppage
          ? s.id === run.stoppage.stage
          : ["running", "failed"].includes(s.status),
      );
      const caption = node("div", "progress-caption");
      caption.append(
        node(
          "span",
          "",
          `${stage?.title || "Waiting"} · ${run.status.replaceAll("_", " ")} · ${done}/${run.stages.length}`,
        ),
        node("span", "", percentage + "%"),
      );
      const track = node("div", "progress-track"),
        fill = node("span");
      fill.style.width = percentage + "%";
      track.append(fill);
      const dots = node("div", "stage-dots");
      for (const stage of run.stages) {
        const dot = node("span", stage.status);
        dot.title = `${stage.title}: ${stage.status}`;
        dots.append(dot);
      }
      select.append(node("strong", "", run.source_name), caption, track, dots);
      card.append(select);
      const controls = node("div", "catalog-run-controls");
      addRunControls(controls, run);
      if (controls.childNodes.length) card.append(controls);
      return card;
    }),
  );
  if (!recent.length)
    $("catalog-progress-list").append(
      node(
        "p",
        "progress-empty",
        "Catalogs appear here when queued. Saved runs remain available in Run results.",
      ),
    );
  renderLocation(current);
}
async function controlAction(key, path) {
  if (pendingControls.has(key)) return;
  pendingControls.add(key);
  renderProgress();
  const current = runs.find((r) => r.id === selectedRun);
  if (current) {
    renderedSignature = "";
    renderRun(current);
  }
  try {
    await api(path, { method: "POST", body: "{}" });
    await poll();
  } catch (error) {
    showError(error);
  } finally {
    pendingControls.delete(key);
    renderProgress();
    const run = runs.find((r) => r.id === selectedRun);
    if (run) {
      renderedSignature = "";
      renderRun(run);
    }
  }
}
function controlButton(label, key, path, danger = false) {
  const control = button(
    label,
    () => controlAction(key, path),
    danger ? "button danger" : "button outline",
  );
  control.disabled = pendingControls.has(key);
  return control;
}
function renderQueueControls() {
  const paused = queueState.paused;
  const hasWork = Boolean(
    queueState.active_run || queueState.queued_runs.length,
  );
  const pause = controlButton(
    paused ? "Resume parsing" : "Pause parsing",
    "queue",
    `/api/queue/${paused ? "resume" : "pause"}`,
  );
  const stop = controlButton("Stop parsing", "queue", "/api/queue/stop", true);
  pause.disabled ||= !hasWork && !paused;
  stop.disabled ||= !hasWork;
  $("queue-controls").replaceChildren(pause, stop);
  $("queue-control-note").textContent = paused
    ? queueState.status === "pausing"
      ? "Pausing after the current page or step is saved. Queued catalogs will wait."
      : "Parsing queue paused. Resume to continue saved progress."
    : queueState.status === "stopping"
      ? "Stopping catalog processing and clearing the waiting queue."
      : "Controls apply to the whole queue. Pause saves the current page; Stop keeps completed pages. An in-flight CPU page may finish in the extractor.";
}
function addRunControls(container, run) {
  if (["paused", "pausing"].includes(run.status) && run.paused_individually)
    container.append(
      controlButton("Resume catalog", run.id, `/api/runs/${run.id}/resume`),
    );
  else if (["running", "awaiting_review", "queued"].includes(run.status))
    container.append(
      controlButton("Pause catalog", run.id, `/api/runs/${run.id}/pause`),
    );
  if (!terminal.has(run.status)) {
    const stop = controlButton(
      run.status === "stopping" ? "Stopping…" : "Stop catalog",
      run.id,
      `/api/runs/${run.id}/cancel`,
      true,
    );
    stop.disabled ||= run.status === "stopping";
    container.append(stop);
  }
}
function renderLocation(run) {
  const task = (run?.review_tasks || []).find((t) => t.status === "waiting");
  const stage =
    run?.stages.find((s) => s.id === run.stoppage?.stage) ||
    run?.stages.find((s) => s.status === "running") ||
    run?.stages.find((s) => s.status === "failed");
  processingStage = run?.stoppage
    ? stageSection[run.stoppage.stage] || "results"
    : stageSection[task?.stage || stage?.id];
  const index = stages.findIndex((s) => s[0] === processingStage);
  $("pipeline-current").textContent = run
    ? `${run.source_name} · ${processingStage ? String(index + 1).padStart(2, "0") + " " + (stage?.title || run.stoppage?.stage_title || task?.stage || stages[index]?.[1]) + " · " : ""}${run.status.replaceAll("_", " ")}`
    : "Ready to choose catalogs";
  $("go-current-stage").hidden = !processingStage;
  renderStoppage(run);
  for (const [id] of stages) {
    const card = $(id);
    let mini = card.querySelector(":scope > .stage-mini-progress");
    if (!mini) {
      mini = node("div", "stage-mini-progress");
      card.querySelector(".stage-heading").after(mini);
    }
    updateMiniProgress(
      mini,
      stages.find((s) => s[0] === id)[1],
      sectionProgress(run, id),
    );
    card.classList.toggle("processing", id === processingStage);
    const link = $("navigation").querySelector(`[href="#${id}"]`);
    link.classList.toggle("processing", id === processingStage);
    link.querySelector(".nav-stage-state")?.remove();
    card.querySelector(".stage-heading .stage-live")?.remove();
    if (id === processingStage) {
      const caption = ["paused", "pausing", "stopping"].includes(run.status)
        ? run.status === "paused"
          ? "Paused"
          : run.status === "pausing"
            ? "Pausing"
            : "Stopping"
        : ["failed", "interrupted", "cancelled"].includes(run.status)
          ? "Stopped here"
          : task
            ? "Waiting for review"
            : "In progress";
      link.append(node("small", "nav-stage-state", caption));
      card
        .querySelector(".stage-heading")
        .append(node("span", "stage-live", caption));
    }
  }
  for (const id of ["review-pending", "final-pending"]) {
    $(id).replaceChildren();
    $(id).hidden = true;
  }
  renderModelLoad(run);
  renderPerformance(run);
  renderOfficialSources(run);
  if (task) {
    const box = $(task.stage === "final" ? "final-pending" : "review-pending");
    box.hidden = false;
    box.append(
      node(
        "strong",
        "",
        `${stage?.title || task.stage} · PDF page ${task.page}`,
      ),
      node(
        "p",
        "help",
        (task.stage === "final"
          ? run.config.final_provider
          : run.config.review_provider) === "local"
          ? "The local vision model is reviewing this bounded task."
          : run.config.automatic_review
            ? "The subscription worker is reviewing this bounded task."
            : "Open this page task, provide its evidence to a fresh ChatGPT session, then import the JSON result to continue.",
      ),
    );
    if (
      !run.config.automatic_review &&
      (task.stage === "final"
        ? run.config.final_provider
        : run.config.review_provider) !== "local"
    )
      box.append(
        button("Continue review", () => openTask(task.id), "button primary"),
      );
  }
  if (
    !task &&
    run &&
    terminal.has(run.status) &&
    run.files?.some((file) => file.filename === "master_canonical.xlsx")
  ) {
    const box = $("review-pending");
    box.hidden = false;
    box.append(
      node("strong", "", "Review the saved tool list"),
      node(
        "p",
        "help",
        "Compare each tool with its source page and saved internet evidence. Search, filter, and record checks or corrections in one workspace.",
      ),
      button(
        "Open manual tool review",
        () => openManual(run.id),
        "button primary",
      ),
    );
  }
}
function renderModelLoad(run) {
  let box = $("model-load-status");
  if (!box) {
    box = node("div", "model-load-status");
    box.id = "model-load-status";
  }
  const info = run?.model_load;
  box.hidden = !info || (info.status === "ready" && terminal.has(run.status));
  const card = $(processingStage || "extract");
  card.querySelector(":scope > .stage-mini-progress").after(box);
  if (box.hidden) return;
  box.dataset.status = info.status;
  const active = ["queued", "loading"].includes(info.status);
  const elapsed = active
    ? Math.max(0, Math.floor((Date.now() - Date.parse(info.started_at)) / 1000))
    : Math.round(info.elapsed_seconds || 0);
  const title = active
    ? "Loading local model"
    : info.status === "ready"
      ? "Model preload completed"
      : info.status === "failed"
        ? "Model preload failed"
        : "Model preloading stopped";
  box.replaceChildren(
    node("strong", "", title),
    node(
      "p",
      "",
      `${info.model} · ${info.load_into_ram ? "CPU RAM" : "CPU memory mapping"}${active ? ` · ${elapsed}s elapsed · limit ${info.load_timeout}s` : info.status === "ready" ? ` · ${elapsed}s` : ""}`,
    ),
  );
  if (info.error) box.append(node("p", "", info.error));
  else if (active) {
    box.append(
      node(
        "p",
        "",
        "PDF processing can continue while weights load. Review waits for the model; its response timeout starts afterward.",
      ),
    );
    const progress = node("progress");
    progress.setAttribute("aria-label", "Loading local model weights");
    box.append(progress);
  }
}
function stoppageContext(run, info) {
  const date = info.occurred_at ? new Date(info.occurred_at) : null;
  return [
    run.source_name,
    info.stage_title,
    info.page ? `PDF page ${info.page}` : null,
    date && !isNaN(date) ? date.toLocaleString() : null,
  ]
    .filter(Boolean)
    .join(" · ");
}
function renderStoppage(run) {
  const info = run?.stoppage;
  const target = info ? stageSection[info.stage] || "results" : null;
  for (const [id] of stages) {
    const card = $(id),
      existing = card.querySelector(".stoppage-info");
    if (id !== target) {
      existing?.remove();
      continue;
    }
    const key = `${run.id}:${info.stage}`,
      signature = JSON.stringify(info);
    if (
      existing?.dataset.key === key &&
      existing.dataset.signature === signature
    )
      continue;
    existing?.remove();
    const details = node("details", "stoppage-info");
    details.dataset.key = key;
    details.dataset.signature = signature;
    const label = ["paused", "pausing"].includes(info.kind)
      ? "Why paused"
      : "Why stopped";
    const summary = node("summary");
    const icon = node("span", "stoppage-icon", "i");
    icon.setAttribute("aria-hidden", "true");
    summary.append(icon, node("span", "", label));
    const context = node("p", "help", stoppageContext(run, info));
    const reason = node("p", "stoppage-reason", info.reason);
    const diagnostics = node("div", "stoppage-diagnostics");
    details.append(summary, context, reason, diagnostics);
    details.addEventListener("toggle", async () => {
      stoppagePanels.set(key, details.open);
      if (!details.open || details.dataset.loaded) return;
      details.dataset.loaded = "true";
      try {
        const data = await api(`/api/runs/${run.id}/stoppage`);
        if (data.stoppage) {
          context.textContent = stoppageContext(run, data.stoppage);
          reason.textContent = data.stoppage.reason;
        }
        for (const diagnostic of data.diagnostics)
          diagnostics.append(
            node("strong", "", diagnostic.label),
            node("pre", "", diagnostic.text),
          );
        if (data.log_url) {
          const link = node("a", "button outline", "Download run log");
          link.href = data.log_url;
          link.download = "run.log";
          diagnostics.append(link);
        }
      } catch (error) {
        diagnostics.append(
          node("p", "help", `Additional details unavailable: ${error.message}`),
        );
      }
    });
    card.querySelector(".stage-heading").after(details);
    details.open = stoppagePanels.get(key) || false;
  }
}
function renderRun(run) {
  const signature = JSON.stringify(run);
  if (renderedRun === run.id && renderedSignature === signature) {
    for (const stage of run.stages) {
      const box = $("run-results").querySelector(
        `[data-stage-id="${stage.id}"] .stage-mini-progress`,
      );
      if (box) updateMiniProgress(box, stage.title, stageProgress(run, stage));
    }
    return;
  }
  if (renderedRun)
    runPanels.set(
      renderedRun,
      [...$("run-results").querySelectorAll("details[data-panel]")]
        .filter((d) => d.open)
        .map((d) => d.dataset.panel),
    );
  renderedRun = run.id;
  renderedSignature = signature;
  const container = $("run-results");
  container.replaceChildren();
  const title = node("div", "run-title");
  title.append(
    node("h3", "", run.source_name),
    node("span", "run-status", run.status.replaceAll("_", " ")),
  );
  addRunControls(title, run);
  if (
    ["failed", "interrupted", "cancelled"].includes(run.status) &&
    run.config.manufacturer_id
  )
    title.append(
      button("Resume saved run", async () => {
        try {
          await api(`/api/runs/${run.id}/resume`, {
            method: "POST",
            body: "{}",
          });
          await poll();
        } catch (error) {
          showError(error);
        }
      }),
    );
  if (run.config.manufacturer_id)
    title.append(
      button("Reuse settings", () => {
        applySettings(run.config);
        selected.clear();
        if (bootstrap.catalogs.some((c) => c.path === run.config.source_pdf))
          selected.set(run.config.source_pdf, run.config.manufacturer_id);
        renderSources();
        refresh();
        $("catalog").scrollIntoView({ behavior: "smooth" });
      }),
    );
  container.append(title);
  container.append(
    node(
      "p",
      "help",
      `${run.manufacturer || run.config.manufacturer || "Manufacturer"} · Catalog ID: ${run.catalog_id || run.config.catalog_id} · Stage 03: ${run.config.model} / ${reasoningLabels[run.config.reasoning || "high"]} · Stage 06: ${run.config.final_model || run.config.model} / ${reasoningLabels[run.config.final_reasoning || "high"]} · ${"manufacturer_id" in run.config ? (run.config.review_provider === "local" && run.config.final_provider === "local" ? "local CPU" : run.config.review_provider === "local" || run.config.final_provider === "local" ? "mixed local CPU / Work" : run.config.automatic_review ? "automatic Work" : "manual Work handoff") : "legacy run"}`,
    ),
  );
  if (!("manufacturer_id" in run.config))
    container.append(
      node(
        "div",
        "notice",
        "Historical run from the previous workflow. Its optional/dry-run checks do not satisfy the new mandatory reviews.",
      ),
    );
  const metrics = node("div", "metric-grid");
  for (const [label, value] of [
    ["Pages parsed", `${run.pages?.parsed || 0}/${run.pages?.total || "—"}`],
    [
      "Tools identified",
      run.metrics?.identified_products ??
        run.metrics?.products ??
        run.metrics?.product_count ??
        "—",
    ],
    ["Blocking issues", run.metrics?.blocking_issues ?? "—"],
    [
      "Average confidence",
      run.confidence?.average == null
        ? "—"
        : `${(run.confidence.average * 100).toFixed(1)}%`,
    ],
    [
      "Worst confidence",
      run.confidence?.worst == null
        ? "—"
        : `${(run.confidence.worst * 100).toFixed(1)}% · p${run.confidence.worst_page}`,
    ],
  ]) {
    const metric = node("div", "metric");
    metric.append(node("strong", "", value), node("small", "", label));
    metrics.append(metric);
  }
  container.append(metrics);
  if (run.confidence)
    container.append(
      node(
        "p",
        "help",
        `${run.confidence.assessed_pages}/${run.confidence.total_pages} pages assessed. Confidence is a conservative diagnostic, not a probability of correctness. Unreviewed pages and skipped checks remain unvalidated.`,
      ),
    );
  for (const stage of run.stages) {
    const row = node("div", "stage-result");
    row.dataset.stageId = stage.id;
    row.append(
      node("span", "", stage.title),
      node(
        "span",
        "",
        stage.quality_blocked
          ? "Completed · findings need review"
          : stage.status.replaceAll("_", " "),
      ),
    );
    row.append(miniProgress(stage.title, stageProgress(run, stage)));
    container.append(row);
  }
  if (run.error) container.append(node("div", "notice error", run.error));
  const tasks = (run.review_tasks || []).filter((t) => t.status === "waiting");
  for (const task of tasks) {
    const row = node(
      "div",
      "task-row",
      `${task.stage} · page ${task.page} · task ${task.chunk}`,
    );
    if (!run.config.automatic_review)
      row.append(button("Open Work task", () => openTask(task.id)));
    else
      row.append(
        node(
          "span",
          "help",
          (task.stage === "final"
            ? run.config.final_provider
            : run.config.review_provider) === "local"
            ? "Local model is reviewing"
            : "Waiting for subscription reviewer",
        ),
      );
    container.append(row);
  }
  const files = node("div", "downloads");
  for (const file of run.files || []) {
    const a = node("a", "", file.name);
    a.href = file.url;
    a.download = file.filename;
    files.append(a);
  }
  container.append(files);
  if (terminal.has(run.status)) {
    container.append(
      button("Review tools & evidence", () => openManual(run.id)),
    );
    const details = node("details");
    details.dataset.panel = "findings";
    details.append(node("summary", "", "Review findings & workbook issues"));
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.loaded) return;
      details.dataset.loaded = "true";
      try {
        const evidence = (run.files || []).find(
          (f) => f.filename === "reviews.json",
        );
        if (evidence) {
          const reviews = await api(evidence.url);
          for (const r of reviews) {
            details.append(
              node(
                "div",
                "issue",
                `${r.stage} · page ${r.page} · ${r.result.status}: ${r.result.summary}`,
              ),
            );
            for (const finding of r.result.findings)
              details.append(node("p", "help", finding.message));
          }
        }
        const issues = await api(`/api/runs/${run.id}/issues`);
        for (const issue of issues.rows)
          details.append(
            node("div", "issue", `${issue.issue_code}: ${issue.message}`),
          );
      } catch (error) {
        details.append(node("p", "help", error.message));
      }
    });
    container.append(details);
    if (runPanels.get(run.id)?.includes("findings")) details.open = true;
  }
  const logs = node("details");
  logs.dataset.panel = "log";
  logs.append(node("summary", "", "Run log"));
  logs.addEventListener("toggle", async () => {
    if (logs.open) {
      const result = await api(`/api/runs/${run.id}/log`);
      logs.querySelector("pre")?.remove();
      logs.append(node("pre", "", result.text));
    }
  });
  container.append(logs);
  if (runPanels.get(run.id)?.includes("log")) logs.open = true;
}
async function openManual(job) {
  try {
    let data;
    try {
      data = await api(`/api/runs/${job}/manual-review`);
    } catch {
      data = await api(`/api/runs/${job}/manual-review`, {
        method: "POST",
        body: "{}",
      });
    }
    captureManualDraft();
    manualTools = data.tools;
    manualJob = job;
    $("manual-tool-select").replaceChildren(
      ...manualTools.map((t, i) =>
        Object.assign(node("option", "", `${i + 1}. ${toolName(t)}`), {
          value: i,
        }),
      ),
    );
    $("manual-search").value = "";
    $("manual-filter").value = "all";
    const previous =
      manualPositions.get(job) ||
      localStorage.getItem("catalogflow-review-" + job);
    const index = manualTools.findIndex(
      (t) => t.product.product_version_id === previous,
    );
    renderManual(Math.max(0, index), false);
    $("manual-dialog").showModal();
  } catch (error) {
    showError(error);
  }
}
function toolName(tool) {
  return (
    tool.product.order_number ||
    tool.product.article_number ||
    tool.product.name ||
    "Unresolved tool " + tool.product.product_version_id.slice(-8)
  );
}
function draftKey(tool) {
  return manualJob + ":" + tool.snapshot_sha256;
}
function captureManualDraft() {
  const tool = manualTools[manualIndex];
  if (tool && manualJob)
    manualDrafts.set(draftKey(tool), $("manual-notes").value);
}
function renderManualList() {
  const query = $("manual-search").value.toLowerCase(),
    filter = $("manual-filter").value;
  const matching = manualTools
    .map((tool, index) => ({ tool, index }))
    .filter(
      ({ tool }) =>
        (filter === "all" || (tool.decision?.status || "pending") === filter) &&
        JSON.stringify([tool.product, tool.parameters, tool.pages])
          .toLowerCase()
          .includes(query),
    );
  const checked = manualTools.filter(
    (t) => t.decision?.status === "checked",
  ).length;
  $("manual-count").textContent =
    `${matching.length}/${manualTools.length} shown · ${checked} checked`;
  $("manual-tool-list").replaceChildren(
    ...matching.map(({ tool, index }) => {
      const item = button(
        "",
        () => renderManual(index),
        "manual-tool" + (index === manualIndex ? " selected" : ""),
      );
      item.setAttribute("aria-pressed", String(index === manualIndex));
      item.append(
        node("strong", "", toolName(tool)),
        node(
          "small",
          "",
          `PDF ${tool.pages.join(", ") || "?"} · ${(tool.decision?.status || "pending").replaceAll("_", " ")}`,
        ),
      );
      return item;
    }),
  );
  if (!matching.length)
    $("manual-tool-list").append(
      node("p", "help", "No tools match this filter."),
    );
}
function renderManual(index, capture = true) {
  if (capture) captureManualDraft();
  const tool = manualTools[index];
  manualIndex = index;
  $("manual-prev").disabled = index <= 0;
  $("manual-next").disabled = index >= manualTools.length - 1;
  $("manual-next-pending").disabled = !manualTools.some(
    (t) => (t.decision?.status || "pending") === "pending",
  );
  $("manual-title").textContent = tool
    ? `${index + 1}/${manualTools.length} · ${toolName(tool)}`
    : "No normalized tool candidates";
  $("manual-tool-select").value = index;
  $("manual-identity").textContent = tool
    ? `Identity: ${tool.product.identity_status} · ${(tool.decision?.status || "pending").replaceAll("_", " ")}${tool.previous_decision ? " · Evidence changed: previous notes require a new check." : ""}`
    : "The normalized workbook has no individual tools yet.";
  $("manual-notes").value = tool
    ? (manualDrafts.get(draftKey(tool)) ?? tool.decision?.notes ?? "")
    : "";
  $("manual-save-status").textContent = tool?.decision?.saved_at
    ? "Saved " + new Date(tool.decision.saved_at).toLocaleString()
    : "";
  for (const id of [
    "manual-notes",
    "manual-checked",
    "manual-correction",
    "manual-save",
  ])
    $(id).disabled = !tool;
  $("manual-page").replaceChildren(
    ...(tool?.pages || []).map((page) =>
      Object.assign(node("option", "", "PDF page " + page), { value: page }),
    ),
  );
  if (tool) {
    manualPositions.set(manualJob, tool.product.product_version_id);
    localStorage.setItem(
      "catalogflow-review-" + manualJob,
      tool.product.product_version_id,
    );
  }
  renderManualList();
  renderManualPage();
  renderManualDetail();
}
function renderManualPage() {
  const page = $("manual-page").value;
  $("manual-pdf").replaceChildren();
  if (page) {
    const image = node("img");
    image.src = `/api/runs/${manualJob}/source-page/${page}`;
    image.alt = "Original PDF page " + page;
    image.style.width = manualZoom + "%";
    image.style.maxWidth = "none";
    $("manual-pdf").append(image);
  } else
    $("manual-pdf").append(
      node("p", "help", "Source page provenance is missing."),
    );
  $("manual-fit").textContent = manualZoom === 100 ? "Fit" : manualZoom + "%";
}
function renderManualDetail() {
  const tool = manualTools[manualIndex],
    detail = $("manual-detail");
  detail.replaceChildren();
  for (const tab of ["parameters", "findings", "internet"])
    $("manual-tab-" + tab).setAttribute(
      "aria-selected",
      String(tab === manualTab),
    );
  detail.setAttribute("aria-labelledby", "manual-tab-" + manualTab);
  if (!tool) return;
  if (manualTab === "parameters") {
    for (const a of tool.parameters) {
      const row = node("div", "manual-parameter");
      row.append(
        node(
          "strong",
          "",
          `${a.raw_parameter_name || "?"} → ${a.canonical_field}`,
        ),
        node(
          "span",
          "",
          `${a.value_decimal ?? a.value_text ?? `${a.value_min ?? "?"}–${a.value_max ?? "?"}`} ${a.canonical_unit || ""}`,
        ),
        node(
          "small",
          "help",
          `${a.review_status || "Unreviewed"}${a.fit_class ? " · Fit " + a.fit_class : ""}${a.tolerance_lower != null || a.tolerance_upper != null ? ` · Tolerance ${a.tolerance_lower ?? "?"} / ${a.tolerance_upper ?? "?"}` : ""}`,
        ),
      );
      detail.append(row);
    }
    if (!tool.parameters.length)
      detail.append(node("p", "help", "No normalized parameters yet."));
  } else if (manualTab === "findings") {
    detail.append(
      node(
        "p",
        "help",
        "Findings from these source pages may apply to multiple tool candidates.",
      ),
    );
    for (const f of tool.findings) {
      const row = node("div", "manual-finding");
      row.append(
        node(
          "strong",
          "",
          `${f.severity || f.status || "Review"} · ${f.code || f.check || f.field || "Finding"}`,
        ),
        node(
          "p",
          "",
          f.message || f.description || f.evidence || JSON.stringify(f),
        ),
      );
      detail.append(row);
    }
    if (!tool.findings.length)
      detail.append(
        node(
          "p",
          "help",
          "No saved findings. Missing checks remain unverified.",
        ),
      );
    if (tool.previous_decision)
      detail.append(
        node(
          "p",
          "notice",
          "Notes from the previous evidence snapshot: " +
            tool.previous_decision.notes,
        ),
      );
  } else {
    for (const source of tool.internet_sources) {
      const row = node("div", "manual-finding"),
        a = node("a", "", source.url);
      a.href = source.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      row.append(
        a,
        node("p", "", source.evidence),
        node(
          "small",
          "help",
          `${source.status} · PDF page ${source.page} · Page evidence`,
        ),
      );
      detail.append(row);
    }
    if (!tool.internet_sources.length)
      detail.append(
        node(
          "p",
          "help",
          "No saved official internet evidence. This remains unverified.",
        ),
      );
  }
  $("manual-tab-findings").textContent = `Findings (${tool.findings.length})`;
  $("manual-tab-internet").textContent =
    `Internet (${tool.internet_sources.length})`;
}
async function saveManualDecision(status) {
  const tool = manualTools[manualIndex];
  if (!tool) return;
  const index = manualIndex;
  try {
    $("manual-save-status").textContent = "Saving…";
    const result = await api(`/api/runs/${manualJob}/manual-review/decision`, {
      method: "POST",
      body: JSON.stringify({
        product_version_id: tool.product.product_version_id,
        snapshot_sha256: tool.snapshot_sha256,
        status: status || tool.decision?.status || "pending",
        notes: $("manual-notes").value,
      }),
    });
    tool.decision = result.decision;
    if (manualIndex === index) renderManual(index);
    else renderManualList();
  } catch (error) {
    $("manual-save-status").textContent = error.message;
  }
}
async function fetchRun(id) {
  try {
    const run = await api("/api/runs/" + id);
    renderRun(run);
  } catch (error) {
    showError(error);
  }
}
async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    [runs, queueState] = await Promise.all([
      api("/api/runs"),
      api("/api/queue"),
    ]);
    if (!selectedRun && runs.length) selectedRun = runs[0].id;
    const runOptions = runs.map((run) => {
      const option = node(
        "option",
        "",
        `${run.source_name} · ${run.created_at?.slice(0, 16) || "saved"} · ${run.status.replaceAll("_", " ")}`,
      );
      option.value = run.id;
      return option;
    });
    $("run-select").replaceChildren(...runOptions);
    $("run-select").value = selectedRun || "";
    renderProgress();
    const current = runs.find((r) => r.id === selectedRun);
    if (current) {
      renderRun(current);
      if (
        current.stages.some(
          (s) => s.id === "mapping" && s.status === "completed",
        ) &&
        current.config.manufacturer_id === $("mapping-manufacturer").value &&
        !refreshedMappings.has(current.id) &&
        currentMapping &&
        $("mapping-json").value === JSON.stringify(currentMapping, null, 2)
      ) {
        refreshedMappings.add(current.id);
        await loadMapping();
      }
    }
  } catch (error) {
    showError(error);
  } finally {
    pollBusy = false;
  }
}
function saveFile(name, data, type = "application/json") {
  const url = URL.createObjectURL(new Blob([data], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function openTask(id) {
  try {
    currentTask = await api("/api/review/tasks/" + id);
    const request = currentTask.request;
    const content = $("task-content");
    content.replaceChildren();
    content.append(
      node(
        "p",
        "notice",
        `${request.model} · ${reasoningLabels[request.reasoning || "high"]} reasoning · ${request.stage} · page ${request.context.page}`,
      ),
    );
    const steps = node("div", "task-steps");
    steps.append(
      node(
        "p",
        "",
        `1. Download the packet. 2. Start a fresh Work session with ${request.model} and ${reasoningLabels[request.reasoning || "high"]} reasoning; attach the prompt, schema and page. 3. Import its JSON answer below.`,
      ),
    );
    const packet = node("a", "button primary", "Download review packet (.zip)");
    packet.href = `/api/review/tasks/${id}/packet`;
    packet.download = `review-${id}.zip`;
    steps.append(
      packet,
      button("Copy task + schema", async (event) => {
        try {
          await navigator.clipboard.writeText(
            currentTask.prompt +
              "\nOutput schema:\n" +
              JSON.stringify(currentTask.schema, null, 2),
          );
          event.currentTarget.textContent = "Copied";
        } catch {
          steps.append(
            node(
              "p",
              "help",
              "Clipboard unavailable. Download the task instead.",
            ),
          );
        }
      }),
    );
    content.append(steps);
    content.append(
      button("Download task & schema", () =>
        saveFile(
          `review-${id}.txt`,
          currentTask.prompt +
            "\nOutput schema:\n" +
            JSON.stringify(currentTask.schema, null, 2),
          "text/plain",
        ),
      ),
    );
    const imageLink = node(
      "a",
      "button outline",
      "Download original page image",
    );
    imageLink.href = currentTask.image_url;
    imageLink.download = `page-${request.context.page}.png`;
    content.append(imageLink);
    const image = document.createElement("img");
    image.src = currentTask.image_url;
    image.alt = "Original PDF page for this review task";
    image.className = "task-image";
    content.append(image);
    $("task-result").value = "";
    $("task-dialog").showModal();
  } catch (error) {
    showError(error);
  }
}
async function init() {
  document.addEventListener("click", (event) => {
    if (
      !event
        .composedPath()
        .some((target) => target.classList?.contains("model-picker"))
    )
      closeModelPickers();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      hideHelp();
      const open = [...modelPickers.values()].find((p) => !p.panel.hidden);
      closeModelPickers();
      open?.trigger.focus();
    }
  });
  window.addEventListener(
    "scroll",
    () => {
      if (!helpAnchor) return;
      const rect = helpAnchor.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > innerHeight) hideHelp();
      else positionHelp(helpAnchor);
    },
    true,
  );
  const savedTheme = localStorage.getItem("catalogflow-theme") || "dark";
  document.documentElement.dataset.theme = savedTheme;
  const themeLabel = () =>
    $("theme-toggle").setAttribute(
      "aria-label",
      `Switch to ${document.documentElement.dataset.theme === "dark" ? "light" : "dark"} theme`,
    );
  themeLabel();
  $("theme-toggle").onclick = () => {
    const theme =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("catalogflow-theme", theme);
    themeLabel();
  };
  try {
    bootstrap = await api("/api/bootstrap");
    build();
    $("library-count").textContent = String(bootstrap.catalogs.length);
    refreshLocalModels();
    await loadMapping();
    await poll();
    $("run-select").onchange = () => {
      selectedRun = $("run-select").value;
      renderProgress();
      fetchRun(selectedRun);
    };
    $("show-pipeline").onclick = () => showView("pipeline");
    $("show-library").onclick = () => showView("library");
    $("sidebar-library").onclick = () => showView("library");
    $("catalog-search").oninput = renderSources;
    $("mapping-manufacturer").onchange = () => loadMapping().catch(showError);
    $("target-manufacturer").onchange = async () => {
      try {
        await api("/api/library/target", {
          method: "PUT",
          body: JSON.stringify({
            manufacturer_id: $("target-manufacturer").value,
          }),
        });
      } catch (error) {
        showError(error);
      }
    };
    $("save-mapping").onclick = async () => {
      try {
        await api(
          `/api/manufacturers/${$("mapping-manufacturer").value}/mapping`,
          {
            method: "PUT",
            body: JSON.stringify(JSON.parse($("mapping-json").value)),
          },
        );
        await loadMapping();
      } catch (error) {
        showError(error);
      }
    };
    $("upload-button").onclick = () => $("pdf-upload").click();
    $("pdf-upload").onchange = (event) => upload([...event.target.files]);
    $("upload-zone").ondragover = (event) => {
      event.preventDefault();
      $("upload-zone").classList.add("drag");
    };
    $("upload-zone").ondragleave = () =>
      $("upload-zone").classList.remove("drag");
    $("upload-zone").ondrop = (event) => {
      event.preventDefault();
      $("upload-zone").classList.remove("drag");
      upload([...event.dataTransfer.files]);
    };
    $("run").onclick = async () => {
      $("error").hidden = true;
      try {
        const configs = bootstrap.catalogs
          .filter((c) => selected.has(c.path))
          .map(configFor);
        const result = await api("/api/batches", {
          method: "POST",
          body: JSON.stringify({ catalogs: configs }),
        });
        selectedRun = result.runs[0].id;
        $("run-announcement").textContent =
          `${result.runs.length} catalogs added to the CPU queue.`;
        await poll();
      } catch (error) {
        showError(error);
      }
    };
    $("preview").onclick = async () => {
      $("error").hidden = true;
      try {
        const plans = await Promise.all(
          bootstrap.catalogs
            .filter((c) => selected.has(c.path))
            .map((c) =>
              api("/api/plan", {
                method: "POST",
                body: JSON.stringify(configFor(c)),
              }),
            ),
        );
        $("plan-body").replaceChildren(
          ...plans.map((plan) => {
            const box = node("div", "notice");
            box.append(node("strong", "", plan.source));
            box.append(
              node(
                "p",
                "",
                `${plan.manufacturer} · ${plan.selected_pages}/${plan.pages} pages · Stage 03: ${plan.review_model} / ${reasoningLabels[plan.reasoning]} · Stage 06: ${plan.final_model} / ${plan.final_model === "none" ? "Off" : reasoningLabels[plan.final_reasoning]}`,
              ),
            );
            box.append(
              node("p", "help", plan.stages.map((s) => s.title).join(" → ")),
            );
            return box;
          }),
        );
        $("plan-dialog").showModal();
      } catch (error) {
        showError(error);
      }
    };
    $("close-preview").onclick = () => $("plan-dialog").close();
    $("close-task").onclick = () => $("task-dialog").close();
    $("close-manual").onclick = () => {
      captureManualDraft();
      $("manual-dialog").close();
    };
    $("manual-dialog").addEventListener("cancel", captureManualDraft);
    $("manual-search").oninput = renderManualList;
    $("manual-filter").onchange = renderManualList;
    $("manual-page").onchange = renderManualPage;
    $("manual-zoom-in").onclick = () => {
      manualZoom = Math.min(250, manualZoom + 25);
      renderManualPage();
    };
    $("manual-zoom-out").onclick = () => {
      manualZoom = Math.max(50, manualZoom - 25);
      renderManualPage();
    };
    $("manual-fit").onclick = () => {
      manualZoom = 100;
      renderManualPage();
    };
    for (const tab of ["parameters", "findings", "internet"])
      $("manual-tab-" + tab).onclick = () => {
        manualTab = tab;
        renderManualDetail();
      };
    $("manual-checked").onclick = () => saveManualDecision("checked");
    $("manual-correction").onclick = () => saveManualDecision("needs_changes");
    $("manual-save").onclick = () => saveManualDecision();
    $("manual-next-pending").onclick = () => {
      for (let offset = 1; offset <= manualTools.length; offset++) {
        const index = (manualIndex + offset) % manualTools.length;
        if ((manualTools[index].decision?.status || "pending") === "pending") {
          renderManual(index);
          break;
        }
      }
    };
    $("manual-dialog").addEventListener("keydown", (event) => {
      if (
        event.target.matches("input, textarea, select") ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey
      )
        return;
      if (event.key === "ArrowRight") {
        event.preventDefault();
        $("manual-next").click();
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        $("manual-prev").click();
      }
    });
    $("go-current-stage").onclick = () => {
      if (processingStage) jumpStage(processingStage);
    };
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length) viewStage(visible[0].target.id);
      },
      { rootMargin: "-90px 0px -55% 0px", threshold: 0 },
    );
    for (const [id] of stages) observer.observe($(id));
    viewStage(viewingStage);
    $("import-task-result").onclick = () => $("task-result-file").click();
    $("task-result-file").onchange = async (event) => {
      try {
        const text = await event.target.files[0].text();
        JSON.parse(text);
        $("task-result").value = text;
      } catch (error) {
        $("task-content").prepend(
          node("p", "notice error", "Cannot import JSON: " + error.message),
        );
      }
      event.target.value = "";
    };
    $("manual-tool-select").onchange = () =>
      renderManual(Number($("manual-tool-select").value));
    $("manual-next").onclick = () =>
      renderManual(
        Math.min(
          manualTools.length - 1,
          Number($("manual-tool-select").value) + 1,
        ),
      );
    $("manual-prev").onclick = () =>
      renderManual(Math.max(0, Number($("manual-tool-select").value) - 1));
    $("submit-task").onclick = async () => {
      try {
        await api(`/api/review/tasks/${currentTask.request.task_id}/result`, {
          method: "POST",
          body: JSON.stringify(JSON.parse($("task-result").value)),
        });
        $("task-dialog").close();
        await poll();
      } catch (error) {
        $("task-content").prepend(node("div", "notice error", error.message));
      }
    };
    $("refresh-library").onclick = () => loadLibrary().catch(showError);
    $("library-search").oninput = () => loadLibrary().catch(showError);
    $("manufacturer-form").onsubmit = async (event) => {
      event.preventDefault();
      try {
        const manufacturer = await api("/api/manufacturers", {
          method: "POST",
          body: JSON.stringify({ name: $("new-manufacturer").value }),
        });
        const fresh = await api("/api/library");
        bootstrap.manufacturers = fresh.manufacturers;
        options(
          $("target-manufacturer"),
          bootstrap.manufacturers,
          fresh.target_manufacturer_id,
        );
        options(
          $("mapping-manufacturer"),
          bootstrap.manufacturers,
          manufacturer.id,
        );
        $("new-manufacturer").value = "";
        renderSources();
        await loadMapping();
        await loadLibrary();
      } catch (error) {
        showError(error);
      }
    };
    $("save-settings").onclick = () =>
      saveFile(
        "catalogflow-settings.json",
        JSON.stringify({ version: 2, settings: settings() }, null, 2),
      );
    $("load-settings").onclick = () => $("settings-file").click();
    $("settings-file").onchange = async (event) => {
      try {
        const data = JSON.parse(await event.target.files[0].text());
        if (data.version !== 2)
          throw new Error(
            "These settings use the previous workflow. Select catalog and manufacturer in the new pipeline.",
          );
        applySettings(data.settings);
      } catch (error) {
        showError(error);
      }
      event.target.value = "";
    };
    const updateServices = async () => {
      const services = await api(
        "/api/services?local_url=" +
          encodeURIComponent(inputs.get("local_url").value),
      );
      bootstrap.reviewer = services.reviewer;
      $("services").textContent =
        `CPU extractor ${services.mineru.available ? "ready" : "offline"} · Local LLM ${(services.local_llm?.available ?? bootstrap.local_llm.available) ? "ready" : "offline"}` +
        (inputs.get("review_provider").value === "work" ||
        inputs.get("final_provider").value === "work"
          ? ` · Work ${services.reviewer.available ? "connected" : "offline"}`
          : "");
      refresh();
    };
    setInterval(() => poll(), 3000);
    setInterval(() => updateServices().catch(showError), 15000);
    setInterval(() => pollModelInstalls(), 1500);
    updateServices().catch(showError);
    checkOCR().catch((error) => {
      $("ocr-status").textContent = error.message;
    });
  } catch (error) {
    showError(error);
  }
}
init();
