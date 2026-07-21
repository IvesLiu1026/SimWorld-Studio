"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const {
  assertPrivateDirectory,
  readPrivateFile,
  syncPrivateDirectory,
} = require("../durable-artifact-io");

test("durable artifact directory validation and fsync accept a private real directory", async (t) => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-durable-dir-"));
  const root = path.join(parent, "records");
  await fs.mkdir(root, { mode: 0o700 });
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  assert.equal(await assertPrivateDirectory(root), root);
  assert.equal(await syncPrivateDirectory(root), root);
});

test("durable artifact directory validation rejects weak modes and symlink ancestry", async (t) => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-durable-dir-bad-"));
  const actual = path.join(parent, "actual");
  const child = path.join(actual, "records");
  await fs.mkdir(child, { recursive: true, mode: 0o700 });
  t.after(() => fs.rm(parent, { recursive: true, force: true }));

  await fs.chmod(child, 0o750);
  await assert.rejects(assertPrivateDirectory(child), (error) => error.code === "DURABLE_ARTIFACT_DIRECTORY_INSECURE");
  await fs.chmod(child, 0o700);

  await fs.chmod(child, 0o500);
  await assert.rejects(assertPrivateDirectory(child), (error) => error.code === "DURABLE_ARTIFACT_DIRECTORY_INSECURE");
  await fs.chmod(child, 0o700);

  const linked = path.join(parent, "linked");
  await fs.symlink(actual, linked);
  await assert.rejects(
    syncPrivateDirectory(path.join(linked, "records")),
    (error) => error.code === "DURABLE_ARTIFACT_DIRECTORY_INSECURE",
  );
});

test("durable artifact file reads require one stable owner-only regular inode", async (t) => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-durable-file-"));
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  const secure = path.join(parent, "secure.json");
  await fs.writeFile(secure, "{}", { mode: 0o600 });
  assert.deepEqual(
    await readPrivateFile(secure, { minBytes: 2, maxBytes: 32 }),
    Buffer.from("{}", "utf8"),
  );

  await fs.chmod(secure, 0o640);
  await assert.rejects(
    readPrivateFile(secure, { minBytes: 2, maxBytes: 32 }),
    (error) => error.code === "DURABLE_ARTIFACT_FILE_INSECURE",
  );
  await fs.chmod(secure, 0o600);

  const hardlink = path.join(parent, "hardlink.json");
  await fs.link(secure, hardlink);
  await assert.rejects(
    readPrivateFile(secure, { minBytes: 2, maxBytes: 32 }),
    (error) => error.code === "DURABLE_ARTIFACT_FILE_BUSY",
  );
  await fs.unlink(hardlink);

  const symlink = path.join(parent, "symlink.json");
  await fs.symlink(secure, symlink);
  await assert.rejects(readPrivateFile(symlink, { minBytes: 2, maxBytes: 32 }));
  await assert.rejects(readPrivateFile(secure, { minBytes: 3, maxBytes: 32 }));
  await assert.rejects(readPrivateFile(secure, { minBytes: 1, maxBytes: 1 }));
});
