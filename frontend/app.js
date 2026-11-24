const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(element => [element.id, element]));
const browserKey = "smartcare.browserId";
const browserId = localStorage.getItem(browserKey) || crypto.randomUUID();
localStorage.setItem(browserKey, browserId);
const categories = {washer: "세탁기", air_conditioner: "에어컨", dryer: "건조기", other: "기타 가전"};
const deviceKey = "smartcare.devices";
const devices = JSON.parse(localStorage.getItem(deviceKey) || "[]");
const legacyModel = localStorage.getItem("smartcare.washerModel");
if (legacyModel && !localStorage.getItem(deviceKey)) {
  devices.push({id: crypto.randomUUID(), category: "washer", model: legacyModel, name: legacyModel});
  localStorage.setItem(deviceKey, JSON.stringify(devices));
}
let supportedModels = [];
let currentCase = null;
let selectedFiles = [];
let busy = false;
let capture = null;
let editingReport = false;
let reportMediaKey = "";
const reportMediaUrls = [];
const maxMediaBytes = 25 * 1024 * 1024;

async function api(path, options) {
  const headers = new Headers(options?.headers);
  headers.set("X-SmartCare-Browser", browserId);
  const response = await fetch(path, {...options, headers});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "요청을 처리하지 못했어요.");
  return data;
}

function post(path, payload) {
  return api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
}

function show(panel) {
  if (panel !== "chat") closeCapture();
  for (const name of ["device", "chat", "history"]) {
    ui[name + "-panel"].hidden = name !== panel;
    if (name === panel) ui[name + "-tab"].setAttribute("aria-current", "page");
    else ui[name + "-tab"].removeAttribute("aria-current");
  }
}

const guidePhotos = {
  "washer-error-display": "세탁기 조작부의 디지털 표시창에 OE가 표시된 예시입니다. 표시창의 위치는 모델에 따라 다를 수 있어요.",
  "drain-hose-down": "세탁기 뒤 배수 호스를 바닥 쪽으로 내린 모습입니다.",
  "drain-hose-straight": "배수 호스가 눌리거나 꺾이지 않게 완만하게 놓인 모습입니다.",
  "balance-laundry": "세탁물이 한쪽에 몰리지 않도록 세탁통에 고르게 편 모습입니다.",
  "washer-door": "세탁기 문이 테두리에 맞춰 완전히 닫힌 모습입니다.",
  "inlet-tap": "세탁기 급수 호스가 연결된 벽면 수도꼭지의 위치를 보여주는 사진입니다.",
};

function appendMessage(text, customer = false, illustration) {
  const message = document.createElement("div");
  message.className = customer ? "message customer" : "message";
  message.textContent = text;
  if (!customer && Object.hasOwn(guidePhotos, illustration)) {
    const figure = document.createElement("figure");
    figure.className = "guide-photo";
    const photo = document.createElement("img");
    photo.src = "/guides/" + illustration + ".png";
    photo.alt = guidePhotos[illustration];
    const caption = document.createElement("figcaption");
    caption.textContent = "생성 예시 사진 · " + photo.alt;
    photo.onerror = () => figure.remove();
    figure.append(photo, caption);
    message.append(figure);
  }
  ui.transcript.append(message);
  return message;
}

function showReply(data) {
  currentCase = data;
  renderSavedReport(data);
  renderCustomerReport(data.customer_report);
  const history = data.related_count ? " · 같은 기기 이전 상담 " + data.related_count + "건" : "";
  ui["current-model"].textContent = [categories[data.category], data.device_name, data.model]
    .filter((value, index, values) => value && values.indexOf(value) === index).join(" · ") + history;
  const reply = data.message ? appendMessage(data.message, false, data.illustration) : ui.transcript.lastElementChild;
  const choices = Array.isArray(data.choices) ? data.choices : [];
  const expectsReply = ["collecting", "checking_condition", "awaiting_action_result", "service_offered"].includes(data.status);
  // A server started before a frontend update may still use the older response format.
  ui["chat-form"].hidden = !expectsReply || choices.length > 0;
  ui.choices.replaceChildren();
  ui.choices.hidden = choices.length === 0;
  if (choices.length) {
    for (const choice of [...choices, "직접 입력"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "choice-bubble";
      button.textContent = choice;
      button.onclick = () => {
        if (busy) return;
        if (choice === "직접 입력") {
          ui["chat-form"].hidden = false;
          ui["chat-input"].focus();
        } else sendMessage(choice);
      };
      ui.choices.append(button);
    }
  }
  if (reply) ui.messages.scrollTop = reply.offsetTop;
  const collectingReport = editingReport || data.status === "collecting_report";
  const collectingMedia = !editingReport && data.status === "collecting_attachments";
  ui["evidence-panel"].hidden = !(collectingReport || collectingMedia);
  if (collectingReport || collectingMedia) {
    ui["report-fields"].hidden = collectingMedia;
    ui["detail-fields"].hidden = collectingMedia;
    ui.detail.required = !collectingMedia;
    updateCustomFields();
    ui["attachment-fields"].hidden = !data.can_attach;
    ui["evidence-title"].textContent = editingReport ? "증상 기록 수정" : collectingReport ? "어떤 문제가 있었나요?" : "증상 사진·영상·녹음";
    ui["evidence-description"].textContent = collectingReport
      ? (data.can_attach ? "증상이 나타난 상황을 적고, 참고할 사진·동영상·녹음이 있으면 함께 첨부해 주세요."
        : "증상이 나타난 상황을 적어 주세요.")
      : "증상을 보여주는 자료를 첨부하거나 바로 촬영·녹음하세요.";
    ui["evidence-submit"].textContent = editingReport ? "수정 사항 저장" : collectingReport ? "증상 기록 저장" : "다음";
    (collectingMedia ? ui.files : ui.detail).focus();
  }
  ui["cancel-report-edit"].hidden = !editingReport;
  ui["schedule-panel"].hidden = editingReport || data.status !== "service_requested";
  ui["schedule-complete"].hidden = editingReport || !data.schedule;
  if (!editingReport && data.status === "service_requested") ui["visit-date"].focus();
  if (!editingReport && data.schedule) {
    ui["scheduled-time"].textContent = scheduleTime.format(new Date(data.schedule.preferred_visit_at));
    ui["completion-title"].focus();
  }
}

function renderCustomerReport(report) {
  const list = ui["customer-report-values"];
  list.replaceChildren();
  ui["customer-report"].hidden = !report;
  if (!report) return;
  const assessment = report.assessment;
  ui["report-assessment"].hidden = !assessment;
  if (assessment) {
    ui["assessment-title"].textContent = assessment.title;
    ui["assessment-explanation"].textContent = assessment.explanation;
    ui["assessment-basis"].textContent = "판단 근거 · " + assessment.basis;
    ui["assessment-observation"].hidden = !assessment.observation;
    ui["assessment-observation"].textContent = assessment.observation ? "고객이 직접 입력한 증상 · " + assessment.observation : "";
    ui["assessment-manual"].hidden = !assessment.manual_note;
    ui["assessment-manual"].textContent = assessment.manual_note || "";
    ui["assessment-source"].hidden = !assessment.source_url;
    if (assessment.source_url) ui["assessment-source"].href = assessment.source_url;
  }
  const outcomes = {resolved: "해결", unresolved: "미해결", pending: "결과 미확인",
    not_attempted: "미시도", skipped: "건너뜀", superseded: "증상 수정으로 중단"};
  const results = {resolved: "자가조치로 해결", service_submitted: "A/S 접수 신청 저장 · 방문 일정 확정 전",
    service_declined: "A/S 신청 없이 상담 종료"};
  const details = [
    ["제품 모델", report.model || "미확인"],
    ["에러코드·표시 문구", report.display_label || report.error_code || (report.display_code_status === "not_displayed" ? "표시 없음" : "미확인")],
    ["신고한 증상", report.symptom || "기록 없음"],
    ["처음 발생", report.first_observed],
    ["발생 상황", [report.cycle_stage, report.frequency].filter(Boolean).join(" · ")],
    ["점검에서 확인한 내용", report.confirmed_checks?.map(question => question + " → 네").join("\n")],
    ["자가조치", report.actions.map(action => (action.previous ? "[수정 전] " : "") + action.description +
      " (" + outcomes[action.result] + ")").join("\n") || "기록 없음"],
    ["참고한 설명서", report.manual_references?.join(", ")],
    ["상담 결과", results[report.status]],
    ["희망 방문", report.preferred_visit_at && scheduleTime.format(new Date(report.preferred_visit_at))],
    ["접수 시각", report.submitted_at && scheduleTime.format(new Date(report.submitted_at))],
    ["다음 단계", report.status === "service_submitted" ? "담당 기사 배정 및 방문 일정 확인" : null],
    ["상담 번호", report.case_id],
  ];
  for (const [label, value] of details) {
    if (!value) continue;
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    list.append(term, description);
  }
}

const reportLabels = {purchase_period: "제품 구매 시기", symptom_category: "증상 유형", cycle_stage: "작동 단계",
  first_observed: "처음 발생한 시점", frequency: "발생 빈도", prior_actions: "이미 해본 조치와 결과"};

function renderSavedReport(data) {
  ui["saved-report"].hidden = !data.detail;
  ui["edit-report"].hidden = editingReport;
  ui["saved-report-values"].replaceChildren();
  renderReportMedia(data);
  if (!data.detail) return;
  const fields = data.report_fields || {};
  for (const [label, value] of [["상세 증상", data.detail], ...Object.entries(reportLabels).map(([key, name]) => [name, fields[key]])]) {
    if (!value) continue;
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = value;
    ui["saved-report-values"].append(term, description);
  }
}

function clearReportMedia() {
  reportMediaKey = "";
  for (const url of reportMediaUrls.splice(0)) URL.revokeObjectURL(url);
  ui["saved-report-media"].replaceChildren();
  ui["saved-report-media"].hidden = true;
}

function renderReportMedia(data) {
  const attachments = data.media || [];
  const key = attachments.length ? [data.case_id, ...attachments.map(item => item.id)].join("/") : "";
  if (key === reportMediaKey) return;
  clearReportMedia();
  if (!key) return;
  reportMediaKey = key;
  const gallery = ui["saved-report-media"];
  gallery.hidden = false;
  for (const item of attachments) {
    const figure = document.createElement("figure");
    const caption = document.createElement("figcaption");
    caption.textContent = item.name;
    figure.append(caption);
    gallery.append(figure);
    fetch("/api/cases/" + data.case_id + "/media/" + item.id, {headers: {"X-SmartCare-Browser": browserId}})
      .then(response => {
        if (!response.ok) throw new Error();
        return response.blob();
      })
      .then(blob => {
        if (!figure.isConnected) return;
        const url = URL.createObjectURL(blob);
        reportMediaUrls.push(url);
        const kind = item.mime.split("/")[0];
        const preview = document.createElement(kind === "image" ? "img" : kind);
        if (kind === "image") preview.alt = "저장한 증상 사진 " + item.name;
        else { preview.controls = true; preview.preload = "metadata"; }
        preview.src = url;
        figure.prepend(preview);
      })
      .catch(() => {
        if (figure.isConnected) caption.textContent = item.name + " · 파일을 불러오지 못했어요.";
      });
  }
}

ui["edit-report"].onclick = () => {
  if (busy || !currentCase?.detail) return;
  closeCapture();
  editingReport = true;
  ui["report-save-status"].textContent = "";
  fillReportForm(currentCase);
  showReply({...currentCase, message: null});
  ui.choices.hidden = true;
  ui["chat-form"].hidden = true;
};

ui["cancel-report-edit"].onclick = () => {
  editingReport = false;
  showReply({...currentCase, message: null});
  ui["edit-report"].focus();
};

async function run(task) {
  if (busy) return;
  busy = true;
  const buttons = ["new-chat", "chat-form", "evidence-submit", "schedule-submit"].map(id =>
    ui[id].matches("button") ? ui[id] : ui[id].querySelector("button"));
  buttons.forEach(button => { button.disabled = true; });
  ui["chat-status"].textContent = "";
  try {
    await task();
  } catch (error) {
    ui["chat-status"].textContent = error.message;
  } finally {
    busy = false;
    buttons.forEach(button => { button.disabled = false; });
  }
}

function clearFiles() {
  for (const entry of selectedFiles) URL.revokeObjectURL(entry.url);
  selectedFiles = [];
  renderFiles();
}

function startChat(force = false) {
  show("chat");
  if (currentCase && !force) return;
  if (busy) return;
  currentCase = null;
  resetChat();
  appendMessage("어떤 제품에 문제가 있나요?");
  setStartChoices(Object.entries(categories).map(([category, label]) => [label, () => chooseDevice(category)]));
}

function resetChat() {
  closeCapture();
  editingReport = false;
  ui["saved-report"].hidden = true;
  renderCustomerReport(null);
  clearReportMedia();
  ui["report-save-status"].textContent = "";
  ui.transcript.replaceChildren();
  ui.choices.replaceChildren();
  ui.choices.hidden = true;
  ui["chat-form"].hidden = true;
  ui["current-model"].textContent = "";
  ui["chat-status"].textContent = "";
  ui["chat-input"].value = "";
  ui["evidence-form"].reset();
  updateCustomFields();
  clearFiles();
  ui["evidence-panel"].hidden = true;
  ui["evidence-form"].hidden = false;
  ui["schedule-form"].reset();
  ui["schedule-panel"].hidden = true;
  ui["schedule-complete"].hidden = true;
}

function setStartChoices(entries) {
  ui.choices.replaceChildren();
  ui.choices.hidden = false;
  for (const [label, action] of entries) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice-bubble";
    button.textContent = label;
    button.onclick = action;
    ui.choices.append(button);
  }
}

function chooseDevice(category) {
  if (busy) return;
  appendMessage(categories[category], true);
  const available = devices.filter(device => device.category === category);
  if (available.length === 0) return createCase(category);
  appendMessage("상담할 기기를 선택해 주세요.");
  setStartChoices([
    ...available.map(device => [deviceLabel(device), () => createCase(category, device)]),
    ["등록하지 않은 기기", () => createCase(category)],
  ]);
}

function createCase(category, device) {
  return run(async () => {
    const data = await post("/api/cases", {category, model: device?.model || null,
      device_id: device?.id || null, device_name: device?.name || null});
    if (device) appendMessage(deviceLabel(device), true);
    showReply(data);
  });
}

function deviceLabel(device) {
  return device.name === device.model ? device.model : device.name + " · " + device.model;
}

ui["device-tab"].onclick = () => show("device");
ui["chat-tab"].onclick = () => startChat();
ui["device-start"].onclick = () => startChat(true);
ui["new-chat"].onclick = () => startChat(true);
for (const [key, label] of Object.entries(categories)) {
  ui["device-category"].add(new Option(label, key));
}

function renderDevices() {
  ui["device-list"].replaceChildren();
  for (const device of devices) {
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = categories[device.category] + " · " + deviceLabel(device);
    item.append(label);
    ui["device-list"].append(item);
  }
}

ui["device-form"].onsubmit = event => {
  event.preventDefault();
  const value = ui["model-input"].value.trim().toUpperCase();
  if (!/^[A-Z0-9.-]{2,40}$/.test(value)) {
    ui["device-status"].textContent = "제품 라벨의 모델명을 확인해 주세요.";
    return;
  }
  const category = ui["device-category"].value;
  const name = ui["device-name"].value.trim() || value;
  if (devices.some(device => device.category === category && device.model === value && device.name === name)) {
    ui["device-status"].textContent = "이미 등록한 기기입니다. 같은 모델이 여러 대라면 별명을 다르게 입력해 주세요.";
    return;
  }
  try {
    const device = {id: crypto.randomUUID(), category, model: value, name};
    localStorage.setItem(deviceKey, JSON.stringify([...devices, device]));
    devices.push(device);
  } catch {
    ui["device-status"].textContent = "브라우저에서 모델명을 저장할 수 없어요.";
    return;
  }
  ui["model-input"].value = "";
  ui["device-name"].value = "";
  renderDevices();
  ui["device-status"].textContent = category === "washer" && supportedModels.includes(value)
    ? value + " 모델을 저장했어요."
    : value + " 모델을 저장했어요. 등록된 자가조치 매뉴얼이 없으면 A/S를 안내합니다.";
};

const statusNames = {collecting: "상담 중", checking_condition: "점검 중", awaiting_action_result: "조치 결과 확인 중",
  resolved: "해결됨", collecting_report: "증상 기록 중", collecting_attachments: "증상 자료 첨부 중", service_offered: "A/S 의사 확인 중",
  service_requested: "방문 일정 입력 전", service_submitted: "A/S 신청 저장", service_declined: "상담 종료"};
const formatTime = new Intl.DateTimeFormat("ko-KR", {dateStyle: "medium", timeStyle: "short"});
const scheduleTime = new Intl.DateTimeFormat("ko-KR", {dateStyle: "long", timeStyle: "short", timeZone: "Asia/Seoul"});
const todayParts = new Intl.DateTimeFormat("en", {year: "numeric", month: "2-digit", day: "2-digit", timeZone: "Asia/Seoul"})
  .formatToParts(new Date());
const today = Object.fromEntries(todayParts.map(part => [part.type, part.value]));
ui["visit-date"].min = [today.year, today.month, today.day].join("-");

ui["schedule-form"].onsubmit = event => {
  event.preventDefault();
  if (currentCase?.status !== "service_requested") return;
  return run(async () => {
    const data = await post("/api/cases/" + currentCase.case_id + "/schedule", {
      date: ui["visit-date"].value, time: ui["visit-time"].value, phone: ui["callback-phone"].value.trim(),
    });
    ui["schedule-form"].reset();
    showReply(data);
  });
};

ui["history-tab"].onclick = async () => {
  show("history");
  ui["history-list"].replaceChildren();
  ui["history-status"].textContent = "상담 이력을 불러오는 중입니다.";
  try {
    const {cases} = await api("/api/cases");
    ui["history-status"].textContent = cases.length ? "" : "아직 저장된 상담이 없습니다.";
    for (const item of cases) {
      const row = document.createElement("li");
      const info = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = [categories[item.category], item.device_name, item.model, item.display_label || item.error_code]
        .filter((value, index, values) => value && values.indexOf(value) === index).join(" · ");
      const dates = document.createElement("p");
      dates.className = "hint";
      dates.textContent = formatTime.format(new Date(item.created_at)) + " 시작 · " +
        formatTime.format(new Date(item.updated_at || item.created_at)) + " 최근 기록 · " +
        (statusNames[item.status] || item.status) + (item.previous_case_id ? " · 재발 상담" : "");
      info.append(name, dates);
      const actions = document.createElement("div");
      actions.className = "record-actions";
      const closed = ["resolved", "service_declined", "service_submitted"].includes(item.status);
      actions.append(historyButton(closed ? "기록 보기" : "이어가기", () => openCase(item.case_id, "open")));
      if (item.status === "resolved") {
        actions.append(historyButton("문제가 다시 발생했어요", () => openCase(item.case_id, "recur")));
      }
      row.append(info, actions);
      ui["history-list"].append(row);
    }
  } catch (error) {
    ui["history-status"].textContent = error.message;
  }
};

function historyButton(label, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.textContent = label;
  button.onclick = action;
  return button;
}

function openCase(caseId, operation) {
  return run(async () => {
    const data = await post("/api/cases/" + caseId + "/" + operation, {});
    resetChat();
    show("chat");
    for (const message of data.history || []) appendMessage(message.text, message.role === "customer", message.illustration);
    fillReportForm(data);
    showReply(data);
    if (data.attachment_names?.length) {
      ui["evidence-description"].textContent += " 저장된 첨부: " + data.attachment_names.join(", ");
    }
  });
}

function fillReportForm(data) {
  ui["evidence-form"].reset();
  ui.detail.value = data.detail || "";
  for (const [name, value] of Object.entries(data.report_fields || {})) {
    const field = ui["evidence-form"].elements.namedItem(name);
    if (!field) continue;
    const date = ui["report-fields"].querySelector('[data-date="' + name + '"]');
    if (date && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
      field.value = "date";
      date.value = value;
      continue;
    }
    field.value = value;
    if (field.matches("select") && !field.value) {
      field.value = "직접 입력";
      ui["report-fields"].querySelector('[data-custom="' + name + '"]').value = value;
    }
  }
  updateCustomFields();
}

function sendMessage(text) {
  if (!currentCase || !text) return;
  return run(async () => {
    const data = await post("/api/cases/" + currentCase.case_id + "/message", {text});
    ui["chat-input"].value = "";
    appendMessage(text, true);
    showReply(data);
    ui.choices.querySelector("button")?.focus({preventScroll: true});
  });
}

ui["chat-form"].onsubmit = event => {
  event.preventDefault();
  return sendMessage(ui["chat-input"].value.trim());
};

function updateCustomFields() {
  for (const input of ui["report-fields"].querySelectorAll("[data-date]")) {
    const select = ui["evidence-form"].elements.namedItem(input.dataset.date);
    input.hidden = input.disabled = select.value !== "date";
  }
  for (const input of ui["report-fields"].querySelectorAll("[data-custom]")) {
    const select = ui["evidence-form"].elements.namedItem(input.dataset.custom);
    input.hidden = input.disabled = select.value !== "직접 입력";
    input.required = !input.disabled && !ui["report-fields"].hidden;
  }
}

ui["report-fields"].addEventListener("change", updateCustomFields);

function renderFiles() {
  ui["file-previews"].replaceChildren();
  for (const entry of selectedFiles) {
    const item = document.createElement("li");
    item.textContent = entry.file.name + (entry.uploaded ? " · 저장됨" : " ");
    const [kind] = entry.file.type.split("/");
    if (["image", "video", "audio"].includes(kind)) {
      const preview = document.createElement(kind === "image" ? "img" : kind);
      if (kind === "image") preview.alt = "첨부 사진 미리보기";
      else preview.controls = true;
      preview.src = entry.url;
      item.append(preview);
    }
    if (!entry.uploaded) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary";
      remove.textContent = "제외";
      remove.onclick = () => {
        if (busy) return;
        selectedFiles = selectedFiles.filter(candidate => candidate !== entry);
        URL.revokeObjectURL(entry.url);
        renderFiles();
      };
      item.append(remove);
    }
    ui["file-previews"].append(item);
  }
}

ui.files.onchange = () => {
  if (busy) return;
  const added = [...ui.files.files];
  ui.files.value = "";
  addFiles(added);
};

function addFiles(added) {
  const supported = /\.(jpe?g|png|webp|mp4|mov|webm|wav|mp3|m4a|ogg)$/i;
  if (attachmentCount() + added.length > 5 || added.some(file =>
    !supported.test(file.name) || file.size === 0 || file.size > maxMediaBytes)) {
    ui["chat-status"].textContent = "지원하는 형식의 파일을 최대 5개, 각 25MB 이하로 선택해 주세요.";
    return;
  }
  ui["chat-status"].textContent = "";
  selectedFiles.push(...added.map(file => ({file, url: URL.createObjectURL(file), uploaded: false})));
  renderFiles();
}

function attachmentCount() {
  return (currentCase?.media?.length || 0) + selectedFiles.length;
}

function closeCapture() {
  const session = capture;
  capture = null;
  if (session) {
    if (session.recorder?.state === "recording") session.recorder.stop();
    session.stream?.getTracks().forEach(track => track.stop());
    clearTimeout(session.timer);
  }
  ui["capture-preview"].srcObject = null;
  ui["capture-preview"].hidden = true;
  ui["capture-session"].hidden = true;
  ui["capture-status"].textContent = "";
}

function captureError(error) {
  closeCapture();
  const messages = {
    NotAllowedError: "카메라 또는 마이크 사용 권한이 허용되지 않았어요.",
    NotFoundError: "사용할 수 있는 카메라 또는 마이크가 없어요.",
    NotReadableError: "카메라 또는 마이크가 다른 앱에서 사용 중이거나 연결되지 않았어요.",
  };
  ui["capture-status"].textContent = (messages[error.name] || "이 브라우저에서 촬영 또는 녹음을 시작하지 못했어요.") +
    " 기존 파일은 선택해서 첨부할 수 있어요.";
}

async function beginCapture(kind) {
  if (busy || capture) return;
  if (attachmentCount() >= 5) {
    ui["capture-status"].textContent = "한 상담에 최대 5개까지 첨부할 수 있어요.";
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || (kind !== "photo" && !window.MediaRecorder)) {
    ui["capture-status"].textContent = "촬영·녹음을 지원하는 브라우저에서 HTTPS 또는 로컬 주소로 접속해 주세요. 기존 파일은 첨부할 수 있어요.";
    return;
  }
  const session = {kind};
  capture = session;
  ui["capture-session"].hidden = false;
  ui["capture-confirm"].disabled = true;
  ui["capture-status"].textContent = "브라우저의 카메라·마이크 권한을 확인해 주세요.";
  const camera = {facingMode: {ideal: "environment"}, width: {ideal: 1280}, height: {ideal: 720}};
  try {
    try {
      session.stream = await navigator.mediaDevices.getUserMedia({
        audio: kind !== "photo", video: kind === "audio" ? false : camera,
      });
    } catch (error) {
      if (capture !== session || kind !== "video" || error.name !== "NotFoundError") throw error;
      session.stream = await navigator.mediaDevices.getUserMedia({audio: false, video: camera});
    }
    if (capture !== session) {
      session.stream.getTracks().forEach(track => track.stop());
      return;
    }
    if (kind !== "audio") {
      ui["capture-preview"].srcObject = session.stream;
      ui["capture-preview"].hidden = false;
      await ui["capture-preview"].play();
    }
    if (capture !== session) return;
    ui["capture-confirm"].textContent = kind === "photo" ? "사진 찍기" : kind === "audio" ? "녹음 시작" : "녹화 시작";
    ui["capture-confirm"].disabled = false;
    ui["capture-status"].textContent = "준비됐어요. 촬영 또는 녹화를 시작해 주세요.";
  } catch (error) {
    if (capture === session) captureError(error);
  }
}

function addRecording(session, blob) {
  const extensions = {"image/jpeg": "jpg", "audio/mp4": "m4a", "video/mp4": "mp4",
    "audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg"};
  const labels = {photo: "증상사진", video: "증상영상", audio: "증상녹음"};
  const mime = blob.type.split(";")[0];
  if (capture !== session) return;
  closeCapture();
  if (!extensions[mime] || !blob.size || blob.size > maxMediaBytes) {
    ui["capture-status"].textContent = "촬영·녹음 자료를 첨부하지 못했어요. 25MB 이하로 다시 촬영·녹음하거나 기존 파일을 선택해 주세요.";
    return;
  }
  addFiles([new File([blob], labels[session.kind] + "-" + Date.now() + "." + extensions[mime], {type: mime})]);
  ui["capture-status"].textContent = "자료를 추가했어요. 아래에서 미리 확인하거나 제외할 수 있어요.";
}

function confirmCapture() {
  const session = capture;
  if (!session?.stream || busy) return;
  if (session.kind === "photo") {
    const video = ui["capture-preview"];
    if (!video.videoWidth || !video.videoHeight) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    ui["capture-confirm"].disabled = true;
    canvas.toBlob(blob => {
      if (blob) addRecording(session, blob);
      else if (capture === session) captureError(new Error());
    }, "image/jpeg", .9);
    return;
  }
  if (session.recorder?.state === "recording") {
    ui["capture-confirm"].disabled = true;
    session.recorder.stop();
    return;
  }
  try {
    const formats = session.kind === "audio" ? ["audio/webm", "audio/mp4", "audio/ogg"] : ["video/webm", "video/mp4"];
    const mimeType = formats.find(type => MediaRecorder.isTypeSupported(type));
    if (!mimeType) throw new Error("Unsupported media format");
    const recorder = new MediaRecorder(session.stream, {mimeType});
    session.recorder = recorder;
    const chunks = [];
    let bytes = 0;
    recorder.ondataavailable = event => {
      bytes += event.data.size;
      if (bytes > maxMediaBytes && capture === session) {
        closeCapture();
        ui["capture-status"].textContent = "25MB 제한을 넘었어요. 더 짧게 다시 촬영·녹음해 주세요.";
      } else if (event.data.size) {
        chunks.push(event.data);
      }
    };
    recorder.onerror = event => { if (capture === session) captureError(event.error || new Error()); };
    recorder.onstop = () => addRecording(session, new Blob(chunks, {type: recorder.mimeType || mimeType}));
    recorder.start(1000);
    session.timer = setTimeout(() => { if (recorder.state === "recording") recorder.stop(); }, 60_000);
    ui["capture-confirm"].textContent = "중지하고 첨부";
    ui["capture-status"].textContent = "기록 중입니다. 최대 60초까지 기록합니다.";
  } catch (error) {
    captureError(error);
  }
}

ui["capture-audio"].onclick = () => beginCapture("audio");
ui["capture-photo"].onclick = () => beginCapture("photo");
ui["capture-video"].onclick = () => beginCapture("video");
ui["capture-confirm"].onclick = confirmCapture;
ui["capture-cancel"].onclick = closeCapture;
window.addEventListener("pagehide", closeCapture);

ui["evidence-form"].onsubmit = event => {
  event.preventDefault();
  const detail = ui.detail.value.trim();
  if (!currentCase || (!detail && (editingReport || currentCase.status !== "collecting_attachments"))) return;
  if (capture) {
    ui["capture-status"].textContent = "촬영이나 녹음을 끝낸 뒤 다음 단계로 진행해 주세요.";
    return;
  }
  const payload = {...Object.fromEntries(new FormData(ui["evidence-form"])), detail};
  delete payload.files;
  for (const input of ui["report-fields"].querySelectorAll("[data-date]:not(:disabled)")) {
    payload[input.dataset.date] = input.value;
  }
  for (const input of ui["report-fields"].querySelectorAll("[data-custom]:not(:disabled)")) {
    payload[input.dataset.custom] = input.value.trim();
  }
  return run(async () => {
    ui.files.disabled = true;
    ui.detail.disabled = true;
    try {
      const root = "/api/cases/" + currentCase.case_id;
      for (const entry of selectedFiles.filter(item => !item.uploaded)) {
        await api(root + "/attachment", {
          method: "POST",
          headers: {"Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(entry.file.name)},
          body: entry.file,
        });
        entry.uploaded = true;
        renderFiles();
      }
      if (editingReport) {
        const updated = await post(root + "/edit-report", payload);
        editingReport = false;
        clearFiles();
        showReply(updated);
        ui["report-save-status"].textContent = "증상 기록을 수정했어요.";
        if (!updated.message) ui["edit-report"].focus();
        return;
      }
      if (currentCase.status === "collecting_report") {
        showReply(await post(root + "/report", payload));
        clearFiles();
        ui.choices.querySelector("button")?.focus({preventScroll: true});
        return;
      }
      if (currentCase.status === "collecting_attachments") {
        showReply(await post(root + "/finish-media", {}));
        clearFiles();
        return;
      }
    } finally {
      ui.files.disabled = false;
      ui.detail.disabled = false;
    }
  });
};

api("/api/models").then(data => {
  supportedModels = data.models;
  ui["supported-models"].replaceChildren(...supportedModels.map(model => new Option(model, model)));
  ui.supported.textContent = "매뉴얼이 연결된 세탁기 모델 " + supportedModels.length + "개";
}).catch(() => {});
renderDevices();
startChat();
