importScripts("platform_urls.js");
importScripts("kugou_session.js");

const HOST_NAME = "com.wongyiuming.yt_autodownload";
const EXPECTED_HOST_VERSION = chrome.runtime.getManifest().version;

function hostVersionMatches(info) {
  return info?.version === EXPECTED_HOST_VERSION;
}

function hostVersionError(info) {
  return `Host 版本不匹配：当前 ${info?.version || "未知"}，需要 ${EXPECTED_HOST_VERSION}。请重新运行最新版 Install-Windows.bat。`;
}

let port = null;
let connected = false;
let compatible = false;
let reconnectTimer = null;
let seq = 1;
const pending = new Map();
let queue = [];
let hostInfo = {};
let kugouPreview = null;
let previewGeneration = 0;

function previewSummary() {
  if (!kugouPreview) return null;
  const { preview_id, status, user_id, playlist_count, known_track_count, unknown_track_count,
    error, tab_id, request_key, loaded_count, confirming } = kugouPreview;
  return { preview_id, status, user_id, playlist_count, known_track_count, unknown_track_count,
    error, tab_id, request_key, loaded_count, confirming };
}

function broadcast() {
  chrome.runtime.sendMessage({ type: "state", connected, compatible, expectedHostVersion: EXPECTED_HOST_VERSION, queue, hostInfo, kugouPreview: previewSummary() }).catch(() => {});
}

function discardKugouPreview(cancelRemote = true) {
  const previous = kugouPreview;
  kugouPreview = null;
  previewGeneration += 1;
  if (cancelRemote && previous?.preview_id && !previous.confirming && previous.status !== "consumed" && port && compatible) {
    rpc("kugou_preview_cancel", { preview_id: previous.preview_id }).catch(() => {});
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectHost();
  }, 1500);
}

function connectHost() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (error) {
    connected = false;
    port = null;
    scheduleReconnect();
    broadcast();
    return;
  }

  const activePort = port;
  activePort.onMessage.addListener((message) => {
    if (port !== activePort) return;
    connected = true;
    if (message.reply_to && pending.has(message.reply_to)) {
      const { resolve, reject, timeout } = pending.get(message.reply_to);
      clearTimeout(timeout);
      pending.delete(message.reply_to);
      if (message.ok === false) reject(new Error(message.error || "Native host error"));
      else resolve(message);
    }
    if (message.event === "queue") queue = message.queue || [];
    if (message.event === "hello" || message.host) {
      hostInfo = message.host || hostInfo;
      compatible = hostVersionMatches(hostInfo);
    }
    broadcast();
  });

  activePort.onDisconnect.addListener(() => {
    if (port !== activePort) return;
    discardKugouPreview(false);
    compatible = false;
    hostInfo = {};
    const error = chrome.runtime.lastError?.message || "Native host disconnected";
    for (const { reject, timeout } of pending.values()) {
      clearTimeout(timeout);
      reject(new Error(error));
    }
    pending.clear();
    port = null;
    connected = false;
    scheduleReconnect();
    broadcast();
  });

  connected = true;
  compatible = false;
  rpc("hello", {}).then((message) => {
    hostInfo = message.host || {};
    compatible = hostVersionMatches(hostInfo);
    queue = message.queue || queue;
    broadcast();
  }).catch(() => {});
}

function rpc(action, payload = {}, timeoutMs = 30000) {
  connectHost();
  if (!port) return Promise.reject(new Error("本机下载组件未连接"));
  if (!["hello", "list"].includes(action) && !compatible) {
    return Promise.reject(new Error(hostVersionError(hostInfo)));
  }
  const id = `r${Date.now()}_${seq++}`;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error("本机下载组件响应超时"));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timeout });
    try {
      port.postMessage({ id, action, ...payload });
    } catch (error) {
      clearTimeout(timeout);
      pending.delete(id);
      reject(error);
    }
  });
}

async function reconnectHost() {
  discardKugouPreview();
  if (port) {
    const oldPort = port;
    port = null;
    for (const { reject, timeout } of pending.values()) {
      clearTimeout(timeout);
      reject(new Error("Host 正在重新连接"));
    }
    pending.clear();
    try { oldPort.disconnect(); } catch (_) {}
  }
  connected = false;
  compatible = false;
  hostInfo = {};
  broadcast();
  connectHost();
  const result = await rpc("hello", {}, 30000);
  hostInfo = result.host || {};
  compatible = hostVersionMatches(hostInfo);
  queue = result.queue || queue;
  broadcast();
  if (!compatible) throw new Error(hostVersionError(hostInfo));
  return { ok: true, host: hostInfo, queue, connected: true, compatible: true };
}

function recognizedSource(raw) {
  const source = classifySourceUrl(raw || "");
  if (source.kind === "unknown") throw new Error("不支持的来源 URL");
  return source;
}

async function currentKugouTab(tabId, expectedUrl) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const source = classifySourceUrl(tab?.url || "");
  if (!Number.isInteger(tab?.id) || (tabId != null && tab.id !== tabId)
      || source.platform !== "kugou" || source.kind !== "library"
      || (expectedUrl != null && tab.url !== expectedUrl)) {
    throw new Error("酷狗标签页已切换，请回到已登录的酷狗主站，重新读取歌单 URL。");
  }
  return tab;
}

function rememberPreviewPage(record, page, offset = 0) {
  if (!page?.preview_id || (record.preview_id && record.preview_id !== page.preview_id)
      || (record.user_id && page.user_id && record.user_id !== page.user_id)
      || !Number.isSafeInteger(page.playlist_count) || page.playlist_count < 0) {
    throw new Error("歌单预览已失效，请重新读取歌单 URL。");
  }
  for (const key of ["preview_id", "status", "user_id", "playlist_count", "known_track_count", "unknown_track_count", "error"]) {
    record[key] = page[key];
  }
  if (page.status === "ready" && offset <= record.loaded_count && Array.isArray(page.playlists)) {
    record.loaded_count = Math.max(record.loaded_count, offset + page.playlists.length);
  }
}

async function requireKugouPreview(message) {
  const record = kugouPreview;
  if (!record?.preview_id || record.preview_id !== message.previewId || record.tab_id !== message.tabId) {
    throw new Error("歌单预览已失效，请重新读取歌单 URL。");
  }
  try {
    await currentKugouTab(record.tab_id, record.tab_url);
  } catch (error) {
    if (kugouPreview === record) discardKugouPreview();
    throw error;
  }
  if (kugouPreview !== record) throw new Error("歌单预览已取消，请重新读取歌单 URL。");
  return record;
}

async function startKugouPreview(message) {
  const source = recognizedSource(message.url);
  if (source.platform !== "kugou" || source.kind !== "library") throw new Error("请从酷狗主站读取个人歌单 URL。");
  if (!compatible) throw new Error(hostVersionError(hostInfo));
  discardKugouPreview();
  const generation = previewGeneration;
  const record = { preview_id: "", status: "loading", tab_id: message.tabId, tab_url: "",
    request_key: message.requestKey || "", loaded_count: 0, confirming: false };
  kugouPreview = record;
  try {
    const tab = await currentKugouTab(message.tabId);
    if (generation !== previewGeneration || kugouPreview !== record) throw new Error("歌单读取已取消。");
    record.tab_id = tab.id;
    record.tab_url = tab.url;
    record.window_id = tab.windowId;
    const cookies = await getKugouSessionCookies(tab.id);
    await currentKugouTab(tab.id, tab.url);
    if (generation !== previewGeneration || kugouPreview !== record) throw new Error("歌单读取已取消。");
    const result = await rpc("kugou_preview_start", { cookies });
    if (generation !== previewGeneration || kugouPreview !== record) {
      if (result.preview?.preview_id && port && compatible) {
        rpc("kugou_preview_cancel", { preview_id: result.preview.preview_id }).catch(() => {});
      }
      throw new Error("歌单读取已取消。");
    }
    rememberPreviewPage(record, result.preview);
    broadcast();
    return result;
  } catch (error) {
    if (kugouPreview === record) discardKugouPreview();
    throw error;
  }
}

async function getKugouPreview(message) {
  const record = await requireKugouPreview(message);
  const offset = Number(message.offset || 0);
  if (!Number.isSafeInteger(offset) || offset < 0) throw new Error("歌单分页无效。");
  const result = await rpc("kugou_preview_get", { preview_id: record.preview_id, offset, limit: 100 });
  if (kugouPreview !== record) throw new Error("歌单预览已取消。");
  try {
    await currentKugouTab(record.tab_id, record.tab_url);
  } catch (error) {
    if (kugouPreview === record) { discardKugouPreview(); broadcast(); }
    throw error;
  }
  if (kugouPreview !== record) throw new Error("歌单预览已取消。");
  rememberPreviewPage(record, result.preview, offset);
  return result;
}

async function confirmKugouPreview(message) {
  const record = await requireKugouPreview(message);
  if (record.confirming || record.status === "consumed") throw new Error("这份歌单已提交或正在提交，请勿重复确认。");
  if (record.status !== "ready" || record.loaded_count < record.playlist_count) {
    throw new Error("请先读取并展示全部歌单 URL，再确认下载。");
  }
  record.confirming = true;
  broadcast();
  try {
    const cookies = await getKugouSessionCookies(record.tab_id);
    await currentKugouTab(record.tab_id, record.tab_url);
    if (kugouPreview !== record) throw new Error("歌单预览已失效，请重新读取。");
    const result = await rpc("kugou_preview_confirm", { preview_id: record.preview_id, cookies }, 60000);
    record.status = "consumed";
    queue = result.queue || queue;
    broadcast();
    return result;
  } finally {
    record.confirming = false;
    if (kugouPreview === record) broadcast();
  }
}

async function singleSongCookies(source, tabId) {
  // A pasted URL must not accidentally borrow another tab/account's session.
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const current = classifySourceUrl(tab?.url || "");
  if (!Number.isInteger(tab?.id) || (tabId != null && tab.id !== tabId)
      || current.platform !== "kugou" || current.kind !== "song"
      || current.normalizedUrl !== source.normalizedUrl) {
    throw new Error("请打开这首歌的酷狗网页并读取当前页，再使用登录态下载；公开下载可取消勾选登录状态。");
  }
  const cookies = await getKugouSessionCookies(tab.id);
  const [after] = await chrome.tabs.query({ active: true, currentWindow: true });
  const still = await chrome.tabs.get(tab.id);
  if (after?.id !== tab.id || still?.url !== tab.url) {
    throw new Error("读取登录态期间歌曲标签页发生切换或跳转，请重新读取当前页。");
  }
  return cookies;
}

async function platformCookies(platform, enabled) {
  const domain = { youtube: "youtube.com", bilibili: "bilibili.com" }[platform];
  // Personal-library retries use the active main-site session. Singles bind
  // their own exact source tab via singleSongCookies, never a different tab.
  if (platform === "kugou" && enabled) return getKugouSessionCookies();
  if (!enabled || !domain) return [];
  try {
    const cookies = await chrome.cookies.getAll({ domain });
    return cookies.filter(cookie => {
      const host = String(cookie.domain || "").replace(/^\./, "").toLowerCase();
      return host === domain || host.endsWith(`.${domain}`);
    });
  } catch (_) {
    return [];
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.type === "getState") {
      sendResponse({ connected, compatible, expectedHostVersion: EXPECTED_HOST_VERSION, queue, hostInfo, kugouPreview: previewSummary() });
      return;
    }
    if (message.type === "reconnectHost") {
      sendResponse(await reconnectHost());
      return;
    }
    if (message.type === "refresh") {
      const result = await rpc("list");
      queue = result.queue || [];
      hostInfo = result.host || hostInfo;
      sendResponse({ connected: true, compatible, expectedHostVersion: EXPECTED_HOST_VERSION, queue, hostInfo, kugouPreview: previewSummary() });
      return;
    }
    if (message.type === "previewKugouLibrary") {
      sendResponse(await startKugouPreview(message));
      return;
    }
    if (message.type === "getKugouPreview") {
      sendResponse(await getKugouPreview(message));
      return;
    }
    if (message.type === "cancelKugouPreview") {
      if (kugouPreview && (!message.previewId || kugouPreview.preview_id === message.previewId)
          && (!message.requestKey || kugouPreview.request_key === message.requestKey)
          && (message.tabId == null || kugouPreview.tab_id === message.tabId)) {
        discardKugouPreview();
        broadcast();
      }
      sendResponse({ ok: true });
      return;
    }
    if (message.type === "confirmKugouLibrary") {
      sendResponse(await confirmKugouPreview(message));
      return;
    }
    if (message.type === "enqueue") {
      const source = recognizedSource(message.url);
      if (source.platform === "kugou" && source.kind === "library") {
        throw new Error("请先读取歌单 URL，再点击确认下载。");
      }
      const singleSong = source.platform === "kugou" && source.kind === "song";
      const useCookies = (source.platform !== "kugou" || singleSong) && Boolean(message.useCookies);
      const cookies = singleSong && useCookies
        ? await singleSongCookies(source, message.tabId)
        : await platformCookies(source.platform, useCookies);
      const result = await rpc("enqueue", {
        url: source.normalizedUrl,
        mode: source.platform === "kugou" ? "audio" : message.mode,
        kind: source.kind,
        platform: source.platform,
        title: message.title || "",
        cookies,
        use_cookies: useCookies,
      }, 60000);
      queue = result.queue || queue;
      sendResponse(result);
      return;
    }
    if (message.type === "repairChannel") {
      const source = recognizedSource(message.url);
      if (source.platform !== "youtube" || source.kind !== "channel") {
        throw new Error("频道整理只适用于 YouTube 公开频道");
      }
      const cookies = await platformCookies(source.platform, Boolean(message.useCookies));
      sendResponse(await rpc("repair_channel", { url: source.normalizedUrl, cookies }, 10 * 60 * 1000));
      return;
    }
    if (message.type === "retry") {
      const task = queue.find(item => item.id === message.taskId);
      if (!task) throw new Error("任务不存在，请刷新队列后重试");
      // Derive the platform from the task itself, never the currently open tab.
      const source = recognizedSource(task.url);
      if (source.platform === "kugou" && source.kind === "library" && task.kugou_confirmed !== true) {
        throw new Error("此个人歌单任务尚未确认，请重新读取歌单 URL 并确认下载。");
      }
      const singleSong = source.platform === "kugou" && source.kind === "song";
      const useCookies = source.platform === "kugou" && !singleSong
        ? source.kind === "library" : Boolean(message.useCookies ?? task.use_cookies);
      const cookies = singleSong && useCookies
        ? await singleSongCookies(source, message.tabId)
        : await platformCookies(source.platform, useCookies);
      const result = await rpc("retry", { task_id: message.taskId, cookies, use_cookies: useCookies });
      queue = result.queue || queue;
      sendResponse(result);
      return;
    }
    if (message.type === "cancel") {
      const result = await rpc("cancel", { task_id: message.taskId });
      queue = result.queue || queue;
      sendResponse(result);
      return;
    }
    if (message.type === "remove") {
      const result = await rpc("remove", { task_id: message.taskId });
      queue = result.queue || queue;
      sendResponse(result);
      return;
    }
    if (message.type === "clearFinished") {
      const result = await rpc("clear_finished");
      queue = result.queue || queue;
      sendResponse(result);
      return;
    }
    if (message.type === "openFolder") {
      sendResponse(await rpc("open_folder", { mode: message.mode || "video" }));
      return;
    }
    sendResponse({ ok: false, error: "Unknown message" });
  })().catch((error) => sendResponse({ ok: false, error: String(error.message || error) }));
  return true;
});

chrome.runtime.onStartup.addListener(connectHost);
chrome.runtime.onInstalled.addListener(connectHost);
chrome.tabs?.onActivated?.addListener(({ tabId, windowId }) => {
  if (kugouPreview && kugouPreview.window_id === windowId && kugouPreview.tab_id !== tabId) {
    discardKugouPreview();
    broadcast();
  }
});
chrome.tabs?.onUpdated?.addListener((tabId, change) => {
  if (kugouPreview?.tab_id === tabId && (change.status === "loading" || (change.url && change.url !== kugouPreview.tab_url))) {
    discardKugouPreview();
    broadcast();
  }
});
chrome.tabs?.onRemoved?.addListener(tabId => {
  if (kugouPreview?.tab_id === tabId) { discardKugouPreview(); broadcast(); }
});
connectHost();
