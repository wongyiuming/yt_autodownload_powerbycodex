const sourceUrl = document.getElementById("sourceUrl");
const kindBadge = document.getElementById("kindBadge");
const sourceHint = document.getElementById("sourceHint");
const enqueueButton = document.getElementById("enqueue");
const messageElement = document.getElementById("message");
const queueElement = document.getElementById("queue");
const queueCount = document.getElementById("queueCount");
const hostState = document.getElementById("hostState");
const hostDetails = document.getElementById("hostDetails");

let detected = { kind: "unknown", normalizedUrl: "", label: "未识别" };
let state = { connected: false, queue: [], hostInfo: {} };
let currentTitle = "";

function classifyYoutubeUrl(raw) {
  try {
    const url = new URL(raw.trim());
    const host = url.hostname.toLowerCase().replace(/^www\./, "");
    if (host === "youtu.be") {
      const id = url.pathname.split("/").filter(Boolean)[0];
      if (!id) throw new Error("缺少视频 ID");
      return { kind: "video", label: "单个视频", normalizedUrl: `https://www.youtube.com/watch?v=${encodeURIComponent(id)}` };
    }
    if (!["youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) {
      return { kind: "unknown", label: "非 YouTube URL", normalizedUrl: raw.trim() };
    }

    const parts = url.pathname.split("/").filter(Boolean);
    const listId = url.searchParams.get("list");
    if (url.pathname === "/playlist" && listId) {
      return { kind: "playlist", label: "视频列表", normalizedUrl: `https://www.youtube.com/playlist?list=${encodeURIComponent(listId)}` };
    }
    if (url.pathname === "/watch" && listId) {
      return { kind: "playlist", label: "视频列表", normalizedUrl: `https://www.youtube.com/playlist?list=${encodeURIComponent(listId)}` };
    }
    if (url.pathname === "/watch" && url.searchParams.get("v")) {
      return { kind: "video", label: "单个视频", normalizedUrl: `https://www.youtube.com/watch?v=${encodeURIComponent(url.searchParams.get("v"))}` };
    }
    if (["shorts", "live", "embed"].includes(parts[0]) && parts[1]) {
      return { kind: "video", label: "单个视频", normalizedUrl: `https://www.youtube.com/watch?v=${encodeURIComponent(parts[1])}` };
    }

    const channelRoot = parts[0]?.startsWith("@")
      ? [parts[0]]
      : ["channel", "c", "user"].includes(parts[0]) && parts[1]
        ? [parts[0], parts[1]]
        : null;
    if (channelRoot) {
      return {
        kind: "channel",
        label: "公开频道",
        normalizedUrl: `https://www.youtube.com/${channelRoot.join("/")}`,
      };
    }
    return { kind: "unknown", label: "无法识别", normalizedUrl: raw.trim() };
  } catch (_) {
    return { kind: "unknown", label: "URL 无效", normalizedUrl: raw.trim() };
  }
}

function updateDetected() {
  detected = classifyYoutubeUrl(sourceUrl.value);
  kindBadge.className = `badge ${detected.kind}`;
  kindBadge.textContent = detected.label;
  const hints = {
    video: "将下载当前视频",
    playlist: "将下载列表内全部公开视频",
    channel: "将下载该公开频道的全部可访问视频",
    unknown: "请打开 YouTube 视频、播放列表或公开频道",
  };
  sourceHint.textContent = hints[detected.kind] || hints.unknown;
  enqueueButton.disabled = detected.kind === "unknown" || !state.connected;
}

function selectedMode() {
  return document.querySelector('input[name="mode"]:checked')?.value || "video";
}

async function readCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
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
  hostState.textContent = state.connected ? "本机组件已连接" : "本机组件未连接";
  hostState.className = `host-state ${state.connected ? "online" : "offline"}`;
  const info = state.hostInfo || {};
  hostDetails.textContent = state.connected
    ? `下载目录：${info.download_root || "-"}${info.node ? ` · Node: ${info.node}` : " · 未检测到 Node.js（部分 YouTube 资源可能受影响）"}`
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
  return { video: "单视频", playlist: "列表", channel: "频道" }[kind] || "来源";
}

function renderQueue(items) {
  queueCount.textContent = `${items.length} 项`;
  queueElement.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "队列为空。打开一个 YouTube 页面后即可加入。";
    queueElement.appendChild(empty);
    return;
  }

  for (const task of items.slice().reverse()) {
    const card = document.createElement("div");
    card.className = "task";
    const percent = Math.max(0, Math.min(100, Number(task.progress || 0)));
    const title = task.current_title || task.title || task.url;
    const currentCount = task.item_index && task.item_count ? ` · ${task.item_index}/${task.item_count}` : "";
    const speed = task.speed || "";
    const eta = task.eta ? ` · ETA ${task.eta}` : "";
    const error = task.error ? ` · ${task.error}` : "";

    card.innerHTML = `
      <div class="task-top">
        <div style="min-width:0">
          <div class="task-title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
          <div class="task-sub">${kindText(task.kind)} · ${task.mode === "audio" ? "MP3" : "MP4"}${currentCount}</div>
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
      cancel.addEventListener("click", () => send({ type: "cancel", taskId: task.id }));
      actions.appendChild(cancel);
    } else {
      const remove = document.createElement("button");
      remove.className = "ghost";
      remove.textContent = "移除";
      remove.addEventListener("click", () => send({ type: "remove", taskId: task.id }));
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

async function send(payload) {
  const response = await chrome.runtime.sendMessage(payload);
  if (!response?.ok && response?.error) throw new Error(response.error);
  if (response?.queue) renderState({ queue: response.queue, connected: true });
  return response;
}

document.getElementById("readTab").addEventListener("click", () => readCurrentTab().catch((e) => setMessage(e.message, "error")));
sourceUrl.addEventListener("input", updateDetected);

document.querySelectorAll(".mode").forEach((label) => {
  label.addEventListener("click", () => {
    document.querySelectorAll(".mode").forEach((item) => item.classList.remove("active"));
    label.classList.add("active");
    label.querySelector("input").checked = true;
  });
});

enqueueButton.addEventListener("click", async () => {
  updateDetected();
  if (detected.kind === "unknown") return;
  enqueueButton.disabled = true;
  setMessage("正在加入队列…");
  try {
    await send({
      type: "enqueue",
      url: detected.normalizedUrl,
      kind: detected.kind,
      mode: selectedMode(),
      title: currentTitle,
    });
    setMessage(`${detected.label} 已加入队列，可以继续打开其他页面继续添加。`, "ok");
  } catch (error) {
    setMessage(error.message || String(error), "error");
  } finally {
    updateDetected();
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
  } catch (error) {
    setMessage(error.message || String(error), "error");
  }
})();
