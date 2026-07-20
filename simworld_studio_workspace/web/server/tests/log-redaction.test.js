"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { redactLogLine } = require("../log-redaction");

test("request log redaction removes credentials and server paths", () => {
  const output = redactLogLine(
    'POST /api/vista/imports body={"token":"abc","sourcePath":"/home/user/private/bundle/manifest.json"} '
      + "Authorization: Bearer top.secret postgres://admin:pw@db.local/assets?password=pw",
  );
  for (const secret of ["abc", "top.secret", "admin:pw", "password=pw", "/home/user/private"]) {
    assert.equal(output.includes(secret), false);
  }
  assert.match(output, /\[redacted\]/);
  assert.match(output, /\[server-path\]/);
});
