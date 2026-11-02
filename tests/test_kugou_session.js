"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../extension/kugou_session.js"), "utf8");
const TOKEN = "fixture-token-never-log";
const TAB = { id: 27, url: "https://www.kugou.com/", incognito: false };

function helper(chrome) {
  const context = vm.createContext({ chrome, URL, setTimeout, clearTimeout, module: { exports: {} } });
  vm.runInContext(source, context);
  return context.module.exports;
}

function cookie(name, value, extra = {}) {
  return { name, value, domain: ".kugou.com", path: "/", session: true, ...extra };
}

function cookieChrome(rows, extra = {}) {
  const calls = [];
  return {
    calls,
    tabs: { get: async id => { assert.equal(id, TAB.id); return { ...TAB }; }, query: async () => [{ ...TAB }] },
    cookies: {
      getAllCookieStores: async () => [{ id: "wrong-profile", tabIds: [98] }, { id: "current-profile", tabIds: [TAB.id] }],
      getAll: async details => {
        calls.push(details);
        assert.equal(details.storeId, "current-profile");
        assert.equal(details.url, TAB.url);
        assert.equal(details.domain, undefined);
        return rows.filter(row => row.name === details.name);
      },
    }, ...extra,
  };
}

function sessionValue(cookies, name) { return cookies.find(item => item.name === name)?.value; }

test("Brave/Chromium page context succeeds even when the Cookie API sees no KuGoo", async () => {
  let injected;
  const chrome = cookieChrome([], {
    tabs: { get: async id => { assert.equal(id, TAB.id); return TAB; }, query: async () => assert.fail("explicit tab must not query another tab") },
    scripting: { executeScript: async details => {
      injected = details;
      return [{ frameId: 0, result: { origin: "https://www.kugou.com", session: {
        userid: 123, token: TOKEN, appid: 1014, mid: "device", dfid: "fingerprint", NickName: "not forwarded",
      } } }];
    } },
  });
  const result = await helper(chrome).getKugouSessionCookies(TAB.id);
  assert.equal(sessionValue(result, "userid"), "123");
  assert.equal(sessionValue(result, "token"), TOKEN);
  assert.equal(result.some(item => item.name === "KuGoo"), false);
  assert.equal(JSON.stringify(result).includes("not forwarded"), false);
  assert.equal(chrome.calls.length, 0);
  assert.equal(injected.world, "MAIN");
  assert.deepEqual(Array.from(injected.target.frameIds), [0]);
  assert.equal(injected.target.tabId, TAB.id);
});

test("injected function uses the official SDK and returns only allowlisted fields", async () => {
  const api = helper({});
  const sandbox = {
    location: new URL(TAB.url), setTimeout, clearTimeout,
    window: { getBaseInfo: (appid, done) => { assert.equal(appid, 1014); done({
      userid: "123", token: TOKEN, appid: 1014, mid: "mid", dfid: "dfid", password: "unrelated-secret",
    }); } },
    document: {},
  };
  Object.defineProperty(sandbox, "localStorage", { get: () => assert.fail("must not inspect localStorage") });
  Object.defineProperty(sandbox, "sessionStorage", { get: () => assert.fail("must not inspect sessionStorage") });
  Object.defineProperty(sandbox.document, "cookie", { get: () => assert.fail("complete official session needs no raw cookie read") });
  const result = await vm.runInNewContext(`(${api.readKugouPageSession.toString()})("https://www.kugou.com")`, sandbox);
  assert.equal(result.session.token, TOKEN);
  assert.equal(JSON.stringify(result).includes("unrelated-secret"), false);
});

test("the serialized injected function awaits an asynchronous official SDK callback", async () => {
  const api = helper({});
  const sandbox = { location: new URL(TAB.url), setTimeout, clearTimeout,
    window: { getBaseInfo: (appid, done) => setTimeout(() => done({
      userid: "123", token: TOKEN, appid, mid: "async-mid", dfid: "-",
    }), 1) }, document: {} };
  Object.defineProperty(sandbox.document, "cookie", { get: () => assert.fail("must await SDK before falling back") });
  // A fresh VM exercises the function after serialization, without its module
  // closure; executeScript awaits its returned Promise per the Chrome API.
  const result = await vm.runInNewContext(`(${api.readKugouPageSession.toString()})("https://www.kugou.com")`, sandbox);
  assert.equal(result.session.token, TOKEN);
  assert.equal(result.session.mid, "async-mid");
});

test("an SDK that never invokes its callback falls back to visible cookies", async () => {
  const api = helper({});
  let deadline;
  const sandbox = { location: new URL(TAB.url), clearTimeout,
    setTimeout: (done, milliseconds) => { deadline = milliseconds; return setTimeout(done, 1); },
    window: { getBaseInfo: () => {} },
    document: { cookie: `KuGoo=KugooID=123&t=${TOKEN}&a_id=1014` } };
  const result = await vm.runInNewContext(`(${api.readKugouPageSession.toString()})("https://www.kugou.com")`, sandbox);
  assert.equal(deadline, 1200);
  assert.equal(result.session.token, TOKEN);
});

test("a page without the SDK uses its actual visible KuGoo and no storage guesses", async () => {
  const api = helper({});
  const raw = `KugooID=123&t=${TOKEN}&a_id=1014&KugooPwd=not-forwarded`;
  const sandbox = { location: new URL(TAB.url), setTimeout, clearTimeout, window: {},
    document: { cookie: `unrelated=not-forwarded; KuGoo=${encodeURIComponent(raw)}; kg_mid=page-mid; kg_dfid=page-dfid` } };
  Object.defineProperty(sandbox, "localStorage", { get: () => assert.fail("must not enumerate storage") });
  const result = await vm.runInNewContext(`(${api.readKugouPageSession.toString()})("https://www.kugou.com")`, sandbox);
  assert.equal(result.session.userid, "123");
  assert.equal(result.session.token, TOKEN);
  assert.equal(result.session.mid, "page-mid");
  assert.equal(JSON.stringify(result).includes("not-forwarded"), false);
});

test("separate H5 fields work without requiring a compound KuGoo cookie", async () => {
  const chrome = cookieChrome([cookie("userid", "123"), cookie("token", TOKEN), cookie("a_id", "1014")]);
  const result = await helper(chrome).getKugouSessionCookies(TAB.id);
  assert.equal(sessionValue(result, "token"), TOKEN);
  assert.ok(chrome.calls.length > 0);
  assert.ok(chrome.calls.every(details => typeof details.name === "string"));
});

test("cookie fallback remains available if MAIN world injection is unavailable", async () => {
  const chrome = cookieChrome([cookie("KuGoo", `KugooID=123&t=${TOKEN}&a_id=1014`)], {
    scripting: { executeScript: async () => { throw new Error(`permission error ${TOKEN}`); } },
  });
  const result = await helper(chrome).getKugouSessionCookies(TAB.id);
  assert.equal(sessionValue(result, "userid"), "123");
});

test("a readable page with incomplete SDK fields still uses a complete Cookie API session", async () => {
  const chrome = cookieChrome([cookie("KuGoo", `KugooID=123&t=${TOKEN}&a_id=1014`)], {
    scripting: { executeScript: async () => [{ frameId: 0, result: {
      origin: "https://www.kugou.com", session: { userid: "123", token: "", appid: 1014 },
    } }] },
  });
  const result = await helper(chrome).getKugouSessionCookies(TAB.id);
  assert.equal(sessionValue(result, "token"), TOKEN);
  assert.ok(chrome.calls.length > 0);
});

test("an unavailable partition API does not block unpartitioned cookies", async () => {
  const chrome = cookieChrome([cookie("KuGoo", `KugooID=123&t=${TOKEN}&a_id=1014`)]);
  chrome.cookies.getPartitionKey = async () => { throw new Error("unsupported API"); };
  const result = await helper(chrome).getKugouSessionCookies(TAB.id);
  assert.equal(sessionValue(result, "token"), TOKEN);
  assert.ok(chrome.calls.every(details => details.partitionKey === undefined));
});

test("partitioned cookies are read with the selected tab's partition key", async () => {
  const chrome = cookieChrome([]);
  const key = { topLevelSite: "https://kugou.com", hasCrossSiteAncestor: false };
  chrome.cookies.getPartitionKey = async details => {
    assert.equal(details.tabId, TAB.id); assert.equal(details.frameId, 0); return { partitionKey: key };
  };
  chrome.cookies.getAll = async details => {
    chrome.calls.push(details);
    assert.equal(details.storeId, "current-profile");
    if (!details.partitionKey || details.name !== "KuGoo") return [];
    return [cookie("KuGoo", `KugooID=123&t=${TOKEN}&a_id=1014`, { partitionKey: key })];
  };
  const result = await helper(chrome).getKugouSessionCookies(TAB.id);
  assert.equal(sessionValue(result, "token"), TOKEN);
  assert.ok(chrome.calls.some(details => details.partitionKey?.topLevelSite === key.topLevelSite));
});

test("half sessions are never joined across cookie partitions", async () => {
  const chrome = cookieChrome([]);
  chrome.cookies.getPartitionKey = async () => ({ partitionKey: { topLevelSite: "https://kugou.com" } });
  chrome.cookies.getAll = async details => details.partitionKey
    ? (details.name === "token" ? [cookie("token", TOKEN)] : [])
    : (details.name === "userid" ? [cookie("userid", "123")] : []);
  await assert.rejects(helper(chrome).getKugouSessionCookies(TAB.id), error => {
    assert.equal(error.code, "KUGOU_SESSION_INCOMPLETE");
    assert.equal(error.message.includes(TOKEN), false);
    return true;
  });
});

test("a missing tab cookie store never falls back to another profile", async () => {
  const chrome = cookieChrome([]);
  chrome.cookies.getAllCookieStores = async () => [{ id: "wrong-profile", tabIds: [99] }];
  chrome.cookies.getAll = async () => assert.fail("must not use the background's default store");
  await assert.rejects(helper(chrome).getKugouSessionCookies(TAB.id), /Cookie API/);
});

test("foreign tabs, subdomains and credentials in URLs are rejected before reading", async () => {
  for (const url of ["https://www.kugou.com.evil.example/", "https://fanxing.kugou.com/",
    "https://user:secret@www.kugou.com/", "http://www.kugou.com/"]) {
    const chrome = cookieChrome([], { tabs: { get: async () => ({ ...TAB, url }) },
      scripting: { executeScript: async () => assert.fail("must not inject into an unrelated tab") } });
    await assert.rejects(helper(chrome).getKugouSessionCookies(TAB.id), /已登录标签页/);
    assert.equal(chrome.calls.length, 0);
  }
});

test("navigation after a session read prevents returning stale credentials", async () => {
  let reads = 0;
  const chrome = cookieChrome([cookie("userid", "123"), cookie("token", TOKEN)], {
    tabs: { get: async () => (++reads === 1 ? TAB : { ...TAB, url: "https://elsewhere.example/" }) },
  });
  await assert.rejects(helper(chrome).getKugouSessionCookies(TAB.id), /发生跳转/);
});

test("incomplete sessions expose only presence diagnostics, not a false logged-out assertion", async () => {
  const chrome = cookieChrome([], { scripting: { executeScript: async () => [{ frameId: 0, result: {
    origin: "https://www.kugou.com", session: { userid: "123", token: "", appid: "1014" },
  } }] } });
  await assert.rejects(helper(chrome).getKugouSessionCookies(TAB.id), error => {
    assert.equal(error.code, "KUGOU_SESSION_INCOMPLETE");
    assert.equal(error.diagnostics.page_user_id_present, true);
    assert.equal(error.diagnostics.page_token_present, false);
    assert.equal(error.message.includes("123"), false);
    assert.equal(error.message.includes("Chrome"), false);
    assert.match(error.message, /用户 ID：有；令牌：无/);
    return true;
  });
});

test("errors from the browser never echo potentially secret exception text", async () => {
  const chrome = cookieChrome([], { scripting: { executeScript: async () => { throw new Error(TOKEN); } } });
  chrome.cookies.getAllCookieStores = async () => { throw new Error(TOKEN); };
  await assert.rejects(helper(chrome).getKugouSessionCookies(TAB.id), error => {
    assert.equal(error.code, "KUGOU_SESSION_ACCESS");
    assert.equal(error.stack.includes(TOKEN), false);
    return true;
  });
});

test("cookie candidates reject unrelated domains, expiration and mixed accounts", () => {
  const api = helper({});
  assert.equal(api.sessionFromCookies([
    cookie("KuGoo", `KugooID=123&t=${TOKEN}&a_id=1014`, { domain: "other.kugou.com" }),
    cookie("KuGoo", `KugooID=123&t=${TOKEN}&a_id=1014`, { expirationDate: 1 }),
  ]), null);
  assert.throws(() => api.sessionFromCookies([
    cookie("KuGoo", `KugooID=123&t=${TOKEN}&a_id=1014`),
    cookie("userid", "456"), cookie("token", "different-account-token"),
  ]), /冲突/);
  assert.equal(api.sessionFromCookies([cookie("KuGoo", "KugooID=123"), cookie("token", TOKEN)]), null);
  assert.throws(() => api.sessionFromCookies([
    cookie("userid", "123"), cookie("token", TOKEN), cookie("a_id", "1014"), cookie("appid", "1005"),
  ]), /冲突/);
});
