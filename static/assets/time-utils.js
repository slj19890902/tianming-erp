(function (global) {
  "use strict";

  const BEIJING_TIME_ZONE = "Asia/Shanghai";
  const DATE_ONLY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
  const RFC3339_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

  function assertDateOnly(value) {
    const match = DATE_ONLY_RE.exec(String(value));
    if (!match) throw new TypeError("business date must be YYYY-MM-DD");
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
    if (month < 1 || month > 12 || day < 1 || day > daysInMonth) {
      throw new RangeError("business date is invalid");
    }
    return { year, month, day };
  }

  function parseRfc3339(value) {
    const text = String(value);
    const match = RFC3339_RE.exec(text);
    if (!match) throw new TypeError("datetime must be RFC3339 with Z or an offset");
    assertDateOnly(match[1] + "-" + match[2] + "-" + match[3]);
    const hour = Number(match[4]);
    const minute = Number(match[5]);
    const second = Number(match[6]);
    if (hour > 23 || minute > 59 || second > 59) throw new RangeError("datetime is invalid");
    if (match[7] !== "Z") {
      const offset = match[7].slice(1).split(":").map(Number);
      if (offset[0] > 23 || offset[1] > 59) throw new RangeError("datetime offset is invalid");
    }
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) throw new RangeError("datetime is invalid");
    return date;
  }

  function beijingParts(value) {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: BEIJING_TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(value);
    return Object.fromEntries(parts.filter(function (part) {
      return part.type !== "literal";
    }).map(function (part) {
      return [part.type, part.value];
    }));
  }

  function formatBeijingDateTime(value) {
    const parts = beijingParts(parseRfc3339(value));
    return parts.year + "-" + parts.month + "-" + parts.day + " " + parts.hour + ":" + parts.minute + ":" + parts.second;
  }

  function formatBeijingDate(value) {
    const parts = beijingParts(parseRfc3339(value));
    return parts.year + "-" + parts.month + "-" + parts.day;
  }

  function beijingToday(now) {
    const parts = beijingParts(now === undefined ? new Date() : now);
    return parts.year + "-" + parts.month + "-" + parts.day;
  }

  function formatBusinessDate(value) {
    if (value instanceof Date) {
      if (Number.isNaN(value.getTime())) throw new RangeError("business date is invalid");
      return beijingToday(value);
    }
    const date = assertDateOnly(value);
    return date.year + "-" + String(date.month).padStart(2, "0") + "-" + String(date.day).padStart(2, "0");
  }

  function addCalendarDays(value, days) {
    const date = assertDateOnly(value);
    const amount = Number(days);
    if (!Number.isInteger(amount)) throw new TypeError("calendar days must be an integer");
    const result = new Date(Date.UTC(date.year, date.month - 1, date.day));
    result.setUTCDate(result.getUTCDate() + amount);
    return formatBusinessDate(
      result.getUTCFullYear() + "-" +
      String(result.getUTCMonth() + 1).padStart(2, "0") + "-" +
      String(result.getUTCDate()).padStart(2, "0"),
    );
  }

  global.TmTime = Object.freeze({
    BEIJING_TIME_ZONE,
    formatBeijingDateTime,
    formatBeijingDate,
    beijingToday,
    addCalendarDays,
    formatBusinessDate,
  });
})(typeof globalThis === "undefined" ? window : globalThis);
