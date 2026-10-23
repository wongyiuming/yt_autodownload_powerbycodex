"use strict";
/** Exercise the real extension scripts; no Chrome installation or network needed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extension = path.join(__dirname, "../extension");
const manifest = JSON.parse(fs.readFileSync(path.join(extension, "manifest.json"), "utf8"));
const youtube = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
const bilibili = "https://space.bilibili.com/123/favlist?fid=456";
const kugou = "https://www.kugou.com/yy/special/single/123.html";
const flush = () => new Promise(setImmediate);

function backgroundHarness(tasks = []) {
  const requests = [];
  const cookieReads = [];
  let listener;
  const chrome = {
    cookies: {
      async getAll({ domain }) {
        cookieReads.push(domain);
        return [
          { domain: `.${domain}`, name: "session", value: `cookie-for-${domain}` },
          { domain: "evil.example", name: "other", value: "must-not-forward" },
        ];
      },
    },
    runtime: {
      getManifest: () => manifest,
      sendMessage: () => Promise.resolve(),
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
      onMessage: { addListener(callback) { listener = callback; } },
      connectNative() {
        let onMessage;
        return {
          onMessage: { addListener(callback) { onMessage = callback; } },
          onDisconnect: { addListener() {} },
          postMessage(request) {
            requests.push(request);
            queueMicrotask(() => onMessage({
              ok: true,
              reply_to: request.id,
              host: { version: manifest.version },
              queue: tasks,
            }));
          },
        };
      },
    },
  };
  const context = vm.createContext({
    chrome, URL, URLSearchParams, console, setTimeout, clearTimeout, queueMicrotask,
    importScripts(filename) { vm.runInContext(fs.readFileSync(path.join(extension, filename), "utf8"), context); },
  });
  vm.runInContext(fs.readFileSync(path.join(extension, "background.js"), "utf8"), context);
  return {
    requests, cookieReads,
    send: message => new Promise(resolve => listener(message, {}, resolve)),
  };
}

async function testBackgroundRouting() {
  const h = backgroundHarness([
    { id: "bili", url: bilibili, platform: "youtube", use_cookies: true },
    { id: "kugou", url: kugou, use_cookies: true },
    { id: "youtube", url: youtube, use_cookies: true },
  ]);
  await flush();
  let result = await h.send({ type: "enqueue", url: youtube, mode: "video", useCookies: true });
  assert.equal(result.ok, true);
  assert.deepEqual(h.cookieReads, ["youtube.com"]);
  assert.equal(h.requests.at(-1).cookies.length, 1);
  assert.equal(h.requests.at(-1).cookies[0].domain, ".youtube.com");

  result = await h.send({ type: "enqueue", url: bilibili, mode: "audio", useCookies: true });
  assert.equal(result.ok, true);
  assert.equal(h.cookieReads.at(-1), "bilibili.com");
  assert.equal(h.requests.at(-1).platform, "bilibili");
  assert.equal(h.requests.at(-1).cookies[0].domain, ".bilibili.com");
  const reads = h.cookieReads.length;

  await h.send({ type: "enqueue", url: kugou, mode: "video", useCookies: true });
  assert.equal(h.cookieReads.length, reads, "Kugou never reads browser cookies");
  assert.equal(h.requests.at(-1).mode, "audio");
  assert.equal(h.requests.at(-1).use_cookies, false);
  assert.equal(h.requests.at(-1).cookies.length, 0);

  result = await h.send({ type: "enqueue", url: "https://www.kugou.com/song/#hash=0123456789ABCDEF0123456789ABCDEF", mode: "audio", useCookies: true });
  assert.equal(result.ok, true);
  assert.equal(h.requests.at(-1).kind, "song");
  assert.equal(h.requests.at(-1).cookies.length, 0);
  assert.equal(h.cookieReads.length, reads);

  await h.send({ type: "enqueue", url: bilibili, mode: "video" });
  assert.equal(h.cookieReads.length, reads, "Bilibili defaults to anonymous");
  assert.equal(h.requests.at(-1).use_cookies, false);

  await h.send({ type: "retry", taskId: "bili", url: youtube, platform: "youtube", useCookies: true });
  assert.equal(h.cookieReads.at(-1), "bilibili.com", "retry uses task URL, ignoring current URL and stale platform");
  assert.equal(h.requests.at(-1).cookies[0].domain, ".bilibili.com");
  await h.send({ type: "retry", taskId: "youtube" });
  assert.equal(h.cookieReads.at(-1), "youtube.com");
  assert.equal(h.requests.at(-1).use_cookies, true, "retry preserves task preference when not explicitly changed");
  const retryReads = h.cookieReads.length;
  await h.send({ type: "retry", taskId: "kugou", useCookies: true });
  assert.equal(h.cookieReads.length, retryReads);
  assert.equal(h.requests.at(-1).use_cookies, false);
  assert.equal(h.requests.at(-1).cookies.length, 0);

  const dispatched = h.requests.length;
  result = await h.send({ type: "retry", taskId: "missing", useCookies: true });
  assert.equal(result.ok, false);
  assert.match(result.error, /任务不存在/);
  result = await h.send({ type: "enqueue", url: "https://www.bilibili.com.evil.example/video/BV1xx411c7mD", useCookies: true });
  assert.equal(result.ok, false);
  result = await h.send({ type: "repairChannel", url: bilibili, useCookies: true });
  assert.equal(result.ok, false);
  assert.match(result.error, /YouTube/);
  assert.equal(h.requests.length, dispatched, "invalid actions do not reach the Host");
  assert.equal(h.cookieReads.length, retryReads, "invalid actions do not read cookies");
}

function element(value = "") {
  const classes = new Set();
  return {
    value, checked: false, disabled: false, hidden: false, textContent: "", innerHTML: "",
    events: {}, children: [], attributes: {},
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
    },
    setAttribute(name, content) { this.attributes[name] = content; },
    addEventListener(name, callback) { this.events[name] = callback; },
    appendChild(child) { this.children.push(child); },
    querySelector(selector) {
      if (selector === ".task-actions") return this.actions ||= element();
      return this.input;
    },
  };
}

async function testPopupModes() {
  const elements = new Map();
  const modes = ["video", "audio"].map(value => {
    const label = element();
    label.input = element(value);
    label.input.checked = value === "video";
    return label;
  });
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    querySelectorAll: () => modes,
    querySelector: () => modes.find(mode => mode.input.checked)?.input,
    createElement: () => element(),
  };
  const messages = [];
  const chrome = {
    tabs: { query: async () => [{ url: youtube, title: "video" }] },
    runtime: {
      getManifest: () => manifest,
      onMessage: { addListener() {} },
      async sendMessage(message) {
        messages.push(message);
        return { ok: true, connected: true, hostInfo: { version: manifest.version }, queue: [] };
      },
    },
  };
  const context = vm.createContext({ document, chrome, URL, URLSearchParams, console });
  for (const filename of ["platform_urls.js", "popup.js"]) {
    vm.runInContext(fs.readFileSync(path.join(extension, filename), "utf8"), context);
  }
  await flush();
  const cookie = elements.get("useCookies");
  function visit(url) {
    elements.get("sourceUrl").value = url;
    vm.runInContext("updateDetected()", context);
  }
  cookie.checked = true;
  cookie.events.change();
  visit(bilibili);
  assert.equal(cookie.checked, false, "new platform defaults to anonymous");
  assert.equal(cookie.disabled, false);
  assert.equal(elements.get("channelTools").hidden, true);
  cookie.checked = true;
  cookie.events.change();

  visit(kugou);
  assert.equal(cookie.checked, false);
  assert.equal(cookie.disabled, true);
  assert.equal(modes[0].input.disabled, true);
  assert.equal(modes[1].input.checked, true);
  assert.equal(vm.runInContext("selectedMode()", context), "audio");
  let prevented = false;
  modes[0].events.click({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(modes[1].input.checked, true);
  await elements.get("enqueue").events.click();
  const request = messages.findLast(message => message.type === "enqueue");
  assert.equal(request.mode, "audio");
  assert.equal(request.useCookies, false);

  visit("https://www.youtube.com/@example");
  assert.equal(cookie.checked, true, "platform preferences remain independent");
  assert.equal(modes[0].input.disabled, false);
  assert.equal(elements.get("channelTools").hidden, false);
  visit(bilibili);
  assert.equal(cookie.checked, true);
  assert.equal(elements.get("channelTools").hidden, true);
}

(async () => {
  await testBackgroundRouting();
  await testPopupModes();
  console.log("EXTENSION_PLATFORMS_SMOKE_OK");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
