const sourceUrl = document.getElementById("sourceUrl");
const kindBadge = document.getElementById("kindBadge");
const sourceHint = document.getElementById("sourceHint");
const enqueueButton = document.getElementById("enqueue");
const repairButton = document.getElementById("repairChannel");
const channelTools = document.getElementById("channelTools");
const messageElement = document.getElementById("message");
const queueElement = document.getElementById("queue");
const queueCount = document.getElementById("queueCount");
const hostState = document.getElementById("hostState");
const hostDetails = document.getElementById("hostDetails");
const useCookies = document.getElementById("useCookies");
const cookieLabel = document.getElementById("cookieLabel");
const libraryPreviewElement = document.getElementById("kugouPreview");
const libraryPreviewSummary = document.getElementById("kugouPreviewSummary");
const libraryPlaylistList = document.getElementById("kugouPlaylistList");
const libraryLoadMore = document.getElementById("kugouLoadMore");
const libraryConfirm = document.getElementById("confirmKugouDownload");
const libraryCancel = document.getElementById("cancelKugouPreview");

let detected = { platform: "", kind: "unknown", normalizedUrl: "", label: "未识别" };
let state = { connected: false, queue: [], hostInfo: {} };
let currentTitle = "";
let currentTabId = null;
let currentTabUrl = "";
let repairing = false;
let libraryPreview = null;
let previewEpoch = 0;
let previewTimer = null;
let previewRequestPending = false;
let previewPagePending = false;
let previewConfirmPending = false;
let previewSourceValue = "";
let previewTabId = null;
let previewRequestKey = "";
const cookiePreferences = { youtube: false, bilibili: false };
const expectedHostVersion = chrome.runtime.getManifest().version;

function hostReady() {
  return state.connected && state.hostInfo?.version === expectedHostVersion;
}

function platformText(platform) {
  return { youtube: "YouTube", bilibili: "bilibili", kugou: "酷狗音乐" }[platform] || "";
}

function setMode(mode) {
  document.querySelectorAll(".mode").forEach(label => {
    const input = label.querySelector("input");
    input.checked = input.value === mode;
    label.classList.toggle("active", input.checked);
  });
}

function updateDetected() {
  const previousPlatform = detected.platform;
  detected = classifySourceUrl(sourceUrl.value);
  if (detected.platform !== previousPlatform) {
    useCookies.checked = Boolean(cookiePreferences[detected.platform]);
  }
  const kugou = detected.platform === "kugou";
  const kugouLibrary = kugou && detected.kind === "library";
  if (libraryPreview && (!kugouLibrary || sourceUrl.value !== previewSourceValue || currentTabId !== previewTabId)) {
    invalidateLibraryPreview();
  }
  document.querySelectorAll(".mode").forEach(label => {
    const input = label.querySelector("input");
    input.disabled = kugou && input.value === "video";
    label.classList.toggle("disabled", input.disabled);
    label.setAttribute("aria-disabled", String(input.disabled));
  });
  if (kugou) setMode("audio");
  useCookies.disabled = !["youtube", "bilibili"].includes(detected.platform);
  if (useCookies.disabled) useCookies.checked = kugouLibrary;
  cookieLabel.textContent = kugouLibrary
    ? "自动使用当前浏览器标签页的酷狗登录状态（个人歌单必需）"
    : kugou ? "酷狗公开歌曲和歌单使用 HTTP 直连，无需读取浏览器 Cookie"
      : `使用${platformText(detected.platform) || "当前平台"}登录 Cookie（仅需登录时勾选）`;
  kindBadge.className = `badge ${detected.kind}`;
  kindBadge.textContent = [platformText(detected.platform), detected.label].filter(Boolean).join(" · ");
  const hints = {
    video: detected.platform === "bilibili" ? "有 p 参数时下载指定分 P；否则下载全部分 P" : "将下载当前视频",
    playlist: "将下载列表内全部公开视频",
    channel: "将读取频道页面的“专辑和单曲”并按专辑分类；其他上传单独归档",
    favorites: "将下载这个收藏夹中当前可访问的全部视频；需要登录时勾选 Cookie",
    collection: "将下载该合集 / 视频列表中的全部可访问视频",
    shortlink: "将解析 bilibili 短链接，再按目标视频或集合下载",
    song: "获取歌曲播放地址并下载为 MP3",
    library: "先读取并展示当前账号的全部自建 / 收藏歌单 URL；确认后再下载全部歌曲",
    unknown: "支持 YouTube 视频 / 列表 / 频道、bilibili 视频 / 收藏夹 / 合集、酷狗歌曲 / 歌单 / 主站个人全部歌单",
  };
  sourceHint.textContent = kugou && detected.kind === "playlist"
    ? "将读取完整歌单，逐曲获取播放地址并下载为 MP3"
    : hints[detected.kind] || hints.unknown;
  enqueueButton.disabled = detected.kind === "unknown" || !hostReady() || repairing || previewRequestPending
    || previewConfirmPending || (kugouLibrary && libraryPreview?.status === "loading");
  enqueueButton.textContent = kugouLibrary ? (libraryPreview?.status === "loading" ? "正在读取歌单 URL…" : "读取歌单 URL") : "加入下载队列";
  libraryConfirm.disabled = !canConfirmLibraryPreview();
  const youtubeChannel = detected.platform === "youtube" && detected.kind === "channel";
  channelTools.hidden = !youtubeChannel;
  repairButton.disabled = !youtubeChannel || !hostReady() || repairing;
}

function selectedMode() {
  if (detected.platform === "kugou") return "audio";
  return document.querySelector('input[name="mode"]:checked')?.value || "video";
}

async function readCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (currentTabId !== (tab?.id ?? null) || currentTabUrl !== (tab?.url || "")) invalidateLibraryPreview();
  currentTabId = tab?.id ?? null;
  currentTabUrl = tab?.url || "";
  if (tab?.url) sourceUrl.value = tab.url;
  currentTitle = tab?.title || "";
  updateDetected();
}

function setMessage(text, type = "") {
  messageElement.textContent = text;
  messageElement.className = `message ${type}`;
}

function renderState(nextState) {
  state = { ...state, ...nextState };
  const info = state.hostInfo || {};
  const ready = hostReady();
  if (libraryPreview && (!ready || (nextState.kugouPreview === null && libraryPreview.preview_id && !previewRequestPending))) {
    invalidateLibraryPreview(false);
    setMessage("歌单预览已失效，请重新读取歌单 URL。", "error");
  }
  hostState.textContent = !state.connected ? "本机组件未连接"
    : ready ? `Host ${expectedHostVersion} 已连接`
    : `Host 版本错误 (${info.version || "未知"} ≠ ${expectedHostVersion})`;
  hostState.className = `host-state ${ready ? "online" : "offline"}`;
  const albumText = info.album_organization ? " · 专辑分类: 已启用" : "";
  const retryText = info.manual_retry ? ` · 自动重试 ${info.auto_retry_count || 0} 次 + 手动重试` : "";
  const ytVersion = info.yt_dlp_version ? ` · yt-dlp: ${info.yt_dlp_version}` : "";
  const ejsStatus = info.ejs_available === true ? " · EJS: 已内置" : info.ejs_available === false ? " · EJS: 缺失" : "";
  const nodeVersion = info.node_version ? ` (${info.node_version})` : "";
  const nodeWarning = info.node_compatible === false ? " [版本过旧/不可用]" : "";
  hostDetails.textContent = state.connected
    ? `${ready ? "" : "⚠ 正在连接旧 Host，已禁止下载。重新运行最新安装器，它会检查注册并重启旧进程。 · "}Host ${info.version || "-"} · 下载目录：${info.download_root || "-"}${albumText}${retryText}${ytVersion}${ejsStatus}${info.node ? ` · Node: ${info.node}${nodeVersion}${nodeWarning}` : " · 未检测到 Node.js（部分 YouTube 资源可能受影响）"}`
    : "首次使用请运行仓库中的 Install-Windows.bat，并在 chrome://extensions 加载 extension 目录。";
  renderQueue(state.queue || []);
  updateDetected();
}

function statusText(status) {
  return {
    queued: "排队中",
    downloading: "下载中",
    done: "已完成",
    error: "失败",
    cancelled: "已取消",
  }[status] || status;
}

function kindText(kind) {
  return { video: "视频", playlist: "列表", channel: "频道", favorites: "收藏夹", collection: "合集", shortlink: "短链接", song: "歌曲", library: "个人全部歌单" }[kind] || "来源";
}

function taskCountText(index, total, label = "") {
  const current = Number(index || 0);
  const count = Number(total);
  return Number.isSafeInteger(current) && current >= 0 && Number.isSafeInteger(count) && count > 0
    ? ` · ${label}${current}/${count}` : "";
}

function renderQueue(items) {
  queueCount.textContent = `${items.length} 项`;
  queueElement.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "队列为空。打开支持的视频、收藏夹或歌单页面后即可加入。";
    queueElement.appendChild(empty);
    return;
  }

  for (const task of items.slice().reverse()) {
    const card = document.createElement("div");
    card.className = "task";
    const percent = Math.max(0, Math.min(100, Number(task.progress || 0)));
    const title = task.current_title || task.title || task.url;
    const taskSource = classifySourceUrl(task.url);
    const taskPlatform = taskSource.platform;
    const personalLibrary = taskPlatform === "kugou" && taskSource.kind === "library";
    const account = personalLibrary && task.kugou_user_id ? ` · 账号 ${escapeHtml(task.kugou_user_id)}` : "";
    const playlistCount = personalLibrary ? taskCountText(task.playlist_index, task.playlist_count, "歌单 ") : "";
    const currentCount = taskCountText(task.item_index, task.item_count, personalLibrary ? "曲目 " : "");
    const speed = task.speed || "";
    const eta = task.eta ? ` · ETA ${task.eta}` : "";
    const error = task.error ? ` · ${task.error}` : "";

    card.innerHTML = `
      <div class="task-top">
        <div style="min-width:0">
          <div class="task-title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
          <div class="task-sub">${platformText(taskPlatform)} · ${kindText(task.kind)} · ${task.mode === "audio" ? "MP3" : "MP4"}${account}${playlistCount}${currentCount}</div>
        </div>
        <span class="task-state ${task.status}">${statusText(task.status)}</span>
      </div>
      <div class="progress"><div style="width:${percent}%"></div></div>
      <div class="task-bottom">
        <span>${percent.toFixed(1)}%${speed ? ` · ${escapeHtml(speed)}` : ""}${eta}${error ? escapeHtml(error) : ""}</span>
        <span class="task-actions"></span>
      </div>`;

    const actions = card.querySelector(".task-actions");
    if (["queued", "downloading"].includes(task.status)) {
      const cancel = document.createElement("button");
      cancel.className = "ghost";
      cancel.textContent = "取消";
      cancel.addEventListener("click", () => send({ type: "cancel", taskId: task.id }).catch((e) => setMessage(e.message, "error")));
      actions.appendChild(cancel);
    } else {
      if (["error", "cancelled"].includes(task.status)) {
        const retry = document.createElement("button");
        retry.className = "ghost";
        const needsConfirmation = personalLibrary && task.kugou_confirmed !== true;
        retry.textContent = needsConfirmation ? "请重新读取歌单 URL 并确认" : "重试";
        retry.addEventListener("click", () => {
          if (needsConfirmation) {
            setMessage("此任务尚未确认，请在已登录的酷狗主站重新读取歌单 URL，再确认下载。", "error");
            return;
          }
          const payload = { type: "retry", taskId: task.id };
          if (taskPlatform === detected.platform) payload.useCookies = useCookies.checked;
          send(payload).catch(e => setMessage(e.message, "error"));
        });
        actions.appendChild(retry);
      }
      const remove = document.createElement("button");
      remove.className = "ghost";
      remove.textContent = "移除";
      remove.addEventListener("click", () => send({ type: "remove", taskId: task.id }).catch((e) => setMessage(e.message, "error")));
      actions.appendChild(remove);
    }
    queueElement.appendChild(card);
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function invalidateLibraryPreview(cancelRemote = true) {
  const previous = libraryPreview;
  const previousTabId = previewTabId;
  const previousRequestKey = previewRequestKey;
  const confirming = previewConfirmPending;
  previewEpoch += 1;
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = null;
  libraryPreview = null;
  previewRequestPending = false;
  previewPagePending = false;
  previewConfirmPending = false;
  previewSourceValue = "";
  previewTabId = null;
  previewRequestKey = "";
  if (cancelRemote && previous && previous.status !== "consumed" && !confirming) {
    send({ type: "cancelKugouPreview", previewId: previous.preview_id || "", tabId: previousTabId,
      requestKey: previousRequestKey }).catch(() => {});
  }
  renderLibraryPreview();
}

function canConfirmLibraryPreview() {
  return hostReady() && libraryPreview?.status === "ready" && !previewConfirmPending && !previewPagePending
    && Number.isSafeInteger(libraryPreview.playlist_count) && libraryPreview.playlist_count > 0
    && libraryPreview.playlists.length === libraryPreview.playlist_count;
}

function safePlaylistUrl(raw) {
  if (typeof raw !== "string") return "";
  const source = classifySourceUrl(raw);
  return source.platform === "kugou" && source.kind === "playlist" ? source.normalizedUrl : "";
}

function safeKugouDiagnostic(data) {
  const result = { version: expectedHostVersion };
  const allowed = {
    code: ["NETWORK_TIMEOUT", "NETWORK_PROXY", "NETWORK_CONNECT", "NETWORK_TLS", "NETWORK_DNS", "NETWORK_TRANSPORT", "HTTP_ERROR", "REDIRECT_BLOCKED", "SESSION_REJECTED", "API_REJECTED", "VERIFICATION_REQUIRED", "INVALID_JSON", "INVALID_RESPONSE", "PAGINATION_INVALID", "PAGINATION_CHANGED", "PAGINATION_REPEATED", "PAGINATION_LIMIT", "PLAYLIST_RECORD_INVALID", "INTERNAL_ERROR"],
    stage: ["preview", "playlist_list", "playlist_tracks"],
    endpoint: ["/v7/get_all_list", "/v4/get_list_all_file"],
    check: ["rows", "total", "page", "end", "version", "duplicate", "list_id", "list_type", "track_count", "user_id", "body"],
    exception_type: ["TypeError", "ValueError", "AttributeError", "KeyError", "RuntimeError", "OSError", "ImportError", "ModuleNotFoundError", "UnknownError"],
    module: ["kugou_library.py", "kugou_preview.py", "kugou_plan.py", "kugou.py", "kugou_diagnostics.py"],
  };
  for (const [key, values] of Object.entries(allowed)) {
    if (values.includes(data[key])) result[key] = data[key];
  }
  for (const [key, low, high] of [["page", 1, 1000], ["http_status", 100, 599], ["api_code", -99999999, 99999999], ["line", 1, 100000]]) {
    if (Number.isSafeInteger(data[key]) && data[key] >= low && data[key] <= high) result[key] = data[key];
  }
  const shape = {};
  for (const key of ["data", "info", "songs", "lists", "list", "items", "file", "total", "total_count", "count", "list_count", "collect_count", "album_count", "has_more", "hasmore", "page", "total_ver"]) {
    const value = data.shape?.[key];
    if (["object", "array", "string", "number", "boolean", "null"].includes(value)) shape[key] = value;
  }
  if (Object.keys(shape).length) result.shape = shape;
  return result;
}

function renderLibraryPreview() {
  libraryPreviewElement.hidden = !libraryPreview;
  libraryPlaylistList.innerHTML = "";
  libraryLoadMore.hidden = true;
  libraryConfirm.disabled = !canConfirmLibraryPreview();
  libraryConfirm.textContent = previewConfirmPending ? "正在确认账号并加入队列…"
    : libraryPreview?.status === "consumed" ? "已加入下载队列" : "确认下载以上歌单";
  libraryCancel.disabled = previewConfirmPending;
  libraryCancel.textContent = libraryPreview?.status === "consumed" ? "关闭预览" : "取消预览";
  if (!libraryPreview) return;
  const preview = libraryPreview;
  if (preview.status === "loading") {
    libraryPreviewSummary.textContent = "正在读取当前账号的全部歌单；此步骤不下载歌曲。";
  } else if (preview.status === "error" || preview.status === "cancelled") {
    libraryPreviewSummary.textContent = preview.error || "歌单预览已取消，请重新读取。";
  } else {
    const unknown = preview.unknown_track_count ? `；${preview.unknown_track_count} 个歌单的曲目数未知` : "";
    const shown = preview.status === "ready" && preview.playlists.length < preview.playlist_count
      ? `；已展示 ${preview.playlists.length}/${preview.playlist_count}，请加载全部 URL 后确认` : "";
    libraryPreviewSummary.textContent = `账号 ${preview.user_id || "未知"} · ${preview.playlist_count || 0} 个歌单 · 已知 ${preview.known_track_count || 0} 首曲目${unknown}${shown}${preview.status === "consumed" ? "；已加入下载队列" : ""}`;
  }
  if (preview.status === "error" && preview.diagnostic && typeof preview.diagnostic === "object") {
    const safe = safeKugouDiagnostic(preview.diagnostic);
    const diagnostic = document.createElement("pre");
    diagnostic.className = "library-diagnostic";
    diagnostic.textContent = JSON.stringify(safe, null, 2);
    libraryPlaylistList.appendChild(diagnostic);
    const copy = document.createElement("button");
    copy.className = "ghost";
    copy.textContent = "复制脱敏诊断";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(diagnostic.textContent);
        setMessage("脱敏诊断已复制，不包含 Cookie、token 或账号 ID。", "ok");
      } catch (_) { setMessage("请选中诊断文本手动复制。", "error"); }
    });
    libraryPlaylistList.appendChild(copy);
  }
  for (const playlist of preview.playlists) {
    const card = document.createElement("div");
    card.className = "library-playlist";
    const title = document.createElement("div");
    title.className = "library-playlist-title";
    title.textContent = String(playlist.title || `歌单 ${playlist.id || ""}`);
    card.appendChild(title);
    const count = document.createElement("div");
    count.className = "library-playlist-count";
    count.textContent = Number.isSafeInteger(playlist.count) && playlist.count >= 0 ? `${playlist.count} 首曲目` : "曲目数未知";
    card.appendChild(count);
    const url = safePlaylistUrl(playlist.url);
    if (url) {
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = url;
      link.title = "打开歌单页面";
      card.appendChild(link);
      const copy = document.createElement("button");
      copy.className = "ghost";
      copy.textContent = "复制 URL";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(url);
          setMessage("歌单 URL 已复制。", "ok");
        } catch (_) {
          setMessage("浏览器未允许复制，请选中显示的 URL 手动复制。", "error");
        }
      });
      card.appendChild(copy);
    } else {
      const unavailable = document.createElement("div");
      unavailable.className = "library-no-url";
      unavailable.textContent = `接口未提供公开 URL（歌单 ID：${playlist.id || "未知"}）`;
      card.appendChild(unavailable);
    }
    libraryPlaylistList.appendChild(card);
  }
  libraryLoadMore.hidden = preview.status !== "ready" || preview.next_offset == null;
  libraryLoadMore.disabled = previewPagePending || previewConfirmPending;
  libraryLoadMore.textContent = previewPagePending ? "正在加载…" : `加载更多（已展示 ${preview.playlists.length}/${preview.playlist_count || 0}）`;
}

function applyLibraryPreviewPage(page, offset = 0) {
  if (!libraryPreview || !page?.preview_id || (libraryPreview.preview_id && libraryPreview.preview_id !== page.preview_id)) {
    throw new Error("歌单预览已失效，请重新读取。");
  }
  const previousRows = libraryPreview.playlists;
  const rows = Array.isArray(page.playlists) ? page.playlists : [];
  if (offset && offset !== previousRows.length) throw new Error("歌单分页发生变化，请重新读取全部歌单。");
  libraryPreview = { ...libraryPreview, ...page, playlists: offset ? [...previousRows, ...rows] : rows };
  if (libraryPreview.status === "ready" && libraryPreview.next_offset != null
      && libraryPreview.next_offset !== libraryPreview.playlists.length) {
    throw new Error("歌单分页不连续，请重新读取全部歌单。");
  }
  if (libraryPreview.status === "error" || libraryPreview.status === "cancelled") {
    setMessage(libraryPreview.error || "歌单读取已停止，请重新读取。", "error");
  } else if (libraryPreview.status === "ready") {
    setMessage("歌单 URL 读取完成；确认后才会下载歌曲。", "ok");
  }
  renderLibraryPreview();
  updateDetected();
}

function failLibraryPreview(error, epoch) {
  if (epoch !== previewEpoch || !libraryPreview) return;
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = null;
  libraryPreview.status = "error";
  libraryPreview.error = error.message || String(error);
  setMessage(libraryPreview.error, "error");
  renderLibraryPreview();
  updateDetected();
}

function scheduleLibraryPreviewPoll(epoch) {
  if (epoch !== previewEpoch || libraryPreview?.status !== "loading") return;
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    previewTimer = null;
    fetchLibraryPreviewPage(0, epoch).catch(error => failLibraryPreview(error, epoch));
  }, 600);
}

async function fetchLibraryPreviewPage(offset = 0, epoch = previewEpoch) {
  if (epoch !== previewEpoch || !libraryPreview) return;
  previewPagePending = true;
  renderLibraryPreview();
  try {
    if (!libraryPreview.preview_id) {
      const response = await send({ type: "getState" });
      if (epoch !== previewEpoch) return;
      const summary = response.kugouPreview;
      if (!summary || summary.tab_id !== previewTabId || summary.request_key !== previewRequestKey) {
        throw new Error("歌单预览已失效，请重新读取。");
      }
      libraryPreview = { ...libraryPreview, ...summary, playlists: [] };
      if (!summary.preview_id) { scheduleLibraryPreviewPoll(epoch); return; }
    }
    const response = await send({ type: "getKugouPreview", previewId: libraryPreview.preview_id, tabId: previewTabId, offset });
    if (epoch !== previewEpoch) return;
    applyLibraryPreviewPage(response.preview, offset);
    scheduleLibraryPreviewPoll(epoch);
  } finally {
    if (epoch === previewEpoch) {
      previewPagePending = false;
      renderLibraryPreview();
      updateDetected();
    }
  }
}

async function beginLibraryPreview() {
  if (!hostReady() || detected.kind !== "library" || previewRequestPending || previewConfirmPending) return;
  invalidateLibraryPreview();
  const epoch = previewEpoch;
  previewSourceValue = sourceUrl.value;
  previewTabId = currentTabId;
  previewRequestKey = `preview-${Date.now()}-${epoch}`;
  libraryPreview = { preview_id: "", status: "loading", playlists: [] };
  previewRequestPending = true;
  renderLibraryPreview();
  updateDetected();
  setMessage("正在读取歌单 URL，确认前不会下载歌曲。");
  try {
    const response = await send({ type: "previewKugouLibrary", url: detected.normalizedUrl,
      tabId: previewTabId, requestKey: previewRequestKey });
    if (epoch !== previewEpoch) return;
    applyLibraryPreviewPage(response.preview);
    scheduleLibraryPreviewPoll(epoch);
  } catch (error) {
    failLibraryPreview(error, epoch);
  } finally {
    if (epoch === previewEpoch) {
      previewRequestPending = false;
      updateDetected();
    }
  }
}

async function restoreLibraryPreview(summary) {
  if (!summary || libraryPreview || detected.kind !== "library" || summary.tab_id !== currentTabId) return;
  previewSourceValue = sourceUrl.value;
  previewTabId = currentTabId;
  previewRequestKey = summary.request_key || "";
  libraryPreview = { ...summary, playlists: [], next_offset: null };
  renderLibraryPreview();
  updateDetected();
  if (["loading", "ready", "consumed"].includes(summary.status)) {
    const epoch = previewEpoch;
    try { await fetchLibraryPreviewPage(0, epoch); } catch (error) { failLibraryPreview(error, epoch); }
  }
}

async function send(payload) {
  const response = await chrome.runtime.sendMessage(payload);
  if (!response?.ok && response?.error) throw new Error(response.error);
  if (response?.queue) renderState({ queue: response.queue, connected: true });
  return response;
}

document.getElementById("readTab").addEventListener("click", () => readCurrentTab().catch((e) => setMessage(e.message, "error")));
sourceUrl.addEventListener("input", () => {
  invalidateLibraryPreview();
  currentTitle = "";
  updateDetected();
});
useCookies.addEventListener("change", () => {
  if (["youtube", "bilibili"].includes(detected.platform)) {
    cookiePreferences[detected.platform] = useCookies.checked;
  }
});

document.querySelectorAll(".mode").forEach((label) => {
  label.addEventListener("click", event => {
    const input = label.querySelector("input");
    if (input.disabled) {
      event.preventDefault();
      return;
    }
    setMode(input.value);
  });
});

enqueueButton.addEventListener("click", async () => {
  updateDetected();
  if (detected.kind === "unknown") return;
  if (detected.platform === "kugou" && detected.kind === "library") {
    await beginLibraryPreview();
    return;
  }
  enqueueButton.disabled = true;
  setMessage("正在加入队列…");
  try {
    await send({
      type: "enqueue",
      url: detected.normalizedUrl,
      kind: detected.kind,
      mode: selectedMode(),
      title: currentTitle,
      useCookies: useCookies.checked,
    });
    const suffix = detected.platform === "youtube" && detected.kind === "channel" ? "；开始前会先整理已经下载的同频道文件。" : "。";
    setMessage(`${detected.label} 已加入队列${suffix}`, "ok");
  } catch (error) {
    setMessage(error.message || String(error), "error");
  } finally {
    updateDetected();
  }
});

libraryLoadMore.addEventListener("click", async () => {
  if (!libraryPreview || previewPagePending || libraryPreview.next_offset == null) return;
  const epoch = previewEpoch;
  try { await fetchLibraryPreviewPage(libraryPreview.next_offset, epoch); }
  catch (error) { failLibraryPreview(error, epoch); }
});

libraryCancel.addEventListener("click", () => {
  const submitted = libraryPreview?.status === "consumed";
  invalidateLibraryPreview();
  updateDetected();
  setMessage(submitted ? "已关闭预览，下载任务仍在队列中。" : "已取消歌单预览，未开始下载。");
});

libraryConfirm.addEventListener("click", async () => {
  if (!canConfirmLibraryPreview()) return;
  const epoch = previewEpoch;
  const previewId = libraryPreview.preview_id;
  previewConfirmPending = true;
  renderLibraryPreview();
  updateDetected();
  try {
    await send({ type: "confirmKugouLibrary", previewId, tabId: previewTabId });
    if (epoch !== previewEpoch || !libraryPreview) return;
    libraryPreview.status = "consumed";
    setMessage("已确认并加入下载队列，将按以上歌单读取全部歌曲。", "ok");
  } catch (error) {
    if (epoch === previewEpoch) setMessage(error.message || String(error), "error");
  } finally {
    if (epoch === previewEpoch) {
      previewConfirmPending = false;
      renderLibraryPreview();
      updateDetected();
    }
  }
});

repairButton.addEventListener("click", async () => {
  updateDetected();
  if (detected.platform !== "youtube" || detected.kind !== "channel" || repairing) return;
  repairing = true;
  updateDetected();
  setMessage("正在读取频道页面的“专辑和单曲”并整理已下载文件；不会重新下载媒体…");
  try {
    const response = await send({ type: "repairChannel", url: detected.normalizedUrl, useCookies: useCookies.checked });
    const repair = response.repair || {};
    const catalog = repair.catalog || {};
    const audio = repair.results?.audio || {};
    const video = repair.results?.video || {};
    const moved = Number(audio.moved || 0) + Number(video.moved || 0);
    const linked = Number(audio.linked || 0) + Number(video.linked || 0);
    const conflicts = Number(audio.conflicts || 0) + Number(video.conflicts || 0);
    setMessage(
      `整理完成：识别 ${catalog.albums || 0} 个专辑/单曲，移动 ${moved} 个文件，补齐 ${linked} 个重复专辑归属${conflicts ? `，${conflicts} 个冲突已保留原文件` : ""}。`,
      conflicts ? "" : "ok",
    );
  } catch (error) {
    setMessage(error.message || String(error), "error");
  } finally {
    repairing = false;
    updateDetected();
  }
});

document.getElementById("reconnectHost").addEventListener("click", async () => {
  if ((state.queue || []).some(task => task.status === "downloading")) {
    setMessage("请先等待当前任务结束或取消任务后，再重新连接 Host。", "error");
    return;
  }
  invalidateLibraryPreview();
  setMessage("正在连接已安装的最新 Host…");
  try {
    const result = await send({ type: "reconnectHost" });
    renderState({ ...result, hostInfo: result.host });
    if (result.host?.version !== expectedHostVersion) {
      throw new Error(`当前 Host ${result.host?.version || "未知"}，需要 ${expectedHostVersion}。请重新运行安装器。`);
    }
    setMessage(`已连接 Host ${result.host.version}（版本验证通过）。`, "ok");
  } catch (error) {
    setMessage(error.message || String(error), "error");
  }
});

document.getElementById("clearFinished").addEventListener("click", () => send({ type: "clearFinished" }).catch((e) => setMessage(e.message, "error")));
document.getElementById("openFolder").addEventListener("click", () => send({ type: "openFolder", mode: selectedMode() }).catch((e) => setMessage(e.message, "error")));

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "state") renderState(message);
});

(async () => {
  await readCurrentTab();
  try {
    const initial = await chrome.runtime.sendMessage({ type: "getState" });
    renderState(initial || {});
    if (initial?.connected) {
      const refreshed = await chrome.runtime.sendMessage({ type: "refresh" });
      renderState(refreshed || initial);
    }
    await restoreLibraryPreview(state.kugouPreview);
  } catch (error) {
    setMessage(error.message || String(error), "error");
  }
})();
