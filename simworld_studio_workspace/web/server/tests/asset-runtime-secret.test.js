"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { resolveRuntimeSecret } = require("../asset-runtime-secret");
const retrievalDb = require("../asset-retrieval-db");

function fixture(t, contents = "s".repeat(40) + "\n", mode = 0o600) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "asset-runtime-secret-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const file = path.join(root, "credential");
  fs.writeFileSync(file, contents, { mode });
  fs.chmodSync(file, mode);
  return { file, root };
}

test("file-backed runtime secret is read from a stable mode-0600 regular file", (t) => {
  const { file } = fixture(t);
  const resolved = resolveRuntimeSecret(
    { QDRANT_API_KEY_FILE: file },
    "QDRANT_API_KEY",
    "QDRANT_API_KEY_FILE",
    { required: true, minimumBytes: 32 },
  );
  assert.equal(resolved.value, "s".repeat(40));
  assert.equal(resolved.source, "QDRANT_API_KEY_FILE");
});

test("runtime secret sources conflict fail closed without exposing either value", (t) => {
  const { file } = fixture(t, "file-secret-that-must-not-leak".repeat(2) + "\n");
  const direct = "direct-secret-that-must-not-leak".repeat(2);
  assert.throws(
    () => resolveRuntimeSecret(
      { EMBED_SERVICE_TOKEN: direct, EMBED_SERVICE_TOKEN_FILE: file },
      "EMBED_SERVICE_TOKEN",
      "EMBED_SERVICE_TOKEN_FILE",
      { required: true },
    ),
    (error) => {
      assert.match(error.message, /mutually exclusive/);
      assert.doesNotMatch(error.message, /direct-secret|file-secret/);
      return true;
    },
  );
});

test("symlinks, weak permissions, and multiline secret files are rejected", (t) => {
  const secure = fixture(t);
  const symlink = path.join(secure.root, "credential-link");
  fs.symlinkSync(secure.file, symlink);
  assert.throws(() => resolveRuntimeSecret(
    { POSTGRES_URL_FILE: symlink },
    "POSTGRES_URL",
    "POSTGRES_URL_FILE",
    { required: true },
  ));

  const weak = fixture(t, "w".repeat(40) + "\n", 0o640);
  assert.throws(
    () => resolveRuntimeSecret(
      { QDRANT_API_KEY_FILE: weak.file },
      "QDRANT_API_KEY",
      "QDRANT_API_KEY_FILE",
      { required: true },
    ),
    /group\/other/,
  );

  const multiline = fixture(t, `${"a".repeat(40)}\n${"b".repeat(40)}\n`);
  assert.throws(
    () => resolveRuntimeSecret(
      { EMBED_SERVICE_TOKEN_FILE: multiline.file },
      "EMBED_SERVICE_TOKEN",
      "EMBED_SERVICE_TOKEN_FILE",
      { required: true },
    ),
    /exactly one line/,
  );
});

test("retrieval client builders consume file-backed Postgres and explicit Qdrant credentials", (t) => {
  const dsn = "postgresql://asset_user:secret@127.0.0.1/assets";
  const { file } = fixture(t, `${dsn}\n`);
  const oldDirect = process.env.POSTGRES_URL;
  const oldFile = process.env.POSTGRES_URL_FILE;
  delete process.env.POSTGRES_URL;
  process.env.POSTGRES_URL_FILE = file;
  try {
    assert.equal(retrievalDb.resolvePostgresConnectionString({}), dsn);
  } finally {
    if (oldDirect === undefined) delete process.env.POSTGRES_URL;
    else process.env.POSTGRES_URL = oldDirect;
    if (oldFile === undefined) delete process.env.POSTGRES_URL_FILE;
    else process.env.POSTGRES_URL_FILE = oldFile;
  }

  const apiKey = "qdrant-key-that-must-stay-in-client-memory";
  const options = retrievalDb.buildQdrantClientOptions({
    qdrantUrl: "http://127.0.0.1:6333",
    qdrantApiKey: apiKey,
    qdrantTimeoutMs: 3210,
  });
  assert.equal(options.apiKey, apiKey);
  assert.equal(options.url, "http://127.0.0.1:6333");
  assert.equal(options.timeout, 3210);
});

test("invalid database credentials and startup status never expose secret values or file paths", (t) => {
  const invalid = "not-a-postgres-dsn-that-must-not-leak";
  assert.throws(
    () => retrievalDb.resolvePostgresConnectionString({ postgresUrl: invalid }),
    (error) => {
      assert.match(error.message, /not a valid DSN/);
      assert.doesNotMatch(error.message, new RegExp(invalid));
      return true;
    },
  );

  const { file } = fixture(t, `${"p".repeat(40)}\n`);
  const startup = fs.readFileSync(path.join(__dirname, "..", "start.sh"), "utf8");
  assert.match(startup, /configured via POSTGRES_URL_FILE \(path hidden\)/);
  assert.doesNotMatch(startup, /echo .*\$\{POSTGRES_URL_FILE\}/);
  assert.doesNotMatch(startup, new RegExp(file));
});
