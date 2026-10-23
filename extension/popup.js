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

let detected = { platform: "", kind: "unknown", normalizedUrl: "", label: "未识别" };
let state = { connected: false, queue: [], hostInfo: {} };
let currentTitle = "";
let repairing = false;
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
  document.querySelectorAll(".mode").forEach(label => {
    const input = label.querySelector("input");
    input.disabled = kugou && input.value === "video";
    label.classList.toggle("disabled", input.disabled);
    label.setAttribute("aria-disabled", String(input.disabled));
  });
  if (kugou) setMode("audio");
  useCookies.disabled = !["youtube", "bilibili"].includes(detected.platform);
  if (useCookies.disabled) useCookies.checked = false;
  cookieLabel.textContent = kugou
    ? "酷狗音乐使用 HTTP 直连，无需读取浏览器 Cookie"
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
    unknown: "支持 YouTube 视频 / 列表 / 频道、bilibili 视频 / 收藏夹 / 合集、酷狗歌曲 / 歌单",
  };
  sourceHint.textContent = kugou && detected.kind === "playlist"
    ? "将读取完整歌单，逐曲获取播放地址并下载为 MP3"
    : hints[detected.kind] || hints.unknown;
  enqueueButton.disabled = detected.kind === "unknown" || !hostReady() || repairing;
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
  return { video: "视频", playlist: "列表", channel: "频道", favorites: "收藏夹", collection: "合集", shortlink: "短链接", song: "歌曲" }[kind] || "来源";
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
    const currentCount = task.item_index && task.item_count ? ` · ${task.item_index}/${task.item_count}` : "";
    const speed = task.speed || "";
    const eta = task.eta ? ` · ETA ${task.eta}` : "";
    const error = task.error ? ` · ${task.error}` : "";
    const taskPlatform = classifySourceUrl(task.url).platform;

    card.innerHTML = `
      <div class="task-top">
        <div style="min-width:0">
          <div class="task-title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
          <div class="task-sub">${platformText(taskPlatform)} · ${kindText(task.kind)} · ${task.mode === "audio" ? "MP3" : "MP4"}${currentCount}</div>
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
        retry.textContent = "重试";
        retry.addEventListener("click", () => {
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

async function send(payload) {
  const response = await chrome.runtime.sendMessage(payload);
  if (!response?.ok && response?.error) throw new Error(response.error);
  if (response?.queue) renderState({ queue: response.queue, connected: true });
  return response;
}

document.getElementById("readTab").addEventListener("click", () => readCurrentTab().catch((e) => setMessage(e.message, "error")));
sourceUrl.addEventListener("input", () => {
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
  } catch (error) {
    setMessage(error.message || String(error), "error");
  }
})();
