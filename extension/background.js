importScripts("platform_urls.js");

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

function broadcast() {
  chrome.runtime.sendMessage({ type: "state", connected, compatible, expectedHostVersion: EXPECTED_HOST_VERSION, queue, hostInfo }).catch(() => {});
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

async function platformCookies(platform, enabled) {
  const domain = { youtube: "youtube.com", bilibili: "bilibili.com" }[platform];
  // Kugou always uses public HTTP requests, including on retries.
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
      sendResponse({ connected, compatible, expectedHostVersion: EXPECTED_HOST_VERSION, queue, hostInfo });
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
      sendResponse({ connected: true, compatible, expectedHostVersion: EXPECTED_HOST_VERSION, queue, hostInfo });
      return;
    }
    if (message.type === "enqueue") {
      const source = recognizedSource(message.url);
      const useCookies = source.platform !== "kugou" && Boolean(message.useCookies);
      const cookies = await platformCookies(source.platform, useCookies);
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
      const useCookies = source.platform !== "kugou" && Boolean(message.useCookies ?? task.use_cookies);
      const cookies = await platformCookies(source.platform, useCookies);
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
connectHost();
