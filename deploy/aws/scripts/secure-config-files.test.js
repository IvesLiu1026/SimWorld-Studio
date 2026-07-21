"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const {
  SecureConfigFileError,
  readSecretFile,
  writePrivateAtomic,
} = require("./secure-config-files");

function temporaryRoot(prefix) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  fs.chmodSync(root, 0o700);
  return root;
}

test("secure secret reader accepts a stable private regular file", () => {
  const root = temporaryRoot("simworld-secure-secret-");
  try {
    const secret = "0123456789abcdef".repeat(4);
    const filename = path.join(root, "turn.secret");
    fs.writeFileSync(filename, `${secret}\n`, { mode: 0o640 });
    assert.equal(
      readSecretFile(filename, {
        label: "TURN secret",
        expectedGroupId: process.getegid(),
      }),
      secret,
    );

    fs.chmodSync(filename, 0o600);
    assert.equal(readSecretFile(filename, { label: "TURN secret" }), secret);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("secure secret reader rejects weak modes, symlinks, hard links, and unsafe bytes", () => {
  const root = temporaryRoot("simworld-unsafe-secret-");
  try {
    const value = "abcdef0123456789".repeat(4);
    const weak = path.join(root, "weak.secret");
    fs.writeFileSync(weak, value, { mode: 0o600 });
    fs.chmodSync(weak, 0o644);
    assert.throws(() => readSecretFile(weak), SecureConfigFileError);

    fs.chmodSync(weak, 0o640);
    assert.throws(() => readSecretFile(weak), SecureConfigFileError);
    assert.throws(
      () => readSecretFile(weak, { expectedGroupId: process.getegid() + 1 }),
      SecureConfigFileError,
    );
    assert.throws(
      () => readSecretFile(weak, { expectedGroupId: 0 }),
      SecureConfigFileError,
    );
    fs.chmodSync(weak, 0o650);
    assert.throws(
      () => readSecretFile(weak, { expectedGroupId: process.getegid() }),
      SecureConfigFileError,
    );

    const source = path.join(root, "source.secret");
    fs.writeFileSync(source, value, { mode: 0o600 });
    const linked = path.join(root, "linked.secret");
    fs.symlinkSync(source, linked);
    assert.throws(() => readSecretFile(linked), SecureConfigFileError);

    const linkedDirectory = path.join(root, "linked-directory");
    fs.symlinkSync(root, linkedDirectory);
    assert.throws(
      () => readSecretFile(path.join(linkedDirectory, "source.secret")),
      SecureConfigFileError,
    );

    assert.throws(
      () => readSecretFile(`${root}/./source.secret`),
      SecureConfigFileError,
    );

    const hard = path.join(root, "hard.secret");
    fs.linkSync(source, hard);
    assert.throws(() => readSecretFile(source), SecureConfigFileError);

    const unsafe = path.join(root, "unsafe.secret");
    fs.writeFileSync(unsafe, `${value}#comment`, { mode: 0o600 });
    assert.throws(() => readSecretFile(unsafe), SecureConfigFileError);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("atomic private writer replaces only a safe target in an owned directory", () => {
  const root = temporaryRoot("simworld-secure-output-");
  try {
    const output = path.join(root, "cirrus.json");
    fs.writeFileSync(output, "old\n", { mode: 0o640 });
    assert.equal(writePrivateAtomic(output, "new\n"), output);
    assert.equal(fs.readFileSync(output, "utf8"), "new\n");
    assert.equal(fs.statSync(output).mode & 0o777, 0o600);
    assert.deepEqual(fs.readdirSync(root), ["cirrus.json"]);

    const groupOutput = path.join(root, "turnserver.conf");
    assert.equal(
      writePrivateAtomic(groupOutput, "group-readable\n", {
        outputGroupId: process.getegid(),
      }),
      groupOutput,
    );
    assert.equal(fs.statSync(groupOutput).mode & 0o777, 0o640);
    assert.equal(fs.statSync(groupOutput).gid, process.getegid());
    assert.throws(
      () => writePrivateAtomic(groupOutput, "missing-gid-policy\n"),
      SecureConfigFileError,
    );

    const actual = path.join(root, "actual.json");
    fs.writeFileSync(actual, "actual\n", { mode: 0o600 });
    const symlink = path.join(root, "symlink.json");
    fs.symlinkSync(actual, symlink);
    assert.throws(() => writePrivateAtomic(symlink, "replacement\n"), SecureConfigFileError);
    assert.equal(fs.readFileSync(actual, "utf8"), "actual\n");

    const hardLinkedOutput = path.join(root, "hard-linked.json");
    fs.linkSync(actual, hardLinkedOutput);
    assert.throws(
      () => writePrivateAtomic(hardLinkedOutput, "replacement\n"),
      SecureConfigFileError,
    );
    assert.equal(fs.readFileSync(actual, "utf8"), "actual\n");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("atomic private writer rejects a group/world-writable parent", () => {
  const root = temporaryRoot("simworld-public-output-");
  try {
    const publicDirectory = path.join(root, "public");
    fs.mkdirSync(publicDirectory, { mode: 0o700 });
    fs.chmodSync(publicDirectory, 0o777);
    assert.throws(
      () => writePrivateAtomic(path.join(publicDirectory, "config.json"), "{}\n"),
      SecureConfigFileError,
    );
    assert.deepEqual(fs.readdirSync(publicDirectory), []);

    fs.chmodSync(publicDirectory, 0o777);
    const privateChild = path.join(publicDirectory, "private-child");
    fs.mkdirSync(privateChild, { mode: 0o700 });
    assert.throws(
      () => writePrivateAtomic(path.join(privateChild, "config.json"), "{}\n"),
      SecureConfigFileError,
    );
    assert.deepEqual(fs.readdirSync(privateChild), []);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("atomic writer reports a committed but unconfirmed directory fsync", () => {
  const root = temporaryRoot("simworld-output-fsync-");
  const originalFsync = fs.fsyncSync;
  let calls = 0;
  try {
    fs.fsyncSync = (descriptor) => {
      calls += 1;
      if (calls === 2) {
        const error = new Error("injected directory fsync failure");
        error.code = "EIO";
        throw error;
      }
      return originalFsync(descriptor);
    };
    const output = path.join(root, "config.json");
    assert.throws(
      () => writePrivateAtomic(output, "installed\n"),
      (error) => error instanceof SecureConfigFileError
        && error.code === "SECURE_CONFIG_COMMITTED_NOT_DURABLE"
        && error.committed === true,
    );
    assert.equal(fs.readFileSync(output, "utf8"), "installed\n");
  } finally {
    fs.fsyncSync = originalFsync;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("AMI and Compose preserve a root-owned release and explicit host secret GID", () => {
  const awsRoot = path.resolve(__dirname, "..");
  const bake = fs.readFileSync(path.join(__dirname, "bake-ami.sh"), "utf8");
  const compose = fs.readFileSync(path.join(awsRoot, "docker/docker-compose.yml"), "utf8");
  const dockerfile = fs.readFileSync(path.join(awsRoot, "docker/Dockerfile.web"), "utf8");
  assert.match(bake, /chown -hR root:root "\$REPO_DIR"/);
  assert.doesNotMatch(bake, /chown -R simworld:simworld "\$REPO_DIR"/);
  assert.doesNotMatch(bake, /chown -R simworld:simworld "\$WEB_DIR"/);
  assert.match(bake, /sudo -u simworld-build/);
  assert.match(bake, /simworld-build must not have supplementary groups/);
  assert.match(bake, /trap finish_release_tree EXIT/);
  assert.match(bake, /du -s --apparent-size --block-size=1/);
  assert.match(bake, /\.dist\.publish\.XXXXXXXX/);
  assert.match(bake, /\.node_modules\.publish\.XXXXXXXX/);
  assert.match(bake, /mv -- "\$DIST_PUBLISH_TEMP" "\$DIST_NEW_FINAL"/);
  assert.match(bake, /mv -- "\$MODULES_PUBLISH_TEMP" "\$MODULES_NEW_FINAL"/);
  assert.match(compose, /group_add:\s*\n\s*- "\$\{SIMWORLD_SECRET_GID:\?/);
  assert.match(compose, /user: "\$\{SIMWORLD_RUNTIME_UID:\?.*\}:\$\{SIMWORLD_RUNTIME_GID:\?/);
  assert.match(compose, /SIMWORLD_RUNTIME_UID=\$\{SIMWORLD_RUNTIME_UID:\?/);
  assert.match(compose, /SIMWORLD_RUNTIME_GID=\$\{SIMWORLD_RUNTIME_GID:\?/);
  assert.match(compose, /SIMWORLD_SECRET_GID=\$\{SIMWORLD_SECRET_GID:\?/);
  assert.match(compose, /TURN_SHARED_SECRET_FILE_GID=\$\{SIMWORLD_SECRET_GID:\?/);
  assert.doesNotMatch(dockerfile, /chown -R simworld:simworld \/app/);
  assert.doesNotMatch(dockerfile, /chown -R simworld:simworld .*\/deploy/);
  assert.match(dockerfile, /ENTRYPOINT \["\/deploy\/scripts\/container-entrypoint\.sh"\]/);
});

test("container entrypoint rejects root group IDs and validates runtime identity", () => {
  const script = path.join(__dirname, "container-entrypoint.sh");
  const good = spawnSync(script, ["--validate-only"], {
    encoding: "utf8",
    env: {
      PATH: process.env.PATH || "",
      SIMWORLD_RUNTIME_UID: String(process.geteuid()),
      SIMWORLD_RUNTIME_GID: String(process.getegid()),
      SIMWORLD_SECRET_GID: String(process.getegid()),
    },
  });
  assert.equal(good.status, 0, good.stderr);

  const bad = spawnSync(script, ["--validate-only"], {
    encoding: "utf8",
    env: {
      PATH: process.env.PATH || "",
      SIMWORLD_RUNTIME_UID: String(process.geteuid()),
      SIMWORLD_RUNTIME_GID: String(process.getegid()),
      SIMWORLD_SECRET_GID: "0",
    },
  });
  assert.equal(bad.status, 78);
  assert.match(bad.stderr, /positive non-root numeric ID/);
});
