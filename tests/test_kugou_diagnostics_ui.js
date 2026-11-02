"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../extension/popup.js"), "utf8");
const safe = source.slice(source.indexOf("function safeKugouDiagnostic("), source.indexOf("function renderLibraryPreview("));
const apply = source.slice(source.indexOf("function applyLibraryPreviewPage("), source.indexOf("function failLibraryPreview("));
const box = { expectedHostVersion: "1.4.2", libraryPreview: { preview_id: "fixture", playlists: [] },
  setMessage: (text, kind) => { box.message = text; box.kind = kind; },
  renderLibraryPreview: () => {}, updateDetected: () => {} };
vm.createContext(box);
vm.runInContext(safe + "\n" + apply, box);
const secret = "fixtureSecretOnlyLetters";
const clean = box.safeKugouDiagnostic({ code: secret, endpoint: "https://bad/?token=" + secret,
  stage: "playlist_list", page: 2, http_status: 403, api_code: -2,
  shape: {info: "array", [secret]: "string", count: secret}, token: secret });
assert.equal(JSON.stringify(clean).includes(secret), false);
assert.equal(clean.stage, "playlist_list");
assert.equal(clean.page, 2);
assert.equal(clean.http_status, 403);
assert.equal(clean.api_code, -2);
assert.equal(clean.shape.info, "array");
assert.equal(clean.endpoint, undefined);
box.message = "正在读取歌单 URL，确认前不会下载歌曲。";
box.applyLibraryPreviewPage({ preview_id: "fixture", status: "error", playlists: [], error: "API_REJECTED 900" });
assert.equal(box.message, "API_REJECTED 900");
assert.equal(box.kind, "error");
assert.equal(box.libraryPreview.status, "error");
box.applyLibraryPreviewPage({ preview_id: "fixture", status: "ready", playlists: [], next_offset: null });
assert.equal(box.message, "歌单 URL 读取完成；确认后才会下载歌曲。");
assert.equal(box.kind, "ok");
console.log("KUGOU_DIAGNOSTICS_UI_OK");
