(function (global) {
  "use strict";

  function phoneParts(value) {
    if (value === null || value === undefined) return [];
    return String(value)
      .split(/[,/]+/)
      .map(function (part) { return part.trim(); })
      .filter(Boolean);
  }

  function suspiciousPart(part) {
    const digits = String(part).replace(/[\s-]+/g, "");
    if (!/^\d+$/.test(digits)) return false;
    return digits.length < 7 || /^(\d)\1+$/.test(digits);
  }

  function analyze(value) {
    const raw = value === null || value === undefined ? "" : String(value);
    const parts = phoneParts(value);
    const needsReview = parts.some(suspiciousPart);
    return Object.freeze({
      raw: raw,
      text: parts.join(" / "),
      needs_review: needsReview,
      reason: needsReview ? "疑似占位，待核对" : "",
    });
  }

  function display(value) {
    return analyze(value).text;
  }

  global.TmPhone = Object.freeze({
    analyze: analyze,
    display: display,
  });
})(globalThis);
