(() => {
  "use strict";
  const tg = window.Telegram?.WebApp;
  tg?.ready();
  tg?.expand();

  const initData = tg?.initData || "";
  const apiHeaders = { "X-Telegram-Init-Data": initData };
  const state = { page: 1, total: 0, pageSize: 24, folders: [], selected: new Set(), selectMode: false, timer: null, currentItems: [] };
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
  function filters() {
    return new URLSearchParams({
      q: $("search").value.trim(), folder: $("folder-filter").value,
      kind: $("kind-filter").value, state: $("state-filter").value,
      tag: $("tag-filter").value.trim(), favorite: $("favorites-filter").getAttribute("aria-pressed") === "true" ? "1" : "0",
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
  function openDetails(id) {
    const item = state.currentItems.find((row) => row.id === id); if (!item) return;
    const tags = tagsOf(item).map((tag) => `<span class="tag-pill">#${escapeHtml(tag)}</span>`).join("");
    const stateLabels = { inbox: "Входящие", later: "На потом", saved: "Разобрано", done: "Готово" };
    const media = item.preview_url ? `<img class="detail-image" data-preview="${item.id}" alt="Фото">` : "";
    const text = item.content || item.link_url || item.file_name || "";
    $("detail-content").innerHTML = `${media}<h2 class="detail-title">${escapeHtml(titleFor(item))}</h2>
      <div class="detail-meta">${escapeHtml(item.folder_emoji || "📁")} ${escapeHtml(item.folder_name)} · ${escapeHtml(stateLabels[item.item_state] || item.item_state)} · ${escapeHtml(displayDate(item.created_at))}${item.sender_name ? ` · ${escapeHtml(item.sender_name)}` : ""}</div>
      ${tags ? `<div class="tag-list detail-tags">${tags}</div>` : ""}
      ${text ? `<div class="detail-text">${escapeHtml(text)}</div>` : ""}
      ${item.context_text ? `<h3>Контекст</h3><div class="detail-text">${escapeHtml(item.context_text)}</div>` : ""}
      <div class="detail-actions">
        <button type="button" data-item-action="move">📁 Переместить</button><button type="button" data-item-action="tag">🏷 Тег</button>
        <button type="button" data-item-action="state">↔ Статус</button><button type="button" data-item-action="favorite">⭐ ${item.is_favorite ? "Убрать из избранного" : "В избранное"}</button>
        <button type="button" class="danger-action" data-item-action="delete">🗑 Удалить</button>
      </div>`;
    $("detail-dialog").showModal();
    const image = $("detail-content").querySelector("img[data-preview]"); if (image) fetchPhoto(image);
    $("detail-content").querySelectorAll("[data-item-action]").forEach((button) => button.addEventListener("click", () => showAction(button.dataset.itemAction, [item.id], !item.is_favorite)));
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

  async function moveOneToFolder(itemId, folderId) {
    try {
      await api("/api/miniapp/items/bulk", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "move", ids: [itemId], folder_id: folderId }) });
      $("folder-drop-zone").hidden = true; toast("Материал перемещён в папку"); await loadFolders(); await loadItems();
    } catch (error) { $("folder-drop-zone").hidden = true; showError(error.message); }
  }
  let activeDrag = null;
  function clearActiveDrag() {
    if (!activeDrag) return;
    clearTimeout(activeDrag.timer); activeDrag.card.classList.remove("dragging"); activeDrag.ghost?.remove();
    $("folder-drop-zone").hidden = true;
    $("folder-drop-targets").querySelectorAll(".drop-hover").forEach((node) => node.classList.remove("drop-hover"));
    activeDrag = null;
  }
  function enableLongPressMove(card) {
    card.addEventListener("click", (event) => {
      if (!card.dataset.suppressClick) return;
      delete card.dataset.suppressClick; event.preventDefault(); event.stopImmediatePropagation();
    }, true);
    card.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || state.selectMode || state.selected.size) return;
      clearActiveDrag();
      const drag = activeDrag = { card, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, dragging: false, ghost: null };
      drag.timer = setTimeout(() => {
        if (activeDrag !== drag) return;
        drag.dragging = true; $("folder-drop-zone").hidden = false; card.classList.add("dragging");
        drag.ghost = card.cloneNode(true); drag.ghost.classList.add("drag-ghost"); document.body.appendChild(drag.ghost);
        tg?.HapticFeedback?.impactOccurred?.("light");
      }, 420);
    });
    card.addEventListener("pointercancel", clearActiveDrag);
  }
  document.addEventListener("pointermove", (event) => {
    const drag = activeDrag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (!drag.dragging && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > 18) { clearTimeout(drag.timer); drag.timer = null; return; }
    if (!drag.dragging) return;
    if (event.cancelable) event.preventDefault();
    if (drag.ghost) { drag.ghost.style.left = `${event.clientX + 14}px`; drag.ghost.style.top = `${event.clientY + 14}px`; }
    const targets = $("folder-drop-targets");
    targets.querySelectorAll(".drop-hover").forEach((node) => node.classList.remove("drop-hover"));
    document.elementFromPoint(event.clientX, event.clientY)?.closest(".folder-drop-target")?.classList.add("drop-hover");
  }, { passive: false });
  document.addEventListener("pointerup", (event) => {
    const drag = activeDrag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const folder = drag.dragging ? document.elementFromPoint(event.clientX, event.clientY)?.closest(".folder-drop-target") : null;
    const folderId = folder ? Number(folder.dataset.folderId) : null;
    if (drag.dragging) drag.card.dataset.suppressClick = "1";
    const itemId = Number(drag.card.dataset.id);
    clearActiveDrag();
    if (folderId) moveOneToFolder(itemId, folderId);
  });

  $("select-mode").addEventListener("click", () => {
    state.selectMode = !state.selectMode;
    renderItems(state.currentItems); updateSelectionBar();
  });
  $("clear-selection").addEventListener("click", () => { state.selected.clear(); state.selectMode = false; renderItems(state.currentItems); updateSelectionBar(); });
  $("selection-bar").addEventListener("click", (event) => { const button = event.target.closest("button[data-action]"); if (button) showAction(button.dataset.action); });
  $("folder-drop-targets").addEventListener("click", (event) => {
    const target = event.target.closest(".folder-drop-target");
    if (target) { $("folder-filter").value = target.dataset.folderId; $("folder-drop-zone").hidden = true; filtersChanged(); }
  });
  $("create-folder").addEventListener("click", () => { $("folder-dialog").showModal(); $("folder-form").elements.name.focus(); });
  $("folder-form").querySelector("[data-cancel]").addEventListener("click", () => $("folder-dialog").close());
  $("folder-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('[type="submit"]'); button.disabled = true;
    try {
      const result = await api("/api/miniapp/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: new FormData(form).get("name") }) });
      $("folder-dialog").close(); form.reset(); await loadFolders(); $("folder-filter").value = String(result.folder.id); filtersChanged(); toast("Папка создана");
    } catch (error) { showError(error.message); } finally { button.disabled = false; }
  });
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
    $("search").value = ""; $("folder-filter").value = ""; $("kind-filter").value = "all"; $("state-filter").value = "all"; $("tag-filter").value = ""; $("favorites-filter").setAttribute("aria-pressed", "false"); filtersChanged();
  });

  loadFolders().then(loadItems).catch((error) => showError(error.message));
})();
