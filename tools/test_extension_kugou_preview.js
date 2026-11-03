"use strict";
/** Real extension scripts with a simulated Native Host and DOM; no account/network. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extension = path.join(__dirname, "../extension");
const manifest = JSON.parse(fs.readFileSync(path.join(extension, "manifest.json"), "utf8"));
const home = "https://www.kugou.com/";
const playlistUrl = id => `https://www.kugou.com/yy/special/single/${id}.html`;
const flush = () => new Promise(setImmediate);
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.events = {};
    this.attributes = {};
    this.value = "";
    this.checked = this.disabled = this.hidden = false;
    this._html = this._text = "";
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name),
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
    };
  }
  set innerHTML(value) { this._html = value; this.children = []; }
  get innerHTML() { return this._html; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  appendChild(child) { this.children.push(child); }
  querySelector(selector) { return selector === ".task-actions" ? (this.actions ||= new Element()) : this.input; }
}

function harness(options = {}) {
  let activeTab = { id: 17, windowId: 1, url: home, title: "酷狗音乐" };
  const tabs = new Map([[17, activeTab]]);
  let account = "123";
  let backgroundListener;
  let nextPreview = 1;
  const popupListeners = new Set();
  const requests = [];
  const previews = new Map();
  const queue = [];
  const authReads = [];
  const copied = [];
  const gates = new Map();
  const tabListeners = {};
  const rows = options.rows || [
    { id: "101", title: '<img src=x onerror="bad()">', count: 0, url: playlistUrl(101) },
    { id: "102", title: "未公开歌单", count: null, url: "" },
    { id: "103", title: "拒绝不安全 URL", count: 4, url: "javascript:alert(1)" },
  ];

  function page(record, offset = 0, limit = 100) {
    const visible = ["ready", "consumed"].includes(record.status) ? record.rows : [];
    const list = visible.slice(offset, offset + limit);
    return { preview_id: record.id, status: record.status, user_id: record.user,
      playlist_count: visible.length, known_track_count: visible.reduce((sum, item) => sum + (item.count || 0), 0),
      unknown_track_count: visible.filter(item => item.count == null).length, playlists: list,
      offset, next_offset: offset + list.length < visible.length ? offset + list.length : null, error: "" };
  }

  async function host(request) {
    if (["hello", "list"].includes(request.action)) return { host: { version: manifest.version }, queue };
    if (request.action === "kugou_preview_start") {
      const record = { id: `preview-${nextPreview++}`, status: options.startReady ? "ready" : "loading",
        user: request.cookies.find(cookie => cookie.name === "userid").value, rows };
      previews.set(record.id, record);
      if (gates.has(request.action)) await gates.get(request.action).promise;
      return { preview: page(record) };
    }
    if (request.action === "kugou_preview_cancel") {
      const record = previews.get(request.preview_id);
      if (record && record.status !== "consumed") record.status = "cancelled";
      return {};
    }
    const record = previews.get(request.preview_id);
    if (request.action === "kugou_preview_get") {
      if (gates.has(request.action)) await gates.get(request.action).promise;
      if (record.status === "loading") record.status = "ready";
      return { preview: page(record, request.offset, request.limit) };
    }
    if (request.action === "kugou_preview_confirm") {
      if (gates.has(request.action)) await gates.get(request.action).promise;
      const user = request.cookies.find(cookie => cookie.name === "userid").value;
      if (user !== record.user) return { ok: false, error: "当前登录账号与预览账号不一致，请重新读取歌单 URL。" };
      if (record.status !== "ready") return { ok: false, error: "歌单预览未就绪或已提交。" };
      record.status = "consumed";
      queue.push({ id: `task-${queue.length + 1}`, url: home, kind: "library", mode: "audio", status: "queued",
        kugou_user_id: user, kugou_confirmed: true });
      return { queue };
    }
    return { queue };
  }

  const browser = {
    tabs: {
      query: async () => [{ ...activeTab }],
      get: async id => { if (!tabs.has(id)) throw new Error("missing tab"); return { ...tabs.get(id) }; },
      onActivated: { addListener(callback) { tabListeners.activated = callback; } },
      onUpdated: { addListener(callback) { tabListeners.updated = callback; } },
      onRemoved: { addListener(callback) { tabListeners.removed = callback; } },
    },
    scripting: {
      async executeScript({ target }) {
        authReads.push(target.tabId);
        return [{ frameId: 0, result: { origin: new URL(tabs.get(target.tabId).url).origin,
          session: { userid: account, token: `fixture-token-${account}`, appid: "1014", mid: "fixture-mid", dfid: "-" } } }];
      },
    },
    cookies: { getAllCookieStores: async () => [{ id: "0", tabIds: [17] }], getAll: async () => [] },
  };
  const backgroundChrome = { ...browser, runtime: {
    getManifest: () => manifest,
    sendMessage(message) { for (const callback of popupListeners) callback(message); return Promise.resolve(); },
    onInstalled: { addListener() {} }, onStartup: { addListener() {} },
    onMessage: { addListener(callback) { backgroundListener = callback; } },
    connectNative() {
      let messageCallback, disconnectCallback;
      let disconnected = false;
      return {
        onMessage: { addListener(callback) { messageCallback = callback; } },
        onDisconnect: { addListener(callback) { disconnectCallback = callback; } },
        disconnect() { disconnected = true; disconnectCallback?.(); },
        postMessage(request) {
          requests.push(request);
          Promise.resolve().then(() => host(request)).then(result => {
            if (!disconnected) messageCallback({ ok: true, reply_to: request.id, ...result });
          });
        },
      };
    },
  } };
  const context = vm.createContext({ chrome: backgroundChrome, URL, URLSearchParams, console, setTimeout, clearTimeout });
  context.importScripts = filename => vm.runInContext(fs.readFileSync(path.join(extension, filename), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(extension, "background.js"), "utf8"), context);
  const send = message => new Promise(resolve => backgroundListener(message, {}, resolve));

  async function openPopup() {
    const elements = new Map();
    const messages = [];
    const timers = new Map();
    const modes = ["video", "audio"].map(value => {
      const label = new Element("label"); label.input = new Element("input");
      label.input.value = value; label.input.checked = value === "video"; return label;
    });
    const document = {
      getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
      createElement: tag => new Element(tag), querySelectorAll: () => modes,
      querySelector: () => modes.find(mode => mode.input.checked)?.input,
    };
    let listener;
    const chrome = { ...browser, runtime: {
      getManifest: () => manifest,
      onMessage: { addListener(callback) { listener = callback; popupListeners.add(callback); } },
      sendMessage(message) { messages.push(message); return send(message); },
    } };
    const popup = vm.createContext({ chrome, document, URL, URLSearchParams, console,
      navigator: { clipboard: { writeText: async value => copied.push(value) } },
      setTimeout(callback) { const id = {}; timers.set(id, callback); return id; },
      clearTimeout: id => timers.delete(id),
    });
    for (const filename of ["platform_urls.js", "popup.js"]) vm.runInContext(fs.readFileSync(path.join(extension, filename), "utf8"), popup);
    await flush();
    await flush();
    return {
      context: popup, elements, messages,
      click: id => elements.get(id).events.click(),
      edit(url) { elements.get("sourceUrl").value = url; elements.get("sourceUrl").events.input(); },
      async poll() { const [id, callback] = timers.entries().next().value || []; if (callback) { timers.delete(id); callback(); } await flush(); },
      close() { popupListeners.delete(listener); timers.clear(); },
    };
  }

  return { requests, previews, queue, authReads, copied, send, openPopup,
    setAccount(value) { account = value; },
    switchTab(id, url = home) { activeTab = { id, url, windowId: 1 }; tabs.set(id, activeTab); tabListeners.activated?.({ tabId: id, windowId: 1 }); },
    navigate(url) { activeTab.url = url; tabListeners.updated?.(activeTab.id, { url }); },
    removeTab(id) { tabs.delete(id); tabListeners.removed?.(id); },
    gate(action) { const result = deferred(); gates.set(action, result); return result; },
    ready() { for (const record of previews.values()) if (record.status === "loading") record.status = "ready"; },
  };
}

async function testPreviewThenExplicitConfirmation() {
  const h = harness();
  const popup = await h.openPopup();
  assert.equal(popup.elements.get("enqueue").textContent, "读取歌单 URL");
  await popup.click("enqueue");
  assert.equal(h.queue.length, 0);
  assert.equal(h.requests.filter(request => request.action === "enqueue").length, 0);
  assert.equal(popup.elements.get("confirmKugouDownload").disabled, true, "loading cannot confirm");
  await popup.click("confirmKugouDownload");
  assert.equal(h.requests.filter(request => request.action === "kugou_preview_confirm").length, 0);
  await popup.poll();
  const cards = popup.elements.get("kugouPlaylistList").children;
  assert.equal(cards.length, 3);
  assert.match(cards[0].children[0].textContent, /<img/);
  assert.equal(cards[0].children[0].innerHTML, "", "playlist names are text, never HTML");
  assert.equal(cards[0].children[1].textContent, "0 首曲目");
  assert.equal(cards[1].children[1].textContent, "曲目数未知");
  assert.equal(cards[0].children.find(item => item.tagName === "a").href, playlistUrl(101));
  assert.equal(cards[2].children.some(item => item.tagName === "a"), false, "javascript URLs cannot become links");
  await cards[0].children.find(item => item.tagName === "button").events.click();
  assert.deepEqual(h.copied, [playlistUrl(101)]);
  assert.equal(popup.elements.get("confirmKugouDownload").disabled, false);
  const snapshot = await h.send({ type: "getState" });
  assert.ok(!JSON.stringify(snapshot).includes("fixture-token"), "credentials never enter preview state");
  h.setAccount("456");
  await popup.click("confirmKugouDownload");
  assert.equal(h.queue.length, 0, "an account switch cannot enqueue the previous account's preview");
  assert.match(popup.elements.get("message").textContent, /账号.*不一致/);
  h.setAccount("123");
  await popup.click("confirmKugouDownload");
  assert.equal(h.queue.length, 1);
  await popup.click("confirmKugouDownload");
  assert.equal(h.queue.length, 1);
  assert.equal(popup.elements.get("confirmKugouDownload").disabled, true);
  assert.deepEqual(h.authReads, [17, 17, 17], "each confirmation reads the current page's session afresh");
  assert.equal(popup.elements.get("cancelKugouPreview").textContent, "关闭预览");
  await popup.click("cancelKugouPreview");
  assert.match(popup.elements.get("message").textContent, /下载任务仍在队列中/);
  popup.close();
}

async function testAllPagesAndDuplicateSubmission() {
  const rows = Array.from({ length: 105 }, (_, index) => ({ id: `${index + 1}`, title: `歌单 ${index + 1}`,
    count: 1, url: playlistUrl(index + 1) }));
  const h = harness({ rows, startReady: true });
  const popup = await h.openPopup();
  await popup.click("enqueue");
  assert.equal(popup.elements.get("kugouPlaylistList").children.length, 100);
  assert.equal(popup.elements.get("confirmKugouDownload").disabled, true, "unshown pages cannot be confirmed");
  let result = await h.send({ type: "confirmKugouLibrary", previewId: "preview-1", tabId: 17 });
  assert.equal(result.ok, false);
  assert.equal(h.queue.length, 0);
  await popup.click("kugouLoadMore");
  assert.equal(popup.elements.get("kugouPlaylistList").children.length, 105);
  const request = h.requests.find(item => item.action === "kugou_preview_get");
  assert.equal(request.offset, 100);
  assert.equal(request.limit, 100);
  assert.equal(popup.elements.get("confirmKugouDownload").disabled, false);
  const gate = h.gate("kugou_preview_confirm");
  const first = popup.click("confirmKugouDownload");
  await flush();
  await popup.click("confirmKugouDownload");
  result = await h.send({ type: "confirmKugouLibrary", previewId: "preview-1", tabId: 17 });
  assert.equal(result.ok, false);
  assert.equal(h.requests.filter(item => item.action === "kugou_preview_confirm").length, 1);
  gate.resolve();
  await first;
  assert.equal(h.queue.length, 1);
  result = await h.send({ type: "confirmKugouLibrary", previewId: "preview-1", tabId: 17 });
  assert.equal(result.ok, false, "consumed previews stay single-use");
  assert.equal(h.queue.length, 1);
  popup.close();
}

async function testCancelledStartAndLatePage() {
  for (const action of ["kugou_preview_start", "kugou_preview_get"]) {
    const h = harness();
    const popup = await h.openPopup();
    const gate = h.gate(action);
    let pending;
    if (action === "kugou_preview_start") { pending = popup.click("enqueue"); await flush(); }
    else { await popup.click("enqueue"); await popup.poll(); }
    popup.edit("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
    gate.resolve();
    if (pending) await pending;
    await flush();
    assert.equal(popup.elements.get("kugouPreview").hidden, true, action);
    assert.equal(h.queue.length, 0);
    assert.equal([...h.previews.values()][0].status, "cancelled", "late results are cancelled instead of reviving a changed source");
    popup.close();
  }
}

async function testTabBindingReconnectAndResume() {
  const h = harness({ startReady: true });
  let popup = await h.openPopup();
  await popup.click("enqueue");
  popup.close();
  popup = await h.openPopup();
  assert.equal(popup.elements.get("kugouPlaylistList").children.length, 3, "reopening the same tab restores the preview");
  h.switchTab(99);
  await popup.click("confirmKugouDownload");
  assert.equal(h.queue.length, 0);
  assert.match(popup.elements.get("message").textContent, /预览已失效|标签页已切换/);
  h.switchTab(17);
  await popup.click("enqueue");
  const previousId = [...h.previews.keys()].at(-1);
  await popup.click("reconnectHost");
  assert.equal(popup.elements.get("kugouPreview").hidden, true);
  const response = await h.send({ type: "confirmKugouLibrary", previewId: previousId, tabId: 17 });
  assert.equal(response.ok, false);
  assert.equal(h.queue.length, 0);
  popup.close();
}

(async () => {
  await testPreviewThenExplicitConfirmation();
  await testAllPagesAndDuplicateSubmission();
  await testCancelledStartAndLatePage();
  await testTabBindingReconnectAndResume();
  console.log("EXTENSION_KUGOU_PREVIEW_SMOKE_OK");
})().catch(error => { console.error(error); process.exitCode = 1; });
