(() => {
  "use strict";
  const tg = window.Telegram?.WebApp;
  tg?.ready();
  tg?.expand();

  const initData = tg?.initData || "";
  const apiHeaders = { "X-Telegram-Init-Data": initData };
  const state = { page: 1, total: 0, pageSize: 24, folders: [], sources: [], source: null, reminders: [], selected: new Set(), selectMode: false, timer: null, currentItems: [] };
  const $ = (id) => document.getElementById(id);
  const grid = $("item-grid");

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }
  function displayDate(value) {
    if (!value) return "";
    const date = new Date(String(value).replace(" ", "T") + (String(value).endsWith("Z") ? "" : "Z"));
    return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("ru", { day: "numeric", month: "short" }).format(date);
  }
  function titleFor(item) {
    if (item.kind === "text") return "Текстовая заметка";
    const source = item.content || item.page_title || item.file_name || ({ photo: "Фото", document: "Файл", video: "Видео", voice: "Голосовое", audio: "Аудио", sticker: "Стикер", animation: "GIF", video_note: "Видеосообщение", link: "Ссылка" }[item.kind] || "Текст");
    const cleaned = String(source).replace(/https?:\/\/\S+/g, (url) => url).trim();
    return cleaned || "Материал";
  }
  function kindIcon(kind) { return ({ photo: "▧", text: "≡", link: "↗", document: "▤", video: "▶", animation: "▧", sticker: "◉", voice: "◖", audio: "♫", video_note: "◉" })[kind] || "▤"; }
  function tagsOf(item) { return String(item.tag || "").split(",").map((tag) => tag.trim()).filter(Boolean).slice(0, 3); }
  function toast(message) {
    const node = $("toast"); node.textContent = message; node.classList.add("visible");
    clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove("visible"), 2400);
    tg?.HapticFeedback?.notificationOccurred?.("success");
  }
  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { ...apiHeaders, ...(options.headers || {}) } });
    if (!response.ok) {
      let message = "Не удалось выполнить запрос.";
      try {
        const body = await response.text();
        try { const data = JSON.parse(body); message = data.message || data.error || data.detail || body || message; }
        catch { message = body || message; }
      } catch {}
      throw new Error(message);
    }
    return response.json();
  }
  function showError(message) { $("error-banner").textContent = message; $("error-banner").hidden = false; }
  function hideError() { $("error-banner").hidden = true; }

  async function loadFolders() {
    const data = await api("/api/miniapp/folders"); state.folders = data.folders;
    const select = $("folder-filter");
    const current = select.value;
    select.innerHTML = '<option value="">Все папки</option>' + state.folders.map((folder) => `<option value="${folder.id}">${escapeHtml(folder.emoji || "📁")} ${escapeHtml(folder.name)} (${folder.item_count})</option>`).join("");
    select.value = current;
    $("folder-drop-targets").innerHTML = state.folders.map((folder) => `<button class="folder-drop-target" type="button" data-folder-id="${folder.id}">${escapeHtml(folder.emoji || "📁")} ${escapeHtml(folder.name)}</button>`).join("");
  }
  async function loadSources() {
    const data = await api("/api/miniapp/sources");
    state.sources = data.sources || [];
    renderSources();
  }
  function renderSources() {
    const query = $("source-search").value.trim().toLocaleLowerCase("ru");
    const sources = state.sources.filter((source) => source.label.toLocaleLowerCase("ru").includes(query));
    const all = `<button type="button" class="source-option${state.source ? "" : " selected"}" data-source-clear="1"><span>✧ Все источники</span><small>Не фильтровать</small></button>`;
    const entries = sources.map((source) => `<button type="button" class="source-option${state.source?.type === source.type && state.source?.value === source.value ? " selected" : ""}" data-source-type="${escapeHtml(source.type)}" data-source-value="${escapeHtml(source.value)}" data-source-label="${escapeHtml(source.label)}"><span>${escapeHtml(source.label)}</span><small>${source.item_count} · последнее ${escapeHtml(displayDate(source.last_at))}</small></button>`).join("");
    $("source-list").innerHTML = sources.length ? all + entries : all + '<p class="action-note">Другие источники пока не найдены.</p>';
    const button = $("source-filter");
    button.textContent = state.source ? `✧ ${state.source.label}` : "✧ Источник: любой";
    button.setAttribute("aria-expanded", $("source-dialog").open ? "true" : "false");
  }
  function filters() {
    return new URLSearchParams({
      q: $("search").value.trim(), folder: $("folder-filter").value,
      kind: $("kind-filter").value, state: $("state-filter").value,
      tag: $("tag-filter").value.trim(), favorite: $("favorites-filter").getAttribute("aria-pressed") === "true" ? "1" : "0",
      source_type: state.source?.type || "", source_value: state.source?.value || "",
      page: String(state.page),
    });
  }
  async function loadItems() {
    hideError();
    grid.setAttribute("aria-busy", "true");
    try {
      const data = await api(`/api/miniapp/items?${filters()}`);
      state.total = data.total; state.pageSize = data.page_size; state.currentItems = data.items;
      renderItems(data.items);
      updatePagination(); updateSelectionBar();
      $("result-count").textContent = `${data.total} ${declension(data.total, "материал", "материала", "материалов")}`;
    } catch (error) {
      showError(`${error.message} Если открыл страницу не из Telegram — зайди в бота и нажми «Открыть архив».`);
      grid.innerHTML = "";
    } finally { grid.removeAttribute("aria-busy"); }
  }
  function declension(number, one, few, many) {
    const n = Math.abs(number) % 100, last = n % 10;
    if (n > 10 && n < 20) return many;
    if (last > 1 && last < 5) return few;
    return last === 1 ? one : many;
  }
  function renderItems(items) {
    grid.querySelectorAll("img").forEach((image) => {
      if (image.src.startsWith("blob:")) URL.revokeObjectURL(image.src);
    });
    $("empty-state").hidden = items.length > 0;
    grid.hidden = items.length === 0;
    if (!items.length) { grid.innerHTML = ""; return; }
    grid.innerHTML = items.map((item) => {
      const selected = state.selected.has(item.id);
      const tags = tagsOf(item).map((tag) => `<span class="tag-pill">#${escapeHtml(tag)}</span>`).join("");
      const title = titleFor(item);
      const isText = item.kind === "text";
      const preview = item.preview_url
        ? `<div class="item-preview"><img data-preview="${item.id}" alt="Превью материала" loading="lazy"></div>`
        : isText ? "" : `<div class="item-preview"><span class="preview-placeholder">${kindIcon(item.kind)}</span></div>`;
      const textExcerpt = isText ? `<div class="text-excerpt">${escapeHtml((item.content || "Текстовая запись").trim())}</div>` : "";
      return `<article class="item-card${isText ? " text-card" : ""}${selected ? " selected" : ""}" data-id="${item.id}" tabindex="0" role="button" aria-label="${escapeHtml(title)}">
        <span class="selection-check" aria-hidden="true">${selected ? "✓" : ""}</span>
        ${item.is_favorite ? '<span class="favorite-mark" aria-label="В избранном">⭐</span>' : ""}
        ${preview}<div class="card-body"><div class="card-title">${escapeHtml(title)}</div>
        ${textExcerpt}
        <div class="card-meta"><span class="card-folder">${escapeHtml(item.folder_emoji || "📁")} ${escapeHtml(item.folder_name)}</span><time>${escapeHtml(displayDate(item.created_at))}</time></div>
        ${tags ? `<div class="tag-list">${tags}</div>` : ""}</div></article>`;
    }).join("");
    grid.querySelectorAll(".item-card").forEach((card) => {
      card.addEventListener("click", () => onCardClick(Number(card.dataset.id)));
      card.addEventListener("contextmenu", (event) => { event.preventDefault(); openDetails(Number(card.dataset.id)); });
      card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onCardClick(Number(card.dataset.id)); } });
      enableLongPressMove(card);
    });
    observePhotos();
  }
  const photoObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) { photoObserver.unobserve(entry.target); fetchPhoto(entry.target); }
  }, { rootMargin: "180px" });
  function observePhotos() { grid.querySelectorAll("img[data-preview]").forEach((image) => photoObserver.observe(image)); }
  async function fetchPhoto(image) {
    try {
      const response = await fetch(`/api/miniapp/items/${image.dataset.preview}/preview`, { headers: apiHeaders });
      if (!response.ok) throw new Error("preview failed");
      image.src = URL.createObjectURL(await response.blob());
    } catch { image.parentElement.innerHTML = '<span class="preview-placeholder">▧</span>'; }
  }
  function isVisualItem(item) {
    return item?.kind === "photo" || (item?.kind === "sticker" && item.mime_type === "image/webp");
  }
  async function showMediaViewerItem(index) {
    const items = state.mediaViewerItems || [];
    if (!items.length) return;
    if (state.mediaViewerObjectUrl) URL.revokeObjectURL(state.mediaViewerObjectUrl);
    state.mediaViewerObjectUrl = null;
    state.mediaViewerIndex = (index + items.length) % items.length;
    const item = items[state.mediaViewerIndex];
    const requestId = state.mediaViewerRequest = (state.mediaViewerRequest || 0) + 1;
    const image = document.createElement("img");
    image.alt = titleFor(item);
    $("media-viewer-content").replaceChildren(image);
    $("media-viewer-title").textContent = titleFor(item);
    $("media-viewer-caption").textContent = `${state.mediaViewerIndex + 1} / ${items.length}${item.content ? ` · ${String(item.content).slice(0, 120)}` : ""}`;
    $("media-viewer-prev").disabled = items.length < 2;
    $("media-viewer-next").disabled = items.length < 2;
    try {
      if (item.preview_url) {
        const response = await fetch(item.preview_url, { headers: apiHeaders });
        if (!response.ok) throw new Error("Не удалось загрузить изображение.");
        const objectUrl = URL.createObjectURL(await response.blob());
        if (requestId !== state.mediaViewerRequest) { URL.revokeObjectURL(objectUrl); return; }
        state.mediaViewerObjectUrl = objectUrl;
        image.src = objectUrl;
      } else {
        const { url } = await api(`/api/miniapp/items/${item.id}/media-link`);
        if (requestId === state.mediaViewerRequest) image.src = url;
      }
    } catch (error) {
      if (requestId === state.mediaViewerRequest) $("media-viewer-caption").textContent = error.message;
    }
  }
  function openMediaViewer(item) {
    state.mediaViewerItems = state.currentItems.filter(isVisualItem);
    let index = state.mediaViewerItems.findIndex((candidate) => candidate.id === item.id);
    if (index < 0) { state.mediaViewerItems.unshift(item); index = 0; }
    if (!$("media-viewer").open) $("media-viewer").showModal();
    document.body.classList.add("media-open");
    showMediaViewerItem(index);
  }
  $("media-viewer-prev").addEventListener("click", () => showMediaViewerItem(state.mediaViewerIndex - 1));
  $("media-viewer-next").addEventListener("click", () => showMediaViewerItem(state.mediaViewerIndex + 1));
  $("close-media-viewer").addEventListener("click", () => $("media-viewer").close());
  $("media-viewer").addEventListener("close", () => {
    document.body.classList.remove("media-open");
    state.mediaViewerRequest = (state.mediaViewerRequest || 0) + 1;
    if (state.mediaViewerObjectUrl) URL.revokeObjectURL(state.mediaViewerObjectUrl);
    state.mediaViewerObjectUrl = null;
    $("media-viewer-content").replaceChildren();
  });
  let mediaSwipeStartX = null;
  $("media-viewer-content").addEventListener("touchstart", (event) => { mediaSwipeStartX = event.changedTouches[0]?.clientX ?? null; }, { passive: true });
  $("media-viewer-content").addEventListener("touchend", (event) => {
    if (mediaSwipeStartX === null) return;
    const delta = event.changedTouches[0].clientX - mediaSwipeStartX;
    mediaSwipeStartX = null;
    if (Math.abs(delta) > 55 && state.mediaViewerItems?.length > 1) showMediaViewerItem(state.mediaViewerIndex + (delta < 0 ? 1 : -1));
  }, { passive: true });
  function updatePagination() {
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    $("page-label").textContent = `${state.page} / ${pages}`;
    $("previous-page").disabled = state.page <= 1;
    $("next-page").disabled = state.page >= pages;
  }
  function updateSelectionBar() {
    const count = state.selected.size;
    $("selection-bar").hidden = count === 0;
    $("selected-count").textContent = String(count);
    $("select-mode").textContent = state.selectMode ? "Готово" : count ? "Продолжить выбор" : "Выбрать";
    document.body.classList.toggle("selecting", state.selectMode || count > 0);
    const deleteButton = document.querySelector('[data-action="delete"]');
    deleteButton.disabled = count > 25;
    deleteButton.title = count > 25 ? "Для удаления выбери не больше 25 материалов" : "Удалить выбранное";
    deleteButton.textContent = count > 25 ? "🗑 до 25" : "🗑";
  }
  function onCardClick(id) {
    if (state.selectMode || state.selected.size) {
      if (state.selected.has(id)) state.selected.delete(id); else if (state.selected.size < 100) state.selected.add(id); else toast("За раз можно выбрать до 100 материалов");
      renderItems(state.currentItems); updateSelectionBar(); tg?.HapticFeedback?.selectionChanged?.();
      return;
    }
    openDetails(id);
  }
  function openDetails(itemOrId) {
    const item = typeof itemOrId === "object" ? itemOrId : state.currentItems.find((row) => row.id === itemOrId);
    if (!item) return;
    const tags = tagsOf(item).map((tag) => `<span class="tag-pill">#${escapeHtml(tag)}</span>`).join("");
    const stateLabels = { inbox: "Входящие", later: "На потом", saved: "Разобрано", done: "Готово" };
    let media = "";
    if (item.preview_url) media = `<img class="detail-image" data-preview="${item.id}" alt="Фото">`;
    else if (item.has_media && ["video", "video_note", "animation"].includes(item.kind)) media = `<video class="detail-video" data-media controls playsinline preload="metadata" aria-label="${escapeHtml(titleFor(item))}"></video>`;
    else if (item.has_media && ["voice", "audio"].includes(item.kind)) media = `<audio class="detail-audio" data-media controls preload="metadata" aria-label="${escapeHtml(titleFor(item))}"></audio>`;
    else if (item.has_media && item.kind === "sticker" && item.mime_type === "image/webp") media = `<img class="detail-image" data-media alt="Стикер">`;
    else if (item.has_media) media = `<a class="media-file-link" data-media href="#" target="_blank" rel="noopener">↗ Открыть файл${item.file_name ? `: ${escapeHtml(item.file_name)}` : ""}</a>`;
    const mediaExpand = item.preview_url || (item.kind === "sticker" && item.mime_type === "image/webp")
      ? '<button type="button" class="media-expand" data-expand-media>⛶ На весь экран</button>'
      : ["video", "video_note", "animation"].includes(item.kind) ? '<button type="button" class="media-expand" data-expand-media>⛶ Развернуть видео</button>' : "";
    const text = item.content || item.link_url || item.file_name || "";
    $("detail-content").dataset.itemId = item.id;
    $("detail-content").innerHTML = `${media}${mediaExpand}<h2 class="detail-title">${escapeHtml(titleFor(item))}</h2>
      <div class="detail-meta">${escapeHtml(item.folder_emoji || "📁")} ${escapeHtml(item.folder_name)} · ${escapeHtml(stateLabels[item.item_state] || item.item_state)} · ${escapeHtml(displayDate(item.created_at))}${item.sender_name ? ` · ${item.is_forwarded ? "↪️ Переслано от " : ""}${escapeHtml(item.sender_name)}` : ""}</div>
      ${tags ? `<div class="tag-list detail-tags">${tags}</div>` : ""}
      ${text ? `<div class="detail-text">${escapeHtml(text)}</div>` : ""}
      ${item.context_text ? `<h3>Контекст</h3><div class="detail-text">${escapeHtml(item.context_text)}</div>` : ""}
      <div class="detail-actions">
        ${item.ocr_available ? '<button type="button" data-item-action="ocr">🔎 Расшифровать картинку</button>' : ""}
        <button type="button" data-item-action="move">📁 Переместить</button><button type="button" data-item-action="tag">🏷 Тег</button>
        <button type="button" data-item-action="state">↔ Статус</button><button type="button" data-item-action="favorite">⭐ ${item.is_favorite ? "Убрать из избранного" : "В избранное"}</button>
        <button type="button" data-item-action="remind">⏰ Напомнить</button>
        <button type="button" class="danger-action" data-item-action="delete">🗑 Удалить</button>
      </div>`;
    $("detail-dialog").showModal();
    const image = $("detail-content").querySelector("img[data-preview]"); if (image) fetchPhoto(image);
    const mediaNode = $("detail-content").querySelector("[data-media]");
    if (mediaNode) api(`/api/miniapp/items/${item.id}/media-link`).then(({ url }) => {
      if (!mediaNode.isConnected || $("detail-content").dataset.itemId !== String(item.id)) return;
      mediaNode.src !== undefined ? (mediaNode.src = url) : (mediaNode.href = url);
      if (mediaNode.load) mediaNode.load();
    }).catch(() => { if (mediaNode.isConnected) mediaNode.replaceWith(Object.assign(document.createElement("p"), { className: "action-note", textContent: "Не удалось открыть файл. Попробуй открыть его в Telegram." })); });
    $("detail-content").querySelectorAll("[data-item-action]").forEach((button) => button.addEventListener("click", () => {
      if (button.dataset.itemAction === "ocr") transcribePhoto(item, button);
      else if (button.dataset.itemAction === "remind") openReminderForm(item);
      else showAction(button.dataset.itemAction, [item.id], !item.is_favorite);
    }));
    const imageNode = $("detail-content").querySelector("img[data-preview], img[data-media]");
    imageNode?.addEventListener("click", () => openMediaViewer(item));
    $("detail-content").querySelector("[data-expand-media]")?.addEventListener("click", () => {
      if (imageNode) openMediaViewer(item);
      else {
        const video = $("detail-content").querySelector("video");
        if (video?.requestFullscreen) {
          try {
            const fullscreen = video.requestFullscreen();
            fullscreen?.catch?.(() => video.play());
          } catch { video.play(); }
        }
        else if (video?.webkitEnterFullscreen) video.webkitEnterFullscreen();
        else video?.play();
      }
    });
  }

  async function transcribePhoto(item, button) {
    const container = $("detail-content");
    let result = container.querySelector(".ocr-result");
    if (!result) {
      result = document.createElement("section");
      result.className = "ocr-result";
      result.innerHTML = '<div class="ocr-result-heading"><strong>Распознанный текст</strong><button type="button" data-ocr-copy>Копировать</button></div><pre data-ocr-text></pre>';
      container.append(result);
      result.querySelector("[data-ocr-copy]").addEventListener("click", async () => {
        const text = result.querySelector("[data-ocr-text]").textContent;
        try { await navigator.clipboard.writeText(text); toast("Текст скопирован"); }
        catch { toast("Не удалось скопировать текст"); }
      });
    }
    result.hidden = false;
    result.querySelector("[data-ocr-text]").textContent = "Распознаю…";
    button.disabled = true;
    button.textContent = "⏳ Распознаю…";
    try {
      const data = await api(`/api/miniapp/items/${item.id}/ocr`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      result.querySelector("[data-ocr-text]").textContent = data.text;
    } catch (error) {
      result.querySelector("[data-ocr-text]").textContent = error.message || "Не удалось распознать текст.";
    } finally {
      if (button.isConnected) { button.disabled = false; button.textContent = "🔎 Расшифровать картинку"; }
    }
  }

  function showAction(action, overrideIds = null, favoriteValue = null) {
    const itemIds = overrideIds || [...state.selected];
    if (!itemIds.length) return;
    const count = itemIds.length;
    const title = { move: "Переместить материалы", tag: "Изменить теги", state: "Изменить статус", favorite: "Избранное", delete: "Удалить материалы" }[action];
    $("action-title").textContent = `${title} · ${count}`;
    const folderOptions = state.folders.map((folder) => `<option value="${folder.id}">${escapeHtml(folder.name)}</option>`).join("");
    const stateOptions = [['inbox','Входящие'],['later','На потом'],['saved','Разобрано'],['done','Готово']].map(([value,label]) => `<option value="${value}">${label}</option>`).join("");
    let form = "";
    if (action === "move") form = `<label>Новая папка<select name="folder_id">${folderOptions}</select></label>`;
    if (action === "tag") form = `<label>Тег<input name="tag" maxlength="64" placeholder="например, прочитать" required></label><label>Как применить<select name="mode"><option value="add">Добавить к существующим</option><option value="replace">Заменить все теги</option></select></label>`;
    if (action === "state") form = `<label>Новый статус<select name="state">${stateOptions}</select></label>`;
    if (action === "favorite") form = `<label>Действие<select name="favorite"><option value="1" ${favoriteValue === true ? "selected" : ""}>Добавить в избранное</option><option value="0" ${favoriteValue === false ? "selected" : ""}>Убрать из избранного</option></select></label>`;
    if (action === "delete") form = `<p class="action-note">Записи удалятся из каталога, бот также попробует удалить их копии и контекст из чата. Telegram может запретить удаление старых сообщений.</p><p class="action-note">Это действие нельзя отменить.</p>`;
    const destructive = action === "delete";
    form += `<div class="form-actions"><button type="button" class="secondary" data-cancel>Отмена</button><button type="submit" class="${destructive ? "danger" : ""}">${destructive ? "Удалить" : "Применить"}</button></div>`;
    $("action-form").innerHTML = `<form class="action-form" data-action="${action}">${form}</form>`;
    $("action-dialog").showModal();
    const formNode = $("action-form").querySelector("form");
    formNode.querySelector("[data-cancel]").addEventListener("click", () => $("action-dialog").close());
    formNode.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = formNode.querySelector('[type="submit"]'); button.disabled = true;
      const values = Object.fromEntries(new FormData(formNode).entries());
      const payload = { action, ids: itemIds };
      if (action === "move") payload.folder_id = Number(values.folder_id);
      if (action === "tag") { payload.tag = values.tag; payload.mode = values.mode; }
      if (action === "state") payload.state = values.state;
      if (action === "favorite") payload.favorite = values.favorite === "1";
      try {
        const result = await api("/api/miniapp/items/bulk", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const leftInChat = action === "delete" ? result.telegram_messages_total - result.telegram_messages_removed : 0;
        const copiesNote = leftInChat > 0 ? `; ${leftInChat} старых сообщений могли остаться в чате` : "";
        $("action-dialog").close();
        if (overrideIds) $("detail-dialog").close();
        state.selected.clear(); state.selectMode = false; state.page = 1; updateSelectionBar(); toast(`Обновлено: ${result.updated}${copiesNote}`); await loadFolders(); await loadItems();
      } catch (error) { button.disabled = false; showError(error.message); $("action-dialog").close(); }
    });
  }

  function localDateTimeValue(date) {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }
  function formatDue(value) {
    const raw = String(value || "").replace(" ", "T");
    const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw) ? raw : `${raw}Z`);
    return Number.isNaN(date.getTime()) ? "время не указано" : new Intl.DateTimeFormat("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
  }
  async function loadReminders() {
    const data = await api("/api/miniapp/reminders");
    state.reminders = data.reminders || [];
    $("open-reminders").textContent = state.reminders.length ? `⏰ ${state.reminders.length}` : "⏰ Напоминания";
    $("reminder-list").innerHTML = state.reminders.length ? state.reminders.map((reminder) => `
      <article class="reminder-row" data-reminder-id="${reminder.id}">
        <div class="reminder-copy"><strong>${escapeHtml(reminder.title)}</strong><time>${escapeHtml(formatDue(reminder.due_at))}</time>
          ${reminder.source_item_id ? `<small>Материал #${reminder.source_item_id}</small>` : ""}</div>
        <div class="reminder-actions">
          ${reminder.source_item_id ? `<button type="button" data-reminder-action="open" data-item-id="${reminder.source_item_id}" aria-label="Открыть материал">↗</button>` : ""}
          <button type="button" data-reminder-action="snooze" aria-label="Отложить на час">＋1 ч</button>
          <button type="button" data-reminder-action="done" aria-label="Завершить напоминание">✓</button>
        </div>
      </article>`).join("") : '<p class="action-note reminder-empty">Пока нет активных напоминаний. Открой материал и нажми «Напомнить».</p>';
  }
  function showReminders() {
    $("reminder-create").hidden = true;
    $("reminder-dialog").showModal();
    loadReminders().catch((error) => showError(error.message));
  }
  function openReminderForm(item) {
    $("detail-dialog").close();
    state.reminderItemId = Number(item.id);
    $("reminder-create").hidden = false;
    $("reminder-title").value = titleFor(item).slice(0, 120);
    const now = new Date();
    $("reminder-due").min = localDateTimeValue(now);
    const due = new Date(now.getTime() + 60 * 60 * 1000);
    due.setMinutes(0, 0, 0);
    if (due <= now) due.setHours(due.getHours() + 1);
    $("reminder-due").value = localDateTimeValue(due);
    $("reminder-dialog").showModal();
    loadReminders().catch((error) => showError(error.message));
    $("reminder-title").focus();
  }
  $("reminder-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      const dueAt = new Date($("reminder-due").value);
      if (Number.isNaN(dueAt.getTime()) || dueAt <= new Date()) throw new Error("Выбери время в будущем.");
      await api("/api/miniapp/reminders", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_item_id: state.reminderItemId, title: $("reminder-title").value.trim(), due_at: dueAt.toISOString() }),
      });
      $("reminder-create").hidden = true;
      state.reminderItemId = null;
      toast("Напоминание поставлено");
      await loadReminders();
    } catch (error) { showError(error.message); }
    finally { submit.disabled = false; }
  });
  $("cancel-reminder-create").addEventListener("click", () => { state.reminderItemId = null; $("reminder-create").hidden = true; });
  $("reminder-dialog").addEventListener("close", () => { state.reminderItemId = null; $("reminder-create").hidden = true; });
  $("detail-dialog").addEventListener("close", () => {
    $("detail-content").querySelectorAll("video, audio").forEach((player) => { player.pause(); player.removeAttribute("src"); player.load(); });
  });
  $("reminder-list").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-reminder-action]");
    if (!button) return;
    const row = button.closest("[data-reminder-id]");
    const id = Number(row.dataset.reminderId);
    button.disabled = true;
    try {
      if (button.dataset.reminderAction === "open") {
        const { item } = await api(`/api/miniapp/items/${Number(button.dataset.itemId)}`);
        $("reminder-dialog").close(); openDetails(item);
      } else {
        const action = button.dataset.reminderAction === "done" ? "done" : "snooze";
        await api(`/api/miniapp/reminders/${id}/${action}`, { method: "POST" });
        toast(action === "done" ? "Напоминание закрыто" : "Отложено на час");
        await loadReminders();
      }
    } catch (error) { button.disabled = false; showError(error.message); }
  });

  async function moveOneToFolder(itemId, folderId) {
    try {
      await api("/api/miniapp/items/bulk", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "move", ids: [itemId], folder_id: folderId }) });
      $("folder-drop-zone").hidden = true; toast("Материал перемещён в папку"); await loadFolders(); await loadItems();
    } catch (error) { $("folder-drop-zone").hidden = true; showError(error.message); }
  }
  let activeDrag = null;
  let suppressDropClickUntil = 0;
  function clearActiveDrag() {
    if (!activeDrag) return;
    clearTimeout(activeDrag.timer); activeDrag.card.classList.remove("dragging"); activeDrag.ghost?.remove();
    $("folder-drop-zone").hidden = true; document.body.classList.remove("drag-mode");
    $("folder-drop-targets").querySelectorAll(".drop-hover").forEach((node) => node.classList.remove("drop-hover"));
    activeDrag = null;
  }
  function beginDrag(drag, x, y) {
    if (activeDrag !== drag) return;
    drag.dragging = true; $("folder-drop-zone").hidden = false;
    document.body.classList.add("drag-mode"); drag.card.classList.add("dragging");
    drag.ghost = drag.card.cloneNode(true); drag.ghost.classList.add("drag-ghost");
    drag.ghost.style.left = `${x + 14}px`; drag.ghost.style.top = `${y + 14}px`;
    document.body.appendChild(drag.ghost);
    tg?.HapticFeedback?.impactOccurred?.("light");
  }
  function moveDrag(x, y) {
    const drag = activeDrag;
    if (!drag?.dragging) return;
    if (drag.ghost) { drag.ghost.style.left = `${x + 14}px`; drag.ghost.style.top = `${y + 14}px`; }
    const targets = $("folder-drop-targets");
    targets.querySelectorAll(".drop-hover").forEach((node) => node.classList.remove("drop-hover"));
    document.elementFromPoint(x, y)?.closest(".folder-drop-target")?.classList.add("drop-hover");
  }
  function finishDrag(x, y) {
    const drag = activeDrag;
    if (!drag) return;
    const folder = drag.dragging ? document.elementFromPoint(x, y)?.closest(".folder-drop-target") : null;
    const folderId = folder ? Number(folder.dataset.folderId) : null;
    const itemId = Number(drag.card.dataset.id);
    if (drag.dragging) {
      drag.card.dataset.suppressClick = "1";
      suppressDropClickUntil = Date.now() + 700;
    }
    clearActiveDrag();
    if (folderId) moveOneToFolder(itemId, folderId);
  }
  function enableLongPressMove(card) {
    card.addEventListener("click", (event) => {
      if (!card.dataset.suppressClick) return;
      delete card.dataset.suppressClick; event.preventDefault(); event.stopImmediatePropagation();
    }, true);
    card.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "touch" || event.button !== 0 || state.selectMode || state.selected.size) return;
      clearActiveDrag();
      const drag = activeDrag = { card, input: "pointer", pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, dragging: false, ghost: null };
      drag.timer = setTimeout(() => {
        beginDrag(drag, drag.startX, drag.startY);
      }, 420);
    });
    card.addEventListener("pointercancel", (event) => {
      if (activeDrag?.input === "pointer" && activeDrag.pointerId === event.pointerId) clearActiveDrag();
    });
    card.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 1 || state.selectMode || state.selected.size) return;
      clearActiveDrag();
      const touch = event.changedTouches[0];
      const drag = activeDrag = { card, input: "touch", touchId: touch.identifier, startX: touch.clientX, startY: touch.clientY, dragging: false, ghost: null };
      drag.timer = setTimeout(() => beginDrag(drag, drag.startX, drag.startY), 460);
    }, { passive: true });
  }
  document.addEventListener("pointermove", (event) => {
    const drag = activeDrag;
    if (!drag || drag.input !== "pointer" || drag.pointerId !== event.pointerId) return;
    if (!drag.dragging && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > 18) { clearActiveDrag(); return; }
    if (!drag.dragging) return;
    if (event.cancelable) event.preventDefault();
    moveDrag(event.clientX, event.clientY);
  }, { passive: false });
  document.addEventListener("pointerup", (event) => {
    const drag = activeDrag;
    if (!drag || drag.input !== "pointer" || drag.pointerId !== event.pointerId) return;
    finishDrag(event.clientX, event.clientY);
  });
  document.addEventListener("touchmove", (event) => {
    const drag = activeDrag;
    if (!drag || drag.input !== "touch") return;
    const touch = Array.from(event.changedTouches).find((item) => item.identifier === drag.touchId);
    if (!touch) return;
    if (!drag.dragging) {
      if (Math.hypot(touch.clientX - drag.startX, touch.clientY - drag.startY) > 18) clearActiveDrag();
      return;
    }
    if (event.cancelable) event.preventDefault();
    moveDrag(touch.clientX, touch.clientY);
  }, { passive: false });
  document.addEventListener("touchend", (event) => {
    const drag = activeDrag;
    if (!drag || drag.input !== "touch") return;
    const touch = Array.from(event.changedTouches).find((item) => item.identifier === drag.touchId);
    if (touch) finishDrag(touch.clientX, touch.clientY);
  });
  document.addEventListener("touchcancel", (event) => {
    const drag = activeDrag;
    if (drag?.input === "touch" && Array.from(event.changedTouches).some((item) => item.identifier === drag.touchId)) clearActiveDrag();
  });
  $("cancel-drag").addEventListener("click", clearActiveDrag);

  $("select-mode").addEventListener("click", () => {
    state.selectMode = !state.selectMode;
    renderItems(state.currentItems); updateSelectionBar();
  });
  $("clear-selection").addEventListener("click", () => { state.selected.clear(); state.selectMode = false; renderItems(state.currentItems); updateSelectionBar(); });
  $("selection-bar").addEventListener("click", (event) => { const button = event.target.closest("button[data-action]"); if (button) showAction(button.dataset.action); });
  $("folder-drop-targets").addEventListener("click", (event) => {
    if (Date.now() < suppressDropClickUntil) { suppressDropClickUntil = 0; event.preventDefault(); event.stopImmediatePropagation(); return; }
    const target = event.target.closest(".folder-drop-target");
    if (target) { $("folder-filter").value = target.dataset.folderId; $("folder-drop-zone").hidden = true; filtersChanged(); }
  }, true);
  $("create-folder").addEventListener("click", () => { $("folder-dialog").showModal(); $("folder-form").elements.name.focus(); });
  $("folder-form").querySelector("[data-cancel]").addEventListener("click", () => $("folder-dialog").close());
  $("folder-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('[type="submit"]'); button.disabled = true;
    try {
      const result = await api("/api/miniapp/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: new FormData(form).get("name") }) });
      $("folder-dialog").close(); form.reset(); await loadFolders(); $("folder-filter").value = String(result.folder.id); filtersChanged(); toast("Папка создана");
    } catch (error) { showError(error.message); } finally { button.disabled = false; }
  });
  $("source-filter").addEventListener("click", () => { renderSources(); $("source-dialog").showModal(); $("source-filter").setAttribute("aria-expanded", "true"); });
  $("source-dialog").addEventListener("close", () => $("source-filter").setAttribute("aria-expanded", "false"));
  $("source-search").addEventListener("input", renderSources);
  $("source-list").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.hasAttribute("data-source-clear")) state.source = null;
    else if (button.dataset.sourceType) state.source = { type: button.dataset.sourceType, value: button.dataset.sourceValue, label: button.dataset.sourceLabel };
    else return;
    $("source-search").value = "";
    $("source-dialog").close(); renderSources(); filtersChanged();
  });
  $("open-reminders").addEventListener("click", showReminders);
  $("previous-page").addEventListener("click", () => { if (state.page > 1) { state.page--; loadItems(); window.scrollTo({ top: 0, behavior: "smooth" }); } });
  $("next-page").addEventListener("click", () => { if (state.page * state.pageSize < state.total) { state.page++; loadItems(); window.scrollTo({ top: 0, behavior: "smooth" }); } });
  function filtersChanged() {
    state.page = 1; state.selected.clear(); state.selectMode = false; updateSelectionBar(); loadItems();
  }
  for (const id of ["folder-filter", "kind-filter", "state-filter", "tag-filter"]) $(id).addEventListener("change", filtersChanged);
  $("search").addEventListener("input", () => { clearTimeout(state.timer); state.timer = setTimeout(filtersChanged, 260); });
  $("clear-search").addEventListener("click", () => { $("search").value = ""; filtersChanged(); });
  $("favorites-filter").addEventListener("click", () => { const button = $("favorites-filter"); button.setAttribute("aria-pressed", button.getAttribute("aria-pressed") === "true" ? "false" : "true"); filtersChanged(); });
  $("reset-filters").addEventListener("click", () => {
    $("search").value = ""; $("folder-filter").value = ""; $("kind-filter").value = "all"; $("state-filter").value = "all"; $("tag-filter").value = ""; $("favorites-filter").setAttribute("aria-pressed", "false"); state.source = null; renderSources(); filtersChanged();
  });

  Promise.all([loadFolders(), loadSources(), loadReminders()]).then(loadItems).then(async () => {
    const sourceItemId = Number(new URLSearchParams(window.location.search).get("item"));
    if (sourceItemId) {
      const { item } = await api(`/api/miniapp/items/${sourceItemId}`);
      openDetails(item);
    }
  }).catch((error) => showError(error.message));
})();
