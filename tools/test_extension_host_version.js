"use strict";
/** Exercise the real MV3 background script with mock native messaging ports. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../extension/background.js"), "utf8");
const expectedVersion = JSON.parse(fs.readFileSync(path.join(__dirname, "../extension/manifest.json"), "utf8")).version;

function harness(actualVersion) {
  const ports = [];
  const dispatched = [];
  const chrome = {
    runtime: {
      getManifest() { return { version: expectedVersion }; },
      lastError: null,
      sendMessage() { return Promise.resolve(); },
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
      onMessage: { addListener() {} },
      connectNative() {
        const port = {
          onMessage: { addListener(callback) { port.messageCallback = callback; } },
          onDisconnect: { addListener(callback) { port.disconnectCallback = callback; } },
          postMessage(request) {
            dispatched.push(request.action);
            queueMicrotask(() => {
              if (!port.disconnected && port.messageCallback) {
                port.messageCallback({
                  ok: true,
                  reply_to: request.id,
                  host: { version: actualVersion, download_root: "C:\\Test" },
                  queue: [],
                });
              }
            });
          },
          disconnect() { port.disconnected = true; if (port.disconnectCallback) port.disconnectCallback(); },
        };
        ports.push(port);
        return port;
      },
    },
  };
  const context = vm.createContext({
    chrome,
    console,
    setTimeout,
    clearTimeout,
    queueMicrotask,
    Map,
    URL,
    URLSearchParams,
    importScripts(filename) {
      vm.runInContext(fs.readFileSync(path.join(__dirname, "../extension", filename), "utf8"), context, { filename });
    },
  });
  vm.runInContext(source, context, { filename: "background.js" });
  return { context, ports, dispatched };
}

async function flush() { await new Promise(setImmediate); }

async function testOldHostRejected() {
  const h = harness("1.2.0");
  await flush();
  assert.equal(vm.runInContext("compatible", h.context), false);
  assert.equal(vm.runInContext("hostInfo.version", h.context), "1.2.0");
  await assert.rejects(
    vm.runInContext("rpc('enqueue', {url: 'https://www.youtube.com/watch?v=test'})", h.context),
    /Host 版本不匹配/,
  );
  assert.equal(h.dispatched.filter(x => x === "enqueue").length, 0);
  await assert.rejects(
    vm.runInContext("reconnectHost()", h.context),
    new RegExp(`当前 1\\.2\\.0.*需要 ${expectedVersion.replaceAll(".", "\\.")}`),
  );
  assert.equal(vm.runInContext("compatible", h.context), false);
  assert.ok(h.ports.length >= 2, "reconnect opened another Native Messaging port");
}

async function testCurrentHostAccepted() {
  const h = harness(expectedVersion);
  await flush();
  assert.equal(vm.runInContext("compatible", h.context), true);
  const result = await vm.runInContext("reconnectHost()", h.context);
  assert.equal(result.ok, true);
  assert.equal(result.host.version, expectedVersion);
  assert.equal(vm.runInContext("compatible", h.context), true);
}

(async () => {
  await testOldHostRejected();
  await testCurrentHostAccepted();
  console.log("EXTENSION_HOST_VERSION_SMOKE_OK");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
