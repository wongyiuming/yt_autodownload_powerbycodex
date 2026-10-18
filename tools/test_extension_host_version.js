"use strict";
/** Exercise the real MV3 background script with mock native messaging ports. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../extension/background.js"), "utf8");

function harness(actualVersion, cancelError = null) {
  const ports = [];
  const dispatched = [];
  let listener;
  const requests = [];
  const chrome = {
    runtime: {
      getManifest() { return { version: "1.2.4" }; },
      lastError: null,
      sendMessage() { return Promise.resolve(); },
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
      onMessage: { addListener(callback) { listener = callback; } },
      connectNative() {
        const port = {
          onMessage: { addListener(callback) { port.messageCallback = callback; } },
          onDisconnect: { addListener(callback) { port.disconnectCallback = callback; } },
          postMessage(request) {
            dispatched.push(request.action);
            requests.push(request);
            queueMicrotask(() => {
              if (!port.disconnected && port.messageCallback) {
                port.messageCallback({
                  ok: !(request.action === "cancel" && cancelError),
                  error: request.action === "cancel" ? cancelError : null,
                  reply_to: request.id,
                  host: { version: actualVersion, download_root: "C:\\Test" },
                  queue: request.action === "cancel" ? [{ id: request.task_id, status: "cancelled" }] : [],
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
  });
  vm.runInContext(source, context, { filename: "background.js" });
  return { context, ports, dispatched, requests,
    send(message) { return new Promise(resolve => listener(message, {}, resolve)); },
  };
}

async function flush() { await new Promise(setImmediate); }

async function testOldHostRejected() {
  const h = harness("1.2.0");
  await flush();
  assert.equal(vm.runInContext("compatible", h.context), false);
  assert.equal(vm.runInContext("hostInfo.version", h.context), "1.2.0");
  for (const message of [
    { type: "enqueue", url: "https://www.youtube.com/watch?v=test" },
    { type: "retry", taskId: "existing" },
    { type: "repairChannel", url: "https://www.youtube.com/@test" },
  ]) {
    const result = await h.send(message);
    assert.equal(result.ok, false);
    assert.match(result.error, /Host 版本不匹配/);
  }
  assert.ok(h.dispatched.every(x => ["hello", "list"].includes(x)));
  const cancelled = await h.send({ type: "cancel", taskId: "active-long-task" });
  assert.equal(cancelled.ok, true);
  assert.equal(h.requests.at(-1).action, "cancel");
  assert.equal(h.requests.at(-1).task_id, "active-long-task");
  assert.equal(vm.runInContext("queue[0].status", h.context), "cancelled");
  assert.equal(vm.runInContext("compatible", h.context), false);
  assert.equal((await h.send({ type: "refresh" })).compatible, false);
  await assert.rejects(
    vm.runInContext("reconnectHost()", h.context),
    /当前 1\.2\.0.*需要 1\.2\.4/,
  );
  assert.equal(vm.runInContext("compatible", h.context), false);
  assert.ok(h.ports.length >= 2, "reconnect opened another Native Messaging port");
}

async function testCancelFailureIsNotReportedAsSuccess() {
  const h = harness("1.2.0", "Task does not exist");
  await flush();
  const result = await h.send({ type: "cancel", taskId: "missing" });
  assert.equal(result.ok, false);
  assert.match(result.error, /Task does not exist/);
  assert.equal(h.dispatched.at(-1), "cancel");
}

async function testCurrentHostAccepted() {
  const h = harness("1.2.4");
  await flush();
  assert.equal(vm.runInContext("compatible", h.context), true);
  const result = await vm.runInContext("reconnectHost()", h.context);
  assert.equal(result.ok, true);
  assert.equal(result.host.version, "1.2.4");
  assert.equal(vm.runInContext("compatible", h.context), true);
}

(async () => {
  await testOldHostRejected();
  await testCurrentHostAccepted();
  await testCancelFailureIsNotReportedAsSuccess();
  console.log("EXTENSION_HOST_VERSION_SMOKE_OK");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
