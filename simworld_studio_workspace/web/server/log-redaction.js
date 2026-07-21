"use strict";

function redactLogLine(value) {
  return String(value || "")
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[redacted]")
    .replace(/([?&](?:token|key|secret|password|signature|credential)=)[^&\s]+/gi, "$1[redacted]")
    .replace(/("(?:token|secret|password|credential|authorization|capability)"\s*:\s*")[^"]+/gi, "$1[redacted]")
    .replace(/\b(X-SimWorld-Run-Capability\s*:\s*)[A-Za-z0-9_-]+/gi, "$1[redacted]")
    .replace(/\b([a-z][a-z0-9+.-]*:\/\/)[^\s/@:]+(?::[^\s/@]*)?@/gi, "$1[redacted]@")
    .replace(/(?:\/[A-Za-z0-9._-]+){3,}/g, "[server-path]");
}

module.exports = { redactLogLine };
