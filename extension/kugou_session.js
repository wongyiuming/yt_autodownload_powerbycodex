/* Read the session of one verified Kugou main-site tab, never another profile.
 * Current official scripts still use KuGoo; getBaseInfo(1014, callback) exposes
 * its userid/token/mid/dfid/appid. Prefer that actual page context, then select
 * the same tab's cookie store and available partition APIs in Chromium/Brave.
 * No storage enumeration, credential logging, or browser-cookie writes here.
 */
(function () {
  "use strict";

  const COOKIE_NAMES = ["KuGoo", "KugooID", "userid", "token", "t", "a_id", "appid", "kg_mid", "kg_dfid"];

  function mainSiteUrl(raw) {
    try {
      const url = new URL(raw);
      return url.protocol === "https:" && ["kugou.com", "www.kugou.com"].includes(url.hostname)
        && !url.username && !url.password && !url.port ? url : null;
    } catch (_) { return null; }
  }

  function safeString(value, maximum) {
    return typeof value === "string" && value.length > 0 && value.length <= maximum && !/[\x00-\x20\x7f]/.test(value);
  }

  function normalizedSession(value) {
    if (!value || typeof value !== "object") return null;
    if (typeof value.userid === "number" && !Number.isSafeInteger(value.userid)) return null;
    const userid = String(value.userid ?? "");
    const token = value.token;
    const appid = String(value.appid || "1014");
    const mid = value.mid == null ? "" : String(value.mid);
    const dfid = value.dfid ? String(value.dfid) : "-";
    if (!/^[1-9][0-9]{0,19}$/.test(userid) || !safeString(token, 4096)
        || !["1010", "1014"].includes(appid) || (mid && !safeString(mid, 256)) || !safeString(dfid, 256)) return null;
    return { userid, token, appid, mid, dfid };
  }

  function decode(value) {
    try { return decodeURIComponent(value); } catch (_) { return value; }
  }

  function compoundFields(raw) {
    let text = String(raw || "");
    if (text.length > 16384) return {};
    if (text.startsWith('"') && text.endsWith('"')) text = text.slice(1, -1);
    for (let i = 0; i < 2 && !text.includes("="); i += 1) text = decode(text);
    const fields = {};
    for (const piece of text.split("&")) {
      const at = piece.indexOf("=");
      if (at < 0) continue;
      const key = piece.slice(0, at);
      if (!["KugooID", "userid", "t", "token", "a_id", "appid"].includes(key)) continue;
      const value = decode(piece.slice(at + 1));
      if (fields[key] != null && fields[key] !== value) return {};
      fields[key] = value;
    }
    return fields;
  }

  function oneField(fields, names) {
    const values = [...new Set(names.map(name => fields[name]).filter(value => value !== undefined && value !== ""))];
    if (values.length > 1) {
      throw new Error("当前酷狗标签页存在冲突的登录会话，请刷新页面后重新读取。");
    }
    return values.length === 1 ? values[0] : "";
  }

  function sessionFromCookies(cookies) {
    const now = Date.now() / 1000;
    const values = {};
    const compoundCandidates = [];
    for (const cookie of cookies || []) {
      const domain = String(cookie?.domain || "").replace(/^\./, "").toLowerCase();
      if (!["kugou.com", "www.kugou.com"].includes(domain) || !COOKIE_NAMES.includes(cookie.name)
          || typeof cookie.value !== "string" || !cookie.value || cookie.value.length > 16384
          || (cookie.expirationDate != null && cookie.expirationDate <= now)) continue;
      if (cookie.name === "KuGoo") compoundCandidates.push(compoundFields(cookie.value));
      else {
        // getAll returns the page's most-specific cookie path first. Refuse
        // competing authentication values instead of combining two accounts.
        if (values[cookie.name] != null && values[cookie.name] !== cookie.value) {
          throw new Error("当前酷狗标签页存在冲突的登录会话，请刷新页面后重新读取。");
        }
        values[cookie.name] = cookie.value;
      }
    }
    const candidates = [...compoundCandidates, values].map(fields => normalizedSession({
      userid: oneField(fields, ["KugooID", "userid"]), token: oneField(fields, ["t", "token"]),
      appid: oneField(fields, ["a_id", "appid"]) || "1014", mid: values.kg_mid || "", dfid: values.kg_dfid || "-",
    })).filter(Boolean);
    if (candidates.some(candidate => candidate.userid !== candidates[0].userid || candidate.token !== candidates[0].token)) {
      throw new Error("当前酷狗标签页存在冲突的登录会话，请刷新页面后重新读取。");
    }
    return candidates[0] || null;
  }

  // Self-contained: Chrome serializes this function into the MAIN world.
  async function readKugouPageSession(expectedOrigin) {
    if (location.origin !== expectedOrigin || location.protocol !== "https:"
        || !["kugou.com", "www.kugou.com"].includes(location.hostname)) return null;

    function allowedFields(value) {
      if (!value || typeof value !== "object") return null;
      return { userid: value.userid, token: value.token, appid: value.appid,
        mid: value.mid, dfid: value.dfid };
    }

    function visibleCookieSession() {
      // Only the three names used by the official web SDK are retained. Never
      // inspect localStorage/sessionStorage or return unrelated cookie values.
      const selected = {};
      for (const part of String(document.cookie || "").split(";")) {
        const at = part.indexOf("=");
        if (at < 0) continue;
        const name = part.slice(0, at).trim();
        if (["KuGoo", "kg_mid", "kg_dfid"].includes(name) && selected[name] == null) selected[name] = part.slice(at + 1);
      }
      let raw = selected.KuGoo || "";
      if (raw.length > 16384) return null;
      if (raw.startsWith('"') && raw.endsWith('"')) raw = raw.slice(1, -1);
      for (let i = 0; i < 2 && !raw.includes("="); i += 1) {
        try { raw = decodeURIComponent(raw); } catch (_) { break; }
      }
      const fields = {};
      for (const part of raw.split("&")) {
        const at = part.indexOf("=");
        if (at < 0) continue;
        const name = part.slice(0, at);
        if (!["KugooID", "t", "a_id"].includes(name)) continue;
        let value = part.slice(at + 1);
        try { value = decodeURIComponent(value); } catch (_) {}
        if (fields[name] != null && fields[name] !== value) return null;
        fields[name] = value;
      }
      return { userid: fields.KugooID, token: fields.t, appid: fields.a_id || "1014",
        mid: selected.kg_mid || "", dfid: selected.kg_dfid || "-" };
    }

    let session = null;
    if (typeof window.getBaseInfo === "function") {
      session = await new Promise(resolve => {
        let finished = false;
        const finish = value => {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          resolve(allowedFields(value));
        };
        const timer = setTimeout(() => finish(null), 1200);
        try { window.getBaseInfo(1014, finish); } catch (_) { finish(null); }
      });
    }
    if (!session?.userid || !session?.token) session = visibleCookieSession();
    if (location.origin !== expectedOrigin) return null;
    return { origin: location.origin, session };
  }

  function transportFields(session) {
    // Minimal normalized fields for native messaging, not browser cookie writes.
    const values = { userid: session.userid, token: session.token, a_id: session.appid,
      kg_mid: session.mid, kg_dfid: session.dfid };
    return Object.entries(values).filter(([, value]) => value !== "").map(([name, value]) => ({
      name, value, domain: "www.kugou.com", path: "/", secure: true, session: true,
    }));
  }

  function sessionError(code, message, diagnostics) {
    const error = new Error(message);
    error.code = code;
    // Only presence flags and fixed outcomes; safe for a UI diagnostic panel.
    error.diagnostics = diagnostics;
    return error;
  }

  async function getKugouSessionCookies(tabId) {
    let tab;
    try {
      if (tabId != null) {
        if (!Number.isInteger(tabId) || tabId < 0) throw new Error("invalid tab");
        tab = await chrome.tabs.get(tabId);
      } else {
        [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      }
    } catch (_) {
      throw new Error("无法定位当前酷狗标签页，请在已登录酷狗的标签页打开扩展。");
    }
    const url = mainSiteUrl(tab?.url);
    if (!url || !Number.isInteger(tab?.id)) {
      throw new Error("请在 https://www.kugou.com/ 的已登录标签页读取个人歌单。");
    }

    let session = null;
    let pageAccessible = false;
    const diagnostics = { page_readable: false, page_user_id_present: false,
      page_token_present: false, cookie_api_readable: false };
    if (typeof chrome.scripting?.executeScript === "function") {
      try {
        const results = await chrome.scripting.executeScript({
          target: { tabId: tab.id, frameIds: [0] }, world: "MAIN",
          func: readKugouPageSession, args: [url.origin],
        });
        const result = results.find(item => item.frameId === 0)?.result;
        pageAccessible = result?.origin === url.origin;
        if (pageAccessible) {
          diagnostics.page_readable = true;
          diagnostics.page_user_id_present = Boolean(result.session?.userid);
          diagnostics.page_token_present = Boolean(result.session?.token);
          session = normalizedSession(result.session);
        }
      } catch (_) { /* Cookie API remains available on older Chromium builds. */ }
    }

    let cookieAccessible = false;
    if (!session) {
      let groups = [];
      try {
        const stores = await chrome.cookies.getAllCookieStores();
        const store = stores.find(item => item.tabIds.includes(tab.id));
        if (!store) throw new Error("missing tab cookie store");
        const scopes = [{ url: url.href, storeId: store.id }];
        if (typeof chrome.cookies.getPartitionKey === "function") {
          try {
            const result = await chrome.cookies.getPartitionKey({ tabId: tab.id, frameId: 0 });
            const key = result?.partitionKey;
            if (key && typeof key.topLevelSite === "string") scopes.push({ ...scopes[0], partitionKey: key });
          } catch (_) { /* Unpartitioned reads still work in older browsers. */ }
        }
        // Read only allowlisted names, from this tab's store and URL. Separate
        // partition groups prevent mixing half a session across partitions.
        const results = await Promise.allSettled(scopes.map(async scope => {
          const sets = await Promise.all(COOKIE_NAMES.map(name => chrome.cookies.getAll({ ...scope, name })));
          return sets.flat();
        }));
        groups = results.filter(result => result.status === "fulfilled").map(result => result.value);
        cookieAccessible = groups.length > 0;
        diagnostics.cookie_api_readable = cookieAccessible;
      } catch (_) { /* A generic permission error is reported below. */ }
      const candidates = groups.map(sessionFromCookies).filter(Boolean);
      if (candidates.some(candidate => candidate.userid !== candidates[0].userid || candidate.token !== candidates[0].token)) {
        throw new Error("当前标签页的登录会话与 Cookie 分区不一致，请刷新酷狗页面后重新读取。");
      }
      session = candidates[0] || null;
    }
    if (!session) {
      if (!pageAccessible && !cookieAccessible) {
        throw sessionError("KUGOU_SESSION_ACCESS", "无法读取当前酷狗标签页的会话（页面读取和 Cookie API 均未成功）。请检查扩展是否获准访问此站点，再重新读取。", diagnostics);
      }
      const details = `页面读取：${pageAccessible ? "成功" : "不可用"}；用户 ID：${diagnostics.page_user_id_present ? "有" : "无"}；令牌：${diagnostics.page_token_present ? "有" : "无"}；Cookie API：${cookieAccessible ? "成功" : "不可用"}`;
      throw sessionError("KUGOU_SESSION_INCOMPLETE", `未能取得当前酷狗标签页的完整会话（${details}）。若页面已显示昵称，请刷新页面后重新读取；否则请先登录。`, diagnostics);
    }
    // Do not silently reuse a successful read after the source tab navigated.
    try {
      const current = await chrome.tabs.get(tab.id);
      if (mainSiteUrl(current?.url)?.origin !== url.origin) throw new Error("tab navigated");
    } catch (_) {
      throw new Error("酷狗标签页已关闭或发生跳转，请重新打开扩展读取。");
    }
    return transportFields(session);
  }

  globalThis.getKugouSessionCookies = getKugouSessionCookies;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { getKugouSessionCookies, readKugouPageSession, sessionFromCookies, normalizedSession };
  }
}());
