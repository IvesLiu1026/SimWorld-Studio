#!/usr/bin/env node
// Decode one embedded 2D KTX2 Basis Universal image to raw RGBA8.
//
// This deliberately has no package resolution, URL, or network path. The
// caller supplies absolute, hash-pinned transcoder files and private output
// paths. PNG framing is performed by the Python caller so that output bytes
// remain deterministic across Node versions.

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import vm from 'node:vm';

function fail(message) {
  throw new Error(message);
}

function parseArgs(argv) {
  const allowed = new Set([
    '--transcoder-js',
    '--transcoder-wasm',
    '--input',
    '--output',
    '--metadata',
  ]);
  if (argv.length !== allowed.size * 2) fail('expected exactly five flag/value pairs');
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!allowed.has(flag) || flag in result || typeof value !== 'string' || value.length === 0) {
      fail(`invalid or duplicated argument: ${flag}`);
    }
    if (!path.isAbsolute(value)) fail(`${flag} must be absolute`);
    result[flag] = value;
  }
  for (const flag of allowed) {
    if (!(flag in result)) fail(`missing argument: ${flag}`);
  }
  return result;
}

function regularInput(filename, label) {
  const stat = fs.lstatSync(filename);
  if (!stat.isFile() || stat.isSymbolicLink()) fail(`${label} must be a regular non-symlink file`);
}

function newOutput(filename, label) {
  if (fs.existsSync(filename)) fail(`${label} must not already exist`);
  const parent = fs.lstatSync(path.dirname(filename));
  if (!parent.isDirectory() || parent.isSymbolicLink()) fail(`${label} parent must be a non-symlink directory`);
}

const args = parseArgs(process.argv.slice(2));
regularInput(args['--transcoder-js'], 'transcoder JS');
regularInput(args['--transcoder-wasm'], 'transcoder WASM');
regularInput(args['--input'], 'KTX2 input');
newOutput(args['--output'], 'RGBA output');
newOutput(args['--metadata'], 'metadata output');

// Three is an ESM package, but its generated Basis wrapper is CommonJS-style
// source. Evaluate that exact pinned file in a minimal Node module wrapper;
// direct require() would treat the .js as ESM and return an empty namespace.
const transcoderFilename = args['--transcoder-js'];
const transcoderRequire = createRequire(transcoderFilename);
const transcoderModule = { exports: {} };
const wrapper = vm.runInThisContext(
  `(function(exports, require, module, __filename, __dirname) {\n${fs.readFileSync(transcoderFilename, 'utf8')}\n})`,
  { filename: transcoderFilename },
);
wrapper(
  transcoderModule.exports,
  transcoderRequire,
  transcoderModule,
  transcoderFilename,
  path.dirname(transcoderFilename),
);
const createBasisModule = transcoderModule.exports;
if (typeof createBasisModule !== 'function') fail('transcoder JS did not export a module factory');
const BasisModule = await createBasisModule({
  wasmBinary: fs.readFileSync(args['--transcoder-wasm']),
  print: () => {},
  printErr: () => {},
});
BasisModule.initializeBasis();
if (typeof BasisModule.KTX2File !== 'function') fail('transcoder does not expose KTX2File');

const source = fs.readFileSync(args['--input']);
const ktx2 = new BasisModule.KTX2File(new Uint8Array(source));
try {
  if (!ktx2.isValid()) fail('invalid or unsupported KTX2 input');
  const encoding = ktx2.isUASTC() ? 'UASTC' : ktx2.isETC1S() ? 'ETC1S' : ktx2.isHDR() ? 'UASTC_HDR' : null;
  if (encoding === null) fail('unknown Basis Universal encoding');
  if (encoding === 'UASTC_HDR') fail('HDR Basis Universal input is unsupported by the RGBA8 contract');
  const layers = ktx2.getLayers() || 1;
  const levels = ktx2.getLevels();
  const faces = ktx2.getFaces();
  if (layers !== 1 || faces !== 1) fail('only one-layer, one-face 2D textures are supported');
  if (!Number.isInteger(levels) || levels < 1) fail('KTX2 has no mip levels');
  if (!ktx2.startTranscoding()) fail('Basis Universal startTranscoding failed');

  const level = ktx2.getImageLevelInfo(0, 0, 0);
  const width = levels > 1 ? level.origWidth : level.width;
  const height = levels > 1 ? level.origHeight : level.height;
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1) {
    fail('invalid decoded dimensions');
  }
  if (width * height * 4 > 256 * 1024 * 1024) fail('decoded RGBA8 image exceeds the closed memory limit');
  const rgba32Format = 13;
  const byteLength = ktx2.getImageTranscodedSizeInBytes(0, 0, 0, rgba32Format);
  if (byteLength !== width * height * 4) fail('unexpected RGBA32 byte length');
  const rgba = new Uint8Array(byteLength);
  if (!ktx2.transcodeImage(rgba, 0, 0, 0, rgba32Format, 0, -1, -1)) {
    fail('Basis Universal transcodeImage failed');
  }
  fs.writeFileSync(args['--output'], rgba, { flag: 'wx', mode: 0o600 });
  fs.writeFileSync(
    args['--metadata'],
    `${JSON.stringify({
      schema_version: 'simworld.basisu-rgba8-decode/v1',
      width,
      height,
      source_levels: levels,
      source_layers: layers,
      source_faces: faces,
      source_encoding: encoding,
      has_alpha: Boolean(ktx2.getHasAlpha()),
      output_format: 'RGBA8',
      output_bytes: byteLength,
      mip_policy: 'base_level_only',
      node_version: process.version,
    })}\n`,
    { flag: 'wx', mode: 0o600 },
  );
} finally {
  ktx2.close();
  ktx2.delete();
}
