const HOST_NAME = "com.wongyiuming.yt_autodownload";

let port = null;
let connected = false;
let reconnectTimer = null;
let seq = 1;
const pending = new Map();
let queue = [];
let hostInfo = {};

function broadcast() {
  chrome.runtime.sendMessage({ type: "state", connected, queue, hostInfo }).catch(() => {});
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

  port.onMessage.addListener((message) => {
    connected = true;
    if (message.reply_to && pending.has(message.reply_to)) {
      const { resolve, reject, timeout } = pending.get(message.reply_to);
      clearTimeout(timeout);
      pending.delete(message.reply_to);
      if (message.ok === false) reject(new Error(message.error || "Native host error"));
      else resolve(message);
    }
    if (message.event === "queue") queue = message.queue || [];
    if (message.event === "hello" || message.host) hostInfo = message.host || hostInfo;
    broadcast();
  });

  port.onDisconnect.addListener(() => {
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
  rpc("hello", {}).then((message) => {
    hostInfo = message.host || {};
    queue = message.queue || queue;
    broadcast();
  }).catch(() => {});
}

function rpc(action, payload = {}, timeoutMs = 30000) {
  connectHost();
  if (!port) return Promise.reject(new Error("本机下载组件未连接"));
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

async function youtubeCookies() {
  try {
    return await chrome.cookies.getAll({ domain: ".youtube.com" });
  } catch (_) {
    return [];
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.type === "getState") {
      sendResponse({ connected, queue, hostInfo });
      return;
    }
    if (message.type === "refresh") {
      const result = await rpc("list");
      queue = result.queue || [];
      hostInfo = result.host || hostInfo;
      sendResponse({ connected: true, queue, hostInfo });
      return;
    }
    if (message.type === "enqueue") {
      const cookies = await youtubeCookies();
      const result = await rpc("enqueue", {
        url: message.url,
        mode: message.mode,
        kind: message.kind,
        title: message.title || "",
        cookies,
      }, 60000);
      queue = result.queue || queue;
      sendResponse(result);
      return;
    }
    if (message.type === "repairChannel") {
      const cookies = await youtubeCookies();
      sendResponse(await rpc("repair_channel", { url: message.url, cookies }, 10 * 60 * 1000));
      return;
    }
    if (message.type === "retry") {
      const result = await rpc("retry", { task_id: message.taskId });
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
