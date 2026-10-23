"use strict";
/** Same public-page and rejected-URL cases as the Native Host classifier. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { classifySourceUrl } = require("../extension/platform_urls.js");
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, "../tests/fixtures/platform_urls.json"), "utf8"));

for (const test of cases.valid) {
  const actual = classifySourceUrl(test.raw);
  assert.deepEqual(
    { platform: actual.platform, kind: actual.kind, url: actual.normalizedUrl },
    { platform: test.platform, kind: test.kind, url: test.url },
    test.raw,
  );
  assert.deepEqual(classifySourceUrl(actual.normalizedUrl), actual, `Canonical URL must be stable: ${test.raw}`);
}
for (const raw of cases.invalid) {
  const actual = classifySourceUrl(raw);
  assert.equal(actual.kind, "unknown", raw);
  assert.equal(actual.platform, "", raw);
}
console.log(`Platform URL fixtures passed: ${cases.valid.length} supported, ${cases.invalid.length} rejected.`);
