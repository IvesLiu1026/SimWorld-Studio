#!/usr/bin/env python3
"""Deterministically patch the pinned UE 5.3 Cirrus server to loopback-only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


PATCH_VERSION = "VISTA_LOOPBACK_PATCH_V1"
EXPECTED_ORIGINAL_SHA256 = "85cc7809250e2de92de0fe16cbabc27392d580a916d10851dc4c48197028215f"
EXPECTED_PATCHED_SHA256 = "724b64ea93b863d42a69a22b10e75046d66ee39a5e852be425d9706bdd11abc7"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"expected exactly one Cirrus patch marker: {old!r}")
    return text.replace(old, new, 1)


def patched_source(original: bytes) -> bytes:
    actual = sha256_bytes(original)
    if actual != EXPECTED_ORIGINAL_SHA256:
        raise RuntimeError(
            f"unreviewed Cirrus source SHA-256: {actual}; expected {EXPECTED_ORIGINAL_SHA256}"
        )
    text = original.decode("utf-8")
    text = replace_once(
        text,
        "// Copyright Epic Games, Inc. All Rights Reserved.\n",
        "// Copyright Epic Games, Inc. All Rights Reserved.\n"
        f"// {PATCH_VERSION}: VISTA-reviewed loopback-only, token-authenticated player service.\n",
    )
    text = replace_once(
        text,
        "var express = require('express');\nvar app = express();\n",
        "var express = require('express');\nvar app = express();\n"
        "const vistaCrypto = require('crypto');\n"
        "const vistaAccessToken = String(process.env.STUDIO_ACCESS_TOKEN || '');\n"
        "if (vistaAccessToken.length < 32) {\n"
        "\tthrow new Error('STUDIO_ACCESS_TOKEN must contain at least 32 characters');\n"
        "}\n"
        "function vistaTokensEqual(left, right) {\n"
        "\tconst a = Buffer.from(String(left || ''));\n"
        "\tconst b = Buffer.from(String(right || ''));\n"
        "\treturn a.length === b.length && vistaCrypto.timingSafeEqual(a, b);\n"
        "}\n"
        "function vistaCookieToken(header) {\n"
        "\tfor (const item of String(header || '').split(';')) {\n"
        "\t\tconst split = item.indexOf('=');\n"
        "\t\tif (split >= 0 && item.slice(0, split).trim() === 'vista_studio_access') {\n"
        "\t\t\ttry { return decodeURIComponent(item.slice(split + 1).trim()); } catch (_) { return ''; }\n"
        "\t\t}\n"
        "\t}\n"
        "\treturn '';\n"
        "}\n"
        "function vistaRequestAuthorized(req) {\n"
        "\tconst auth = String((req.headers && req.headers.authorization) || '');\n"
        "\tconst bearer = auth.startsWith('Bearer ') ? auth.slice(7) : '';\n"
        "\treturn vistaTokensEqual(bearer, vistaAccessToken) ||\n"
        "\t\tvistaTokensEqual(vistaCookieToken(req.headers && req.headers.cookie), vistaAccessToken);\n"
        "}\n"
        "function vistaVerifyClient(info, done) {\n"
        "\tdone(vistaRequestAuthorized(info.req), 401, 'Unauthorized');\n"
        "}\n"
        "app.use(function vistaAccessGuard(req, res, next) {\n"
        "\tif (vistaRequestAuthorized(req)) return next();\n"
        "\treturn res.status(401).send('Unauthorized');\n"
        "});\n",
    )
    text = replace_once(
        text,
        '\tPublicIp: "localhost",\n\tHttpPort: 80,',
        '\tPublicIp: "localhost",\n\tBindAddress: "127.0.0.1",\n\tHttpPort: 80,',
    )
    text = replace_once(
        text,
        "const config = require('./modules/config.js').init(configFile, defaultConfig);\n",
        "const config = require('./modules/config.js').init(configFile, defaultConfig);\n"
        "const bindAddress = String(config.BindAddress || '');\n"
        "if (bindAddress !== '127.0.0.1') {\n"
        "\tthrow new Error(`BindAddress must be 127.0.0.1, got: ${bindAddress}`);\n"
        "}\n",
    )
    text = replace_once(
        text,
        "http.listen(httpPort, function () {",
        "http.listen(httpPort, bindAddress, function () {",
    )
    text = replace_once(
        text,
        "https.listen(httpsPort, function () {",
        "https.listen(httpsPort, bindAddress, function () {",
    )
    text = replace_once(
        text,
        "new WebSocket.Server({ port: streamerPort, backlog: 1 })",
        "new WebSocket.Server({ port: streamerPort, host: bindAddress, backlog: 1 })",
    )
    text = replace_once(
        text,
        "new WebSocket.Server({ port: sfuPort })",
        "new WebSocket.Server({ port: sfuPort, host: bindAddress })",
    )
    text = replace_once(
        text,
        "new WebSocket.Server({ server: config.UseHTTPS ? https : http})",
        "new WebSocket.Server({ server: config.UseHTTPS ? https : http, verifyClient: vistaVerifyClient })",
    )
    result = text.encode("utf-8")
    actual_patched = sha256_bytes(result)
    if actual_patched != EXPECTED_PATCHED_SHA256:
        raise RuntimeError(
            f"unexpected patched Cirrus SHA-256: {actual_patched}; expected {EXPECTED_PATCHED_SHA256}"
        )
    return result


def patch_file(cirrus_js: Path) -> Path:
    cirrus_js = Path(cirrus_js)
    if cirrus_js.is_symlink():
        raise RuntimeError(f"Cirrus path must be a regular file: {cirrus_js}")
    cirrus_js = cirrus_js.resolve(strict=True)
    if not cirrus_js.is_file():
        raise RuntimeError(f"Cirrus path must be a regular file: {cirrus_js}")
    original = cirrus_js.read_bytes()
    patched = patched_source(original)
    backup = cirrus_js.with_name("cirrus.js.vista-original")
    receipt = cirrus_js.with_name("cirrus.js.vista-receipt.json")
    if backup.exists() or receipt.exists():
        raise FileExistsError("Cirrus patch backup/receipt already exists; refusing overwrite")
    shutil.copy2(cirrus_js, backup)
    temporary = cirrus_js.with_suffix(".js.vista-tmp")
    try:
        temporary.write_bytes(patched)
        os.chmod(temporary, cirrus_js.stat().st_mode & 0o777)
        os.replace(temporary, cirrus_js)
        receipt.write_text(
            json.dumps(
                {
                    "schema": "vista-cirrus-loopback-patch/v1",
                    "patched_at": datetime.now(timezone.utc).isoformat(),
                    "patch_version": PATCH_VERSION,
                    "cirrus_path": str(cirrus_js),
                    "original_sha256": EXPECTED_ORIGINAL_SHA256,
                    "patched_sha256": EXPECTED_PATCHED_SHA256,
                    "backup_path": str(backup),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        if backup.exists():
            shutil.copy2(backup, cirrus_js)
            backup.unlink()
        receipt.unlink(missing_ok=True)
        raise
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cirrus_js", type=Path)
    args = parser.parse_args()
    print(patch_file(args.cirrus_js))


if __name__ == "__main__":
    main()
